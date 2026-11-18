// Run: node --test tests/test_shortcuts.cjs
// 全局快捷键与通用确认框：任务页裸按 Esc 不误取消下载；书库页 Esc 收起选择；confirmBox 输入框自动聚焦并支持回车提交。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../src/quire/server/web/app.js'), 'utf8');

function shortcutContext({ view = 'tasks', sel = ['b1'], dialog = false, readerOpen = false, activeTag = 'BODY', contentEditable = false } = {}) {
  const requests = [], selections = [];
  let keydown;
  const start = source.indexOf('function typing()');
  const end = source.indexOf('document.addEventListener("paste"');
  vm.runInNewContext(source.slice(start, end), {
    document: {
      activeElement: { tagName: activeTag, isContentEditable: contentEditable },
      addEventListener: (ev, fn) => { if (ev === 'keydown') keydown = fn; },
      querySelector: (sel) => (sel === 'dialog[open]' && dialog ? { id: 'confirm' } : null),
    },
    $: (id) => (id === 'reader' ? { hidden: !readerOpen } : { focus() {} }),
    state: { view, sel: new Set(sel), job: { id: 'j1', status: 'running' }, probe: null },
    api: (url) => { requests.push(url); return Promise.resolve(); },
    setSelection: (next) => { selections.push([...next]); },
    openPalette() {}, resetCapture() {}, go() {}, startJob() {}, selectAllBooks() {}, askDelete() {},
  });
  return {
    requests,
    selections,
    press: (extra = {}) => keydown({
      key: 'Escape', metaKey: false, ctrlKey: false, defaultPrevented: false, isComposing: false, keyCode: 27,
      preventDefault() {}, ...extra,
    }),
  };
}

test('Escape on tasks view never cancels active downloads', () => {
  const c = shortcutContext({ view: 'tasks' });
  c.press();
  assert.deepEqual(c.requests, []);
});

test('Escape on library view clears selection only when idle', () => {
  const idle = shortcutContext({ view: 'library', sel: ['b1'] });
  idle.press();
  assert.deepEqual(idle.selections, [[]]);

  for (const [label, opts, ev] of [
    ['input focused', { view: 'library', activeTag: 'INPUT' }, {}],
    ['dialog open', { view: 'library', dialog: true }, {}],
    ['reader open', { view: 'library', readerOpen: true }, {}],
    ['IME composition', { view: 'library' }, { isComposing: true }],
    ['contenteditable', { view: 'library', contentEditable: true }, {}],
  ]) {
    const blocked = shortcutContext(opts);
    blocked.press(ev);
    assert.deepEqual(blocked.selections, [], label);
  }
});

test('confirmBox autofocuses text input, submits on Enter, and closes on backdrop click', async () => {
  const start = source.indexOf('function confirmBox(');
  const end = source.indexOf('\n/* ----', start);
  let focused = false, selected = false;
  const okBtn = {};
  const input = {
    tagName: 'INPUT',
    focus() { focused = true; },
    select() { selected = true; },
    closest: (s) => (s.startsWith('input') ? input : null),
  };
  const box = {
    className: '', innerHTML: '', onclick: null, onkeydown: null,
    querySelector: (s) => (s.includes('input') ? input : s === '[data-r="1"]' ? okBtn : null),
  };
  const dialog = {
    open: false, onclick: null, onclose: null,
    showModal() { this.open = true; },
    close() { this.open = false; if (this.onclose) this.onclose(); },
  };
  const ctx = vm.createContext({
    $: (id) => (id === 'confirmIn' ? box : dialog),
    html: (strings, ...vals) => strings.reduce((a, s, i) => a + s + (vals[i] ?? ''), ''),
  });
  vm.runInContext(source.slice(start, end), ctx);

  const p1 = ctx.confirmBox({ title: '新建分组', body: '<input>', ok: '保存' });
  assert.equal(focused, true);
  assert.equal(selected, true);
  let prevented = false;
  box.onkeydown({ key: 'Enter', isComposing: false, keyCode: 13, target: input, preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(await p1, true);

  const p2 = ctx.confirmBox({ title: '删除？', body: '<p>确认</p>', ok: '删除', cancel: '取消' });
  dialog.onclick({ target: dialog });
  assert.equal(await p2, false);
});
