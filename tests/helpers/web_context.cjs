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
  const empty = () => ({dataset:{},querySelector:empty,replaceChildren(){},append(){},
    setAttribute(){},removeAttribute(){},scrollIntoView(){}});
  const panel = empty();
  const context = vm.createContext({
    URL, FormData, console, crypto: require('node:crypto').webcrypto, Uint8Array,
    setTimeout: () => 1, clearTimeout() {}, setInterval() {},
    location: { href: 'http://testserver/' },
    window: { location: { href: 'http://testserver/' } },
    document: {
      body: { dataset: { group: input.group, state: input.state, guardedStages: input.stage } },
      getElementById: id => id.startsWith('acts-') ? box : id.startsWith('card-') ? row : {},
      querySelectorAll: () => [], querySelector: () => panel, createElement: empty, addEventListener() {},
    },
    fetch: async (url, options) => {
      if (url === '/admin/jobs/current') return {ok:true,json:async () => null};
      requests.push({ url, data: options.body ? Object.fromEntries(options.body) : {},
                      headers: options.headers || {} });
      return {ok:true,status:202,text:async () => '{}',json:async () => ({status:'running',steps:[]})};
    },
  });
  context.window.litradarAuth = {fetch: (...args) => context.fetch(...args)};
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../../litradar/web/static/app.js'), 'utf8'), context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../../litradar/web/static/pipeline.js'), 'utf8'), context);
  // Rendering and animation are outside this test; retain the real action/undo/run handlers.
  context._swapActs = () => box;
  let undo;
  context.litradarToast = (text, options) => { if (options?.action) undo = options.action.run; };
  await context.litradarAction(input.itemId, 'ignore');
  await undo();
  await context.litradarRun(input.stage, button);
  process.stdout.write(JSON.stringify(requests));
})().catch(error => { console.error(error); process.exitCode = 1; });
