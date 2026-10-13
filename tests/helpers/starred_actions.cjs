// Run the shipped action and undo handlers without a browser or network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

(async () => {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const requests = [];
  const classes = new Set();
  const box = {classList: {add() {}, remove() {}}};
  const row = {
    isConnected: true,
    classList: {add: name => classes.add(name), remove: name => classes.delete(name)},
    remove() { this.isConnected = false; },
    parentNode: {insertBefore(node) {node.isConnected = true;}},
  };
  let remove, undo;
  const context = {
    URL, FormData, console,
    setTimeout: callback => {remove = callback; return 1;}, clearTimeout() {},
    window: {location: {href: 'http://testserver/?state=starred'}},
    document: {
      body: {dataset: {state: 'starred'}},
      getElementById: id => id.startsWith('acts-') ? box : row,
    },
    fetch: async (url, options) => {
      requests.push({url, data: Object.fromEntries(options.body)});
      return {ok: true, text: async () => '<div></div>'};
    },
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../../litradar/web/static/app.js'), 'utf8'), context);
  context._swapActs = () => box;
  context.litradarToast = (text, options) => {undo = options.action.run;};
  await context.litradarAction(input.itemId, 'unstar');
  assert.equal(classes.has('is-leaving'), true);
  remove();
  assert.equal(row.isConnected, false);
  await undo();
  assert.equal(row.isConnected, true);
  assert.equal(classes.has('is-leaving'), false);
  assert.deepEqual(requests.map(request => request.data.action), ['unstar', 'star']);
  process.stdout.write(JSON.stringify(requests));
})().catch(error => {console.error(error); process.exitCode = 1;});
