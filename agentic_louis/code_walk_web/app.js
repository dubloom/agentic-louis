'use strict';
const $ = id => document.getElementById(id);
const state = {document: null, imported: null, index: 0, file: null, selection: null, history: [], busy: false, token: null, statuses: {}};
let lineDrag = null;
let dragFrame = null;
function element(tag, className, content) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (content !== undefined) el.textContent = content;
  return el;
}
function notice(message) { $('notice').textContent = message; $('notice').hidden = !message; }
function showPanel(panel, focus = false) {
  const chat = panel === 'chat';
  $('guide-panel').hidden = chat;
  $('chat-panel').hidden = !chat;
  for (const name of ['guide', 'chat']) {
    const selected = name === panel;
    $(`${name}-tab`).setAttribute('aria-selected', String(selected));
    $(`${name}-tab`).tabIndex = selected ? 0 : -1;
  }
  if (chat) {
    $('reply-indicator').hidden = true;
    $('chat-panel').scrollTop = $('chat-panel').scrollHeight;
  }
  if (focus) $(`${panel}-tab`).focus({preventScroll: true});
}
function closeSidebar() {
  document.body.classList.remove('sidebar-open');
  $('toggle-sidebar').setAttribute('aria-expanded', 'false');
}
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Code-Walk-Token': state.token}, body: JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Request failed');
  return data;
}
// Limited Markdown rendered exclusively through text nodes; source and model HTML never execute.
function inline(parent, text) {
  text.split(/(`[^`]+`|\*\*[^*]+\*\*)/g).forEach(part => {
    if (part.startsWith('`') && part.endsWith('`')) parent.append(element('code', '', part.slice(1, -1)));
    else if (part.startsWith('**') && part.endsWith('**')) parent.append(element('strong', '', part.slice(2, -2)));
    else parent.append(document.createTextNode(part));
  });
}
function prose(parent, text) {
  parent.replaceChildren();
  text.split(/(```[\s\S]*?```)/g).filter(Boolean).forEach(block => {
    if (block.startsWith('```')) {
      const pre = element('pre'); pre.append(element('code', '', block.slice(3, -3).replace(/^[^\n]*\n/, ''))); parent.append(pre);
    } else block.trim().split(/\n\s*\n/).filter(Boolean).forEach(paragraph => {
      const p = element('p'); inline(p, paragraph); parent.append(p);
    });
  });
}
function highlight(parent, text) {
  const tokens = /("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|\/\/.*$|#.*$|\b(?:def|class|return|if|else|elif|try|except|finally|raise|import|from|for|in|with|as|not|and|or|None|True|False|const|let|function|async|await|throw|new|export|while)\b|\b\d+\b)/g;
  let previous = 0;
  for (const match of text.matchAll(tokens)) {
    parent.append(document.createTextNode(text.slice(previous, match.index)));
    const token = match[0];
    const kind = /^["']/.test(token) ? 'string' : /^(#|\/\/)/.test(token) ? 'comment' : /^\d/.test(token) ? 'number' : 'keyword';
    parent.append(element('span', `syntax-${kind}`, token)); previous = match.index + token.length;
  }
  parent.append(document.createTextNode(text.slice(previous)));
}
function activeStep() { return state.document.steps[state.index]; }
function setIntroductionOpen(open) {
  $('introduction-content').hidden = !open;
  $('introduction').classList.toggle('collapsed', !open);
  $('toggle-introduction').setAttribute('aria-expanded', String(open));
  $('toggle-introduction').textContent = open ? 'Hide introduction ↑' : 'Show introduction ↓';
}
function renderIntroduction(doc) {
  const intro = doc.introduction;
  $('introduction').hidden = false;
  $('introduction-request').hidden = !intro?.request;
  $('introduction-request').textContent = intro?.request || '';
  prose($('summary'), intro?.body || doc.summary);
  $('introduction-flow').replaceChildren();
  $('introduction-flow').hidden = !intro?.flow?.length;
  for (const [index, node] of (intro?.flow || []).entries()) {
    const item = element('li', 'flow-item');
    if (index) { const arrow = element('span', 'flow-arrow', '→'); arrow.setAttribute('aria-hidden', 'true'); item.append(arrow); }
    const box = element(node.step_id ? 'button' : 'div', 'flow-node');
    box.append(element('span', 'flow-title', node.title));
    if (node.description) box.append(element('span', 'flow-description', node.description));
    if (node.step_id) {
      box.dataset.step = node.step_id;
      box.title = 'Jump to this part of the walkthrough';
      box.onclick = () => { state.index = state.document.steps.findIndex(step => step.id === node.step_id); renderStep(); };
    }
    item.append(box); $('introduction-flow').append(item);
  }
  setIntroductionOpen(true);
}
function updateIntroductionFlow() {
  for (const item of $('introduction-flow').children) {
    const node = item.lastElementChild;
    const active = node.dataset.step === activeStep().id;
    node.classList.toggle('active', active);
    if (active) node.setAttribute('aria-current', 'step'); else node.removeAttribute('aria-current');
  }
}
function updateContext() {
  const step = activeStep();
  $('context').textContent = state.selection ? `Context: ${state.file}:${state.selection.start}–${state.selection.end}` : `Context: step ${state.index + 1} · ${step.file}:${step.start_line}–${step.end_line}`;
  $('context').title = $('context').textContent;
  $('range').textContent = state.selection
    ? `Selected ${state.selection.start === state.selection.end ? 'line' : 'lines'} ${state.selection.start}–${state.selection.end}`
    : step.file === state.file ? `Focus: lines ${step.start_line}–${step.end_line}` : 'Browsing source';
  $('clear-selection').hidden = !state.selection;
}
function selectLines(anchor, line) {
  state.selection = {start: Math.min(anchor, line), end: Math.max(anchor, line), anchor};
  for (const row of $('code').children) {
    const n = Number(row.dataset.line);
    row.classList.toggle('selected', n >= state.selection.start && n <= state.selection.end);
  }
  updateContext();
}
function extendLineDrag() {
  if (!lineDrag) return;
  const code = $('code'), first = code.firstElementChild;
  if (!first) return;
  const bounds = code.getBoundingClientRect();
  const y = Math.max(bounds.top + 1, Math.min(bounds.bottom - 1, lineDrag.y));
  const firstBounds = first.getBoundingClientRect();
  const line = Math.max(1, Math.min(code.children.length, Math.floor((y - firstBounds.top) / firstBounds.height) + 1));
  selectLines(lineDrag.anchor, line);
}
function scrollLineDrag() {
  if (!lineDrag) return;
  const code = $('code'), bounds = code.getBoundingClientRect();
  const edge = Math.min(32, bounds.height / 4);
  const delta = lineDrag.y < bounds.top + edge ? -10 : lineDrag.y > bounds.bottom - edge ? 10 : 0;
  if (delta) { code.scrollTop += delta; extendLineDrag(); }
  dragFrame = requestAnimationFrame(scrollLineDrag);
}
function finishLineDrag(focus = false) {
  if (!lineDrag) return;
  const pointer = lineDrag.pointer;
  lineDrag = null;
  cancelAnimationFrame(dragFrame);
  dragFrame = null;
  $('code').classList.remove('selecting-lines');
  if ($('code').hasPointerCapture(pointer)) $('code').releasePointerCapture(pointer);
  if (focus) $('question').focus({preventScroll: true});
}
function renderCode(scroll = true) {
  finishLineDrag();
  const step = activeStep(), file = state.document.files.find(file => file.path === state.file);
  $('files').value = state.file;
  $('language').textContent = file.language;
  $('range').textContent = step.file === file.path ? `Focus: lines ${step.start_line}–${step.end_line}` : 'Browsing source';
  const status = state.statuses[file.path] || 'unavailable';
  $('source-status').className = `status ${status}`;
  $('source-status').textContent = {current: '● Current', changed: 'Snapshot · changed', missing: 'Snapshot · file missing', unavailable: 'Snapshot'}[status];
  $('code').replaceChildren();
  file.content.split(/\r\n|\n|\r/).forEach((line, index) => {
    const n = index + 1;
    const row = element('div', 'code-line'); row.dataset.line = n;
    row.classList.toggle('highlighted', step.file === file.path && n >= step.start_line && n <= step.end_line);
    row.classList.toggle('selected', !!state.selection && n >= state.selection.start && n <= state.selection.end);
    row.append(element('span', 'line-number', String(n)));
    const code = element('span', 'line-text'); highlight(code, line); row.append(code);
    $('code').append(row);
  });
  if (scroll && (state.selection || step.file === file.path)) {
    const first = $('code').querySelector(`[data-line="${state.selection ? state.selection.start : step.start_line}"]`);
    $('code').scrollTop = Math.max(0, first.offsetTop - $('code').firstElementChild.offsetTop - 70);
  }
  updateContext();
}
function renderStep() {
  const step = activeStep(), total = state.document.steps.length;
  state.file = step.file; state.selection = null;
  $('step-number').textContent = `${String(state.index + 1).padStart(2, '0')} / ${String(total).padStart(2, '0')}`;
  $('step-location').textContent = `${step.file}:${step.start_line}–${step.end_line}`;
  $('step-title').textContent = step.title; prose($('explanation'), step.explanation);
  $('guide-panel').scrollTop = 0;
  $('step-details').open = false;
  $('details').replaceChildren();
  for (const [key, label] of [['inputs', 'GOES IN'], ['outputs', 'COMES OUT'], ['watch_for', 'KEEP IN MIND']]) {
    if (!step[key]) continue;
    const detail = element('div', 'detail'); detail.append(element('div', 'detail-label', label), element('p', '', step[key])); $('details').append(detail);
  }
  $('step-details').hidden = !$('details').children.length;
  $('previous').disabled = state.index === 0;
  $('next').disabled = state.index === total - 1;
  $('next').textContent = state.index === total - 1 ? 'Complete ✓' : 'Next step →';
  $('progress').style.width = `${(state.index + 1) / total * 100}%`;
  $('progress-label').textContent = `Step ${state.index + 1} of ${total}`;
  [...$('steps').children].forEach((button, index) => { button.classList.toggle('active', index === state.index); if (index === state.index) button.setAttribute('aria-current', 'step'); else button.removeAttribute('aria-current'); });
  updateIntroductionFlow();
  showPanel('guide');
  renderCode();
}
async function loadDocument(doc) {
  if (state.busy) throw new Error('Wait for the current answer before changing walkthroughs.');
  const result = await api('/api/validate', {document: doc});
  state.document = doc; state.index = 0; state.statuses = result.statuses; state.history = [];
  $('messages').replaceChildren();
  $('chat-empty').hidden = false;
  $('reply-indicator').hidden = true;
  $('title').textContent = doc.title;
  renderIntroduction(doc);
  document.title = `${doc.title} · Code Walk`;
  $('file-count').textContent = `${doc.files.length} source ${doc.files.length === 1 ? 'file' : 'files'}`;
  $('step-count').textContent = doc.steps.length;
  $('steps').replaceChildren();
  doc.steps.forEach((step, index) => {
    const button = element('button', 'step-link'); button.append(element('span', 'step-dot', String(index + 1)), element('span', '', step.title));
    button.addEventListener('click', () => { state.index = index; renderStep(); closeSidebar(); }); $('steps').append(button);
  });
  renderFileOptions();
  $('glossary').replaceChildren();
  for (const entry of doc.glossary || []) { const item = element('div', 'glossary-item'); item.append(element('strong', '', entry.term), document.createTextNode(entry.definition)); $('glossary').append(item); }
  $('glossary-section').hidden = !doc.glossary?.length;
  renderStep(); notice('');
}
function renderFileOptions() {
  $('files').replaceChildren();
  for (const file of state.document.files) {
    const option = element('option', '', file.path + (file.origin === 'conversation' ? ' · added' : ''));
    option.value = file.path; $('files').append(option);
  }
  $('files').value = state.file;
  const count = state.document.files.length;
  $('file-count').textContent = `${count} source ${count === 1 ? 'file' : 'files'}`;
}
function attachAnswerFiles(result) {
  for (const file of result.files || []) {
    if (!state.document.files.some(existing => existing.path === file.path)) state.document.files.push(file);
  }
  Object.assign(state.statuses, result.statuses || {});
  renderFileOptions();
}
function openReference(reference) {
  if (!state.document.files.some(file => file.path === reference.path)) return;
  state.file = reference.path;
  state.selection = {start: reference.start_line, end: reference.end_line, anchor: reference.start_line};
  renderCode();
}
function message(role, text, references = [], warnings = []) {
  const container = element('div', `message ${role}`), body = element('div', 'prose');
  container.append(element('div', 'message-label', {user: 'YOU', assistant: 'CODEX', error: 'COULD NOT ANSWER'}[role]));
  if (role === 'user') body.textContent = text; else prose(body, text);
  container.append(body);
  if (references.length) {
    const related = element('div', 'related-code');
    related.append(element('div', 'eyebrow', 'RELATED CODE'));
    for (const reference of references) {
      const button = element('button', 'code-reference');
      const added = state.document.files.find(file => file.path === reference.path)?.origin === 'conversation';
      button.append(element('span', 'reference-path', `${reference.path}:${reference.start_line}–${reference.end_line}${added ? ' · added' : ''}`), element('span', 'reference-reason', reference.explanation));
      button.title = 'Open these lines in the code pane';
      button.onclick = () => openReference(reference);
      related.append(button);
    }
    container.append(related);
  }
  for (const warning of warnings) container.append(element('p', 'attachment-warning', warning));
  $('messages').append(container);
  $('chat-empty').hidden = true;
  $('chat-panel').scrollTop = $('chat-panel').scrollHeight;
  if (role !== 'user' && $('chat-panel').hidden) $('reply-indicator').hidden = false;
  return container;
}
async function ask(event) {
  event.preventDefault();
  const question = $('question').value.trim(); if (!question || state.busy || !state.document) return;
  const step = activeStep(), selection = state.selection;
  const contextualQuestion = selection ? `${question}\n\nSelected snapshot: ${state.file}, lines ${selection.start}–${selection.end}.` : question;
  const model = $('model').value === 'custom' ? $('custom-model').value.trim() : $('model').value;
  if ($('model').value === 'custom' && !model) { $('custom-model').focus({preventScroll: true}); notice('Enter a model ID, or choose Codex default.'); return; }
  notice('');
  const payload = {document: state.document, question: contextualQuestion, step_id: step.id, history: state.history.slice(-20), model};
  showPanel('chat');
  state.busy = true; $('send').disabled = true; $('import-button').disabled = true; $('library').disabled = true; $('clear-chat').disabled = true;
  $('model').disabled = true; $('custom-model').disabled = true;
  message('user', question); $('question').value = '';
  const pending = element('div', 'pending', 'Reading the code…'); pending.setAttribute('role', 'status'); $('messages').append(pending);
  $('chat-panel').scrollTop = $('chat-panel').scrollHeight;
  try {
    const result = await api('/api/chat', payload);
    attachAnswerFiles(result);
    message('assistant', result.answer, result.references, result.warnings);
    const sources = (result.references || []).map(ref => `${ref.path}:${ref.start_line}–${ref.end_line}: ${ref.explanation}`).join('\n');
    const historyAnswer = result.answer + (sources ? `\n\nRelated code:\n${sources}` : '');
    state.history.push({role: 'user', content: contextualQuestion.slice(0, 16000)}, {role: 'assistant', content: historyAnswer.slice(0, 16000)});
  } catch (error) { message('error', error.message); $('question').value = question; }
  finally { pending.remove(); state.busy = false; $('send').disabled = false; $('import-button').disabled = false; $('library').disabled = false; $('clear-chat').disabled = false; $('model').disabled = false; $('custom-model').disabled = false; }
}
$('previous').onclick = () => { if (state.index > 0) { state.index--; renderStep(); } };
$('toggle-introduction').onclick = () => setIntroductionOpen($('introduction-content').hidden);
$('next').onclick = () => { if (state.index < state.document.steps.length - 1) { state.index++; renderStep(); } };
$('files').onchange = () => { state.file = $('files').value; state.selection = null; renderCode(); };
$('code').onpointerdown = event => {
  if (event.button !== 0 || event.pointerType === 'touch' || lineDrag) return;
  const row = event.target.closest('.code-line');
  if (!row) return;
  event.preventDefault();
  const line = Number(row.dataset.line);
  const anchor = event.shiftKey && state.selection ? state.selection.anchor : line;
  selectLines(anchor, line);
  lineDrag = {pointer: event.pointerId, anchor, y: event.clientY};
  $('code').classList.add('selecting-lines');
  $('code').setPointerCapture(event.pointerId);
  dragFrame = requestAnimationFrame(scrollLineDrag);
};
$('code').onpointermove = event => {
  if (!lineDrag || event.pointerId !== lineDrag.pointer) return;
  lineDrag.y = event.clientY;
  extendLineDrag();
};
$('code').onpointerup = event => {
  if (lineDrag && event.pointerId === lineDrag.pointer) finishLineDrag(true);
};
$('code').onpointercancel = $('code').onlostpointercapture = () => finishLineDrag();
// Touch taps select a line without interfering with native scrolling.
$('code').onclick = event => {
  if (event.pointerType && event.pointerType !== 'touch') return;
  const row = event.target.closest('.code-line');
  if (!row) return;
  const line = Number(row.dataset.line);
  selectLines(event.shiftKey && state.selection ? state.selection.anchor : line, line);
  $('question').focus({preventScroll: true});
};
$('clear-selection').onclick = () => { state.selection = null; renderCode(false); };
$('chat-form').onsubmit = ask;
$('model').onchange = () => {
  $('custom-model').hidden = $('model').value !== 'custom';
  if (!$('custom-model').hidden) $('custom-model').focus({preventScroll: true});
};
$('question').onkeydown = event => { if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) $('chat-form').requestSubmit(); };
$('ask-step').onclick = () => { $('question').value = 'Help me understand this step. Walk through a concrete example.'; $('question').focus({preventScroll: true}); };
for (const button of $('suggestions').children) button.onclick = () => { $('question').value = button.dataset.question; $('question').focus({preventScroll: true}); };
$('clear-chat').onclick = () => { state.history = []; $('messages').replaceChildren(); $('chat-empty').hidden = false; $('reply-indicator').hidden = true; };
$('guide-tab').onclick = () => showPanel('guide');
$('chat-tab').onclick = () => showPanel('chat');
for (const name of ['guide', 'chat']) $(`${name}-tab`).onkeydown = event => {
  if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
    event.preventDefault();
    showPanel(event.key === 'Home' ? 'guide' : event.key === 'End' ? 'chat' : name === 'guide' ? 'chat' : 'guide', true);
  }
};
$('toggle-sidebar').onclick = () => {
  const open = document.body.classList.toggle('sidebar-open');
  $('toggle-sidebar').setAttribute('aria-expanded', String(open));
};
document.addEventListener('keydown', event => { if (event.key === 'Escape') closeSidebar(); });
$('download').onclick = () => {
  if (!state.document) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(state.document, null, 2)], {type: 'application/json'}));
  const a = element('a'); a.href = url; a.download = 'walkthrough.walk.json'; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
};
$('import-button').onclick = () => $('import-file').click();
$('import-file').onchange = async () => {
  try {
    const file = $('import-file').files[0]; if (!file) return;
    if (file.size > 2000000) throw new Error('Walkthrough must be smaller than 2 MB.');
    await loadDocument(JSON.parse(await file.text()));
    state.imported = state.document;
    let imported = $('library').querySelector('[value="imported"]');
    if (!imported) { imported = element('option'); imported.value = 'imported'; $('library').append(imported); }
    imported.textContent = state.document.title; $('library').value = 'imported';
  } catch (error) { notice(error.message); }
  finally { $('import-file').value = ''; }
};
$('library').onchange = async () => {
  try { await loadDocument($('library').value === 'imported' ? state.imported : await api(`/api/walkthrough?id=${encodeURIComponent($('library').value)}`)); }
  catch (error) { notice(error.message); }
};
async function start() {
  try {
    const config = await api('/api/config'); state.token = config.token; $('repo').textContent = `⌘ ${config.repo}`;
    $('agent-status').textContent = config.agent_available ? 'Codex · read-only' : 'Install Codex to ask questions';
    const custom = $('model').lastElementChild;
    for (const model of config.models || []) {
      const option = element('option', '', model.name); option.value = model.id;
      $('model').insertBefore(option, custom);
    }
    const entries = await api('/api/walkthroughs');
    for (const entry of entries) { const option = element('option', '', `${entry.invalid ? '⚠ ' : ''}${entry.title}`); option.value = entry.id; $('library').append(option); }
    const first = entries.find(entry => !entry.invalid); if (first) { $('library').value = first.id; await loadDocument(await api(`/api/walkthrough?id=${encodeURIComponent(first.id)}`)); }
  } catch (error) { notice(`Could not open Code Walk: ${error.message}`); }
}
start();
