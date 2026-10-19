from copy import deepcopy
from dataclasses import replace
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agentic_louis.config import Repository, Workspace
from agentic_louis.github import GitHub
from agentic_louis.process import atomic_json
from agentic_louis.run_agent import terminal
from agentic_louis.watch_prs import PRWatcher, bot_thread, fingerprint
from agentic_louis.worker import main


def thread(author="chatgpt-codex-connector", resolved=False):
    return {"id": "thread-1", "isResolved": resolved, "isOutdated": False,
            "comments": {"pageInfo": {"hasNextPage": False}, "nodes": [
                {"id": "comment-1", "databaseId": 12, "body": "Check this", "updatedAt": "now",
                 "author": {"login": author}}]}}


class GitHubWatchTests(unittest.TestCase):
    def test_explicit_pr_is_fetched_directly_and_rejects_forks(self):
        pr = {"head": {"repo": {"full_name": "org/repo"}}}
        with patch("agentic_louis.github.command", return_value=json.dumps(pr)) as cli:
            self.assertEqual(GitHub().watch_pr("org/repo", 7), pr)
        cli.assert_called_once_with(["gh", "api", "repos/org/repo/pulls/7"])
        for repository in ({"full_name": "other/fork"}, None):
            pr["head"]["repo"] = repository
            with patch("agentic_louis.github.command", return_value=json.dumps(pr)):
                with self.assertRaisesRegex(ValueError, "fork"):
                    GitHub().watch_pr("org/repo", 7)

    def test_pr_discovery_paginates_and_selects_owned_same_repo_prs(self):
        def pr(author, repository):
            return {"user": {"login": author}, "head": {"repo": {"full_name": repository}}}
        own = pr("ME", "org/repo")
        with patch("agentic_louis.github.command", return_value='{"login":"me"}'), patch.object(
                GitHub, "api_pages", return_value=[[own, pr("other", "org/repo")], [pr("me", "me/fork")]]) as pages:
            self.assertEqual(GitHub().watched_prs("org/repo"), [own])
        self.assertIn("per_page=100", pages.call_args.args[0])
        self.assertIn("state=open", pages.call_args.args[0])

    def test_check_runs_and_latest_statuses_across_pages(self):
        run = {"id": 1, "app": {"id": 5}, "name": "test", "status": "completed", "conclusion": "failure"}
        statuses = [[{"id": 3, "context": "external", "state": "success"}],
                    [{"id": 2, "context": "external", "state": "failure"}]]
        with patch.object(GitHub, "api_pages", side_effect=[[{"check_runs": [run]}], statuses]) as pages:
            checks = GitHub().commit_checks("org/repo", "feature/name")
        self.assertEqual([c["state"] for c in checks], ["failure", "success"])
        self.assertIn("feature%2Fname", pages.call_args_list[0].args[0])

    def test_successful_and_running_retries_replace_old_failures(self):
        failed = {"id": 10, "app": {"id": 5}, "name": "test", "status": "completed", "conclusion": "failure",
                  "started_at": "2026-10-19T10:00:00Z"}
        for retry in ({**failed, "id": 11, "conclusion": "success", "started_at": "2026-10-19T11:00:00Z"},
                      {**failed, "id": 9, "conclusion": "success", "started_at": "2026-10-19T11:00:00Z"},
                      {**failed, "id": 11, "status": "in_progress", "conclusion": None, "started_at": None}):
            with self.subTest(retry=retry), patch.object(GitHub, "api_pages", side_effect=[
                    [{"check_runs": [failed]}, {"check_runs": [retry]}], []]):
                checks = GitHub().commit_checks("org/repo", "head")
            self.assertEqual(len(checks), 1)
            self.assertEqual(checks[0]["state"], retry["conclusion"] or retry["status"])

    def test_new_failed_retry_is_not_hidden_by_older_success(self):
        passed = {"id": 10, "app": {"id": 5}, "name": "test", "status": "completed", "conclusion": "success"}
        failed = {**passed, "id": 11, "conclusion": "failure"}
        with patch.object(GitHub, "api_pages", side_effect=[[{"check_runs": [passed, failed]}], []]):
            self.assertEqual(GitHub().commit_checks("org/repo", "head")[0]["state"], "failure")

    def test_review_threads_and_long_comment_conversations_are_paginated(self):
        first = thread()
        first["comments"]["pageInfo"]["hasNextPage"] = True
        second = thread(resolved=True)
        def page(nodes):
            return {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": nodes}}}}}
        replies = [{"data": {"node": {"comments": {"nodes": ["one"]}}}},
                   {"data": {"node": {"comments": {"nodes": ["two"]}}}}]
        with patch("agentic_louis.github.command", side_effect=[json.dumps([page([first]), page([second])]),
                                                                json.dumps(replies)]) as cli:
            result = GitHub().review_threads("org/repo", 7)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["comments"]["nodes"], ["one", "two"])
        for call in cli.call_args_list:
            self.assertIn("--paginate", call.args[0])
            query = next(arg for arg in call.args[0] if arg.startswith("query="))
            self.assertEqual(query.count("{"), query.count("}"))
            self.assertIn("author{login __typename}", query)


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = Repository("repo", self.root / "original", "org/repo", "main", (("test",),))
        self.config = Workspace("trajectory", self.root / "state", None, (self.repo,))
        self.github = Mock()
        self.watcher = PRWatcher(self.config, self.github)
        self.pr = {"number": 7, "title": "Fix the watcher", "html_url": "https://github.com/org/repo/pull/7",
                   "head": {"ref": "feature", "sha": "head"}, "base": {"ref": "main"}, "merge_commit_sha": "merge"}
        self.github.watched_prs.return_value = [self.pr]
        self.github.review_threads.return_value = [thread()]
        self.github.commit_checks.return_value = []
        self.command = patch("agentic_louis.watch_prs.command", return_value='{"sha":"base"}')
        self.cli = self.command.start()
        self.addCleanup(self.command.stop)

    def checkpoint(self, **values):
        directory = self.watcher.directory / "repo" / "7"
        directory.mkdir(parents=True)
        state = {"phase": "done", **values}
        atomic_json(directory / "state.json", state)
        return directory, state

    def test_only_unresolved_bot_threads_trigger_agents(self):
        self.assertTrue(bot_thread(thread()))
        self.assertTrue(bot_thread(thread("chatgpt-codex-connector[bot]")))
        self.assertTrue(bot_thread(thread("reviewer[bot]")))
        typed_bot = thread("automation")
        typed_bot["comments"]["nodes"][0]["author"]["__typename"] = "Bot"
        self.assertTrue(bot_thread(typed_bot))
        self.assertFalse(bot_thread(thread("reviewer[bot]", resolved=True)))
        self.assertFalse(bot_thread(thread(resolved=True)))
        self.assertFalse(bot_thread(thread("human")))
        self.assertFalse(bot_thread(thread(None)))
        self.github.review_threads.return_value = [thread("human"), thread(resolved=True)]
        self.assertIsNone(self.watcher.snapshot(self.repo, self.pr))

    def test_failing_head_and_merge_checks_do_not_prefetch_base_checks(self):
        failure = {"key": "1:test", "id": 10, "name": "test", "state": "failure"}
        self.github.commit_checks.side_effect = [[], [failure]]
        snapshot = self.watcher.snapshot(self.repo, self.pr)
        self.assertEqual(snapshot["failures"], [failure])
        self.assertNotIn("base_checks", snapshot)
        self.assertEqual([c.args[1] for c in self.github.commit_checks.call_args_list], ["head", "merge"])
        self.cli.assert_not_called()

    def test_successful_or_pending_retry_across_head_and_merge_suppresses_failure(self):
        failure = {"key": "1:test", "id": 10, "name": "test", "state": "failure"}
        self.github.review_threads.return_value = []
        for state in ("success", "in_progress", "pending"):
            for reverse in (False, True):
                retry = {**failure, "id": 11, "state": state}
                attempts = [[failure], [retry]]
                if reverse:
                    attempts.reverse()
                with self.subTest(state=state, reverse=reverse):
                    self.github.commit_checks.side_effect = attempts
                    self.assertIsNone(self.watcher.snapshot(self.repo, self.pr))

    def test_failed_jobs_are_not_combined_across_different_ci_providers(self):
        failure = {"key": "1:test", "id": 10, "name": "test", "state": "failure"}
        self.github.commit_checks.side_effect = [[failure], [{**failure, "key": "2:test", "id": 11, "state": "success"}]]
        self.assertEqual(self.watcher.snapshot(self.repo, self.pr)["failures"], [failure])

    def test_dry_run_does_not_write_state_or_launch(self):
        with patch.object(self.watcher, "launch") as launch:
            self.assertEqual(len(self.watcher.tick(dry_run=True)), 1)
        launch.assert_not_called()
        self.assertFalse(self.config.state_dir.exists())

    def test_explicit_pr_only_fetches_selected_repository_and_number(self):
        other = replace(self.repo, name="other", github="org/other")
        watcher = PRWatcher(replace(self.config, repositories=(other, self.repo)), self.github,
                            pr_url=self.pr["html_url"] + "/?tab=files#discussion")
        self.github.watch_pr.return_value = self.pr
        self.assertEqual(len(watcher.tick(dry_run=True)), 1)
        self.github.watch_pr.assert_called_once_with("org/repo", 7)
        self.github.watched_prs.assert_not_called()
        self.assertFalse(self.config.state_dir.exists())

    def test_explicit_pr_does_not_collect_other_pr_agents(self):
        self.checkpoint(phase="running")
        watcher = PRWatcher(self.config, self.github, pr_url="https://github.com/org/repo/pull/8")
        self.github.watch_pr.return_value = {**self.pr, "number": 8, "state": "closed"}
        with patch.object(watcher, "collect") as collect, patch.object(watcher, "launch") as launch:
            self.assertEqual(watcher.tick(), [])
        collect.assert_not_called()
        launch.assert_not_called()

    def test_explicit_pr_validates_url_and_repository(self):
        for url in ("https://example.com/org/repo/pull/7", "https://github.com/org/repo/issues/7",
                    "https://github.com/org/repo/pull/0", "https://github.com/org/other/pull/7"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                PRWatcher(self.config, self.github, pr_url=url)

    def test_dry_run_prints_pr_summary_and_counts_only_bot_comments(self):
        failure = {"key": "1:test", "id": 10, "name": "test", "state": "failure"}
        self.github.commit_checks.return_value = [failure]
        conversation = thread()
        conversation["comments"]["nodes"] += thread("human")["comments"]["nodes"]
        self.github.review_threads.return_value = [conversation, thread("reviewer[bot]"), thread(resolved=True)]
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(self.watcher.run(dry_run=True), 0)
        self.assertEqual(output.getvalue(), "- [7] Fix the watcher\n\n"
                         "  Failing checks: test\n  Bot comments (unresolved): 2\n-------------------------------------\n")
        self.assertFalse(self.config.state_dir.exists())

    def test_dry_run_reports_no_matching_prs(self):
        self.github.review_threads.return_value = []
        output = io.StringIO()
        with redirect_stdout(output):
            self.watcher.run(dry_run=True)
        self.assertEqual(output.getvalue(),
                         "No PRs with failing checks or unresolved bot comments.\n")

    def test_title_edits_do_not_change_feedback_fingerprint(self):
        snapshot = self.watcher.snapshot(self.repo, self.pr)
        self.assertEqual(fingerprint(snapshot), fingerprint({**snapshot, "title": "New title"}))

    def test_dry_run_surfaces_discovery_errors(self):
        self.github.watched_prs.side_effect = ValueError("GitHub unavailable")
        with self.assertRaisesRegex(ValueError, "GitHub unavailable"):
            self.watcher.tick(dry_run=True)

    def test_completed_or_blocked_input_is_not_relaunched(self):
        snapshot = self.watcher.snapshot(self.repo, self.pr)
        directory, state = self.checkpoint(fingerprint=fingerprint(snapshot))
        with patch.object(self.watcher, "launch") as launch:
            self.watcher.tick()
            state["phase"] = "blocked"
            atomic_json(directory / "state.json", state)
            self.watcher.tick()
        launch.assert_not_called()

    def test_new_comments_and_checks_on_same_commit_do_not_start_new_work(self):
        snapshot = self.watcher.snapshot(self.repo, self.pr)
        self.github.review_threads.reset_mock()
        self.checkpoint(fingerprint=fingerprint(snapshot))
        updated = thread()
        updated["comments"]["nodes"][0]["body"] = "New information"
        self.github.review_threads.return_value = [updated]
        self.github.commit_checks.return_value = [
            {"key": "1:test", "id": 11, "name": "test", "state": "failure"}]
        with patch.object(self.watcher, "launch") as launch:
            self.watcher.tick()
        launch.assert_not_called()
        self.github.review_threads.assert_not_called()

    def test_changed_head_starts_new_work(self):
        snapshot = self.watcher.snapshot(self.repo, self.pr)
        self.checkpoint(fingerprint=fingerprint(snapshot), attempted_heads=["head"])
        self.pr["head"]["sha"] = "new-head"
        with patch.object(self.watcher, "launch") as launch:
            self.watcher.tick()
        launch.assert_called_once()
        changed = deepcopy(snapshot)
        changed["head"] = "new-head"
        self.assertNotEqual(fingerprint(snapshot), fingerprint(changed))

    def test_previously_attempted_commit_is_skipped_after_head_changes_and_restart(self):
        self.checkpoint(attempted_heads=["head", "new-head"], fingerprint="latest")
        restarted = PRWatcher(self.config, self.github)
        with patch.object(restarted, "launch") as launch:
            self.assertEqual(restarted.tick(), [])
            self.assertEqual(restarted.tick(dry_run=True), [])
        launch.assert_not_called()
        self.github.review_threads.assert_not_called()

    def test_closed_prs_are_not_processed(self):
        self.pr["state"] = "closed"
        with patch.object(self.watcher, "launch") as launch:
            self.assertEqual(self.watcher.tick(), [])
        launch.assert_not_called()
        self.github.review_threads.assert_not_called()

    def test_running_supervisor_survives_watcher_restart_and_prevents_overlap(self):
        attempt = self.root / "attempt"
        attempt.mkdir()
        atomic_json(attempt / "supervisor.json", {"pid": 42, "identity": "start", "token": "token"})
        self.checkpoint(phase="running", attempt=str(attempt), token="token", started=0)
        with patch("agentic_louis.watch_prs.identity", return_value="start"), patch.object(self.watcher, "launch") as launch:
            self.watcher.tick()
        launch.assert_not_called()
        self.github.review_threads.assert_not_called()

    def test_collect_requires_successful_report_and_matching_exit_token(self):
        attempt = self.root / "attempt"
        attempt.mkdir()
        directory, state = self.checkpoint(phase="running", attempt=str(attempt), token="token")
        atomic_json(attempt / "exit.json", {"token": "token", "exit_code": 0})
        self.assertEqual(self.watcher.collect(directory, state)["phase"], "blocked")
        state["phase"] = "running"
        atomic_json(attempt / "result.json", {"outcome": "done", "summary": "fixed"})
        self.assertEqual(self.watcher.collect(directory, state)["phase"], "done")
        state["phase"] = "running"
        state["token"] = "other"
        with self.assertRaisesRegex(ValueError, "token"):
            self.watcher.collect(directory, state)

    def test_orphan_cleanup_happens_before_allowing_new_agent(self):
        attempt = self.root / "attempt"
        attempt.mkdir()
        directory, state = self.checkpoint(phase="running", attempt=str(attempt), token="token", started=0)
        with patch("agentic_louis.watch_prs.stop_terminal") as stop:
            self.assertEqual(self.watcher.collect(directory, state)["phase"], "blocked")
        stop.assert_called_once_with(attempt, "token")

    def test_launch_refuses_a_head_that_changed_during_fetch(self):
        snapshot = self.watcher.snapshot(self.repo, self.pr)
        self.cli.return_value = "new-head"
        directory = self.watcher.directory / "repo" / "7"
        with patch("agentic_louis.watch_prs.subprocess.Popen") as spawn:
            with self.assertRaisesRegex(ValueError, "head changed"):
                self.watcher.launch(self.repo, snapshot, directory, fingerprint(snapshot))
        spawn.assert_not_called()
        self.assertEqual(json.loads((directory / "state.json").read_text())["phase"], "blocked")

    def test_retry_blocked_is_once_per_invocation(self):
        snapshot = self.watcher.snapshot(self.repo, self.pr)
        self.checkpoint(phase="blocked", fingerprint=fingerprint(snapshot))
        self.watcher.retry_blocked = True
        with patch.object(self.watcher, "launch") as launch:
            self.watcher.tick()
            self.watcher.tick()
        launch.assert_called_once()

    def test_successful_launch_saves_supervision_and_defers_setup(self):
        snapshot = self.watcher.snapshot(self.repo, self.pr)
        snapshot["failures"] = [{"name": "unit tests"}, {"name": "unit tests"}]
        directory = self.watcher.directory / "repo" / "7"
        def git(args, **kwargs):
            if args[:3] == ["git", "worktree", "add"]:
                Path(args[-2]).mkdir()
            if args[:2] == ["git", "rev-parse"]:
                return "head" if args[-1].startswith("refs/agentic-watch-prs/") else str(self.root / ".git")
            return ""
        self.cli.side_effect = git
        repo = replace(self.repo, setup=(("install",),))
        self.checkpoint(attempted_heads=["old-head"])
        with patch("agentic_louis.watch_prs.subprocess.Popen") as spawn, self.assertLogs(
                "agentic_louis.watch_prs", level="INFO") as logs:
            self.watcher.launch(repo, snapshot, directory, fingerprint(snapshot))
        state = json.loads((directory / "state.json").read_text())
        launch = json.loads((Path(state["attempt"]) / "launch.json").read_text())
        self.assertEqual(state["phase"], "running")
        self.assertEqual(state["attempted_heads"], ["head", "old-head"])
        self.assertEqual(launch["token"], state["token"])
        self.assertEqual(launch["transport"], "tmux")
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].getMessage(),
                         "Started agent for PR #7: Fix the watcher\n"
                         "  https://github.com/org/repo/pull/7\n"
                         "  Feedback: 1 failing check, 1 unresolved bot comment\n"
                         f"  Attach: tmux -L agentic-louis-{state['token']} "
                         "-f /dev/null attach-session -t agent\n")
        self.assertEqual(launch["args"][launch["args"].index("--model") + 1], "gpt-6.1-sol")
        self.assertEqual(launch["setup"], [["install"]])
        self.assertFalse(any(call.args[0] == ["install"] for call in self.cli.call_args_list))
        self.assertEqual(launch["setup_cwd"], str(Path(state["attempt"]) / "repository"))
        self.assertTrue(spawn.call_args.kwargs["start_new_session"])

    def test_terminal_setup_failure_never_starts_codex(self):
        attempt = self.root / "attempt"
        attempt.mkdir()
        atomic_json(attempt / "launch.json", {"token": "token", "setup": [["install"]],
                                              "setup_cwd": str(self.root), "timeout": 5, "args": ["codex"]})
        with patch("agentic_louis.run_agent.identity", return_value="start"), patch(
                "agentic_louis.run_agent.command", side_effect=ValueError("install failed")), patch(
                "agentic_louis.run_agent.subprocess.call") as codex:
            self.assertEqual(terminal(attempt), 1)
        codex.assert_not_called()
        self.assertEqual(json.loads((attempt / "terminal-exit.json").read_text())["error"], "install failed")

    def test_terminal_authenticates_on_host_and_only_passes_token_in_environment(self):
        attempt = self.root / "attempt"
        attempt.mkdir()
        saved = {"token": "launch-token", "args": ["codex"], "cwd": str(attempt),
                 "gitlab_repository": "DataDog/trajectory"}
        atomic_json(attempt / "launch.json", saved)
        with patch("agentic_louis.run_agent.identity", return_value="start"), patch(
                "agentic_louis.run_agent.gitlab_project_token", return_value="private-gitlab-credential") as auth, patch(
                "agentic_louis.run_agent.subprocess.call", return_value=0) as codex, redirect_stdout(io.StringIO()):
            self.assertEqual(terminal(attempt), 0)
        auth.assert_called_once_with("DataDog", "trajectory")
        self.assertEqual(codex.call_args.kwargs["env"]["GITLAB_TOKEN"], "private-gitlab-credential")
        self.assertEqual(codex.call_args.kwargs["env"]["GITLAB_HOST"], "gitlab.ddbuild.io")
        self.assertEqual(codex.call_args.args[0], ["codex"])
        for path in attempt.glob("*.json"):
            self.assertNotIn("private-gitlab-credential", path.read_text())

    def test_terminal_auth_failure_never_starts_codex(self):
        attempt = self.root / "attempt"
        attempt.mkdir()
        atomic_json(attempt / "launch.json", {"token": "launch-token", "args": ["codex"],
                                              "cwd": str(attempt), "gitlab_repository": "DataDog/trajectory"})
        with patch("agentic_louis.run_agent.identity", return_value="start"), patch(
                "agentic_louis.run_agent.gitlab_project_token", side_effect=ValueError("GitLab authentication failed")), patch(
                "agentic_louis.run_agent.subprocess.call") as codex:
            self.assertEqual(terminal(attempt), 1)
        codex.assert_not_called()
        self.assertEqual(json.loads((attempt / "terminal-exit.json").read_text())["error"], "GitLab authentication failed")

    def test_datadog_launch_enables_host_authentication_and_prompt_explains_token_use(self):
        repo = replace(self.repo, github="DataDog/trajectory")
        snapshot = self.watcher.snapshot(repo, self.pr)
        def git(args, **kwargs):
            if args[:3] == ["git", "worktree", "add"]:
                Path(args[-2]).mkdir()
            if args[:2] == ["git", "rev-parse"]:
                return "head" if args[-1].startswith("refs/agentic-watch-prs/") else str(self.root / ".git")
            return ""
        self.cli.side_effect = git
        directory = self.watcher.directory / "repo" / "7"
        with patch("agentic_louis.watch_prs.subprocess.Popen"), patch(
                "agentic_louis.run_agent.gitlab_project_token") as auth:
            self.watcher.launch(repo, snapshot, directory, fingerprint(snapshot))
        auth.assert_not_called()  # Acquisition is deferred to the supervised host terminal.
        state = json.loads((directory / "state.json").read_text())
        launch = json.loads((Path(state["attempt"]) / "launch.json").read_text())
        self.assertEqual(launch["gitlab_repository"], "DataDog/trajectory")
        self.assertIn("GITLAB_HOST and GITLAB_TOKEN", launch["prompt"])
        self.assertIn("do not call ddtool", launch["prompt"])

    def test_other_repositories_do_not_require_ddtool(self):
        attempt = self.root / "attempt"
        attempt.mkdir()
        atomic_json(attempt / "launch.json", {"token": "launch-token", "args": ["codex"], "cwd": str(attempt)})
        with patch("agentic_louis.run_agent.identity", return_value="start"), patch(
                "agentic_louis.run_agent.gitlab_project_token") as auth, patch(
                "agentic_louis.run_agent.subprocess.call", return_value=0):
            self.assertEqual(terminal(attempt), 0)
        auth.assert_not_called()

    def test_poll_interval_does_not_wait_for_agent_completion(self):
        with patch.object(self.watcher, "tick") as tick, patch("agentic_louis.watch_prs.time.monotonic",
                side_effect=[100, 102]), patch("agentic_louis.watch_prs.time.sleep", side_effect=KeyboardInterrupt) as sleep:
            with self.assertRaises(KeyboardInterrupt):
                self.watcher.run()
        tick.assert_called_once()
        sleep.assert_called_once_with(298)

    def test_prompt_requires_validation_then_push_then_thread_updates(self):
        snapshot = self.watcher.snapshot(self.repo, self.pr)
        prompt = self.watcher.prompt(snapshot, self.root, self.root, self.repo, self.root)
        for requirement in ("independently double-check", "Fetch logs yourself", "commit the changes and push HEAD",
                            "Never force-push", "outcome=blocked", "resolveReviewThread",
                            "only after any required push succeeds", "without an empty commit"):
            self.assertIn(requirement, prompt)
        self.assertLess(prompt.index("validation passes"), prompt.index("commit the changes"))
        self.assertLess(prompt.index("commit the changes"), prompt.index("Then use gh api"))
        self.assertNotIn("TEMPORARY UX TESTING", prompt)
        self.assertNotIn("Do not commit or push", prompt)
        self.assertNotIn("Do not publish these replies", prompt)

    def test_prompt_only_passes_failed_job_names_as_ci_context(self):
        snapshot = self.watcher.snapshot(self.repo, self.pr)
        snapshot["failures"] = [{"name": "test", "id": 987, "state": "failure", "url": "https://ci.example/job"},
                                {"name": "test", "id": 988}, {"name": "lint", "id": 989}]
        snapshot["base_checks"] = [{"name": "base-ci-secret"}]
        prompt = self.watcher.prompt(snapshot, self.root, self.root, self.repo, self.root)
        context = json.loads("{" + prompt.rsplit("\n{", 1)[1])
        self.assertEqual(context["failed_jobs"], ["lint", "test"])
        self.assertNotIn("failures", context)
        self.assertNotIn("base_checks", context)
        self.assertNotIn("checks", context)
        self.assertNotIn("https://ci.example/job", prompt)
        self.assertNotIn("base-ci-secret", prompt)
        self.assertEqual(context["threads"], snapshot["threads"])
        self.assertIn("Recheck their latest attempts first", prompt)
        self.assertIn("superseded by successful retries", prompt)

    def test_cli_forwards_pr_url(self):
        with patch("agentic_louis.worker.Workspace.discover", return_value=self.config), patch(
                "agentic_louis.watch_prs.PRWatcher") as watcher:
            watcher.return_value.run.return_value = 0
            self.assertEqual(main(["watch-prs", "--project", "trajectory", "--pr", self.pr["html_url"],
                                   "--dry-run"]), 0)
        watcher.assert_called_once_with(self.config, unittest.mock.ANY, retry_blocked=False,
                                        pr_url=self.pr["html_url"])

    def test_cli_positional_url_only_processes_requested_pr(self):
        other = replace(self.repo, name="other", github="org/other")
        config = replace(self.config, repositories=(other, self.repo))
        self.github.watch_pr.return_value = self.pr
        with patch("agentic_louis.worker.Workspace.discover", return_value=config), patch(
                "agentic_louis.worker.GitHub", return_value=self.github), patch.object(PRWatcher, "launch") as launch:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["watch-prs", self.pr["html_url"], "--project", "trajectory", "--dry-run"]), 0)
        self.github.watch_pr.assert_called_once_with("org/repo", 7)
        self.github.watched_prs.assert_not_called()
        self.github.review_threads.assert_called_once_with("org/repo", 7)
        launch.assert_not_called()

    def test_cli_invalid_positional_url_never_scans_all_prs(self):
        with patch("agentic_louis.worker.Workspace.discover", return_value=self.config), patch(
                "agentic_louis.worker.GitHub", return_value=self.github):
            self.assertEqual(main(["watch-prs", "not-a-pr-url", "--project", "trajectory"]), 1)
        self.github.watched_prs.assert_not_called()
        self.github.watch_pr.assert_not_called()

    def test_cli_rejects_two_pr_urls(self):
        with patch("sys.stderr", new_callable=io.StringIO), patch.object(PRWatcher, "run") as run:
            with self.assertRaises(SystemExit) as error:
                main(["watch-prs", self.pr["html_url"], "--pr", self.pr["html_url"], "--project", "trajectory"])
        self.assertEqual(error.exception.code, 2)
        run.assert_not_called()

    def test_startup_logs_selected_scope_before_polling(self):
        watcher = PRWatcher(self.config, self.github, pr_url=self.pr["html_url"])
        with patch.object(watcher, "tick"), patch("agentic_louis.watch_prs.time.sleep", side_effect=KeyboardInterrupt), \
                self.assertLogs("agentic_louis.watch_prs", level="INFO") as logs:
            with self.assertRaises(KeyboardInterrupt):
                watcher.run()
        self.assertIn("Watching https://github.com/org/repo/pull/7 only", logs.output[0])
        self.assertIn("Model: gpt-6.1-sol", logs.output[0])
        self.assertNotIn("Testing:", logs.output[0])
        self.github.check.assert_called_once_with("org/repo")

    def test_cli_rejects_pr_option_for_other_actions(self):
        with redirect_stdout(io.StringIO()), patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as error:
                main(["run", "--project", "trajectory", "--pr", self.pr["html_url"]])
        self.assertEqual(error.exception.code, 2)

    def test_cli_watch_does_not_require_jira_or_slack(self):
        with patch("agentic_louis.worker.Workspace.discover", return_value=self.config), patch(
                "agentic_louis.worker.Jira") as jira, patch.object(PRWatcher, "run", return_value=0) as run:
            self.assertEqual(main(["watch-prs", "--project", "trajectory", "--dry-run"]), 0)
        jira.assert_not_called()
        run.assert_called_once_with(dry_run=True)


if __name__ == "__main__":
    unittest.main()
