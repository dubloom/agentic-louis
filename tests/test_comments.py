from copy import deepcopy
import unittest
from unittest.mock import patch

from agentic_louis import comments
from agentic_louis.jira import Jira, document, plain_text


class CommentTests(unittest.TestCase):
    def test_unmatched_backticks_remain_literal_without_empty_text_nodes(self):
        for value in ("`", "``", "Command `failed", "`file.py` failed"):
            body = comments.paragraph(value)
            self.assertTrue(all(node["text"] for node in body["content"]))
            self.assertEqual(plain_text(body).strip(), value.replace("`file.py`", "file.py"))

    def test_conversation_commands_are_exact_copyable_code_blocks(self):
        local = "codex resume session --cd '/path with spaces'"
        remote = "ssh -t workspace 'codex resume session --cd /task'"
        attach = "tmux -L agentic-louis-token attach-session -t agent"
        remote_attach = "ssh -t workspace 'tmux -L agentic-louis-token attach-session -t agent'"
        body = comments.started(3, attach, remote_attach)
        blocks = [node for node in body["content"] if node["type"] == "codeBlock"]
        self.assertEqual([node["content"][0]["text"] for node in blocks], [remote_attach, attach])
        self.assertTrue(all(node["attrs"]["language"] == "bash" for node in blocks))
        self.assertNotIn("resume", plain_text(body).casefold())
        for body in (comments.blocked({"error": "Tests failed"}, (local, remote)),
                     comments.ready({"result": {"summary": "Done", "validation": "Passed", "prs": []}}, (local, remote))):
            blocks = [node for node in body["content"] if node["type"] == "codeBlock"]
            self.assertEqual([node["content"][0]["text"] for node in blocks], [remote, local])

    def test_blocked_comment_separates_validation_and_links_without_empty_sections(self):
        url = "https://github.com/DataDog/dd-trace-py/tree/dubloom/ltra-11"
        body = comments.blocked({"attempt": 3, "error": "Codex reported incomplete work: Saved `patch.py`.\n"
                                 "Validation: `git diff --check` passed. Tests could not start.",
                                 "partial_branches": [url]})
        headings = [plain_text(node).strip() for node in body["content"] if node["type"] == "heading"]
        self.assertEqual(headings, ["Work blocked · attempt 3", "Saved branches", "Validation", "Next steps"])
        link = next(node for node in body["content"] if node["type"] == "bulletList")["content"][0]["content"][0]["content"][0]
        self.assertEqual(link["marks"], [{"type": "link", "attrs": {"href": url}}])
        self.assertEqual(link["text"], "DataDog/dd-trace-py · dubloom/ltra-11")
        self.assertIn("Tests could not start.", plain_text(body))
        self.assertEqual(body["content"][1]["content"][1]["marks"], [{"type": "code"}])

    def test_multiline_command_errors_are_preserved_in_collapsible_details(self):
        detail = "Working directory: /task\nfatal: authentication failed\n<output> & `literal`"
        body = comments.blocked({"error": "git push failed\n" + detail,
                                 "branch_publication_errors": ["push failed\npermission denied"]})
        expand = next(node for node in body["content"] if node["type"] == "expand")
        self.assertEqual(expand["attrs"]["title"], "Failure details")
        self.assertEqual(expand["content"][0]["content"][0]["text"], detail)
        self.assertIn("push failed\npermission denied", plain_text(body))

    def test_review_comment_links_prs_and_keeps_validation_evidence(self):
        url = "https://github.com/DataDog/repo/pull/42"
        body = comments.ready({"attempt": 2, "result": {"summary": "Updated `file.py`.",
                               "validation": "All tests passed.", "prs": [{"url": url}]},
                               "verified_checks": ["repo: tests passed"]})
        self.assertIn("DataDog/repo #42", plain_text(body))
        self.assertIn("All tests passed.", plain_text(body))
        self.assertIn("repo: tests passed", plain_text(body))

    def test_transport_preserves_rich_body_without_mutation_and_finds_hidden_token(self):
        jira = object.__new__(Jira)
        body = comments.started(1, "tmux attach", "ssh workspace tmux attach")
        original = deepcopy(body)
        with patch.object(jira, "get", return_value={"comments": [], "total": 0}), patch.object(jira, "request") as write:
            jira.comment_once("LTRA-11", "[token]", body)
        posted = write.call_args.kwargs["payload"]["body"]
        self.assertEqual(body, original)
        self.assertEqual(posted["content"][:-1], body["content"])
        self.assertEqual(posted["content"][-1]["type"], "expand")
        self.assertIn("[token]", plain_text(posted))
        for existing in (posted, document("Old comment\n[token]")):
            with patch.object(jira, "get", return_value={"comments": [{"body": existing}], "total": 1}), patch.object(jira, "request") as write:
                jira.comment_once("LTRA-11", "[token]", body)
            write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
