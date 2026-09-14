/* Progressive settings forms retain their inputs after validation and auth errors. */
(() => {
  async function action(url, body, password) {
    const headers = password ? {'X-Admin-Password':password} : {};
    const response = await fetch(url, {method:'POST', body, headers});
    if (response.status === 401 && response.headers.get('X-Admin-Password-Required')) {
      const value = prompt('请输入管理员密码');
      if (value) return action(url, body, value);
      throw new Error('已取消。');
    }
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || '操作失败，请稍后再试。');
    return result;
  }
  document.getElementById('add-query')?.addEventListener('click', () => {
    document.getElementById('query-rows').append(document.getElementById('query-template').content.cloneNode(true));
  });
  document.addEventListener('click', event => {
    if (event.target.matches('[data-remove-query]')) event.target.closest('.query-row').remove();
  });
  document.querySelectorAll('[data-add-pair]').forEach(button => button.addEventListener('click', () => {
    const rows = document.getElementById(button.dataset.addPair);
    const clone = rows.lastElementChild.cloneNode(true);
    clone.querySelectorAll('input').forEach(input => {input.value='';});
    rows.append(clone);
  }));
  document.querySelectorAll('[data-check-service]').forEach(button => button.addEventListener('click', async () => {
    const output = document.getElementById('check-' + button.dataset.checkService);
    button.disabled = true; output.textContent = '正在测试…';
    try {output.textContent = (await action('/settings/check/' + button.dataset.checkService)).message;}
    catch (error) {output.textContent = error.message;}
    finally {button.disabled = false;}
  }));
  document.querySelector('[data-preview]')?.addEventListener('click', async event => {
    const button = event.currentTarget;
    const output = document.getElementById('query-preview');
    button.disabled = true; output.textContent = '正在预览…';
    try {
      const result = await action('/settings/preview' + (button.dataset.preview ? '?slug=' + encodeURIComponent(button.dataset.preview) : ''), new FormData(button.form));
      output.textContent = result.message;
      const list = document.createElement('ul');
      result.items.forEach(item => {
        const li = document.createElement('li');
        if (item.url && /^https?:\/\//i.test(item.url)) {
          const link = document.createElement('a'); link.href=item.url; link.textContent=item.title;
          link.target='_blank'; link.rel='noopener noreferrer'; li.append(link);
        } else li.textContent=item.title;
        list.append(li);
      }); output.append(list);
    } catch (error) {output.textContent = error.message;}
    finally {button.disabled = false;}
  });
  document.getElementById('preview-journal')?.addEventListener('click', async event => {
    const body = new FormData(event.currentTarget.form);
    body.set('sample',document.getElementById('journal-sample').value);
    const output = document.getElementById('journal-preview');
    try {output.textContent=(await action('/settings/journal-preview',body)).message;}
    catch (error) {output.textContent=error.message;}
  });
  document.getElementById('suggest-terms')?.addEventListener('click', async event => {
    const button = event.currentTarget;
    const output = document.getElementById('term-suggestions');
    button.disabled=true; output.textContent='正在生成建议…';
    const body=new FormData();body.set('direction',document.getElementById('direction').value);
    try {
      const result=await action('/settings/suggest-terms',body);
      output.textContent=result.message;
      const list=document.createElement('p');list.textContent=result.keywords.join('、');output.append(list);
      const adopt=document.createElement('button');adopt.type='button';adopt.className='btn';adopt.textContent='采用这些关键词（仍需保存）';
      adopt.addEventListener('click',()=>{
        const field=document.getElementById('keywords.core');
        field.value=[...new Set([...field.value.split('\n'),...result.keywords].filter(Boolean))].join('\n');
        adopt.disabled=true;
      });output.append(adopt);
    } catch(error){output.textContent=error.message;}
    finally {button.disabled=false;}
  });
  async function submit(form, submitter, password) {
    const body = new FormData(form);
    if (submitter?.name) body.set(submitter.name, submitter.value);
    const headers = {};
    if (password) headers['X-Admin-Password'] = password;
    // A button named "action" shadows HTMLFormElement.action.
    const response = await fetch(form.getAttribute('action') || location.href, {method: 'POST', body, headers});
    if (response.status === 401 && response.headers.get('X-Admin-Password-Required')) {
      const value = prompt('输入管理员密码以保存设置');
      if (value) return submit(form, submitter, value);
      return;
    }
    if (response.redirected) { location.assign(response.url); return; }
    const type = response.headers.get('content-type') || '';
    if (type.includes('text/html')) {
      const html = await response.text();
      document.open(); document.write(html); document.close();
    } else {
      const result = await response.json();
      alert(result.detail || result.message || '操作已完成');
    }
  }
  document.querySelectorAll('[data-settings-form]').forEach(form => {
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (event.submitter?.dataset.confirm && !confirm(event.submitter.dataset.confirm)) return;
      if (form.dataset.busy) return;
      form.dataset.busy = '1';
      try { await submit(form, event.submitter); }
      catch (_) { alert('未能确认保存结果。您的输入仍保留，请在新标签页查看设置后再试。'); }
      finally { delete form.dataset.busy; }
    });
  });
  document.querySelectorAll('[data-run-group]').forEach(button => {
    button.addEventListener('click', async () => {
      if (!confirm('更新将执行检索、邮件采集、补全、排序和摘要；AI 调用可能产生费用。现在开始？')) return;
      const output = document.getElementById('run-out');
      output.hidden = false;
      button.disabled = true;
      output.textContent = '正在更新，请稍候…';
      const url = '/admin/run/all?g=' + encodeURIComponent(button.dataset.runGroup);
      try {
        let response = await fetch(url, {method:'POST'});
        if (response.status === 401 && response.headers.get('X-Admin-Password-Required')) {
          const password = prompt('输入管理员密码以运行更新');
          if (!password) { output.textContent = '已取消。'; return; }
          response = await fetch(url, {method:'POST', headers:{'X-Admin-Password':password}});
        }
        const result = await response.json();
        output.textContent = response.ok
          ? '更新已结束。请在文献列表查看结果，并在统计页检查各阶段运行记录。'
          : result.detail || '更新未完成，请在统计页检查运行记录。';
      } catch (_) { output.textContent = '连接中断，请在统计页查看运行记录后再试。'; }
      finally { button.disabled = false; }
    });
  });
})();
