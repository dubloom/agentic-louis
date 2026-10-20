import copy
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agentic_louis.code_walk import (
    ASSETS, WalkHandler, WalkServer, ask_codex, bundle, cached_models, chat_prompt, chat_result, model_id, read_json, snapshot_status, validate,
)


def plan():
    return {"version": 1, "title": "Example", "summary": "An example flow",
            "files": [{"path": "source.py", "language": "python"}],
            "steps": [{"id": "entry", "title": "Enter", "explanation": "Return the result.",
                       "file": "source.py", "start_line": 1, "end_line": 2}]}


def structured_answer(text='Answer', references=None):
    return json.dumps({'answer': text, 'references': references or []})


def reference(path='helper.py', start=1, end=2):
    return {'path': path, 'start_line': start, 'end_line': end, 'explanation': 'This helper is called by the selected function.'}


class WalkthroughTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "source.py").write_bytes(b'def hello():\r\n    return "hello"\r\n')
        self.doc = bundle(plan(), self.root)

    def test_bundle_preserves_source_and_reports_staleness(self):
        self.assertIn('\r\n', self.doc['files'][0]['content'])
        self.assertEqual(snapshot_status(self.doc, self.root), {'source.py': 'current'})
        (self.root / 'source.py').write_text('changed')
        self.assertEqual(snapshot_status(self.doc, self.root), {'source.py': 'changed'})
        (self.root / 'source.py').unlink()
        self.assertEqual(snapshot_status(self.doc, self.root), {'source.py': 'missing'})
        self.assertEqual(validate(self.doc), self.doc)

    def test_rejects_escaping_paths_and_symlinks(self):
        (self.root / 'link.py').symlink_to(ASSETS / 'app.js')
        for path in ('../outside.py', '/etc/passwd', 'C:/secrets', 'a\\b', 'link.py'):
            doc = plan()
            doc['files'][0]['path'] = path
            with self.subTest(path=path), self.assertRaises(ValueError):
                bundle(doc, self.root)

    def test_rejects_invalid_artifacts(self):
        mutations = [lambda d: d.update(version=True), lambda d: d.update(files=[]),
                     lambda d: d['steps'][0].update(start_line=0),
                     lambda d: d['steps'][0].update(end_line=3),
                     lambda d: d['steps'][0].update(file='unknown.py'),
                     lambda d: d['files'][0].update(content='modified'),
                     lambda d: d['steps'].append(copy.deepcopy(d['steps'][0])),
                     lambda d: d['files'].append(copy.deepcopy(d['files'][0])),
                     lambda d: d.update(glossary=[{'term': 'Missing definition'}])]
        for mutation in mutations:
            doc = copy.deepcopy(self.doc)
            mutation(doc)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate(doc)

    def test_prompt_includes_context_and_rejects_bad_history(self):
        payload = {'document': self.doc, 'question': 'Why?', 'step_id': 'entry',
                   'history': [{'role': 'user', 'content': 'Previous question'}]}
        prompt = chat_prompt(payload)
        self.assertIn('Previous question', prompt)
        self.assertIn('entry', prompt)
        self.assertIn('read-only', prompt)
        for change in ({'step_id': 'missing'}, {'history': [{'role': 'system', 'content': 'bad'}]},
                       {'question': ''}, {'history': 'bad'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                chat_prompt({**payload, **change})

    def test_codex_extracts_answer_and_enforces_readonly(self):
        process = Mock(returncode=0)
        process.communicate.return_value = ('\n'.join(json.dumps(e) for e in [
            {'type': 'item.completed', 'item': {'type': 'command_execution', 'aggregated_output': 'private output'}},
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'The answer'}}]), '')
        with patch('agentic_louis.code_walk.subprocess.Popen', return_value=process) as popen:
            self.assertEqual(ask_codex('prompt', self.root), 'The answer')
        args = popen.call_args.args[0]
        self.assertIn('read-only', args)
        self.assertIn('approval_policy="never"', args)
        self.assertIn('--ignore-user-config', args)
        self.assertEqual(args[args.index('--output-schema') + 1], str(ASSETS / 'answer.schema.json'))
        self.assertNotIn('--model', args)
        self.assertNotIn('shell', popen.call_args.kwargs)
        self.assertEqual(process.communicate.call_args.args[0], 'prompt')

    def test_selected_model_is_passed_as_one_cli_argument(self):
        process = Mock(returncode=0)
        process.communicate.return_value = ('{"type":"item.completed","item":{"type":"agent_message","text":"Answer"}}', '')
        with patch('agentic_louis.code_walk.subprocess.Popen', return_value=process) as popen:
            self.assertEqual(ask_codex('prompt', self.root, model='example-model'), 'Answer')
        args = popen.call_args.args[0]
        self.assertEqual(args[args.index('--model') + 1], 'example-model')
        self.assertEqual(args[-1], '-')
        for invalid in ('--help', 'model with spaces', 'model\nname', 'x' * 129, [], 7):
            with self.subTest(invalid=invalid), patch('agentic_louis.code_walk.subprocess.Popen') as popen:
                with self.assertRaises(ValueError):
                    ask_codex('prompt', self.root, model=invalid)
                popen.assert_not_called()
        self.assertIsNone(model_id(None))
        self.assertIsNone(model_id(''))

    def test_model_picker_uses_only_valid_visible_cached_models(self):
        cache = self.root / 'models_cache.json'
        catalog = {'identity': 'must-not-be-exposed', 'models': [
            {'slug': 'example-model', 'display_name': 'Example Model', 'visibility': 'list'},
            {'slug': 'hidden-model', 'visibility': 'hide'},
            {'slug': '--bad-option', 'visibility': 'list'},
            {'slug': None, 'visibility': 'list'},
            'malformed',
        ]}
        with patch.dict(os.environ, {'CODEX_HOME': str(self.root)}):
            self.assertEqual(cached_models(), [])
            cache.write_text(json.dumps(catalog))
            self.assertEqual(cached_models(), [{'id': 'example-model', 'name': 'Example Model'}])
            for invalid in ('not json', '[]', '{"models": {}}'):
                cache.write_text(invalid)
                self.assertEqual(cached_models(), [])

    def test_chat_route_forwards_model_and_rejects_invalid_model_without_launching(self):
        for model, status in (('example-model', 200), (None, 200), ('--help', 400)):
            with self.subTest(model=model):
                body = json.dumps({'document': self.doc, 'question': 'Why?', 'model': model}).encode()
                handler = object.__new__(WalkHandler)
                handler.server = SimpleNamespace(token='token', root=self.root, chat_slot=threading.BoundedSemaphore(1))
                handler.headers = {'X-Code-Walk-Token': 'token', 'Content-Length': str(len(body))}
                handler.path = '/api/chat'
                handler.rfile = io.BytesIO(body)
                handler.local_request = Mock(return_value=True)
                handler.reply = Mock()
                with patch('agentic_louis.code_walk.ask_codex', return_value=structured_answer()) as ask:
                    handler.do_POST()
                self.assertEqual(handler.reply.call_args.args[0], status)
                if status == 200:
                    self.assertEqual(ask.call_args.kwargs['model'], model)
                else:
                    ask.assert_not_called()

    def test_codex_failures_do_not_return_raw_output(self):
        process = Mock(returncode=1)
        process.communicate.return_value = ('secret', 'secret')
        with patch('agentic_louis.code_walk.subprocess.Popen', return_value=process):
            with self.assertRaises(ValueError) as error:
                ask_codex('prompt', self.root)
        self.assertNotIn('secret', str(error.exception))
        process.returncode = 0
        process.communicate.return_value = ('{"type":"turn.failed"}', '')
        with patch('agentic_louis.code_walk.subprocess.Popen', return_value=process):
            with self.assertRaises(ValueError):
                ask_codex('prompt', self.root)

    def test_related_file_is_attached_from_disk_and_available_to_followup_and_export(self):
        source = b'def helper():\r\n    return 42\r\n'
        (self.root / 'helper.py').write_bytes(source)
        result = chat_result(structured_answer(references=[reference(), reference()]), self.doc, self.root)
        self.assertEqual(len(result['files']), 1)
        self.assertEqual(len(result['references']), 1)
        self.assertEqual(result['files'][0]['content'].encode(), source)
        self.assertEqual(result['files'][0]['origin'], 'conversation')
        self.assertEqual(result['statuses'], {'helper.py': 'current'})
        self.assertEqual(result['warnings'], [])
        self.assertEqual(len(self.doc['files']), 1)  # Caller owns the original document.
        expanded = {**self.doc, 'files': [*self.doc['files'], *result['files']]}
        validate(json.loads(json.dumps(expanded)))
        self.assertIn('return 42', chat_prompt({'document': expanded, 'question': 'How is helper called?'}))

    def test_existing_references_keep_original_snapshot_when_local_code_changes(self):
        (self.root / 'source.py').write_text('changed local file')
        result = chat_result(structured_answer(references=[reference('source.py')]), self.doc, self.root)
        self.assertEqual(result['files'], [])
        self.assertEqual(result['references'][0]['path'], 'source.py')
        self.assertEqual(result['warnings'], [])
        self.assertIn('hello', self.doc['files'][0]['content'])

    def test_invalid_file_references_do_not_destroy_answer_or_expose_file_contents(self):
        (self.root / 'helper.py').write_text('one\ntwo\n')
        (self.root / '.env').write_text('SECRET=private-value\n')
        (self.root / 'binary.py').write_bytes(b'\x00binary')
        (self.root / 'nonutf.py').write_bytes(b'\xff')
        (self.root / 'big.py').write_text('x' * 250001)
        (self.root / 'outside.py').symlink_to(ASSETS / 'app.js')
        cases = [reference('missing.py'), reference('../outside.py'), reference('/etc/passwd'),
                 reference('outside.py'), reference('.env', 1, 1), reference('binary.py', 1, 1),
                 reference('nonutf.py', 1, 1), reference('big.py', 1, 1), reference(end=99),
                 reference(start=0), reference(start=True), {'path': 'helper.py'}]
        for citation in cases:
            with self.subTest(citation=citation):
                result = chat_result(structured_answer('Still readable', [citation]), self.doc, self.root)
                self.assertEqual(result['answer'], 'Still readable')
                self.assertEqual(result['files'], [])
                self.assertEqual(result['references'], [])
                self.assertEqual(len(result['warnings']), 1)
                self.assertNotIn('private-value', json.dumps(result))

    def test_attachment_budget_and_malformed_responses(self):
        citations = []
        for i in range(3):
            path = f'large-{i}.py'
            (self.root / path).write_text('#' * 200000 + '\n')
            citations.append(reference(path, 1, 1))
        result = chat_result(structured_answer(references=citations), self.doc, self.root)
        self.assertEqual(len(result['files']), 2)
        self.assertEqual(len(result['references']), 2)
        self.assertEqual(len(result['warnings']), 1)
        for raw in ('not json', '[]', '{"answer":""}', '{"answer":"OK","references":{}}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                chat_result(raw, self.doc, self.root)

    def test_adapter_uses_final_structured_message_after_progress(self):
        final = structured_answer(references=[reference()])
        events = [{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': text}}
                  for text in ('I am inspecting the helper.', final)]
        process = Mock(returncode=0)
        process.communicate.return_value = ('\n'.join(json.dumps(event) for event in events), '')
        with patch('agentic_louis.code_walk.subprocess.Popen', return_value=process):
            self.assertEqual(ask_codex('prompt', self.root), final)

    def test_timeout_stops_process_group(self):
        process = Mock()
        process.communicate.side_effect = [subprocess.TimeoutExpired('codex', 180), ('', '')]
        with patch('agentic_louis.code_walk.subprocess.Popen', return_value=process), \
                patch('agentic_louis.code_walk.stop_group') as stop:
            with self.assertRaisesRegex(ValueError, 'longer than'):
                ask_codex('prompt', self.root)
        stop.assert_called_once_with(process)

    def test_http_import_listing_chat_and_origin_protection(self):
        directory = self.root / 'walkthroughs'
        directory.mkdir()
        (directory / 'example.walk.json').write_text(json.dumps(self.doc))
        server = WalkServer(('127.0.0.1', 0), self.root, directory)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def request(method, path, body=None, headers=None):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
            self.addCleanup(connection.close)
            connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read(), response.headers

        status, data, _ = request('GET', '/api/config')
        self.assertEqual(status, 200)
        token = json.loads(data)['token']
        headers = {'X-Code-Walk-Token': token, 'Content-Type': 'application/json'}
        self.assertEqual(request('GET', '/')[0], 200)
        self.assertIn('script-src', request('GET', '/')[2]['Content-Security-Policy'])
        self.assertEqual(len(json.loads(request('GET', '/api/walkthroughs')[1])), 2)
        self.assertEqual(json.loads(request('GET', '/api/walkthrough?id=example.walk.json')[1]), self.doc)
        self.assertEqual(request('GET', '/api/walkthrough?id=../../etc/passwd')[0], 404)
        self.assertEqual(request('POST', '/api/validate', {'document': self.doc})[0], 403)
        self.assertEqual(request('GET', '/api/config', headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(request('POST', '/api/validate', {'document': self.doc},
                                 {**headers, 'Origin': 'https://evil.example'})[0], 403)
        status, data, _ = request('POST', '/api/validate', {'document': self.doc}, headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)['statuses']['source.py'], 'current')
        with patch('agentic_louis.code_walk.ask_codex', return_value=structured_answer('An explanation')) as ask:
            status, data, _ = request('POST', '/api/chat',
                                      {'document': self.doc, 'question': 'Why?', 'step_id': 'entry', 'model': 'example-model'}, headers)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(data)['answer'], 'An explanation')
            ask.assert_called_once()
            self.assertEqual(ask.call_args.kwargs['model'], 'example-model')
        self.assertEqual(request('POST', '/api/validate', [], headers)[0], 400)

    def test_bundled_demo_matches_real_repository_source(self):
        demo = validate(read_json(ASSETS / 'demo.walk.json'))
        self.assertEqual(snapshot_status(demo, Path(__file__).resolve().parents[1]),
                         {'agentic_louis/process.py': 'current'})
