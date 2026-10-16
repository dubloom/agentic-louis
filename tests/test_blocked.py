from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid
import sqlite3

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
import questionary
from rich.console import Console

from agentic_louis.blocked import interrupted_session, session_for
from agentic_louis.process import atomic_json
from agentic_louis.state import State
from agentic_louis.worker import main
from test_workflow import FakeJira, issue


class BlockedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.jira = FakeJira()
        self.output = io.StringIO()
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root)}))
        stack.enter_context(patch("agentic_louis.worker.Jira", return_value=self.jira))
        stack.enter_context(redirect_stdout(self.output))
        stack.enter_context(patch("agentic_louis.blocked.sys.stdin.isatty", return_value=True))
        stack.enter_context(patch("agentic_louis.blocked.sys.stdout.isatty", return_value=True))
        stack.enter_context(patch("agentic_louis.blocked.make_console", side_effect=lambda: Console(color_system=None)))
        self.launch = stack.enter_context(patch("agentic_louis.blocked.subprocess.call", return_value=0))

    def saved_run(self, key="LTRA-1", project="trajectory", *, state_dir=None, attempt=1):
        self.jira.issues[key] = issue(key, "5", project)
        directory = state_dir or self.root / "agentic-louis" / project
        task = directory / "runs" / key
        attempt_dir = task / f"attempt-{attempt}"
        attempt_dir.mkdir(parents=True)
        session = str(uuid.uuid4())
        run = {"key": key, "project": project, "phase": "blocked", "attempt": attempt,
               "attempt_dir": str(attempt_dir), "launch_token": "launch-token", "error": "Tests need Docker."}
        atomic_json(attempt_dir / "launch.json", {"cwd": str(task), "token": "launch-token"})
        (attempt_dir / "events.jsonl").write_text(json.dumps({"type": "thread.started", "thread_id": session}) + "\n")
        self.jira.save_run(key, run)
        return run, session, task

    def test_bare_command_selects_across_projects_and_inherits_terminal(self):
        self.saved_run()
        run, session, task = self.saved_run("LTRA-2", "dd-trace-py")
        before = deepcopy(self.jira.issues)
        with patch("agentic_louis.blocked.selection_prompt", return_value="LTRA-2") as picker, patch.dict(os.environ, {
                "JIRA_API_TOKEN": "private-token", "JIRA_EMAIL": "private-email", "SLACK_WORKFLOW_URL": "secret"}):
            self.assertEqual(main(["blocked"]), 0)
        tickets = picker.call_args.args[1]
        self.assertEqual({entry[0]["key"] for entry in tickets}, {"LTRA-1", "LTRA-2"})
        self.assertTrue(all(entry[2] == "Tests need Docker." for entry in tickets))
        self.launch.assert_called_once()
        args, kwargs = self.launch.call_args
        self.assertEqual(args[0], ["codex", "resume", session, "--cd", str(task), "--ask-for-approval", "on-request"])
        self.assertEqual(kwargs["cwd"], task)
        for name in ("JIRA_API_TOKEN", "JIRA_EMAIL", "SLACK_WORKFLOW_URL"):
            self.assertNotIn(name, kwargs["env"])
        for name in ("stdin", "stdout", "stderr", "start_new_session"):
            self.assertNotIn(name, kwargs)
        self.assertEqual(self.jira.issues, before)

    def test_arrow_keys_and_enter_resume_highlighted_ticket(self):
        self.saved_run()
        self.jira.issues["LTRA-2"] = issue("LTRA-2", "5")  # No saved session: arrows skip it.
        _, session, _ = self.saved_run("LTRA-3")
        select = questionary.select
        with create_pipe_input() as keys:
            keys.send_text("\x1b[B\x1b[A\x1b[B\r")  # Down, up, down, Enter.
            with patch("agentic_louis.terminal.questionary.select",
                       side_effect=lambda *a, **kw: select(*a, **kw, input=keys, output=DummyOutput())):
                self.assertEqual(main(["blocked"]), 0)
        self.assertEqual(self.launch.call_args.args[0][2], session)

    def test_arrow_picker_ctrl_c_cancels_without_launching(self):
        self.saved_run()
        select = questionary.select
        with create_pipe_input() as keys:
            keys.send_text("\x03")
            with patch("agentic_louis.terminal.questionary.select",
                       side_effect=lambda *a, **kw: select(*a, **kw, input=keys, output=DummyOutput())):
                self.assertEqual(main(["blocked"]), 0)
        self.launch.assert_not_called()

    def test_arrow_picker_cancel_option_does_not_launch(self):
        self.saved_run()
        select = questionary.select
        with create_pipe_input() as keys:
            keys.send_text("\x1b[B\r")
            with patch("agentic_louis.terminal.questionary.select",
                       side_effect=lambda *a, **kw: select(*a, **kw, input=keys, output=DummyOutput())):
                self.assertEqual(main(["blocked"]), 0)
        self.launch.assert_not_called()

    def test_arrow_picker_without_saved_sessions_selects_cancel(self):
        self.jira.issues["LTRA-1"] = issue("LTRA-1", "5")
        select = questionary.select
        with create_pipe_input() as keys:
            keys.send_text("\r")
            with patch("agentic_louis.terminal.questionary.select",
                       side_effect=lambda *a, **kw: select(*a, **kw, input=keys, output=DummyOutput())):
                self.assertEqual(main(["blocked"]), 0)
        self.launch.assert_not_called()

    def test_project_filter_custom_directory_profile_and_exit_code(self):
        custom = self.root / "custom"
        self.saved_run("LTRA-2", "dd-trace-py")
        _, session, task = self.saved_run(state_dir=custom)
        self.launch.return_value = 7
        with patch("agentic_louis.blocked.selection_prompt") as prompt:
            self.assertEqual(main(["blocked", "LTRA-1", "--project", "trajectory", "--state-dir", str(custom),
                                   "--profile", "personal"]), 7)
        prompt.assert_not_called()
        self.assertNotIn("LTRA-2", self.output.getvalue())
        self.assertEqual(self.launch.call_args.args[0][2], session)
        self.assertEqual(self.launch.call_args.args[0][-2:], ["--profile", "personal"])
        self.assertEqual(self.launch.call_args.kwargs["cwd"], task)

    def test_works_while_worker_holds_lock_and_needs_no_repository(self):
        _, _, task = self.saved_run()
        state = State(task.parent.parent, "trajectory", self.jira)
        self.addCleanup(state.close)
        with patch("agentic_louis.worker.Workspace.discover") as discover:
            self.assertEqual(main(["blocked", "LTRA-1"]), 0)
        discover.assert_not_called()
        self.launch.assert_called_once()

    def test_only_managed_currently_blocked_tickets_are_listed(self):
        self.jira.issues = {i["key"]: i for i in [issue("LTRA-1"), issue("LTRA-2", "5", managed=False),
                                                 issue("LTRA-3", "3"), issue("LTRA-4", "5")]}
        self.jira.issues["LTRA-5"] = issue("LTRA-5", "5")
        self.jira.issues["LTRA-5"]["fields"]["labels"].append("extra")
        with patch("agentic_louis.blocked.selection_prompt", return_value=None) as picker:
            self.assertEqual(main(["blocked"]), 0)
        tickets = picker.call_args.args[1]
        self.assertEqual([entry[0]["key"] for entry in tickets], ["LTRA-4"])
        self.assertIn("Session unavailable", tickets[0][2])
        self.assertFalse(tickets[0][3])
        self.launch.assert_not_called()

    def test_empty_list_does_not_prompt_or_launch(self):
        with patch("agentic_louis.blocked.selection_prompt") as prompt:
            self.assertEqual(main(["blocked"]), 0)
        prompt.assert_not_called()
        self.launch.assert_not_called()
        self.assertIn("No blocked agent tickets", self.output.getvalue())

    def test_dry_run_lists_without_terminal_or_prompt(self):
        self.saved_run()
        with patch("agentic_louis.blocked.selection_prompt") as prompt, patch("agentic_louis.blocked.sys.stdin.isatty", return_value=False):
            self.assertEqual(main(["blocked", "--dry-run"]), 0)
        prompt.assert_not_called()
        self.launch.assert_not_called()

    def test_noninteractive_open_fails_with_helpful_error(self):
        self.saved_run()
        with patch("agentic_louis.blocked.sys.stdin.isatty", return_value=False), self.assertLogs(level="ERROR") as logs:
            self.assertEqual(main(["blocked"]), 1)
        self.assertIn("interactive terminal", " ".join(logs.output))
        self.launch.assert_not_called()

    def test_cancellation_and_eof_do_not_launch(self):
        self.saved_run()
        for answer in (None, "", EOFError()):
            with self.subTest(answer=answer), patch("agentic_louis.blocked.selection_prompt", side_effect=[answer]):
                self.assertEqual(main(["blocked"]), 0)
        self.launch.assert_not_called()

    def test_status_change_during_selection_prevents_launch(self):
        self.saved_run()
        def choose(*_):
            self.jira.issues["LTRA-1"]["fields"]["status"] = {"id": "6", "name": "Ready"}
            return "LTRA-1"
        with patch("agentic_louis.blocked.selection_prompt", side_effect=choose), self.assertLogs(level="ERROR"):
            self.assertEqual(main(["blocked"]), 1)
        self.launch.assert_not_called()

    def test_live_previous_process_prevents_launch(self):
        run, _, _ = self.saved_run()
        atomic_json(Path(run["attempt_dir"]) / "child.json", {"token": run["launch_token"], "pid": 123, "identity": "same-process"})
        with patch("agentic_louis.blocked.identity", return_value="same-process"), self.assertLogs(level="ERROR") as logs:
            self.assertEqual(main(["blocked", "LTRA-1"]), 1)
        self.assertIn("still running", " ".join(logs.output))
        self.launch.assert_not_called()

    def test_resume_completed_attempt_without_discovery_or_mutations(self):
        run, session, task = self.saved_run()
        before = deepcopy(self.jira.issues)
        self.launch.return_value = 7
        with patch("agentic_louis.worker.Workspace.discover") as discover:
            self.assertEqual(main(["--project", "trajectory", "resume", "LTRA-1", "--attempt", "1",
                                   "--profile", "personal"]), 7)
        discover.assert_not_called()
        self.assertEqual(self.launch.call_args.args[0], ["codex", "resume", session, "--cd", str(task),
                                                         "--ask-for-approval", "on-request", "--profile", "personal"])
        self.assertEqual(self.jira.issues, before)

    def test_resume_rejects_live_supervisor_child_or_fallback_pid(self):
        run, _, _ = self.saved_run()
        for name in ("supervisor.json", "child.json", None):
            with self.subTest(process=name):
                if name:
                    path = Path(run["attempt_dir"]) / name
                    atomic_json(path, {"token": run["launch_token"], "pid": 123, "identity": "live"})
                else:
                    self.jira.save_run("LTRA-1", {**run, "supervisor_pid": 123, "supervisor_identity": "live"})
                with patch("agentic_louis.blocked.identity", return_value="live"), self.assertLogs(level="ERROR") as logs:
                    self.assertEqual(main(["--project", "trajectory", "resume", "LTRA-1", "--attempt", "1"]), 1)
                self.assertIn("still running", " ".join(logs.output))
                if name:
                    path.unlink()
        self.launch.assert_not_called()

    def test_resume_rejects_stale_attempt_and_unfinished_worker(self):
        run, _, _ = self.saved_run()
        for changes, attempt in (({}, "2"), ({"phase": "verifying"}, "1"), ({"phase": "running"}, "1")):
            self.jira.save_run("LTRA-1", {**run, **changes})
            with self.subTest(changes=changes, attempt=attempt), self.assertLogs(level="ERROR"):
                self.assertEqual(main(["--project", "trajectory", "resume", "LTRA-1", "--attempt", attempt]), 1)
        self.launch.assert_not_called()

    def test_resume_allows_review_but_rejects_requeued_ticket(self):
        run, _, _ = self.saved_run()
        self.jira.save_run("LTRA-1", {**run, "phase": "review"})
        self.jira.issues["LTRA-1"]["fields"]["status"] = {"id": "3", "name": "Custom review status"}
        args = ["--project", "trajectory", "resume", "LTRA-1", "--attempt", "1"]
        self.assertEqual(main(args), 0)
        self.launch.reset_mock()
        self.jira.issues["LTRA-1"]["fields"]["status"] = {"id": "6", "name": "Ready"}
        with self.assertLogs(level="ERROR"):
            self.assertEqual(main(args), 1)
        self.launch.assert_not_called()

    def test_resume_dry_run_and_noninteractive(self):
        self.saved_run()
        args = ["--project", "trajectory", "resume", "LTRA-1", "--attempt", "1"]
        self.assertEqual(main([*args, "--dry-run"]), 0)
        with patch("agentic_louis.blocked.sys.stdin.isatty", return_value=False), self.assertLogs(level="ERROR"):
            self.assertEqual(main(args), 1)
        self.launch.assert_not_called()

    def test_only_latest_attempt_is_used_and_incomplete_events_are_tolerated(self):
        old, old_session, task = self.saved_run()
        run, session, _ = self.saved_run(attempt=2)
        events = Path(run["attempt_dir"]) / "events.jsonl"
        events.write_text('not json\nnull\n' + events.read_text() + '{"type":')
        self.assertEqual(session_for(run, "trajectory", task.parent.parent, "LTRA-1"), (session, task))
        self.assertNotEqual(session, old_session)
        events.write_text('{"type":')
        with self.assertLogs(level="ERROR") as logs:
            self.assertEqual(main(["blocked", "LTRA-1"]), 1)
        self.assertIn("No Codex session ID", " ".join(logs.output))
        self.launch.assert_not_called()

    def test_interrupted_terminal_session_requires_exact_unambiguous_prompt(self):
        run, session, task = self.saved_run()
        attempt = Path(run["attempt_dir"])
        codex_home = self.root / "codex-home"
        codex_home.mkdir()
        launch = {"token": run["launch_token"], "cwd": str(task), "transport": "tmux",
                  "codex_home": str(codex_home), "prompt": "unique attempt prompt"}
        atomic_json(attempt / "launch.json", launch)
        with sqlite3.connect(codex_home / "state_5.sqlite") as db:
            db.execute("CREATE TABLE threads (id TEXT, cwd TEXT, first_user_message TEXT, source TEXT)")
            db.execute("INSERT INTO threads VALUES (?, ?, ?, 'cli')", (session, str(task), "other attempt"))
            db.commit()
            self.assertIsNone(interrupted_session(launch))
            db.execute("INSERT INTO threads VALUES (?, ?, ?, 'cli')", (session, str(task), launch["prompt"]))
            db.commit()
            self.assertEqual(session_for(run, "trajectory", task.parent.parent, run["key"]), (session, task))
            db.execute("INSERT INTO threads VALUES (?, ?, ?, 'cli')", (str(uuid.uuid4()), str(task), launch["prompt"]))
            db.commit()
            self.assertIsNone(interrupted_session(launch))

    def test_unavailable_or_mismatched_artifacts_never_launch(self):
        run, _, task = self.saved_run()
        changes = [{"attempt_dir": str(self.root / "different-machine" / "attempt-1")},
                   {"project": "dd-trace-py"}, {"key": "LTRA-99"}, {"launch_token": "wrong-token"}]
        for change in changes:
            self.jira.save_run("LTRA-1", {**run, **change})
            with self.subTest(change=change), self.assertLogs(level="ERROR"):
                self.assertEqual(main(["blocked", "LTRA-1"]), 1)
        self.launch.assert_not_called()

    def test_missing_ticket_rejected_and_other_actions_still_require_project(self):
        with self.assertLogs(level="ERROR"):
            self.assertEqual(main(["blocked", "LTRA-99"]), 1)
        for args in ([], ["status"], ["blocked", "--state-dir", str(self.root)]):
            with self.subTest(args=args), self.assertRaises(SystemExit) as error, patch("sys.stderr", io.StringIO()):
                main(args)
            self.assertEqual(error.exception.code, 2)
        self.launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
