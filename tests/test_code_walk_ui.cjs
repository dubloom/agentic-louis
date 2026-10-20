// DOM-level regression checks; no browser, server, packages, or model calls needed.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Node {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.dataset = {}; this.style = {};
    this.attributes = {}; this.hidden = false; this.value = ''; this.textContent = '';
    this.scrollTop = 0; this.scrollHeight = 200; this.classes = new Set();
    this.classList = {add: name => this.classes.add(name), remove: name => this.classes.delete(name),
      toggle: (name, active) => active ? this.classes.add(name) : this.classes.delete(name)};
  }
  append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  get firstElementChild() { return this.children[0]; }
  get lastElementChild() { return this.children.at(-1); }
  get offsetTop() { return Number(this.dataset.line || 1) * 23; }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener() {}
  focus() {}
  hasPointerCapture() { return false; }
  querySelector(query) {
    const match = query.match(/data-line="(\d+)"/);
    return match ? this.children.find(child => child.dataset.line == match[1]) : null;
  }
  remove() { if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1); }
}
function app() {
  const nodes = new Map();
  const document = {getElementById(id) { if (!nodes.has(id)) nodes.set(id, new Node()); return nodes.get(id); },
    createElement: tag => new Node(tag), createTextNode: text => Object.assign(new Node('#text'), {textContent: text}),
    addEventListener() {}, body: new Node('body')};
  const requests = [];
  const context = vm.createContext({document, setTimeout, requestAnimationFrame: () => 1, cancelAnimationFrame() {},
    fetch: async (url, options) => {
      requests.push({url, body: JSON.parse(options.body)});
      return {ok: true, json: async () => ({answer: 'A follow-up answer', files: [], references: [], warnings: [], statuses: {}})};
    }});
  const script = fs.readFileSync(path.join(__dirname, '../agentic_louis/code_walk_web/app.js'), 'utf8');
  vm.runInContext(script.replace(/start\(\);\s*$/, ''), context);
  const api = vm.runInContext('({state, attachAnswerFiles, message, ask, renderIntroduction, renderStep})', context);
  api.state.document = {version: 1, title: 'Example', summary: 'Example flow',
    files: [{path: 'main.py', language: 'python', content: 'def main():\n    return helper()\n', sha256: 'existing'}],
    steps: [{id: 'entry', title: 'Main', explanation: 'Calls the helper.', file: 'main.py', start_line: 1, end_line: 2}]};
  api.state.file = 'main.py';
  return {api, nodes, document, requests};
}

test('a related file opens its exact range, retains the step, and joins export and follow-up context', async () => {
  const {api, nodes, document, requests} = app();
  const file = {path: 'helper.py', language: 'python', content: 'def helper():\n    value = 42\n    return value\n', sha256: 'new', origin: 'conversation'};
  const reference = {path: 'helper.py', start_line: 2, end_line: 3, explanation: 'The implementation called from main.'};
  const result = {answer: 'The result comes from helper.py.', files: [file], references: [reference], warnings: [], statuses: {'helper.py': 'current'}};
  api.attachAnswerFiles(result);
  api.attachAnswerFiles(result); // A later answer can cite the same file again.
  assert.equal(api.state.document.files.length, 2);
  assert.equal(api.state.file, 'main.py');
  const message = api.message('assistant', result.answer, result.references);
  const card = message.children.find(node => node.className === 'related-code').children.find(node => node.tagName === 'button');
  assert.match(card.children[0].textContent, /helper.py:2–3 · added/);
  card.onclick();
  assert.equal(api.state.index, 0);
  assert.equal(api.state.file, 'helper.py');
  assert.equal(api.state.selection.start, 2);
  assert.equal(api.state.selection.end, 3);
  assert.equal(nodes.get('code').children.filter(row => row.classes.has('selected')).length, 2);
  assert.match(nodes.get('context').textContent, /helper.py:2–3/);
  assert.equal(JSON.parse(JSON.stringify(api.state.document)).files[1].content, file.content);
  document.getElementById('question').value = 'Why does it return value?';
  await api.ask({preventDefault() {}});
  assert.equal(requests[0].url, '/api/chat');
  assert.equal(requests[0].body.document.files[1].path, 'helper.py');
  assert.match(requests[0].body.question, /helper.py, lines 2–3/);
  assert.equal(requests[0].body.step_id, 'entry');
});

test('related code and unavailable-file messages are rendered as text', () => {
  const {api} = app();
  const message = api.message('assistant', '<script>bad()</script>', [], ['File unavailable: <img src=x>']);
  assert.equal(message.children.find(node => node.className === 'attachment-warning').textContent, 'File unavailable: <img src=x>');
  assert.ok(message.children.find(node => node.className === 'prose').children.every(node => node.tagName === 'p'));
});

test('introduction is visible by default, toggles, and remains collapsed when navigating', () => {
  const {api, nodes} = app();
  api.renderIntroduction(api.state.document);
  assert.equal(nodes.get('introduction').hidden, false);
  assert.equal(nodes.get('introduction-content').hidden, false);
  assert.equal(nodes.get('toggle-introduction').attributes['aria-expanded'], 'true');
  assert.equal(nodes.get('summary').children[0].children[0].textContent, 'Example flow');
  assert.equal(nodes.get('introduction-flow').hidden, true);
  nodes.get('toggle-introduction').onclick();
  assert.equal(nodes.get('introduction-content').hidden, true);
  api.renderStep();
  assert.equal(nodes.get('introduction-content').hidden, true);
  nodes.get('toggle-introduction').onclick();
  assert.equal(nodes.get('introduction-content').hidden, false);
  nodes.get('toggle-introduction').onclick();
  api.renderIntroduction(api.state.document);
  assert.equal(nodes.get('introduction-content').hidden, false); // A newly opened walkthrough starts expanded.
});

test('authored flow opens its step and highlights it without hiding the introduction', () => {
  const {api, nodes} = app();
  api.state.document.steps.push({id: 'result', title: 'Result', explanation: 'Returns the value.', file: 'main.py', start_line: 2, end_line: 2});
  api.state.document.introduction = {request: 'Where does the value come from?', body: 'Main delegates to a helper.',
    flow: [{title: 'Main', step_id: 'entry'}, {title: 'Return value', description: 'The result', step_id: 'result'}]};
  api.renderIntroduction(api.state.document);
  assert.equal(nodes.get('introduction-request').textContent, 'Where does the value come from?');
  const diagram = nodes.get('introduction-flow');
  assert.equal(diagram.hidden, false);
  assert.equal(diagram.children.length, 2);
  diagram.children[1].lastElementChild.onclick();
  assert.equal(api.state.index, 1);
  assert.equal(nodes.get('step-title').textContent, 'Result');
  assert.equal(diagram.children[1].lastElementChild.attributes['aria-current'], 'step');
  assert.equal(nodes.get('introduction-content').hidden, false);
});
