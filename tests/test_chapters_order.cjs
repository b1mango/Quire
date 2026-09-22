// Run: node --test tests/test_chapters_order.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function setup() {
  class Element {
    constructor() {
      this.children = []; this.value = ''; this.listeners = {}; this.attributes = {};
      this.classList = { add() {}, remove() {} };
    }
    set textContent(value) { this.text = value; this.children = []; }
    get textContent() { return this.text; }
    appendChild(child) {
      this.children = this.children.filter(x => x !== child);
      this.children.push(child);
      return child;
    }
    cloneNode() { const copy = new Element(); copy.value = this.value; copy.text = this.text; return copy; }
    setAttribute(key, value) { this.attributes[key] = value; }
    addEventListener(name, fn) { this.listeners[name] = fn; }
  }
  const elements = new Map();
  const $ = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const state = { kind: 'novel', captureMode: 'catalogue' };
  const context = vm.createContext({ $, state, document: { createElement: () => new Element() }, updateSizeEstimate() {} });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../src/quire/server/web/chapters.js'), 'utf8'), context);
  const render = (kind = 'novel', count = 4) => {
    state.kind = kind;
    state.probe = { kind, chapters: Array.from({ length: count }, (_, i) => ({ title: `章${i + 1}` })) };
    context.renderRange(state.probe);
  };
  const button = () => $('rangeField').children.find(x => x.id === 'rangeOrderBtn');
  const order = id => $(id).children.map(x => x.value);
  return { $, state, context, render, button, order };
}

test('sorting changes only option display; selection, expression, payload and source remain intact', () => {
  const { $, state, context, render, button, order } = setup();
  render();
  $('rangeFirst').value = '2'; $('rangeLast').value = '3';
  $('rangeExpr').value = '1,3-'; context.updateRangeSummary();
  const source = JSON.stringify(state.probe);
  const spec = JSON.stringify(context.chapterRangeSpec());
  const summary = $('rangeSummary').textContent;
  button().listeners.click();
  for (const id of ['rangeFirst', 'rangeLast']) assert.deepEqual(order(id), ['4', '3', '2', '1']);
  assert.equal($('rangeFirst').value, '2'); assert.equal($('rangeLast').value, '3');
  assert.equal(JSON.stringify(context.chapterRangeSpec()), spec);
  assert.equal($('rangeSummary').textContent, summary);
  assert.equal(JSON.stringify(state.probe), source);
  assert.equal(button().attributes['aria-pressed'], 'true');
  $('rangeFirst').value = '4'; $('rangeFirst').listeners.change();
  assert.equal($('rangeLast').value, '4'); assert.equal($('rangeExpr').value, '4');
  button().listeners.click();
  assert.deepEqual(order('rangeFirst'), ['1', '2', '3', '4']);
  assert.equal($('rangeFirst').value, '4'); assert.equal($('rangeExpr').value, '4');
});

test('module sort state survives rerender and stays independent across capture tabs', () => {
  const { $, state, render, button, order } = setup();
  render(); button().listeners.click();
  render('manga'); assert.deepEqual(order('rangeFirst'), ['1', '2', '3', '4']);
  render('novel', 3); assert.deepEqual(order('rangeFirst'), ['3', '2', '1']);
  assert.equal($('rangeFirst').value, '1'); assert.equal($('rangeLast').value, '3');
  assert.equal($('rangeField').children.filter(x => x.id === 'rangeOrderBtn').length, 1);
  state.captureMode = 'single'; render(); assert.equal($('rangeField').hidden, true);
  state.captureMode = 'catalogue'; render('novel', 1); assert.equal($('rangeField').hidden, true);
  render('novel', 2); assert.equal($('rangeField').hidden, false);
  assert.deepEqual(order('rangeFirst'), ['2', '1']);
});
