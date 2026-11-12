// Run: node --test tests/test_library_delete.cjs
// 删除不可撤销：确认框写清后果；批量部分失败时只重试失败的书，不会回到旧的目标。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function setup(responses, answers) {
  const elements = new Map(), requests = [], dialogs = [];
  const el = () => ({
    listeners: {}, value: '', innerHTML: '', textContent: '', className: '', dataset: {},
    classList: { toggle() {}, add() {}, remove() {} },
    addEventListener(event, fn) { this.listeners[event] = fn; },
    setAttribute() {}, removeAttribute() {}, querySelectorAll: () => [], focus() {},
  });
  const $ = (id) => { if (!elements.has(id)) elements.set(id, el()); return elements.get(id); };
  const state = {
    view: 'library', group: 'all', layout: 'grid', search: '', sel: new Set(['a', 'b']), anchor: null,
    books: [{ id: 'a', title: '甲书' }, { id: 'b', title: '乙书' }, { id: 'old', title: '旧书' }], groups: [],
  };
  let loads = 0;
  const toasts = [];
  const context = vm.createContext({
    $, state, VIEW_HOOKS: {}, localStorage: { getItem: () => null, setItem() {} },
    document: { addEventListener() {}, querySelectorAll: () => [], querySelector: () => null },
    html: (strings, ...values) => strings.reduce((out, s, i) => out + s + (i < values.length ? [].concat(values[i]).join('') : ''), ''),
    errText: (err) => err.message, toast: (msg) => toasts.push(msg), renderInspector() {}, renderSidebar() {},
    async loadBooks() { loads++; },
    async confirmBox(opts) { dialogs.push(opts); return answers.shift(); },
    async api(url, options) {
      requests.push({ url, ...JSON.parse(JSON.stringify(options)) });
      const response = responses.shift();
      if (response instanceof Error) throw response;
      return response;
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../src/quire/server/web/library.js'), 'utf8'), context);
  // loadBooks 由 library.js 定义，这里换成计数桩，避免依赖服务端
  context.loadBooks = async () => { loads++; };
  return { state, requests, dialogs, toasts, context, loads: () => loads };
}

test('partial batch failure retries only failed books, then closes', async () => {
  const h = setup([
    { failures: [{ id: 'b', error: '被占用' }] },
    { failures: [{ id: 'b', error: '仍被占用' }] },
    { failures: [] },
  ], [true, true, true]);
  await h.context.askDelete();
  assert.deepEqual(h.requests.map((r) => r.body.ids), [['a', 'b'], ['b'], ['b']]);
  assert.ok(h.requests.every((r) => r.url === '/api/books/batch' && r.body.action === 'delete'));
  assert.equal(h.dialogs[0].title, '删除 2 本书？');
  assert.match(h.dialogs[0].body, /无法恢复/);
  assert.match(h.dialogs[1].title, /剩余的 1 本/);
  assert.match(h.dialogs[1].body, /被占用/);
  assert.deepEqual([...h.state.sel], []);
  assert.equal(h.loads(), 3);
});

test('request error keeps the same batch; cancelling leaves only the remaining failures selected', async () => {
  const h = setup([new Error('请求失败'), { failures: [{ id: 'b', error: '被占用' }] }], [true, true, false]);
  await h.context.askDelete();
  assert.deepEqual(h.requests.map((r) => r.body.ids), [['a', 'b'], ['a', 'b']]);
  assert.match(h.dialogs[1].body, /请求失败/);
  assert.deepEqual([...h.state.sel], ['b']);
});

test('single selection deletes exactly that book, never a stale target', async () => {
  const h = setup([{ failures: [] }], [true]);
  h.state.sel = new Set(['old']);
  await h.context.askDelete();
  assert.deepEqual(h.requests, [{ url: '/api/books/batch', method: 'POST', body: { action: 'delete', ids: ['old'] } }]);
  assert.equal(h.dialogs[0].title, '删除《旧书》？');
  assert.deepEqual(h.toasts, ['已删除《旧书》']);
});

test('declining the confirmation sends nothing', async () => {
  const h = setup([], [false]);
  await h.context.askDelete();
  assert.equal(h.requests.length, 0);
  assert.deepEqual([...h.state.sel], ['a', 'b']);
});
