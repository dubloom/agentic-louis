import json
import hashlib
import os
import pty
import select
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from agentic_louis import ROOT
from agentic_louis.process import atomic_json, command, CommandError, identity
from agentic_louis.run_agent import notify
from agentic_louis.tmux import stop_terminal, terminal_args


class TmuxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agentic tmux 'test' ")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.token = str(uuid.uuid4())
        self.session = str(uuid.uuid4())
        self.launch = {"token": self.token, "transport": "tmux", "cwd": str(self.directory), "timeout": 10}
        atomic_json(self.directory / "launch.json", self.launch)

    def notification(self):
        return json.dumps({"type": "agent-turn-complete", "thread-id": self.session, "cwd": str(self.directory)})

    def start(self, script, timeout=10):
        self.launch.update(args=[sys.executable, "-c", script, str(self.directory)], timeout=timeout)
        atomic_json(self.directory / "launch.json", self.launch)
        process = subprocess.Popen([sys.executable, "-m", "agentic_louis.run_agent", str(self.directory)],
                                   cwd=ROOT, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def cleanup():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            stop_terminal(self.directory, self.token)

        self.addCleanup(cleanup)
        return process

    def wait_for(self, filename):
        deadline = time.monotonic() + 10
        while not (self.directory / filename).exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue((self.directory / filename).exists(), f"Missing {filename}")

    def assert_stopped(self):
        with self.assertRaises(CommandError):
            command(terminal_args(self.launch) + ["has-session", "-t", "agent"])
        child = json.loads((self.directory / "child.json").read_text())
        self.assertIsNone(identity(child["pid"]))

    def test_notification_records_session_but_only_hands_off_a_finished_result(self):
        notify(self.directory, self.token, self.notification())
        saved = json.loads((self.directory / "session.json").read_text())
        self.assertEqual(saved, {"token": self.token, "session_id": self.session})
        self.assertFalse((self.directory / "completion.json").exists())
        (self.directory / "result.json").write_text('{}')
        notify(self.directory, self.token, self.notification())
        self.assertEqual(json.loads((self.directory / "completion.json").read_text()), saved)
        with self.assertRaises(ValueError):
            notify(self.directory, "wrong-token", self.notification())

    def test_gitlab_token_is_acquired_on_host_and_reaches_agent_through_tmux(self):
        helper_directory = tempfile.TemporaryDirectory()
        self.addCleanup(helper_directory.cleanup)
        helper = Path(helper_directory.name) / "ddtool"
        helper.write_text("#!/bin/sh\nprintf '%s\\n' 'fresh-test-gitlab-credential'\n")
        helper.chmod(0o700)
        self.launch["gitlab_repository"] = "DataDog/trajectory"
        digest = hashlib.sha256(b"fresh-test-gitlab-credential").hexdigest()
        script = (
            "import hashlib,json,os,sys; from pathlib import Path; "
            "Path(sys.argv[1], 'auth-check.json').write_text(json.dumps({"
            f"'authenticated': hashlib.sha256(os.environ.get('GITLAB_TOKEN', '').encode()).hexdigest() == '{digest}',"
            "'host': os.environ.get('GITLAB_HOST')}))"
        )
        with patch.dict(os.environ, {"PATH": helper_directory.name + os.pathsep + os.environ["PATH"],
                                     "GITLAB_TOKEN": "stale-inherited-credential"}):
            process = self.start(script)
            process.wait(timeout=10)
        report = json.loads((self.directory / "auth-check.json").read_text())
        self.assertEqual(report, {"authenticated": True, "host": "gitlab.ddbuild.io"})
        for path in self.directory.iterdir():
            if path.is_file():
                self.assertNotIn("fresh-test-gitlab-credential", path.read_text())

    def test_terminal_keeps_running_between_turns_and_completes_after_handoff(self):
        process = self.start("import sys,time; print('interactive terminal', flush=True); time.sleep(60)")
        self.wait_for("terminal.json")
        self.wait_for("child.json")
        notify(self.directory, self.token, self.notification())
        self.assertIsNone(process.poll())
        command(terminal_args(self.launch) + ["has-session", "-t", "agent"])
        (self.directory / "result.json").write_text('{}')
        time.sleep(0.3)
        self.assertIsNone(process.poll(), "A result file alone must not stop an active turn")
        notify(self.directory, self.token, self.notification())
        process.wait(timeout=10)
        result = json.loads((self.directory / "exit.json").read_text())
        self.assertEqual(result, {"exit_code": 0, "error": None, "token": self.token})
        self.assertTrue((self.directory / "terminal.log").exists())
        self.assert_stopped()

    def test_timeout_stops_private_terminal(self):
        process = self.start("import time; time.sleep(60)", timeout=0.5)
        process.wait(timeout=10)
        result = json.loads((self.directory / "exit.json").read_text())
        self.assertIn("timeout", result["error"])
        self.assertNotEqual(result["exit_code"], 0)
        self.assert_stopped()

    def test_attaching_and_detaching_keeps_the_original_terminal_running(self):
        process = self.start("import time; print('same terminal', flush=True); time.sleep(60)")
        self.wait_for("terminal.json")
        self.wait_for("child.json")
        original = json.loads((self.directory / "child.json").read_text())
        master, slave = pty.openpty()
        try:
            attached = subprocess.Popen(terminal_args(self.launch) + ["attach-session", "-t", "agent"],
                                        stdin=slave, stdout=slave, stderr=slave,
                                        env={**os.environ, "TERM": "xterm-256color"})
            try:
                ready, _, _ = select.select([master], [], [], 5)
                self.assertTrue(ready, "Attached terminal did not render")
                os.read(master, 65536)
                os.write(master, b"\x02d")
                self.assertEqual(attached.wait(timeout=5), 0)
            finally:
                if attached.poll() is None:
                    attached.kill()
                    attached.wait(timeout=5)
        finally:
            os.close(master)
            os.close(slave)
        self.assertIsNone(process.poll())
        self.assertEqual(json.loads((self.directory / "child.json").read_text()), original)
        command(terminal_args(self.launch) + ["has-session", "-t", "agent"])

    def test_terminal_exit_without_handoff_is_failure(self):
        process = self.start("print('user exited')")
        process.wait(timeout=10)
        result = json.loads((self.directory / "exit.json").read_text())
        self.assertNotEqual(result["exit_code"], 0)
        self.assertIsNotNone(result["error"])
        self.assert_stopped()

    def test_supervisor_signal_cleans_up_terminal(self):
        process = self.start("import time; time.sleep(60)")
        self.wait_for("terminal.json")
        self.wait_for("child.json")
        os.kill(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
        self.assertIn("Worker stopped", json.loads((self.directory / "exit.json").read_text())["error"])
        self.assert_stopped()

    def test_orphan_terminal_is_cleaned_up_using_saved_metadata(self):
        process = self.start("import time; time.sleep(60)")
        self.wait_for("terminal.json")
        self.wait_for("child.json")
        process.kill()
        process.wait(timeout=5)
        command(terminal_args(self.launch) + ["has-session", "-t", "agent"])
        stop_terminal(self.directory, self.token)
        self.assert_stopped()

    def test_timeout_also_stops_tools_that_started_a_new_process_session(self):
        script = (
            "import subprocess,sys,time; from pathlib import Path; "
            "child=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'], "
            "start_new_session=True); "
            "Path(sys.argv[1],'tool.pid').write_text(str(child.pid)); time.sleep(60)"
        )
        process = self.start(script, timeout=1)
        self.wait_for("tool.pid")
        tool_pid = int((self.directory / "tool.pid").read_text())

        def cleanup_tool():
            if identity(tool_pid) is not None:
                os.kill(tool_pid, signal.SIGKILL)

        self.addCleanup(cleanup_tool)
        process.wait(timeout=10)
        self.assertIsNone(identity(tool_pid))
        self.assert_stopped()
