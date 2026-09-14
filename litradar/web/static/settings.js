/* Progressive settings forms retain their inputs after validation and auth errors. */
(() => {
  const modelForm = document.getElementById('model-settings');
  const savedModel = modelForm ? JSON.stringify([...new FormData(modelForm)]) : null;
  const modelAddress = document.getElementById('llm.base_url');
  const modelName = document.getElementById('llm.model');
  const modelChoices = document.getElementById('available-models');
  const modelListField = document.getElementById('model-list-field');
  const modelListOutput = document.getElementById('model-list-result');
  let modelListRevision = 0;
  function clearModels() {
    modelListRevision += 1;
    modelChoices.replaceChildren(new Option('请选择模型', ''));
    modelChoices.disabled = true;
    modelListField.hidden = true;
    modelListOutput.textContent = '';
  }
  modelAddress?.addEventListener('input', clearModels);
  document.getElementById('credential-llm')?.addEventListener('input', clearModels);
  modelChoices?.addEventListener('change', () => {
    if (modelChoices.value) modelName.value = modelChoices.value;
  });
  document.getElementById('fetch-models')?.addEventListener('click', async event => {
    if (!modelAddress.reportValidity()) return;
    if (document.getElementById('credential-llm')?.value.trim()) {
      modelListOutput.textContent = '请先保存新密钥，再获取模型列表。';
      return;
    }
    const button = event.currentTarget;
    clearModels();
    const revision = modelListRevision;
    button.disabled = true;
    modelListOutput.textContent = '正在获取模型…';
    const body = new FormData(); body.set('base_url', modelAddress.value);
    try {
      const result = await action('/settings/models', body);
      if (revision !== modelListRevision) return;
      result.models.forEach(name => modelChoices.append(new Option(name, name)));
      modelChoices.value = result.models.includes(modelName.value) ? modelName.value : '';
      modelChoices.disabled = false;
      modelListField.hidden = false;
      modelListOutput.textContent = result.message;
    } catch (error) {
      if (revision === modelListRevision) modelListOutput.textContent = error.message;
    } finally { button.disabled = false; }
  });
  document.getElementById('model-preset-deepseek')?.addEventListener('click', () => {
    clearModels();
    document.getElementById('llm.base_url').value = 'https://api.deepseek.com';
    document.getElementById('llm.model').value = 'deepseek-chat';
    document.getElementById('llm.json_mode').value = 'auto';
    document.getElementById('llm.token_limit_parameter').value = 'auto';
    document.getElementById('llm.temperature').value = '0.2';
  });
  // Reveal the relevant fields without clearing values in collapsed sections.
  const mailMode = document.getElementById('mail.mode');
  mailMode?.addEventListener('change', () => {
    document.getElementById('mail-connection').open = mailMode.value === 'imap';
  });
  document.addEventListener('invalid', event => {
    let section = event.target.closest('details');
    while (section) {
      section.open = true;
      section = section.parentElement.closest('details');
    }
  }, true);
  document.addEventListener('click', event => {
    document.querySelectorAll('.direction-menu[open]').forEach(menu => {
      if (!menu.contains(event.target)) menu.open = false;
    });
  });
  async function action(url, body) {
    const response = await window.litradarAuth.fetch(url, {method:'POST', body});
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || '操作失败，请稍后再试。');
    return result;
  }
  document.getElementById('add-query')?.addEventListener('click', () => {
    document.getElementById('query-rows').append(document.getElementById('query-template').content.cloneNode(true));
  });
  document.addEventListener('click', event => {
    const remove = event.target.closest('[data-remove-query]');
    if (remove) remove.closest('.query-row').remove();
  });
  document.querySelectorAll('[data-add-pair]').forEach(button => button.addEventListener('click', () => {
    const rows = document.getElementById(button.dataset.addPair);
    const clone = rows.lastElementChild.cloneNode(true);
    clone.querySelectorAll('input').forEach(input => {input.value='';});
    rows.append(clone);
  }));
  document.querySelectorAll('[data-check-service]').forEach(button => button.addEventListener('click', async () => {
    const output = document.getElementById('check-' + button.dataset.checkService);
    if (button.dataset.checkService === 'llm' && modelForm &&
        (modelForm.dataset.unsaved === 'true' || JSON.stringify([...new FormData(modelForm)]) !== savedModel ||
         document.getElementById('credential-llm')?.value.trim())) {
      output.textContent = '连接设置或密钥尚未保存，请先保存后再测试。';
      return;
    }
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
  async function submit(form, submitter) {
    const body = new FormData(form);
    if (submitter?.name) body.set(submitter.name, submitter.value);
    // A button named "action" shadows HTMLFormElement.action.
    const response = await window.litradarAuth.fetch(form.getAttribute('action') || location.href, {method: 'POST', body});
    if (response.redirected) {
      const destination = new URL(response.url);
      if (form.id === 'model-settings' || form.id === 'model-credential') destination.hash = 'model-connection';
      const current = new URL(location.href);
      if (destination.origin === current.origin && destination.pathname === current.pathname &&
          destination.search === current.search) {
        // A fragment-only navigation would retain the old revision and dirty-form baseline.
        location.hash = destination.hash;
        location.reload();
      } else location.assign(destination.href);
      return;
    }
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
})();
