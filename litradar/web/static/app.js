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
  var r = await fetch('/item/' + itemId + '/action', { method: 'POST', body: fd });
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

/* 列表页:动作让条目不再属于当前视图(未读页点了已读 / 不感兴趣,
   已读页点了设为未读)时,淡出移除,并给几秒撤销机会 —— 手机上误触很常见。 */
function litradarAfterAction(itemId, action) {
  var view = document.body.dataset.state;
  var leaves = (view === 'new' && (action === 'read' || action === 'ignore'))
            || (view === 'read' && action === 'unread');
  if (!leaves) return;
  var row = document.getElementById('card-' + itemId);
  if (!row) return;
  var parent = row.parentNode, next = row.nextSibling;
  row.classList.add('is-leaving');
  var timer = setTimeout(function () { row.remove(); }, 260);
  var label = { read: '已标为已读', ignore: '已标为不感兴趣', unread: '已设为未读' }[action];
  var reverse = { read: 'unread', ignore: 'unignore', unread: 'read' }[action];
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
    var r = await fetch('/admin/run/' + stage + '?days=30', { method: 'POST' });
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
