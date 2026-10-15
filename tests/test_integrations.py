import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from agentic_louis.github import GitHub
from agentic_louis import ROOT
from agentic_louis.jira import ApiError, Jira
from agentic_louis.process import CommandError, atomic_json, command
from agentic_louis.webhook import send
from test_workflow import issue


class Response(io.BytesIO):
    status = 200


class CommandTests(unittest.TestCase):
    def test_git_failure_includes_stderr_command_and_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(CommandError) as raised:
                command(["git", "worktree", "list"], cwd=directory, env={"PATH": os.environ["PATH"]})
            message = str(raised.exception)
            self.assertIn("git worktree list failed (exit 128)", message)
            self.assertIn("not a git repository", message)
            self.assertIn(str(Path(directory).resolve()), message)

    def test_failure_preserves_diagnostic_but_redacts_credentials(self):
        script = "import os, sys; sys.stderr.write('fatal: denied ' + os.environ['TEST_API_TOKEN'] + ' https://user:pass@example.com/repo'); sys.exit(1)"
        with self.assertRaises(CommandError) as raised:
            command([sys.executable, "-c", script], env={"TEST_API_TOKEN": "test-private-token"})
        message = str(raised.exception)
        self.assertIn("fatal: denied [redacted] https://[redacted]@example.com/repo", message)
        self.assertNotIn("test-private-token", message)
        self.assertNotIn("user:pass", message)


class TransportTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, {"JIRA_EMAIL": "test@example.com", "JIRA_API_TOKEN": "private"}):
            self.jira = Jira()
        self.jira.opener = Mock()

    def response(self, value):
        self.jira.opener.open.return_value = Response(json.dumps(value).encode())

    def test_empty_write_response(self):
        self.jira.opener.open.return_value = Response(b"")
        self.assertEqual(self.jira.request("POST", "/rest/api/3/issue/LTRA-1/transitions", payload={}), {})

    def test_rate_limits_keep_retry_after_and_hide_credentials(self):
        self.jira.opener.open.side_effect = HTTPError("https://jira.example", 429, "Too many requests", {"Retry-After": "12"}, None)
        with self.assertRaises(ApiError) as raised:
            self.jira.get("/test")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.retry_after, 12)
        self.assertNotIn("private", str(raised.exception))

    def test_network_failure_marks_writes_uncertain(self):
        self.jira.opener.open.side_effect = URLError("Disconnected")
        with self.assertRaises(ApiError) as raised:
            self.jira.request("POST", "/test", payload={})
        self.assertTrue(raised.exception.uncertain)

    def test_auth_error_is_not_retryable(self):
        self.jira.opener.open.side_effect = HTTPError("https://jira.example", 403, "Forbidden", {}, None)
        with self.assertRaises(ApiError) as raised:
            self.jira.get("/test")
        self.assertFalse(raised.exception.retryable)

    def test_board_pagination_is_preserved(self):
        pages = [{"issues": [{"key": "LTRA-1"}], "isLast": False, "nextPageToken": "second"},
                 {"issues": [{"key": "LTRA-2"}], "isLast": True}]
        with patch.object(self.jira, "get", side_effect=pages):
            self.assertEqual(len(self.jira.candidates("proj-trajectory", ["1"])), 2)

    def test_transition_requires_unique_available_destination(self):
        with patch.object(self.jira, "issue", return_value=issue("LTRA-1")), patch.object(self.jira, "get", return_value={"transitions": []}):
            with self.assertRaisesRegex(ApiError, "Expected one"):
                self.jira.transition("LTRA-1", ["2"])

    def test_transition_reconciles_already_changed_status(self):
        with patch.object(self.jira, "issue", return_value=issue("LTRA-1", "2")), patch.object(self.jira, "request") as write:
            self.jira.transition("LTRA-1", ["2"])
        write.assert_not_called()

    def test_block_uses_destination_status_name_without_requiring_a_board_column(self):
        transitions = {"transitions": [{"id": "42", "name": "Stop work", "to": {"id": "5", "name": "Blocked"}}]}
        with patch.object(self.jira, "issue", return_value=issue("LTRA-1", "2")), patch.object(self.jira, "get", return_value=transitions), patch.object(self.jira, "transition") as transition:
            self.jira.block("LTRA-1")
        transition.assert_called_once_with("LTRA-1", ["5"])

    def test_already_blocked_issue_needs_no_transition(self):
        with patch.object(self.jira, "issue", return_value=issue("LTRA-1", "5")), patch.object(self.jira, "request") as write:
            self.jira.block("LTRA-1")
        write.assert_not_called()

    def test_comment_token_prevents_repeat_after_uncertain_response(self):
        from agentic_louis.jira import document
        pages = [{"comments": [{"body": document("Done\n[token]")}], "total": 1}]
        with patch.object(self.jira, "get", side_effect=pages), patch.object(self.jira, "request") as write:
            self.jira.comment_once("LTRA-1", "[token]", "Done")
        write.assert_not_called()

    def test_github_rejects_foreign_urls_without_invoking_cli(self):
        for url in ("https://github.com/other/repo/pull/1", "https://evil.example/DataDog/repo/pull/1",
                    "https://github.com/DataDog/repo/pull/1?redirect=somewhere"):
            with patch("agentic_louis.github.command") as cli, self.assertRaises(ValueError):
                GitHub().view(url, "DataDog/repo")
            cli.assert_not_called()

    def test_slack_failure_and_payload_contract(self):
        opener = Mock()
        opener.open.return_value = Response(b'{"ok":true}')
        payload = {"jira_ticket_name": "Task", "project_label": "proj-trajectory", "jira_ticket_url": "https://jira.example/LTRA-1", "prs": "one\ntwo"}
        with patch("agentic_louis.webhook.build_opener", return_value=opener):
            send("https://hooks.slack.com/triggers/test", payload)
        request = opener.open.call_args.args[0]
        self.assertEqual(json.loads(request.data), payload)
        opener.open.return_value = Response(b'{"ok":false}')
        with patch("agentic_louis.webhook.build_opener", return_value=opener), self.assertRaisesRegex(ValueError, "ok=false"):
            send("https://hooks.slack.com/triggers/test", payload)


