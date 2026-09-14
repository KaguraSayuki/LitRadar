// Execute the shipped script with a small DOM/fetch adapter. No browser or network.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

(async function () {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const requests = [];
  const classes = { add() {}, remove() {} };
  const box = { classList: classes };
  const row = { classList: classes, parentNode: {}, isConnected: true };
  const button = { innerHTML: 'Run' };
  const context = vm.createContext({
    URL, FormData, console,
    setTimeout: () => 1, clearTimeout() {},
    window: { location: { href: 'http://testserver/' }, prompt: () => 'test-password' },
    document: {
      body: { dataset: { group: input.group, state: input.state } },
      getElementById: id => id.startsWith('acts-') ? box : id.startsWith('card-') ? row : {},
      querySelectorAll: () => [button],
    },
    fetch: async (url, options) => {
      requests.push({ url, data: options.body ? Object.fromEntries(options.body) : {},
                      headers: options.headers || {} });
      const challenge = url.startsWith('/admin/') && !options.headers;
      return { ok: !challenge, status: challenge ? 401 : 200,
               headers: { get: () => challenge ? '1' : null }, text: async () => '{}' };
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../../litradar/web/static/app.js'), 'utf8'), context);
  // Rendering and animation are outside this test; retain the real action/undo/run handlers.
  context._swapActs = () => box;
  let undo;
  context.litradarToast = (text, options) => { if (options?.action) undo = options.action.run; };
  await context.litradarAction(input.itemId, 'ignore');
  await undo();
  await context.litradarRun(input.stage, button);
  process.stdout.write(JSON.stringify(requests));
})().catch(error => { console.error(error); process.exitCode = 1; });
