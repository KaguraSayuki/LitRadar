// Execute the shipped authorization controller with a clock and small DOM adapter.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

(async () => {
  let clock = 0, serverNow = 1000, grant = 1300, accepted = true, rejectWrite = false;
  let holdStatus = false, statusResponse, failLock = false, channel;
  const nodes = new Map(), timers = [], writes = [];
  const node = name => {
    if (!nodes.has(name)) nodes.set(name, {dataset:{}, value:'draft remains', handlers:{},
      querySelector: node, focus() {}, scrollIntoView() {},
      addEventListener(type, callback) {this.handlers[type] = callback;}});
    return nodes.get(name);
  };
  const form = node('[data-auth-form]'), panel = node('[data-auth-panel]');
  const draft = node('[data-auth-protected]');
  const context = {performance:{now:() => clock},
    BroadcastChannel: class {
      constructor() {channel = this; this.messages = [];}
      postMessage(message) {this.messages.push(message);}
      addEventListener(type, handler) {this.receive = handler;}
    },
    FormData: class extends Map {constructor(){super([['password', node('input[type="password"]').value]]);}},
    document: {body:{dataset:{adminRequired:'true',adminExpires:'1300',serverTime:'1000'}},
      querySelector:node, addEventListener() {}},
    window:{addEventListener() {}}, location:{reload(){throw Error('unexpected reload');}},
    setInterval: (fn, ms) => {timers.push({fn,ms});},
    fetch: async (url, options) => {
      if (url === '/access/status') {
        const result = {required:true, expires_at:grant, server_time:serverNow};
        if (holdStatus) return new Promise(resolve => {statusResponse = () => resolve({ok:true,json:async () => result});});
        return {ok:true,json:async () => result};
      }
      if (url === '/access/lock') {
        if (failLock) throw Error('Connection failed');
        grant = 0;
        return {ok:true,json:async () => ({required:true, expires_at:0, server_time:serverNow})};
      }
      if (url === '/access/unlock') {
        if (accepted) grant = serverNow + 300;
        return {ok:accepted, json:async () => accepted ? {expires_at:grant,server_time:serverNow} : {detail:'密码不正确'}};
      }
      writes.push({url,options});
      if (rejectWrite) {rejectWrite = false; grant = 0; return {status:401,headers:{get:()=> '1'}};}
      return {status:200};
    },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../litradar/web/static/authorization.js'),'utf8'), context);
  assert.equal(draft.hidden, false);
  assert.equal(form.hidden, true);
  clock = 300000; serverNow = 1300; grant = 0;
  timers.find(t => t.ms === 1000).fn();
  assert.equal(draft.hidden, true);
  assert.equal(draft.inert, true);
  assert.equal(draft.value, 'draft remains');
  assert.match(node('[data-auth-heading]').textContent, /过期/);
  const body = new Map([['action','copy'],['version','unsaved-revision']]);
  const next = context.window.litradarAuth.fetch('/settings/group-action?slug=A', {method:'POST',body});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(writes.length, 0);
  accepted = false;
  await form.handlers.submit({preventDefault(){}});
  assert.equal(node('[data-auth-error]').hidden, false);
  assert.equal(draft.hidden, true);
  assert.equal(writes.length, 0);
  accepted = true;
  await form.handlers.submit({preventDefault(){}});
  await next;
  assert.equal(writes.length, 1);
  assert.equal(writes[0].options.body, body);
  assert.equal(writes[0].options.headers, undefined);
  assert.equal(node('input[type="password"]').value, '');
  assert.equal(draft.hidden, false);
  assert.equal(draft.value, 'draft remains');
  clock += 15000; serverNow += 15;
  await context.window.litradarAuth.refresh();
  assert.equal(node('[data-auth-countdown]').textContent, '4:45');
  // Server expiry can race with a click: only a rejected request is retried.
  rejectWrite = true;
  const retry = context.window.litradarAuth.fetch('/settings/group-action?slug=A', {method:'POST',body});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(writes.length, 2);
  assert.equal(form.hidden, false);
  await form.handlers.submit({preventDefault(){}});
  await retry;
  assert.equal(writes.length, 3);
  assert.equal(writes[2].url, writes[1].url);
  assert.equal(writes[2].options.body, writes[1].options.body);
  assert.equal(panel.hidden, false);
  // A status request from before exit must not reopen the editor afterward.
  holdStatus = true;
  const stale = context.window.litradarAuth.refresh();
  const exit = node('[data-auth-lock]').handlers.submit;
  await exit({preventDefault(){}});
  assert.equal(draft.hidden, true);
  assert.equal(draft.value, 'draft remains');
  assert.match(node('[data-auth-heading]').textContent, /已退出/);
  assert.equal(channel.messages.at(-1), 'locked');
  statusResponse(); await stale; holdStatus = false;
  assert.equal(context.window.litradarAuth.valid(), false);
  assert.equal(draft.hidden, true);
  // Other tabs get a notification, then validate the shared browser cookie.
  grant = serverNow + 300;
  await channel.receive({data:'changed'});
  assert.equal(draft.hidden, false);
  failLock = true;
  const broadcasts = channel.messages.length;
  await exit({preventDefault(){}});
  assert.equal(node('[data-auth-lock-error]').hidden, false);
  assert.equal(channel.messages.length, broadcasts);
  assert.equal(node('button[type="submit"]').disabled, false);
  grant = 0;
  await channel.receive({data:'locked'});
  assert.equal(draft.hidden, true);
  assert.equal(context.window.litradarAuth.valid(), false);
  assert.equal(writes.length, 3); // Exiting never replays pending settings operations.
  process.stdout.write('authorization passed');
})().catch(error => {console.error(error);process.exitCode=1;});
