"""Real file and HTTP regressions for the structured settings workflow."""
import copy

import pytest
import yaml
from fastapi.testclient import TestClient

from litradar.config import load_config
from litradar.settings import SettingsError, SettingsStore, group_patch
from litradar.web import app as web


@pytest.fixture
def settings_env(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    interests = tmp_path / "interests.yaml"
    path.write_text(yaml.safe_dump({"app": {"interests": str(interests),
        "db_path": str(tmp_path / "test.db")}, "llm": {"enabled": False},
        "extension": {"keep": 12}}), encoding="utf-8")
    monkeypatch.setattr(web, "CONFIG_PATH", str(path))
    from litradar.credentials import write
    from litradar.passwords import hash_password
    from litradar.web.access import SESSION_COOKIE, session_value
    cfg = load_config(path)
    write(cfg, cfg.app.admin_password_env, hash_password("test-password", iterations=1000))
    client = TestClient(web.app)
    client.cookies.set(SESSION_COOKIE, session_value(load_config(path)))
    return path, interests, client


def test_empty_instance_can_add_and_rename_without_changing_identity(settings_env):
    path, interests, client = settings_env
    assert "新增研究方向" in client.get("/settings/groups").text
    store = SettingsStore(load_config(path))
    slug = store.update_group(None, group_patch({"name": "材料化学", "enabled": "on"}), store.version())
    assert slug == "default"
    store.update_group(slug, {"name": "光催化"}, store.version())
    entries = store.group_entries(store.snapshot().interests)
    assert [(g["slug"], g["name"]) for g in entries] == [("default", "光催化")]
    assert "光催化" in client.get("/settings/groups").text


def test_migrate_legacy_copy_disable_reorder_preserves_unedited_data(settings_env):
    path, interests, _ = settings_env
    original = {"name": "旧方向", "direction": "organic", "keywords": {"core": ["old"],
        "future": ["retain"]}, "s2_queries": ['(a | b) + -c'], "extension": {"private": 1}}
    interests.write_text(yaml.safe_dump(original), encoding="utf-8")
    store = SettingsStore(load_config(path))
    store.update_group("default", {"name": "新名字", "keywords.core": ["new"]}, store.version())
    copied = store.update_group("default", {}, store.version(), action="copy")
    store.update_group("default", {}, store.version(), action="disable")
    store.update_group(copied, {}, store.version(), action="up")
    entries = store.snapshot().interests["groups"]
    assert [g["slug"] for g in entries] == [copied, "default"]
    assert entries[1]["enabled"] is False
    assert entries[0]["extension"] == entries[1]["extension"] == original["extension"]
    assert entries[1]["keywords"]["future"] == ["retain"]
    assert entries[1]["s2_queries"] == original["s2_queries"]
    assert len(list(interests.parent.glob("interests.yaml.*.bak"))) == 4


def test_stale_form_cannot_overwrite_other_page_or_cli(settings_env):
    path, interests, _ = settings_env
    store = SettingsStore(load_config(path))
    before = store.version()
    store.update_group(None, {"name": "A"}, before)
    with pytest.raises(SettingsError, match="另一页面"):
        store.update_group("default", {"name": "stale"}, before)
    before = store.version()
    path.write_text(path.read_text() + "cli_extension: 42\n")
    with pytest.raises(SettingsError):
        store.update_group("default", {"name": "stale"}, before)
    assert store.snapshot().interests["groups"][0]["name"] == "A"


def test_failed_write_retains_file_and_only_five_backups(settings_env, monkeypatch):
    from litradar import settings
    path, interests, _ = settings_env
    store = SettingsStore(load_config(path))
    store.update_group(None, {"name": "first"}, store.version())
    for i in range(7):
        store.update_group("default", {"name": str(i)}, store.version())
    assert len(list(interests.parent.glob("interests.yaml.*.bak"))) == 5
    before = interests.read_bytes()
    def fail(*args):
        raise OSError("simulated disk failure")
    monkeypatch.setattr(settings.os, "replace", fail)
    with pytest.raises(SettingsError, match='磁盘空间'):
        store.update_group("default", {"name": "lost"}, store.version())
    assert interests.read_bytes() == before
    assert not list(interests.parent.glob(".interests.yaml.*"))


def test_form_validation_preserves_input_and_rejects_cross_origin(settings_env):
    path, interests, client = settings_env
    version = SettingsStore(load_config(path)).version()
    payload = {"name": "", "direction": "保留这段输入", "version": version}
    response = client.post("/settings/groups/new", data=payload)
    assert response.status_code == 400
    assert "保留这段输入" in response.text and "请填写研究方向名称" in response.text
    assert not interests.exists()
    response = client.post("/settings/groups/new", data={**payload, "name": "A"},
                           headers={"Origin": "https://untrusted.example"})
    assert response.status_code == 403
    assert not interests.exists()


def test_partial_config_save_preserves_extensions(settings_env):
    path, _, _ = settings_env
    store = SettingsStore(load_config(path))
    original = copy.deepcopy(store.snapshot().config)
    store.update_config({"llm.deep_summary_top_n": 3}, store.version())
    data = store.snapshot().config
    assert data["extension"] == original["extension"]
    assert data["app"] == original["app"]
    assert data["llm"]["enabled"] is False
    assert data["llm"]["deep_summary_top_n"] == 3


@pytest.mark.parametrize('route', ['/settings/groups/new','/settings/services','/settings/reading',
                                  '/settings/schedule','/settings/maintenance'])
def test_every_settings_page_renders_without_secrets(settings_env, route):
    path, _, client = settings_env
    from litradar.credentials import write
    cfg = load_config(path)
    write(cfg, cfg.llm.api_key_env, 'a-private-llm-key')
    response = client.get(route)
    assert response.status_code == 200, response.text
    assert 'a-private-llm-key' not in response.text
    assert 'pbkdf2_sha256' not in response.text
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['referrer-policy'] == 'same-origin'


def test_credentials_rotate_clear_mask_legacy_and_preserve_running_snapshot(settings_env, monkeypatch):
    from litradar import credentials, config
    path, _, _ = settings_env
    cfg = load_config(path)
    monkeypatch.setattr(config, '_file_values', {'DEEPSEEK_API_KEY':'old-file-value'})
    credentials.write(cfg,cfg.llm.api_key_env,'first-key')
    running = load_config(path)
    with credentials.snapshot(running):
        assert config.read_secret(cfg.llm.api_key_env) == 'first-key'
        credentials.write(cfg,cfg.llm.api_key_env,'second-key')
        assert config.read_secret(cfg.llm.api_key_env) == 'first-key'
        assert running.llm.api_key == 'first-key'
    assert load_config(path).llm.api_key == 'second-key'
    credentials.write(cfg,cfg.llm.api_key_env,None)
    assert load_config(path).llm.api_key is None  # old .env key stays masked
    monkeypatch.setenv(cfg.llm.api_key_env,'deployment-key')
    assert load_config(path).llm.api_key == 'deployment-key'
    with pytest.raises(SettingsError,match='部署环境'):
        credentials.write(cfg,cfg.llm.api_key_env,'cannot-win')


def test_blank_secret_keeps_key_and_clear_is_explicit(settings_env):
    from litradar import credentials
    from litradar.settings import revision
    path, _, client = settings_env
    cfg = load_config(path)
    credentials.write(cfg,cfg.llm.api_key_env,'keep-key')
    version = revision(credentials.store_path(cfg))
    response = client.post('/settings/credentials/llm',data={'secret':'','action':'save','credential_version':version})
    assert response.status_code == 200
    assert load_config(path).llm.api_key == 'keep-key'
    client.post('/settings/credentials/llm',data={'action':'clear','credential_version':version})
    assert load_config(path).llm.api_key is None


def test_setup_requires_local_link_expires_and_cannot_be_reused(settings_env):
    from litradar import credentials
    from litradar.web.access import issue_setup_link, SETUP_COOKIE
    from urllib.parse import urlsplit
    path, _, client = settings_env
    cfg = load_config(path)
    credentials.write(cfg,cfg.app.admin_password_env,None)
    client.cookies.clear()
    cfg = load_config(path)
    response = client.post('/setup',data={'password':'owner-password','confirm':'owner-password'})
    assert response.status_code == 403
    link = issue_setup_link(cfg,'http://testserver')
    client.get(urlsplit(link).path + '?' + urlsplit(link).query)
    code = client.cookies.get(SETUP_COOKIE)
    response = client.post('/setup',data={'password':'owner-password','confirm':'owner-password'})
    assert response.status_code == 200 and '访问密码已设置' in response.text
    assert load_config(path).app.admin_password_hash
    attacker = TestClient(web.app)
    attacker.cookies.set(SETUP_COOKIE,code,path='/setup')
    response = attacker.post('/setup',data={'password':'attacker-password','confirm':'attacker-password'})
    assert response.status_code == 409
    from litradar.passwords import verify_password
    assert verify_password('owner-password',load_config(path).app.admin_password_hash)


def test_expired_link_is_rejected_and_password_rotation_invalidates_sessions(settings_env):
    from litradar import credentials
    from litradar.web.access import SESSION_COOKIE
    path, _, client = settings_env
    cfg = load_config(path)
    other = TestClient(web.app)
    other.cookies.set(SESSION_COOKIE,client.cookies.get(SESSION_COOKIE))
    from litradar.settings import revision
    response = client.post('/settings/password', data={'current':'test-password','password':'new-password',
        'confirm':'new-password','credential_version':revision(credentials.store_path(cfg))})
    assert response.status_code == 200
    assert other.get('/settings/groups',follow_redirects=False).headers['location'] == '/login'
    assert client.get('/settings/groups').status_code == 200
    client.post('/logout')
    assert client.post('/login',data={'password':'test-password'}).status_code == 401
    assert client.post('/login',data={'password':'new-password'}).status_code == 200


def test_unauthenticated_and_cross_origin_checks_do_not_call_network(settings_env, monkeypatch):
    from litradar import connection_checks
    _, _, client = settings_env
    calls = []
    monkeypatch.setattr(connection_checks, 'check',lambda *args: calls.append(args))
    assert client.post('/settings/check/llm',headers={'Origin':'https://evil.test'}).status_code == 403
    assert TestClient(web.app).post('/settings/check/llm').status_code == 401
    assert not calls


def test_mail_check_is_read_only_and_errors_redact_credentials(settings_env, monkeypatch):
    from litradar import connection_checks, credentials
    from litradar.sources import mail
    path, _, _ = settings_env
    store = SettingsStore(load_config(path))
    store.update_config({'mail.mode':'imap','mail.imap_host':'imap.test','mail.imap_user':'researcher'},store.version())
    cfg = load_config(path)
    credentials.write(cfg,cfg.mail.imap_password_env,'do-not-return-this')
    cfg = load_config(path)
    calls = []
    class FakeMailbox:
        def login(self,*args): return ('OK',[])
        def select(self,folder,readonly):
            calls.append(('select',readonly)); return ('OK',[])
        def uid(self,*args):
            calls.append(args); return ('OK',[b'1 2'])
        def logout(self): calls.append(('logout',))
    monkeypatch.setattr(mail,'open_imap',lambda cfg:FakeMailbox())
    assert '2 封' in connection_checks.check(cfg,'mail')
    assert ('select',True) in calls
    assert not any(call[0] in ('STORE','FETCH','CLOSE') for call in calls)
    def fail(cfg): raise RuntimeError('do-not-return-this')
    monkeypatch.setattr(mail,'open_imap',fail)
    with pytest.raises(SettingsError) as error:
        connection_checks.check(cfg,'mail')
    assert 'do-not-return-this' not in str(error.value)


def test_queries_compile_phrases_and_preserve_opaque_semantics():
    from litradar import query_builder
    from starlette.datastructures import FormData
    row = {'source':'s2_queries','all':['Sample detection'],'any':['optical','sensor'], 'exclude':['review']}
    query = query_builder.compile_rule(row)[0]
    assert query == '"Sample detection" + ("optical" | "sensor") + -"review"'
    assert query_builder.parse_simple(query) == {k:row[k] for k in ('all','any','exclude')}
    entry = {'s2_queries':['((a | b) + (c | d)) ~3','"simple" + "query"'],
             'search_query':'legacy full phrase'}
    form = FormData([('queries_present','1'),('query_source','s2_queries'),
        ('query_all','simple\nquery'),('query_any',''),('query_exclude',''),('query_original','s2_queries:1')])
    patch = query_builder.parse_form(form,entry)
    assert patch['s2_queries'] == entry['s2_queries']
    assert patch['search_queries'] == ['legacy full phrase']
    assert patch['search_query'] == ''


def test_restoring_config_preserves_paths_and_auth_and_pauses_schedule(settings_env):
    path, interests, client = settings_env
    store = SettingsStore(load_config(path))
    store.update_config({'llm.deep_summary_top_n':12},store.version())
    backup = store.backups('config')[0]
    data = yaml.safe_load(backup.read_text())
    data['app'].update(db_path='/wrong/database',admin_password_env='EMPTY_ENV',interests='/wrong/preferences')
    data['schedule']={'enabled':True}
    backup.write_text(yaml.safe_dump(data))
    store.restore('config',backup.name,store.version())
    cfg = load_config(path)
    assert cfg.interests_file == interests
    assert cfg.db_file != '/wrong/database'
    assert cfg.app.admin_password_hash
    assert cfg.schedule.enabled is False
    assert client.get('/settings/groups').status_code == 200


def test_previews_use_unsaved_terms_and_do_not_write_settings(settings_env,monkeypatch):
    from litradar import connection_checks
    path, interests, client = settings_env
    got=[]
    monkeypatch.setattr(connection_checks,'preview',lambda cfg,patch: got.append(patch) or [])
    before = path.read_bytes()
    form = {'version':SettingsStore(load_config(path)).version(),'queries_present':'1','query_source':'s2_queries',
            'query_all':'catalyst','query_any':'','query_exclude':''}
    result=client.post('/settings/preview/new',data=form)
    assert result.status_code == 200
    assert got[0]['s2_queries'] == ['"catalyst"']
    assert not interests.exists() and path.read_bytes() == before


def test_new_general_queries_reopen_as_editable_conditions():
    from litradar import query_builder
    from starlette.datastructures import FormData
    form=FormData([('queries_present','1'),('query_source','search_queries'),
                   ('query_all','organic\nphotocatalysis'),('query_any',''),('query_exclude','')])
    saved=query_builder.parse_form(form,{})
    rows,opaque=query_builder.rows_for(saved)
    assert not opaque
    assert rows[0]['all']==['organic','photocatalysis']


def test_action_button_does_not_shadow_destination_and_auth_retry_keeps_action():
    import json
    import shutil
    import subprocess
    from pathlib import Path
    node=shutil.which('node')
    if not node:
        pytest.skip('Node.js required for frontend regression')
    result=subprocess.run([node,str(Path(__file__).parent/'helpers/settings_context.cjs')],
                          capture_output=True,text=True,check=True)
    data=json.loads(result.stdout)
    assert [r['url'] for r in data['requests']]==['/settings/group-action?slug=default']*2
    assert all(r['body']=={'version':'v1','action':'copy'} for r in data['requests'])
    assert data['requests'][1]['headers']['X-Admin-Password']=='test-password'
    assert data['destination'].endswith('?saved=1') and data['busy']==''


def test_access_token_changes_never_render_credential_and_preserve_login(settings_env):
    from litradar import credentials
    from litradar.settings import revision
    path, _, client = settings_env
    cfg = load_config(path)
    response = client.post('/settings/credentials/access', data={
        'secret': 'private-script-credential', 'action': 'save',
        'credential_version': revision(credentials.store_path(cfg)),
    })
    assert response.status_code == 200 and '接口口令已设置' in response.text
    assert 'private-script-credential' not in response.text
    assert load_config(path).app.token == 'private-script-credential'
    response = client.post('/settings/credentials/access', data={
        'action': 'clear', 'credential_version': revision(credentials.store_path(cfg)),
    })
    assert response.status_code == 200 and '接口口令未设置' in response.text
    assert load_config(path).app.token is None
    assert client.get('/settings/groups').status_code == 200


def test_duplicate_provider_journal_names_have_one_editor_and_keep_order(settings_env):
    from litradar.web.settings import journal_context, journal_patch
    from starlette.datastructures import FormData
    path, _, _ = settings_env
    cfg = load_config(path)
    cfg.journal_rank.fields = ['sciWarning', 'sci', 'ei']
    context = journal_context(cfg)
    assert list(context['journal_fields'].values()).count('SCIWARN') == 1
    assert list(context['journal_fields'].values()).count('EI检索') == 1
    patch = journal_patch(FormData([
        ('journal_fields_present', '1'), ('journal_field', 'sciwarn'),
        ('journal_field', 'sci'), ('journal_field', 'eii'),
        ('journal_label_sciwarn', '预警'),
    ]), cfg)
    assert patch['journal_rank.fields'] == ['sciWarning', 'sci', 'ei']
    assert patch['journal_rank.map']['SCIWARN'] == '预警'


def test_invalid_restore_is_rejected_without_changing_current_settings(settings_env):
    path, _, _ = settings_env
    store = SettingsStore(load_config(path))
    store.update_config({'llm.deep_summary_top_n': 3}, store.version())
    backup = store.backups('config')[0]
    data = yaml.safe_load(backup.read_text())
    data['app']['timezone'] = 'Invalid/Zone'
    backup.write_text(yaml.safe_dump(data))
    current = path.read_bytes()
    with pytest.raises(SettingsError, match='时区'):
        store.restore('config', backup.name, store.version())
    assert path.read_bytes() == current


def test_owner_session_authorizes_guarded_run_without_second_password(settings_env, monkeypatch):
    path, _, client = settings_env
    store = SettingsStore(load_config(path))
    store.update_group(None, {'name': 'Research'}, store.version())
    calls = []
    monkeypatch.setattr(web.pipeline, 'run_all', lambda *a, **kw: calls.append(kw) or {'done': True})
    response = client.post('/admin/run/all?g=default')
    assert response.status_code == 200
    assert len(calls) == 1 and calls[0]['group'].slug == 'default'


def test_access_logs_do_not_include_setup_code_or_legacy_token():
    import logging
    from litradar.web.access import PrivateAccessLog
    record = logging.LogRecord('uvicorn.access', logging.INFO, '', 0,
        '%s - "%s %s HTTP/%s" %d',
        ('client', 'GET', '/setup?code=private-setup-code&k=private-api-token', '1.1', 303), None)
    assert PrivateAccessLog().filter(record)
    assert '/setup' in record.getMessage()
    assert 'private-' not in record.getMessage()
