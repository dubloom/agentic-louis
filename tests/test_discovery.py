from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agentic_louis.config import Workspace, default_state_dir, github_repository
from agentic_louis.process import command
from agentic_louis.worker import main
from agentic_louis import ROOT
from agentic_louis.projects import REGISTRY
from test_workflow import FakeJira


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def repository(self, name, remote=None):
        path = self.root / name
        path.mkdir()
        command(["git", "init", str(path)])
        command(["git", "remote", "add", "origin", remote or f"git@github.com:DataDog/{name}.git"], cwd=path)
        return path

    @staticmethod
    def git(args, **kwargs):
        if list(args[:3]) == ["git", "ls-remote", "--symref"]:
            return "ref: refs/heads/develop\tHEAD\n0123456789\tHEAD"
        return command(args, **kwargs)

    def test_discovers_immediate_repositories_and_remote_default_branches(self):
        self.repository("backend")
        self.repository("frontend", "https://github.com/DataDog/frontend.git")
        nested = self.root / "dependencies"
        nested.mkdir()
        command(["git", "init", str(nested / "nested-repo")])
        with patch("agentic_louis.config.command", side_effect=self.git), patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")}):
            config = Workspace.discover(self.root, "trajectory")
        self.assertEqual([r.github for r in config.repositories], ["DataDog/backend", "DataDog/frontend"])
        self.assertEqual([r.base for r in config.repositories], ["develop", "develop"])
        self.assertEqual(config.state_dir, self.root / "state" / "agentic-louis" / "trajectory")
        self.assertIsNone(config.profile)
        self.assertTrue(all(not r.checks for r in config.repositories))

    def test_repository_root_uses_only_that_repository(self):
        path = self.repository("backend")
        command(["git", "init", str(path / "nested")])
        with patch("agentic_louis.config.command", side_effect=self.git):
            config = Workspace.discover(path, "dd-trace-py", state_dir=self.root / "state")
        self.assertEqual([r.path for r in config.repositories], [path])

    def test_no_repositories_fails_before_polling(self):
        with self.assertRaisesRegex(ValueError, "No Git repositories"):
            Workspace.discover(self.root, "trajectory")

    def test_unknown_project_does_not_read_git(self):
        with patch("agentic_louis.config.command") as git, self.assertRaises(ValueError):
            Workspace.discover(self.root, "invented")
        git.assert_not_called()

    def test_unresolved_remote_head_never_guesses_main(self):
        path = self.repository("backend")
        def no_head(args, **kwargs):
            if list(args[:3]) == ["git", "ls-remote", "--symref"]:
                return "0123456789\tHEAD"
            return command(args, **kwargs)
        with patch("agentic_louis.config.command", side_effect=no_head), self.assertRaisesRegex(ValueError, "default branch"):
            Workspace.discover(path, "trajectory")

    def test_github_origin_formats_and_invalid_hosts(self):
        for remote in ("git@github.com:DataDog/dd-trace-py.git", "https://github.com/DataDog/dd-trace-py.git",
                       "ssh://git@github.com/DataDog/dd-trace-py", "https://github.com/DataDog/dd-trace-py/"):
            self.assertEqual(github_repository(remote), "DataDog/dd-trace-py")
        for remote in ("https://user:secret@github.com/DataDog/repo.git", "https://gitlab.com/DataDog/repo.git", "/tmp/local.git"):
            with self.assertRaises(ValueError):
                github_repository(remote)

    def test_shared_project_state_is_independent_of_launch_directory(self):
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")}):
            state_dir = default_state_dir("trajectory")
            with patch("pathlib.Path.cwd", return_value=self.root / "elsewhere"):
                self.assertEqual(default_state_dir("trajectory"), state_dir)
            self.assertNotEqual(default_state_dir("dd-trace-py"), state_dir)

    def test_worker_and_separate_skill_agree_on_routing_labels(self):
        skill_registry = json.loads((ROOT / "skills" / "queue-project-task" / "projects.json").read_text())
        for field in ("jira_project", "managed_label", "projects"):
            self.assertEqual(REGISTRY[field], skill_registry[field])

    def test_cli_loads_dotenv_from_cwd_and_exported_values_win(self):
        (self.root / ".env").write_text("JIRA_EMAIL=file@example.com\nJIRA_API_TOKEN=file-token\nSLACK_WORKFLOW_URL=file-workflow\n")
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state"), "JIRA_API_TOKEN": "exported-token"}, clear=True):
            with patch("pathlib.Path.cwd", return_value=self.root), patch("agentic_louis.worker.Jira", return_value=FakeJira()), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["--project", "trajectory", "status"]), 0)
            self.assertEqual(json.loads(output.getvalue()), [])
            self.assertEqual(os.environ["JIRA_EMAIL"], "file@example.com")
            self.assertEqual(os.environ["JIRA_API_TOKEN"], "exported-token")
            self.assertEqual(os.environ["SLACK_WORKFLOW_URL"], "file-workflow")

    def test_cli_works_with_exported_environment_without_dotenv(self):
        with patch.dict(os.environ, {"JIRA_EMAIL": "export@example.com", "XDG_STATE_HOME": str(self.root / "state")}, clear=True):
            with patch("pathlib.Path.cwd", return_value=self.root), patch("agentic_louis.worker.Jira", return_value=FakeJira()), redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--project", "trajectory", "status"]), 0)
            self.assertEqual(os.environ["JIRA_EMAIL"], "export@example.com")


if __name__ == "__main__":
    unittest.main()
