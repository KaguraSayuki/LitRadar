/* LitRadar 前端脚本:原生 fetch,零依赖、零构建、离线可用。
   - 主题切换(浅 / 深),记在 localStorage;不切换就跟随系统
   - 反馈按钮:服务端返回整块按钮组,前端原地替换
   - 列表页:动作让条目离开当前视图时淡出移除,并给几秒"撤销"
   - 统计页:手动触发流水线 */

/* ---------------------------------------------------------------- 主题 */
function litradarToggleTheme() {
  var root = document.documentElement;
  var current = root.getAttribute('data-theme')
    || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  var next = current === 'dark' ? 'light' : 'dark';
  root.setAttribute('data-theme', next);
  try { localStorage.setItem('litradar-theme', next); } catch (e) { /* 隐私模式下写不进去,无所谓 */ }
}

/* ---------------------------------------------------------------- 轻提示 */
var _toastTimer = null;
function litradarToast(text, opts) {
  var el = document.getElementById('toast');
  if (!el) return;
  opts = opts || {};
  el.textContent = '';
  var msg = document.createElement('span');
  msg.textContent = text;
  el.appendChild(msg);
  function hide() { el.classList.remove('is-show'); }
  if (opts.action) {
    var b = document.createElement('button');
    b.type = 'button';
    b.textContent = opts.action.label;
    b.onclick = function () { hide(); opts.action.run(); };
    el.appendChild(b);
  }
  el.classList.add('is-show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(hide, opts.ms || 3500);
}

/* ---------------------------------------------------------------- 反馈按钮 */
function _groupUrl(path) {
  var url = new URL(path, window.location.href);
  var group = document.body.dataset.group;
  if (group) url.searchParams.set('g', group);
  return url.pathname + url.search + url.hash;
}

function _swapActs(box, html) {
  var tpl = document.createElement('template');
  tpl.innerHTML = html.trim();
  var fresh = tpl.content.firstElementChild;
  if (fresh) box.replaceWith(fresh);
  return fresh;
}

async function _postAction(itemId, action) {
  var fd = new FormData();
  fd.append('action', action);
  var r = await fetch(_groupUrl('/item/' + itemId + '/action'), { method: 'POST', body: fd });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.text();
}

async function litradarAction(itemId, action) {
  var box = document.getElementById('acts-' + itemId);
  if (!box) return;
  box.classList.add('is-busy');
  try {
    _swapActs(box, await _postAction(itemId, action));
    litradarAfterAction(itemId, action);
  } catch (e) {
    console.error(e);
    box.classList.remove('is-busy');
    litradarToast('操作没成功:' + e.message);
  }
}

/* 列表页:动作让条目不再属于当前视图时,淡出移除,并给几秒撤销机会 ——
   手机上误触很常见。

   "不感兴趣"会让条目从 未读/已读/全部 三个视图里同时消失(它只留在
   "不感兴趣"页签),所以在这些视图里都要移除。反过来,在"不感兴趣"页签里
   点"恢复",它也该立刻离开。收藏页签不吃这套:收藏的条目不受否决影响。 */
function litradarAfterAction(itemId, action) {
  var view = document.body.dataset.state;
  var leaves = (action === 'ignore' && view !== 'ignored' && view !== 'starred')
            || (action === 'unignore' && view === 'ignored')
            || (view === 'new' && action === 'read')
            || (view === 'read' && action === 'unread');
  if (!leaves) return;
  var row = document.getElementById('card-' + itemId);
  if (!row) return;
  var parent = row.parentNode, next = row.nextSibling;
  row.classList.add('is-leaving');
  var timer = setTimeout(function () { row.remove(); }, 260);
  var label = { read: '已标为已读', ignore: '已否决,可在「不感兴趣」里找到',
                unread: '已设为未读', unignore: '已恢复' }[action];
  var reverse = { read: 'unread', ignore: 'unignore', unread: 'read',
                  unignore: 'ignore' }[action];
  litradarToast(label, { ms: 4500, action: { label: '撤销', run: async function () {
    clearTimeout(timer);
    if (!row.isConnected) parent.insertBefore(row, (next && next.isConnected) ? next : null);
    row.classList.remove('is-leaving');
    var box = document.getElementById('acts-' + itemId);
    try { if (box) _swapActs(box, await _postAction(itemId, reverse)); }
    catch (e) { console.error(e); litradarToast('撤销没成功:' + e.message); }
  } } });
}

/* ---------------------------------------------------------------- 手动触发流水线 */
async function litradarRun(stage, btn) {
  var out = document.getElementById('run-out');
  var all = document.querySelectorAll('.steps .btn');
  var old = btn.innerHTML;
  all.forEach(function (b) { b.disabled = true; });
  btn.textContent = '运行中…';
  if (out) out.textContent = '正在运行「' + stage + '」,可能要几分钟,请不要关掉页面……';
  try {
    // 不带 days —— 服务端统一取 config 里的 app.pipeline_window_days。
    // 写死 30 天会让抓取(180 天)回来的条目永远进不了排序器。
    var url = _groupUrl('/admin/run/' + stage);
    var r = await fetch(url, { method: 'POST' });
    // 花钱阶段(rank / summarize / all)另要一次密码。服务端用这个头区分
    // "该弹密码框"和"URL 里的 token 不对" —— 两者都是 401。
    // 密码只活在这一次调用里,不写 localStorage / cookie,用完即忘。
    if (r.status === 401 && r.headers.get('X-Admin-Password-Required')) {
      var pw = window.prompt('「' + stage + '」会消耗 AI 额度,请输入管理员密码:');
      if (pw) {
        btn.textContent = '验证中…';
        r = await fetch(url, {
          method: 'POST', headers: { 'X-Admin-Password': pw },
        });
      }
    }
    var text = await r.text();
    try { text = JSON.stringify(JSON.parse(text), null, 2); } catch (e) { /* 不是 JSON 就原样显示 */ }
    if (out) out.textContent = text;
    litradarToast(r.ok ? '运行完成' : '运行失败:HTTP ' + r.status);
  } catch (e) {
    if (out) out.textContent = '失败:' + e.message;
    litradarToast('运行失败:' + e.message);
  } finally {
    all.forEach(function (b) { b.disabled = false; });
    btn.innerHTML = old;
  }
}
