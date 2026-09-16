/* Poll persisted progress; a page refresh never resubmits a paid run. */
(() => {
  const panel = document.querySelector('[data-pipeline]');
  if (!panel) return;
  const buttons = [...document.querySelectorAll('[data-run-stage], [data-run-group]')];
  const find = name => panel.querySelector(`[data-run-${name}]`);
  const guarded = (document.body.dataset.guardedStages || '').split(',');
  const labels = {running:'运行中', completed:'运行完成', partial:'部分完成', failed:'运行失败',
    interrupted:'运行中断', waiting:'等待', skipped:'已跳过'};
  let job = null, starting = false, uncertain = false, polling, timer;
  let actionError = '', connectionError = '';

  function controls() {
    buttons.forEach(button => { button.disabled = starting || uncertain || job?.status === 'running'; });
  }
  function elapsed() {
    if (!job) return;
    const seconds = Math.max(0, Math.floor((job.finished_at || Date.now() / 1000) - job.started_at));
    find('elapsed').textContent = `已用时 ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
  }
  function render(state) {
    job = state;
    controls();
    if (!job) return;
    panel.dataset.status = job.status;
    find('title').textContent = `${labels[job.status] || job.status} · ${job.group_name}`;
    find('message').textContent = job.message;
    const running = job.status === 'running';
    const countKnown = Number.isFinite(job.total) && job.total > 0 && Number.isFinite(job.current);
    find('meter').hidden = !running;
    if (countKnown) {
      find('bar').max = job.total;
      find('bar').value = job.current;
      find('count').textContent = `${job.current} / ${job.total}`;
    } else {
      find('bar').removeAttribute('value');
      find('count').textContent = Number.isFinite(job.current) ? `已处理 ${job.current} 项` : '正在处理';
    }
    const steps = job.steps.map(step => {
      const row = document.createElement('li');
      row.dataset.status = step.status;
      const marker = document.createElement('span');
      marker.className = 'pipeline-progress__marker';
      marker.textContent = ({completed:'✓', partial:'!', failed:'!', interrupted:'!', skipped:'−', running:'●'})[step.status] || '·';
      marker.setAttribute('aria-hidden', 'true');
      const text = document.createElement('div');
      const label = document.createElement('strong');
      label.textContent = `${step.label} · ${labels[step.status] || step.status}`;
      const detail = document.createElement('span');
      detail.textContent = step.message;
      text.append(label, detail);
      row.append(marker, text);
      return row;
    });
    find('steps').replaceChildren(...steps);
    find('steps').hidden = false;
    elapsed();
  }
  function error(message, connection = false) {
    if (connection) connectionError = message;
    else actionError = message;
    find('error').textContent = connectionError || actionError;
    find('error').hidden = !(connectionError || actionError);
  }
  async function poll() {
    if (polling) return polling;
    polling = (async () => {
      try {
        const response = await fetch('/admin/jobs/current', {cache:'no-store', headers:{Accept:'application/json'}});
        if (!response.ok) throw Error(response.status === 401 ? '阅读登录已过期，请重新登录后查看进度。' : '暂时无法读取进度，正在重连。');
        const state = await response.json();
        uncertain = false;
        render(state);
        error('', true);
      } catch (failure) {
        uncertain = true;
        controls();
        error(failure.message || '连接中断，正在重连。任务不会因此停止。', true);
      }
    })();
    try { await polling; } finally { polling = null; }
  }
  async function schedule() {
    clearTimeout(timer);
    if (!starting) await poll();
    timer = setTimeout(schedule, job?.status === 'running' || uncertain ? 1200 : 5000);
  }
  async function start(stage, button, group) {
    if (starting || uncertain || job?.status === 'running') return;
    starting = true; controls(); error('');
    let runId;
    try {
      if (polling) await polling;
      if (job?.status === 'running' || uncertain) return;
      const url = new URL('/admin/run/' + stage, location.href);
      url.searchParams.set('background', '1');
      if (button?.dataset?.runForce === 'true') url.searchParams.set('force', '1');
      if (group) url.searchParams.set('g', group);
      // getRandomValues also works on local-network HTTP installations.
      runId = [...crypto.getRandomValues(new Uint8Array(16))].map(n => n.toString(16).padStart(2, '0')).join('');
      const options = {method:'POST', headers:{'X-Run-ID':runId, Accept:'application/json'}};
      const send = guarded.includes(stage) ? window.litradarAuth.fetch : fetch;
      const response = await send(url.pathname + url.search, options);
      const state = await response.json();
      if (!response.ok) {
        await poll();
        error(state.detail || '未能启动任务，请稍后重试。');
        return;
      }
      render(state);
      panel.scrollIntoView({behavior:'smooth', block:'nearest'});
    } catch (_) {
      // Never automatically retry a POST whose execution is uncertain.
      uncertain = true;
      await poll();
      if (uncertain) error('暂时无法确认启动结果，正在查询进度。请勿重复启动。', true);
      else if (job?.id !== runId) error('未能确认本次启动结果，请核对最近一次运行后再试。');
    } finally {
      starting = false; controls();
      clearTimeout(timer);
      timer = setTimeout(schedule, 1200);
    }
  }
  buttons.forEach(button => button.addEventListener('click', () =>
    start(button.dataset.runStage || 'all', button, button.dataset.runGroup || document.body.dataset.group)));
  document.addEventListener('visibilitychange', () => {if (!document.hidden) schedule();});
  window.litradarPipeline = {start};
  setInterval(elapsed, 1000);
  schedule();
})();
