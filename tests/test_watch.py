from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agentic_louis.watch import render_event, watch_attempt
from agentic_louis.worker import main


class WatchTests(unittest.TestCase):
    def test_command_output_is_bounded_and_full_output_is_available(self):
        output = "\n".join(f"line {number}: " + "x" * 300 for number in range(100))
        event = {"type": "item.completed", "item": {"type": "command_execution",
                 "aggregated_output": output, "status": "completed", "exit_code": 0}}
        rendered = render_event(event)
        self.assertIn("exit 0", rendered)
        self.assertIn("--full-output", rendered)
        self.assertIn("line 99:", rendered)
        self.assertNotIn("line 0:", rendered)
        self.assertLess(len(rendered), 1800)
        self.assertIn(output, render_event(event, full_output=True))

    def test_messages_files_and_errors_are_readable_without_raw_json(self):
        self.assertEqual(render_event({"type": "item.completed", "item": {
            "type": "agent_message", "text": "I’m running the tests."}}), "\nAgent: I’m running the tests.")
        self.assertEqual(render_event({"type": "item.completed", "item": {
            "type": "file_change", "changes": [{"kind": "update", "path": "patch.py"}]}}),
            "\nFiles changed:\n  update: patch.py")
        self.assertEqual(render_event({"type": "turn.failed", "error": {"message": "Connection lost"}}),
                         "Error: Connection lost")
        for event in (None, [], {"type": "item.started", "item": {"type": "reasoning"}},
                      {"type": "unknown"}):
            self.assertEqual(render_event(event), "")

    def test_follows_partial_events_and_exits_after_supervisor_finishes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "runs" / "LTRA-1" / "attempt-1"
            directory.mkdir(parents=True)
            path = directory / "events.jsonl"
            event = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Done"}})
            path.write_text("not json\nnull\n" + event[:20])

            def append(_):
                with path.open("a") as events:
                    events.write(event[20:] + "\n")
                (directory / "exit.json").write_text('{}')

            output = io.StringIO()
            with redirect_stdout(output), patch("agentic_louis.watch.time.sleep", side_effect=append) as sleep:
                self.assertEqual(watch_attempt(state_dir=root, key="LTRA-1", attempt=1), 0)
            sleep.assert_called_once()
            self.assertIn("Agent: Done", output.getvalue())
            self.assertIn("Agent process stopped", output.getvalue())
            self.assertNotIn('"item.completed"', output.getvalue())

    def test_cli_snapshot_needs_no_jira_codex_or_repository_discovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "runs" / "LTRA-1" / "attempt-1"
            directory.mkdir(parents=True)
            (directory / "events.jsonl").write_text('{"type":"turn.started"}\n')
            with redirect_stdout(io.StringIO()) as output, patch("agentic_louis.worker.Jira") as jira, \
                    patch("agentic_louis.worker.Workspace.discover") as discover:
                self.assertEqual(main(["--project", "trajectory", "--state-dir", temporary,
                                       "watch", "LTRA-1", "--attempt", "1", "--dry-run"]), 0)
            jira.assert_not_called()
            discover.assert_not_called()
            self.assertIn("Working…", output.getvalue())

    def test_interrupt_only_stops_watching_and_invalid_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "runs" / "LTRA-1" / "attempt-1"
            directory.mkdir(parents=True)
            (directory / "events.jsonl").touch()
            with redirect_stdout(io.StringIO()), patch("agentic_louis.watch.time.sleep", side_effect=KeyboardInterrupt):
                self.assertEqual(watch_attempt(state_dir=Path(temporary), key="LTRA-1", attempt=1), 0)
            for key, attempt in (("../escape", 1), ("LTRA-1", 0)):
                with self.assertRaises(ValueError):
                    watch_attempt(state_dir=Path(temporary), key=key, attempt=attempt)
