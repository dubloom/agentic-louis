from contextlib import redirect_stdout
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from agentic_louis.clean import _lock_processes, clean, record_worktree
from agentic_louis.process import CommandError, atomic_json, command, identity
from agentic_louis.worker import main


class CleanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self.state = self.root / "state"
        command(["git", "init", "-b", "main"], cwd=self.repository)
        command(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                 "commit", "--allow-empty", "-m", "Initial"], cwd=self.repository)

    def branches(self):
        return command(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"],
                       cwd=self.repository).splitlines()

    def watcher(self, *, record=True):
        token = str(uuid.uuid4())
        attempt = self.state / "watch-prs" / "repository" / "7" / token
        attempt.mkdir(parents=True)
        worktree = attempt / "repository"
        branch = f"watch-prs/7/{token}"
        command(["git", "worktree", "add", "-b", branch, str(worktree)], cwd=self.repository)
        common = command(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=self.repository)
        atomic_json(attempt / "launch.json", {"token": token, "args": [
            "permissions.agentic-pr.filesystem={" + json.dumps(common) + '="write"}']})
        if record:
            record_worktree(self.state, self.repository, worktree, branch)
        (attempt / "supervisor.log").write_text("Keep this log")
        return worktree, branch

    def run_clean(self, **kwargs):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(clean(self.repository, state_dir=self.state, **kwargs), 0)
        return output.getvalue()

    def test_removes_dirty_worktrees_and_branches_keeps_logs_and_other_branches(self):
        worktree, branch = self.watcher()
        (worktree / "uncommitted.txt").write_text("discard")
        unrelated = "watch-prs/123/" + str(uuid.uuid4())
        command(["git", "branch", unrelated], cwd=self.repository)
        self.run_clean()
        self.assertFalse(worktree.exists())
        self.assertNotIn(branch, self.branches())
        self.assertIn("main", self.branches())
        self.assertIn(unrelated, self.branches())
        self.assertEqual((worktree.parent / "supervisor.log").read_text(), "Keep this log")
        self.assertIn("Removed 0 worktree(s) and 0 local branch(es).", self.run_clean())

    def test_dry_run_preserves_everything_without_creating_lock_files(self):
        worktree, branch = self.watcher()
        paths = set(self.state.rglob("*"))
        output = self.run_clean(dry_run=True)
        self.assertIn("Would remove 1 worktree(s) and 1 local branch(es).", output)
        self.assertTrue(worktree.exists())
        self.assertIn(branch, self.branches())
        self.assertEqual(paths, set(self.state.rglob("*")))

    def test_legacy_watcher_branch_is_removed_even_after_worktree_was_deleted(self):
        worktree, branch = self.watcher(record=False)
        command(["git", "worktree", "remove", str(worktree)], cwd=self.repository)
        self.run_clean()
        self.assertNotIn(branch, self.branches())

    def test_legacy_jira_worktree_and_branch_are_removed(self):
        worktree = self.state / "runs" / "LTRA-1" / "repositories" / "repository"
        worktree.parent.mkdir(parents=True)
        command(["git", "worktree", "add", "-b", "dubloom/ltra-1", str(worktree)], cwd=self.repository)
        self.run_clean()
        self.assertFalse(worktree.exists())
        self.assertNotIn("dubloom/ltra-1", self.branches())

    def test_custom_worker_branch_is_recorded_and_cleaned(self):
        worktree = self.state / "runs" / "LTRA-1" / "repositories" / "repository"
        worktree.parent.mkdir(parents=True)
        branch = "custom/task"
        command(["git", "worktree", "add", "-b", branch, str(worktree)], cwd=self.repository)
        record_worktree(self.state, self.repository, worktree, branch)
        self.run_clean()
        self.assertFalse(worktree.exists())
        self.assertNotIn(branch, self.branches())

    def test_reused_existing_branch_is_preserved(self):
        worktree, branch = self.watcher(record=False)
        record_worktree(self.state, self.repository, worktree, branch, created_branch=False)
        self.run_clean()
        self.assertFalse(worktree.exists())
        self.assertIn(branch, self.branches())

    def test_live_agent_prevents_deletion(self):
        worktree, branch = self.watcher()
        atomic_json(worktree.parent / "supervisor.json", {"pid": os.getpid(), "identity": identity(os.getpid())})
        with self.assertRaisesRegex(ValueError, "still running"):
            self.run_clean()
        self.assertTrue(worktree.exists())
        self.assertIn(branch, self.branches())

    def test_active_child_without_supervisor_prevents_deletion(self):
        worktree, _ = self.watcher()
        atomic_json(worktree.parent / "child.json", {"pid": os.getpid(), "identity": identity(os.getpid())})
        with self.assertRaisesRegex(ValueError, "still running"):
            self.run_clean()
        self.assertTrue(worktree.exists())

    def test_default_project_scope_and_explicit_project_filter(self):
        worktree, branch = self.watcher()
        with patch("agentic_louis.clean.default_state_dir", side_effect=lambda name: (
                self.state if name == "trajectory" else self.root / "unused")):
            with redirect_stdout(io.StringIO()):
                clean(self.repository, project="dd-trace-py")
            self.assertTrue(worktree.exists())
            with redirect_stdout(io.StringIO()):
                clean(self.repository)
        self.assertFalse(worktree.exists())
        self.assertNotIn(branch, self.branches())

    def test_resource_for_another_repository_is_preserved(self):
        worktree, branch = self.watcher()
        other = self.root / "other"
        command(["git", "init", str(other)])
        with redirect_stdout(io.StringIO()):
            clean(other, state_dir=self.state)
        self.assertTrue(worktree.exists())
        self.assertIn(branch, self.branches())

    def test_running_service_identifies_pid_and_prevents_deletion(self):
        worktree, _ = self.watcher()
        for service, lock_path in (("worker", self.state / "worker.lock"),
                                   ("watcher (watch-prs)", self.state / "watch-prs" / "watch.lock")):
            for dry_run in (False, True):
                with self.subTest(service=service, dry_run=dry_run):
                    holder = subprocess.Popen(
                        [sys.executable, "-c", "import fcntl, sys; "
                         "lock = open(sys.argv[1], 'a+'); "
                         "fcntl.flock(lock, fcntl.LOCK_EX); "
                         "print('ready', flush=True); sys.stdin.read()", str(lock_path)],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
                    try:
                        self.assertEqual(holder.stdout.readline().strip(), "ready")
                        with self.assertRaises(ValueError) as raised:
                            self.run_clean(dry_run=dry_run)
                        self.assertEqual(str(raised.exception),
                                         f"A {service} or cleanup is running (PID {holder.pid}). "
                                         "Stop it before cleaning.")
                        self.assertTrue(worktree.exists())
                    finally:
                        holder.communicate(timeout=5)

    def test_lock_process_lookup_excludes_cleanup_and_duplicates(self):
        with patch("agentic_louis.clean.command", return_value=f"{os.getpid()}\n123\n123\n"):
            self.assertEqual(_lock_processes(self.state / "worker.lock"), "PID 123")

    def test_lock_process_lookup_failure_still_blocks_cleaning(self):
        self.state.mkdir()
        with (self.state / "worker.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch("agentic_louis.clean.command", side_effect=CommandError("lsof unavailable")):
                with self.assertRaisesRegex(ValueError, "PID unavailable"):
                    self.run_clean(dry_run=True)

    def test_changed_branch_is_preserved(self):
        worktree, branch = self.watcher()
        command(["git", "checkout", "-b", "human-work"], cwd=worktree)
        with self.assertRaisesRegex(ValueError, "changed branches"):
            self.run_clean()
        self.assertTrue(worktree.exists())
        self.assertIn(branch, self.branches())

    def test_branch_checked_out_elsewhere_is_preserved(self):
        worktree, branch = self.watcher()
        command(["git", "worktree", "remove", str(worktree)], cwd=self.repository)
        command(["git", "worktree", "add", str(self.root / "human-worktree"), branch], cwd=self.repository)
        with self.assertRaisesRegex(ValueError, "another worktree"):
            self.run_clean()
        self.assertIn(branch, self.branches())

    def test_locked_worktree_is_preserved(self):
        worktree, _ = self.watcher()
        command(["git", "worktree", "lock", str(worktree)], cwd=self.repository)
        with self.assertRaisesRegex(ValueError, "locked"):
            self.run_clean()
        self.assertTrue(worktree.exists())

    def test_cli_clean_needs_no_project_or_authentication(self):
        self.watcher()
        with patch("agentic_louis.worker.Path.cwd", return_value=self.repository), patch(
                "agentic_louis.worker.Jira") as jira, patch("agentic_louis.worker.GitHub") as github:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["clean", "--state-dir", str(self.state)]), 0)
        jira.assert_not_called()
        github.assert_not_called()

    def test_cli_config_clean_does_not_contact_remote(self):
        worktree, branch = self.watcher()
        command(["git", "remote", "add", "origin", "git@github.com:org/repository.git"], cwd=self.repository)
        config = self.root / "workspace.json"
        config.write_text(json.dumps({"project": "trajectory", "state_dir": str(self.state),
                                      "repositories": [{"path": str(self.repository)}]}))
        with patch("agentic_louis.config.command", wraps=command) as git:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["clean", "--config", str(config)]), 0)
        self.assertFalse(any("ls-remote" in call.args[0] for call in git.call_args_list))
        self.assertFalse(worktree.exists())
        self.assertNotIn(branch, self.branches())
