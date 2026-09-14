/* A short server-validated grant. Passwords are sent only to the unlock endpoint. */
(() => {
  const panel = document.querySelector('[data-auth-panel]');
  const form = panel?.querySelector('[data-auth-form]');
  const status = panel?.querySelector('[data-auth-status]');
  const lockForm = panel?.querySelector('[data-auth-lock]');
  const channel = panel && typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel('litradar-authorization') : null;
  const protectedContent = document.querySelector('[data-auth-protected]');
  let required = document.body.dataset.adminRequired === 'true';
  let expires = Number(document.body.dataset.adminExpires) || 0;
  let deadline = performance.now() + Math.max(0, expires - Number(document.body.dataset.serverTime)) * 1000;
  let hadGrant = expires > 0;
  let waiting = [];
  let pending;
  let revision = 0, locking = false, lockedManually = false;

  function valid() { return !locking && (!required || performance.now() < deadline); }
  function render() {
    if (locking) {
      if (protectedContent) protectedContent.inert = true;
      return false;
    }
    const unlocked = valid();
    if (panel) panel.hidden = !required;
    if (status) status.hidden = !unlocked;
    if (form) form.hidden = unlocked;
    if (protectedContent) {
      protectedContent.hidden = !unlocked;
      protectedContent.inert = !unlocked;
    }
    if (unlocked && panel) {
      const seconds = Math.max(0, Math.ceil((deadline - performance.now()) / 1000));
      panel.querySelector('[data-auth-countdown]').textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
    } else if (panel && lockedManually) {
      panel.querySelector('[data-auth-heading]').textContent = '已退出操作授权';
      panel.querySelector('[data-auth-message]').textContent = '当前浏览器已锁定。重新验证可继续编辑，未保存的输入会保留；阅读和已启动的任务不受影响。';
    } else if (panel && hadGrant) {
      panel.querySelector('[data-auth-heading]').textContent = '操作授权已过期';
      panel.querySelector('[data-auth-message]').textContent = '请重新验证后继续。未保存的输入会保留，已启动的任务会继续运行；也可以前往其他页面。';
    }
    return unlocked;
  }

  function apply(state) {
    required = state.required ?? required;
    expires = Number(state.expires_at) || 0;
    deadline = performance.now() + Math.max(0, expires - state.server_time) * 1000;
    if (expires) {hadGrant = true; lockedManually = false;}
    if (render()) {
      waiting.splice(0).forEach(resolve => resolve());
      if (panel?.dataset.reload === 'true') location.reload();
    }
  }

  async function refresh() {
    if (pending) return pending;
    const requestedRevision = revision;
    pending = (async () => {
      try {
        const response = await fetch('/access/status', {headers:{Accept:'application/json'}, cache:'no-store'});
        const state = response.ok ? await response.json() : null;
        if (locking || requestedRevision !== revision) return;
        if (state) apply(state);
        else if (response.status === 401) {deadline = 0; render();}
      } catch (_) { render(); } // The server still validates every protected write.
    })();
    try { await pending; } finally { pending = null; }
  }

  async function ensure() {
    await refresh();
    if (locking) throw new Error('正在退出授权，请稍候。');
    if (render()) return;
    if (!form) { location.assign('/login'); throw new Error('请先验证访问密码。'); }
    form.scrollIntoView({behavior:'smooth', block:'center'});
    form.querySelector('input[type="password"]').focus({preventScroll:true});
    return new Promise(resolve => waiting.push(resolve));
  }

  async function authorizedFetch(url, options = {}) {
    await ensure();
    let response = await fetch(url, options);
    if (response.status === 401 && response.headers.get('X-Admin-Password-Required')) {
      deadline = 0; render();
      await ensure();
      response = await fetch(url, options); // Only retry an explicitly rejected, unexecuted request.
    }
    return response;
  }

  lockForm?.addEventListener('submit', async event => {
    event.preventDefault();
    if (locking) return;
    locking = true; revision += 1;
    const button = lockForm.querySelector('button[type="submit"]');
    const error = panel.querySelector('[data-auth-lock-error]');
    button.disabled = true; error.hidden = true; render();
    try {
      const response = await fetch('/access/lock', {method:'POST', body:new FormData(lockForm), headers:{Accept:'application/json'}});
      const state = await response.json();
      if (!response.ok) throw new Error(state.detail || '退出未成功，请重试。');
      revision += 1; locking = false; lockedManually = true;
      form.querySelector('input[type="password"]').value = '';
      apply(state);
      channel?.postMessage('locked');
    } catch (_) {
      error.textContent = '未能确认退出授权，请检查连接后重试。'; error.hidden = false;
    } finally { locking = false; button.disabled = false; render(); }
  });

  form?.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('button[type="submit"]');
    if (button.disabled) return;
    button.disabled = true;
    const error = form.querySelector('[data-auth-error]');
    error.hidden = true;
    try {
      const response = await fetch('/access/unlock', {method:'POST', body:new FormData(form), headers:{Accept:'application/json'}});
      const result = await response.json();
      form.querySelector('input[type="password"]').value = '';
      if (!response.ok) throw new Error(result.detail || '验证失败，请重新输入。');
      revision += 1;
      panel.querySelector('[data-auth-lock-error]').hidden = true;
      apply(result);
      channel?.postMessage('changed');
    } catch (failure) { error.textContent = failure.message; error.hidden = false; }
    finally { button.disabled = false; }
  });
  channel?.addEventListener('message', async event => {
    revision += 1;
    if (event.data === 'locked') {
      lockedManually = true; deadline = 0; expires = 0; render();
    } else if (event.data === 'changed') {
      if (pending) await pending;
      await refresh();
    }
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden && panel) {render(); refresh();} });
  window.addEventListener('pageshow', () => { if (panel) {render(); refresh();} });
  // Absolute five-minute expiry, even while editing; use a monotonic clock.
  if (panel) { render(); setInterval(render, 1000); setInterval(refresh, 15000); }
  window.litradarAuth = {ensure, fetch:authorizedFetch, refresh, valid};
})();
