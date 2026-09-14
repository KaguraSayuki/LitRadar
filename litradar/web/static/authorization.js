/* A short server-validated grant. Passwords are sent only to the unlock endpoint. */
(() => {
  const panel = document.querySelector('[data-auth-panel]');
  const form = panel?.querySelector('[data-auth-form]');
  const status = panel?.querySelector('[data-auth-status]');
  const protectedContent = document.querySelector('[data-auth-protected]');
  let required = document.body.dataset.adminRequired === 'true';
  let expires = Number(document.body.dataset.adminExpires) || 0;
  let deadline = performance.now() + Math.max(0, expires - Number(document.body.dataset.serverTime)) * 1000;
  let hadGrant = expires > 0;
  let waiting = [];
  let pending;

  function valid() { return !required || performance.now() < deadline; }
  function render() {
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
    if (expires) hadGrant = true;
    if (render()) {
      waiting.splice(0).forEach(resolve => resolve());
      if (panel?.dataset.reload === 'true') location.reload();
    }
  }

  async function refresh() {
    if (pending) return pending;
    pending = (async () => {
      try {
        const response = await fetch('/access/status', {headers:{Accept:'application/json'}, cache:'no-store'});
        if (response.ok) apply(await response.json());
        else if (response.status === 401) {deadline = 0; render();}
      } catch (_) { render(); } // The server still validates every protected write.
    })();
    try { await pending; } finally { pending = null; }
  }

  async function ensure() {
    await refresh();
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
      apply(result);
    } catch (failure) { error.textContent = failure.message; error.hidden = false; }
    finally { button.disabled = false; }
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden && panel) {render(); refresh();} });
  window.addEventListener('pageshow', () => { if (panel) {render(); refresh();} });
  // Absolute five-minute expiry, even while editing; use a monotonic clock.
  if (panel) { render(); setInterval(render, 1000); setInterval(refresh, 15000); }
  window.litradarAuth = {ensure, fetch:authorizedFetch, refresh, valid};
})();
