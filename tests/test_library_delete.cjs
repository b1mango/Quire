// Run: node --test tests/test_library_delete.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function setup(responses) {
  const elements = new Map(), requests = [];
  const $ = id => {
    if (!elements.has(id)) elements.set(id, {
      listeners: {}, open: false,
      addEventListener(event, fn) { this.listeners[event] = fn; },
      showModal() { this.open = true; }, close() { this.open = false; },
      matches() { return false; }, focus() {},
    });
    return elements.get(id);
  };
  const state = { selected: new Set(['a', 'b']), selectedBook: { id: 'old', title: '旧书' } };
  let loads = 0;
  const context = vm.createContext({ $, state,
    document: { addEventListener() {} }, window: { addEventListener() {} },
    loadBooks() { loads++; },
    async api(url, options) {
      requests.push({ url, ...JSON.parse(JSON.stringify(options)) });
      const response = responses.shift();
      if (response instanceof Error) throw response;
      return response;
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../src/quire/server/web/library.js'), 'utf8'), context);
  // Completion's rendering is unrelated to deletion state; keep this stub focused.
  context.setManaging = on => { state.managing = on; state.selected = new Set(); };
  const click = id => $(id).listeners.click();
  return { $, state, requests, click, loads: () => loads };
}

test('partial batch failure retries only failed books, never stale single-book target', async () => {
  const h = setup([
    { failures: [{ id: 'b', error: '被占用' }] },
    { failures: [{ id: 'b', error: '仍被占用' }] },
    { failures: [] },
  ]);
  h.click('bookDeleteBtn'); // Seed a previous single-book confirmation.
  h.click('deleteBookCancel');
  h.click('batchDeleteBtn');
  await h.click('deleteBookConfirm');
  assert.equal(h.$('deleteBookDialog').open, true);
  assert.equal(h.$('deleteBookConfirm').disabled, false);
  assert.deepEqual([...h.state.selected], ['b']);
  assert.match(h.$('deleteBookMessage').textContent, /剩余的 1 本/);
  await h.click('deleteBookConfirm');
  await h.click('deleteBookConfirm');
  assert.deepEqual(h.requests.map(r => r.body.ids), [['a', 'b'], ['b'], ['b']]);
  assert.ok(h.requests.every(r => r.url === '/api/books/batch'));
  assert.equal(h.$('deleteBookDialog').open, false);
  assert.equal(h.loads(), 3);
  await h.click('deleteBookConfirm');
  assert.equal(h.requests.length, 3);
});

test('request error retains pending batch and cancel/reopen keeps only remaining failures', async () => {
  const h = setup([
    new Error('请求失败'), { failures: [{ id: 'b', error: '被占用' }] }, { failures: [] },
  ]);
  h.click('batchDeleteBtn');
  await h.click('deleteBookConfirm');
  assert.equal(h.$('deleteBookError').textContent, '请求失败');
  assert.equal(h.$('deleteBookConfirm').disabled, false);
  await h.click('deleteBookConfirm');
  h.click('deleteBookCancel');
  h.click('batchDeleteBtn');
  await h.click('deleteBookConfirm');
  assert.deepEqual(h.requests.map(r => r.body.ids), [['a', 'b'], ['a', 'b'], ['b']]);
});

test('single deletion still uses the selected single-book target', async () => {
  const h = setup([{}]);
  h.click('bookDeleteBtn');
  await h.click('deleteBookConfirm');
  assert.deepEqual(h.requests, [{ url: '/api/books/old', method: 'DELETE' }]);
  assert.equal(h.$('deleteBookDialog').open, false);
});
