const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../src/quire/server/web/app.js'), 'utf8');
const start = source.indexOf('document.addEventListener("keydown", (e) => {');
const end = source.indexOf('\n});', start) + 4;

function context({hidden = false, editable = false, dialog = false} = {}) {
  const requests = [];
  let keydown;
  vm.runInNewContext(source.slice(start, end), {
    document: {addEventListener: (_, handler) => {keydown = handler;}, querySelector: selector => selector === 'dialog[open]' ? (dialog ? {id:'updateDialog'} : null) : ({hidden})},
    $: () => ({open: dialog}), state: {job: {id: 'test', status: 'running'}},
    api: url => {requests.push(url); return Promise.resolve();},
  });
  return {requests, press: extra => keydown({key: 'Escape', target: {closest: () => editable}, ...extra})};
}

test('Escape in the task view cancels the active job', () => {
  const c = context(); c.press({});
  assert.deepEqual(c.requests, ['/api/jobs/test/cancel']);
});

for (const [label, setup, event] of [
  ['composition', {}, {isComposing: true}], ['legacy IME', {}, {keyCode: 229}],
  ['handled event', {}, {defaultPrevented: true}], ['input/select/editable', {editable: true}, {}],
  ['another page', {hidden: true}, {}], ['open dialog', {dialog: true}, {}],
  ['contenteditable', {}, {target: {isContentEditable: true}}],
]) test(`Escape does not cancel during ${label}`, () => {
  const c = context(setup); c.press(event); assert.deepEqual(c.requests, []);
});
