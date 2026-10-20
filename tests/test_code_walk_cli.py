import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from agentic_louis import code_walk
from agentic_louis.worker import main


class CodeWalkCLITests(unittest.TestCase):
    def setUp(self):
        # Tests never launch the user's browser or start a real server.
        browser_thread = patch.object(code_walk.threading, 'Thread')
        self.browser_thread = browser_thread.start()
        self.addCleanup(browser_thread.stop)

    def test_routes_console_arguments_before_loading_worker_configuration(self):
        with patch('sys.argv', ['agentic-louis', 'code-walk', 'serve', '--repo', '/tmp/repo']), \
                patch.object(code_walk, 'main', return_value=0) as run, \
                patch('agentic_louis.worker.load_dotenv') as dotenv:
            self.assertEqual(main(), 0)
        run.assert_called_once_with(['serve', '--repo', '/tmp/repo'], prog='agentic-louis code-walk')
        dotenv.assert_not_called()

    def test_bundle_and_validate_target_repository_without_worker_services(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'source.py').write_text('def example():\n    return 42\n')
            plan = {'version': 1, 'title': 'Example', 'summary': 'Return a value',
                    'files': [{'path': 'source.py', 'language': 'python'}],
                    'steps': [{'id': 'return', 'title': 'Return', 'explanation': 'Returns 42.',
                               'file': 'source.py', 'start_line': 1, 'end_line': 2}]}
            plan_path, output = root / 'plan.json', root / 'walkthroughs/example.walk.json'
            plan_path.write_text(json.dumps(plan))
            with patch('agentic_louis.worker.load_dotenv') as dotenv, \
                    patch('agentic_louis.worker.Jira') as jira, redirect_stdout(io.StringIO()) as stdout:
                main(['code-walk', 'bundle', str(plan_path), '--repo', str(root), '--output', str(output)])
                main(['code-walk', 'validate', str(output)])
            self.assertEqual(code_walk.validate(code_walk.read_json(output))['files'][0]['content'],
                             (root / 'source.py').read_text())
            self.assertIn('Valid: Example', stdout.getvalue())
            dotenv.assert_not_called()
            jira.assert_not_called()

    def test_serve_forwards_repository_and_port_without_binding_a_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(code_walk, 'WalkServer') as server, redirect_stdout(io.StringIO()):
                server.return_value.server_port = 7433
                main(['code-walk', 'serve', '--repo', str(root), '--port', '7433'])
            server.assert_called_once_with(('127.0.0.1', 7433), root, Path.home() / '.agentic-louis/walkthroughs')
            server.return_value.serve_forever.assert_called_once_with()
            server.return_value.server_close.assert_called_once_with()
            self.browser_thread.assert_called_once_with(target=code_walk.open_viewer,
                                                        args=('http://127.0.0.1:7433',), daemon=True)
            self.browser_thread.return_value.start.assert_called_once_with()

    def test_no_open_keeps_serving_without_launching_a_browser(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(code_walk, 'WalkServer') as server, redirect_stdout(io.StringIO()):
            main(['code-walk', 'serve', '--repo', directory, '--no-open'])
        self.browser_thread.assert_not_called()
        server.return_value.serve_forever.assert_called_once_with()
        server.return_value.server_close.assert_called_once_with()

    def test_exact_walkthrough_is_selected_before_opening_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / 'my-explanation.walk.json'
            artifact.write_bytes((code_walk.ASSETS / 'demo.walk.json').read_bytes())
            with patch.object(code_walk, 'WalkServer') as server, redirect_stdout(io.StringIO()) as stdout:
                server.return_value.server_port = 7845
                main(['code-walk', 'serve', str(artifact), '--repo', str(root), '--port', '0'])
            server.assert_called_once_with(('127.0.0.1', 0), root,
                                          code_walk.default_walkthroughs_dir(), walkthrough=artifact.resolve())
            self.browser_thread.assert_called_once_with(target=code_walk.open_viewer,
                                                        args=('http://127.0.0.1:7845',), daemon=True)
            self.assertIn(f'Walkthrough: {artifact.resolve()}', stdout.getvalue())

    def test_scoped_library_exposes_only_the_requested_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = root / 'chosen.json'
            selected.write_bytes((code_walk.ASSETS / 'demo.walk.json').read_bytes())
            (root / 'unrelated.walk.json').write_bytes(selected.read_bytes())
            server = object.__new__(code_walk.WalkServer)
            server.walkthrough = selected
            server.directory = root
            self.assertEqual(server.documents(), {'selected': selected})
            handler = object.__new__(code_walk.WalkHandler)
            handler.server = server
            handler.path = '/api/walkthrough'
            with patch.object(handler, 'local_request', return_value=True), patch.object(handler, 'reply') as reply:
                handler.do_GET()
            reply.assert_called_once_with(200, code_walk.read_json(selected))
            handler.path = '/api/walkthroughs'
            with patch.object(handler, 'local_request', return_value=True), patch.object(handler, 'reply') as reply:
                handler.do_GET()
            self.assertEqual(reply.call_args.args[1], [{'id': 'selected', 'title': code_walk.read_json(selected)['title']}])

    def test_invalid_walkthrough_is_rejected_before_starting_server_or_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / 'bad.walk.json'
            artifact.write_text('{"version": 999}')
            with patch.object(code_walk, 'WalkServer') as server, redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as error:
                main(['code-walk', 'serve', str(artifact), '--repo', str(root)])
            self.assertEqual(error.exception.code, 1)
            server.assert_not_called()
            self.browser_thread.assert_not_called()

    def test_browser_opening_and_unavailable_browser_fallback(self):
        url = 'http://127.0.0.1:7432'
        with patch.object(code_walk.webbrowser, 'open_new_tab', return_value=True) as browser, \
                redirect_stdout(io.StringIO()) as stdout:
            code_walk.open_viewer(url)
        browser.assert_called_once_with(url)
        self.assertEqual(stdout.getvalue(), '')
        for failure in (False, OSError('No desktop'), code_walk.webbrowser.Error('No browser')):
            options = {'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure}
            with self.subTest(failure=failure), patch.object(code_walk.webbrowser, 'open_new_tab', **options), \
                    redirect_stdout(io.StringIO()) as stdout:
                code_walk.open_viewer(url)
            self.assertIn(url, stdout.getvalue())

    def test_bundle_default_and_viewer_share_home_library(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / 'home'
            repo = Path(directory) / 'example-repo'
            repo.mkdir()
            (repo / 'source.py').write_text('print(42)\n')
            plan = {'version': 1, 'title': 'Example', 'summary': 'Print a value',
                    'files': [{'path': 'source.py', 'language': 'python'}],
                    'steps': [{'id': 'print', 'title': 'Print', 'explanation': 'Prints 42.',
                               'file': 'source.py', 'start_line': 1, 'end_line': 1}]}
            plan_path = Path(directory) / 'printing.plan.json'
            plan_path.write_text(json.dumps(plan))
            with patch.object(Path, 'home', return_value=home), redirect_stdout(io.StringIO()):
                main(['code-walk', 'bundle', str(plan_path), '--repo', str(repo)])
                library = home / '.agentic-louis/walkthroughs'
                output = library / 'example-repo-printing.walk.json'
                self.assertEqual(code_walk.validate(code_walk.read_json(output))['title'], 'Example')
                self.assertFalse((repo / 'walkthroughs').exists())
                with patch.object(code_walk, 'WalkServer') as server:
                    main(['code-walk', 'serve', '--repo', str(repo)])
                server.assert_called_once_with(('127.0.0.1', 7432), repo, library)

    def test_viewer_accepts_an_explicit_library_and_expands_tilde(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with patch.dict('os.environ', {'HOME': str(home)}), \
                    patch.object(code_walk, 'WalkServer') as server, redirect_stdout(io.StringIO()):
                main(['code-walk', 'serve', '--repo', str(home), '--walkthroughs', '~/custom-library'])
            server.assert_called_once_with(('127.0.0.1', 7432), home, home / 'custom-library')

    def test_help_and_errors_identify_the_installed_command(self):
        for arguments, status in ((['code-walk', '--help'], 0), (['code-walk', 'serve', '--help'], 0),
                                  (['code-walk', 'unknown'], 2)):
            with self.subTest(arguments=arguments), redirect_stdout(io.StringIO()) as stdout, \
                    redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit) as error:
                main(arguments)
            self.assertEqual(error.exception.code, status)
            self.assertIn('agentic-louis code-walk', stdout.getvalue() + stderr.getvalue())
        with redirect_stdout(io.StringIO()) as stdout, self.assertRaises(SystemExit):
            main(['--help'])
        self.assertIn('code-walk', stdout.getvalue())

    def test_misplaced_worker_options_cannot_start_a_worker_for_code_walk(self):
        with redirect_stderr(io.StringIO()), patch('agentic_louis.worker.Jira') as jira, \
                self.assertRaises(SystemExit) as error:
            main(['--project', 'trajectory', 'code-walk'])
        self.assertEqual(error.exception.code, 2)
        jira.assert_not_called()
