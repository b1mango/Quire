// Run: node --test tests/test_chapters_order.cjs
// 章节范围控件：目录正倒序只改显示；表达式是唯一事实源；续抓默认从已抓之后开始。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

class Element {
  constructor(value = '') {
    this.children = []; this.value = value; this.listeners = {}; this.attributes = {};
    this.textContent = ''; this.classList = { toggle() {}, add() {}, remove() {} };
  }
  appendChild(child) {
    this.children = this.children.filter((x) => x !== child);
    this.children.push(child);
    return child;
  }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  addEventListener(name, fn) { this.listeners[name] = fn; }
}

function setup() {
  let elements = new Map();
  const $ = (id) => elements.get(id) || null;
  const state = { probe: null };
  const html = (strings, ...values) => strings.reduce((out, s, i) => out + s + (i < values.length ? [].concat(values[i]).join('') : ''), '');
  const context = vm.createContext({ $, state, html });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../src/quire/server/web/chapters.js'), 'utf8'), context);
  const changes = [];
  /* 模拟 showResult：按 rangeRowHTML 的结构建出控件，再 bindRange */
  const render = (kind = 'novel', count = 4, owned = 0) => {
    state.probe = { kind, count, chapters: Array.from({ length: count }, (_, i) => ({ title: `章${i + 1}` })),
                    library: owned ? { chapters: owned } : null };
    elements = new Map();
    if (context.rangeRowHTML() === '') { context.bindRange(kind, (s) => changes.push(s)); return; }
    for (const id of ['rangeFirst', 'rangeLast']) {
      const select = new Element();
      for (let n = 1; n <= count; n += 1) select.appendChild(new Element(String(n)));
      elements.set(id, select);
    }
    for (const id of ['rangeAll', 'rangeOrderBtn', 'rangeExpr', 'rangeSum']) elements.set(id, new Element());
    context.bindRange(kind, (s) => changes.push(s));
  };
  const order = (id) => $(id).children.map((x) => x.value);
  const click = (id) => $(id).listeners.click();
  return { $, state, context, render, order, click, changes };
}

test('sorting changes only option display; selection, expression and payload stay intact', () => {
  const { $, context, render, order, click } = setup();
  render();
  $('rangeExpr').value = '1,3-'; $('rangeExpr').listeners.input();
  const spec = JSON.stringify(context.chapterRangeSpec());
  const summary = $('rangeSum').textContent;
  click('rangeOrderBtn');
  for (const id of ['rangeFirst', 'rangeLast']) assert.deepEqual(order(id), ['4', '3', '2', '1']);
  assert.equal(JSON.stringify(context.chapterRangeSpec()), spec);
  assert.equal($('rangeSum').textContent, summary);
  assert.equal($('rangeOrderBtn').attributes['aria-pressed'], 'true');
  assert.equal($('rangeOrderBtn').textContent, '目录倒序');
  $('rangeFirst').value = '4'; $('rangeFirst').listeners.change();
  assert.equal($('rangeLast').value, '4'); assert.equal($('rangeExpr').value, '4');
  click('rangeOrderBtn');
  assert.deepEqual(order('rangeFirst'), ['1', '2', '3', '4']);
  assert.equal($('rangeFirst').value, '4'); assert.equal($('rangeExpr').value, '4');
});

test('sort order is remembered per kind across re-probes', () => {
  const { render, order, click } = setup();
  render(); click('rangeOrderBtn');
  render('manga'); assert.deepEqual(order('rangeFirst'), ['1', '2', '3', '4']);
  render('novel', 3); assert.deepEqual(order('rangeFirst'), ['3', '2', '1']);
});

test('single chapter pages get no range row and submit the full range', () => {
  const { $, context, render } = setup();
  render('novel', 1);
  assert.equal($('rangeFirst'), null);
  assert.deepEqual({ ...context.chapterRangeSpec() }, { chapter_first: 1, chapter_last: 0, chapter_ranges: '' });
  assert.equal(context.rangeSummary().count, 1);
});

test('resume starts after chapters already in the library', () => {
  const { $, context, render } = setup();
  render('novel', 6, 2);
  assert.equal($('rangeFirst').value, '3'); assert.equal($('rangeLast').value, '6');
  assert.equal($('rangeExpr').value, '3-6');
  assert.equal(context.rangeSummary().count, 4);
  context.applyRangeOrder();
  $("rangeAll").listeners.click();
  assert.equal($('rangeExpr').value, '');
  assert.equal(context.rangeSummary().label, '全部 6 章');
});

test('invalid expressions surface an error and block the dock', () => {
  const { $, context, render, changes } = setup();
  render();
  $('rangeExpr').value = '3-x'; $('rangeExpr').listeners.input();
  assert.match(context.rangeSummary().error, /无法识别/);
  assert.ok(changes.at(-1).error);
  $('rangeExpr').value = '2-9'; $('rangeExpr').listeners.input();
  assert.match(context.rangeSummary().error, /超出当前目录/);
  $('rangeExpr').value = '2-3'; $('rangeExpr').listeners.input();
  assert.equal($('rangeFirst').value, '2'); assert.equal($('rangeLast').value, '3');
  assert.equal(context.rangeSummary().count, 2);
});
