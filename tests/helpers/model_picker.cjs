// Exercise stale-result handling and manual selection using the shipped controller.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
(async () => {
  const nodes = new Map();
  for (const id of ['model-settings', 'llm.base_url', 'llm.model', 'available-models',
    'model-list-field', 'model-list-result', 'credential-llm', 'fetch-models']) {
    nodes.set(id, {id, value:'', dataset:{}, handlers:{}, children:[],
      addEventListener(type, fn) {this.handlers[type] = fn;},
      reportValidity() {return true;},
      replaceChildren(...children) {this.children = children;},
      append(child) {this.children.push(child);},
    });
  }
  const address = nodes.get('llm.base_url');
  const name = nodes.get('llm.model');
  const choices = nodes.get('available-models');
  const button = nodes.get('fetch-models');
  const result = nodes.get('model-list-result');
  address.value = 'https://first.invalid/v1';
  name.value = 'manually-entered';
  const requests = [];
  let respond;
  const context = {
    Option: class {constructor(text, value) {this.text = text; this.value = value;}},
    FormData: class extends Map {constructor() {super();}},
    document: {getElementById: id => nodes.get(id) || null, querySelector: () => null,
      querySelectorAll: () => [], addEventListener() {}},
    fetch: (url, request) => {requests.push({url, body:Object.fromEntries(request.body)});
      return new Promise(resolve => {respond = models => resolve({ok:true, status:200,
        json:async () => ({models, message:'请选择模型'})});});},
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../../litradar/web/static/settings.js'), 'utf8'), context);
  const first = button.handlers.click({currentTarget:button});
  assert.equal(button.disabled, true);
  address.value = 'https://second.invalid/v1';
  address.handlers.input();
  respond(['obsolete-model']);
  await first;
  assert.equal(nodes.get('model-list-field').hidden, true);
  assert.equal(name.value, 'manually-entered');
  const second = button.handlers.click({currentTarget:button});
  respond(['new-model', '<model-name>']);
  await second;
  assert.equal(requests[1].body.base_url, 'https://second.invalid/v1');
  assert.equal(nodes.get('model-list-field').hidden, false);
  assert.equal(name.value, 'manually-entered'); // Fetching must not silently change the model.
  choices.value = 'new-model';
  choices.handlers.change();
  assert.equal(name.value, 'new-model');
  assert.equal(choices.children[2].text, '<model-name>'); // Model names remain text.
  nodes.get('credential-llm').value = 'unsaved-key';
  nodes.get('credential-llm').handlers.input();
  await button.handlers.click({currentTarget:button});
  assert.equal(requests.length, 2);
  assert.match(result.textContent, /先保存新密钥/);
  process.stdout.write('model picker passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