class SupervisorTests(unittest.TestCase):
    def supervise(self, script, timeout=10):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        directory = Path(self.temp.name)
        atomic_json(directory / "launch.json", {"token": "test-launch", "args": [sys.executable, "-c", script],
                                                "cwd": str(ROOT), "prompt": "task context", "timeout": timeout})
        process = subprocess.Popen([sys.executable, "-m", "agentic_louis.run_agent", str(directory)],
                                   cwd=ROOT, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            stdout, stderr = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, 9)
            process.communicate()
            self.fail("Supervisor did not finish within the test deadline.")
        self.assertEqual(process.returncode, 0, stderr.decode())
        return directory

    def test_persists_exit_and_captures_stdin_and_logs(self):
        directory = self.supervise("import sys; print(sys.stdin.read()); print('diagnostic', file=sys.stderr)")
        self.assertEqual(json.loads((directory / "exit.json").read_text())["exit_code"], 0)
        self.assertIn("task context", (directory / "events.jsonl").read_text())
        self.assertIn("diagnostic", (directory / "stderr.log").read_text())
        self.assertEqual(json.loads((directory / "supervisor.json").read_text())["token"], "test-launch")

    def test_timeout_records_failure_and_stops_child(self):
        directory = self.supervise("import time; time.sleep(60)", timeout=0.1)
        result = json.loads((directory / "exit.json").read_text())
        self.assertNotEqual(result["exit_code"], 0)
        self.assertIn("timeout", result["error"])
        pid = json.loads((directory / "child.json").read_text())["pid"]
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


if __name__ == "__main__":
    unittest.main()
