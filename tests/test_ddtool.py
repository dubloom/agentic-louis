import io
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

from agentic_louis.ddtool import gitlab_project_token


class DDToolTests(unittest.TestCase):
    def test_returns_token_without_printing_and_passes_project_as_arguments(self):
        output = io.StringIO()
        result = Mock(returncode=0, stdout="  private-project-token\n", stderr="")
        with patch("agentic_louis.ddtool.subprocess.run", return_value=result) as run:
            with redirect_stdout(output), redirect_stderr(output):
                token = gitlab_project_token("DataDog", "trajectory", timeout=20)
        self.assertEqual(token, "private-project-token")
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(run.call_args.args[0],
                         ["ddtool", "auth", "gitlab", "project-token", "DataDog", "trajectory"])
        self.assertEqual(run.call_args.kwargs["timeout"], 20)
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.PIPE)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_auth_failure_does_not_expose_command_output(self):
        result = Mock(returncode=1, stdout="secret-in-stdout", stderr="secret-in-stderr")
        with patch("agentic_louis.ddtool.subprocess.run", return_value=result):
            with self.assertRaisesRegex(ValueError, "Check authentication") as error:
                gitlab_project_token("DataDog", "trajectory")
        self.assertNotIn("secret", str(error.exception))

    def test_timeout_and_unavailable_cli_have_safe_errors(self):
        for failure, message in ((FileNotFoundError(), "not installed"),
                                 (PermissionError("sensitive diagnostic"), "Could not execute"),
                                 (subprocess.TimeoutExpired("ddtool", 1, output="secret", stderr="secret"), "timed out")):
            with self.subTest(failure=failure), patch("agentic_louis.ddtool.subprocess.run", side_effect=failure):
                with self.assertRaisesRegex(ValueError, message) as error:
                    gitlab_project_token("DataDog", "trajectory")
                self.assertTrue(error.exception.__suppress_context__)
                self.assertNotIn("secret", str(error.exception))

    def test_rejects_empty_or_multiline_tokens(self):
        for output in ("", " \n", "diagnostic\nprivate-token", "private token"):
            with self.subTest(output=output), patch("agentic_louis.ddtool.subprocess.run",
                                                   return_value=Mock(returncode=0, stdout=output)):
                with self.assertRaisesRegex(ValueError, "empty or malformed"):
                    gitlab_project_token("DataDog", "trajectory")

    def test_invalid_projects_and_timeouts_do_not_invoke_ddtool(self):
        for owner, repository, timeout in (("--help", "trajectory", 60), ("DataDog", "a/b", 60),
                                            ("", "trajectory", 60), ("DataDog", "trajectory", 0)):
            with self.subTest(owner=owner, repository=repository), patch("agentic_louis.ddtool.subprocess.run") as run:
                with self.assertRaises(ValueError):
                    gitlab_project_token(owner, repository, timeout=timeout)
                run.assert_not_called()
