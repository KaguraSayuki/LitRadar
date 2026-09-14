"""Background execution remains observable, authorized, and mutually exclusive."""
import json
import threading
import time
import uuid

import pytest
import yaml
from fastapi.testclient import TestClient

from litradar import credentials, progress
from litradar.config import load_config
from litradar.lock import single_instance
from litradar.passwords import hash_password
from litradar.settings import SettingsStore
from litradar.web import access, app as web
from litradar.web.jobs import JobStore


@pytest.fixture
def owner(tmp_path, monkeypatch):
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump({'app': {'db_path': str(tmp_path / 'test.db'),
        'interests': str(tmp_path / 'interests.yaml')}, 'llm': {'enabled': False},
        'admin': {'cooldown_seconds': 0, 'daily_limit': 0}}))
    cfg = load_config(path)
    credentials.write(cfg, cfg.app.admin_password_env, hash_password('test-password', iterations=1000))
    store = SettingsStore(load_config(path))
    store.update_group(None, {'name': 'Research A'}, store.version())
    monkeypatch.setattr(web, 'CONFIG_PATH', str(path))
    client = TestClient(web.app)
    assert client.post('/login', data={'password': 'test-password'}, follow_redirects=False).status_code == 303
    return client, load_config(path)


def post_run(client, stage='rank', *, run_id=None, **kwargs):
    return client.post(f'/admin/run/{stage}?background=1&g=default',
                       headers={'X-Run-ID': run_id or uuid.uuid4().hex}, **kwargs)


def test_one_grant_covers_settings_and_paid_runs_without_sliding_expiry(owner, monkeypatch):
    client, cfg = owner
    now = int(time.time())
    monkeypatch.setattr(access.time, 'time', lambda: now)
    client.cookies.delete(access.ADMIN_COOKIE)
    locked = client.get('/settings/services')
    assert 'data-auth-form' in locked.text and 'id="model-settings"' not in locked.text
    assert client.post('/settings/services', data={}).status_code == 401
    assert client.post('/admin/run/rank').status_code == 401
    # A reading session cannot be substituted for a short operation grant.
    client.cookies.set(access.ADMIN_COOKIE, client.cookies.get(access.SESSION_COOKIE))
    assert client.get('/access/status').json()['expires_at'] == 0
    client.cookies.delete(access.ADMIN_COOKIE)
    response = client.post('/access/unlock', data={'password': 'test-password'}, headers={'Accept': 'application/json'})
    assert response.json()['expires_at'] == now + 300
    cookie = response.headers.get_list('set-cookie')[-1]
    assert 'HttpOnly' in cookie and 'SameSite=strict' in cookie and 'Max-Age=300' in cookie
    grant = client.cookies.get(access.ADMIN_COOKIE)
    expires = response.json()['expires_at']
    monkeypatch.setattr(web.rank, 'run', lambda *a, **kw: {'scored': 2})
    now += 299
    for _ in range(2):
        store = SettingsStore(load_config(cfg.config_file))
        saved = client.post('/settings/group-action?slug=default',
            data={'version': store.version(), 'action': 'copy'}, follow_redirects=False)
        assert saved.status_code == 303
    assert client.post('/admin/run/rank?g=default').status_code == 200
    assert client.get('/access/status').json()['expires_at'] == expires
    assert client.cookies.get(access.ADMIN_COOKIE) == grant
    now += 1
    assert client.get('/access/status').json()['expires_at'] == 0
    assert 'id="model-settings"' not in client.get('/settings/services').text
    assert client.post('/settings/group-action?slug=default', data={}).status_code == 401
    assert client.post('/admin/run/summarize').status_code == 401
    assert client.get('/stats').status_code == 200
    assert client.get('/').status_code == 200
    assert client.post('/access/unlock', data={'password':'test-password'},
                       headers={'Accept':'application/json'}).json()['expires_at'] == now + 300
    assert 'id="model-settings"' in client.get('/settings/services').text


def test_unlock_shares_login_throttle_and_rejects_cross_origin(owner):
    client, _ = owner
    client.cookies.delete(access.ADMIN_COOKIE)
    response = client.post('/access/unlock', data={'password':'test-password'}, headers={'Origin':'https://elsewhere.invalid'})
    assert response.status_code == 403
    for i in range(10):
        url = '/login' if i % 2 else '/access/unlock'
        assert client.post(url, data={'password':'wrong'}, headers={'Accept':'application/json'}).status_code == 401
    assert '尝试次数过多' in client.post('/access/unlock', data={'password':'test-password'},
                                           headers={'Accept':'application/json'}).json()['detail']
    assert client.get('/access/status').json()['expires_at'] == 0


def test_unlock_redirect_and_cookie_invalidation(owner):
    client, cfg = owner
    for destination in ['//elsewhere.invalid', '/\\elsewhere.invalid', 'https://elsewhere.invalid']:
        response = client.post('/access/unlock', data={'password':'test-password', 'next':destination}, follow_redirects=False)
        assert response.headers['location'] == '/settings'
    old_grant = client.cookies.get(access.ADMIN_COOKIE)
    old_reading = client.cookies.get(access.SESSION_COOKIE)
    assert client.post('/logout', follow_redirects=False).status_code == 303
    assert access.ADMIN_COOKIE not in client.cookies and access.SESSION_COOKIE not in client.cookies
    credentials.write(cfg, cfg.app.admin_password_env, hash_password('changed-password', iterations=1000))
    client.cookies.set(access.ADMIN_COOKIE, old_grant)
    client.cookies.set(access.SESSION_COOKIE, old_reading)
    assert client.post('/admin/run/rank').status_code == 401


def test_operation_grants_and_exit_are_isolated_between_browsers(owner, monkeypatch):
    first, _ = owner
    second = TestClient(web.app)
    assert second.post('/login', data={'password': 'test-password'}, follow_redirects=False).status_code == 303
    first_reading = first.cookies.get(access.SESSION_COOKIE)
    second_grant = second.cookies.get(access.ADMIN_COOKIE)
    assert first.cookies.get(access.ADMIN_COOKIE) != second_grant
    monkeypatch.setattr(web.rank, 'run', lambda *a, **kw: {'scored': 0})
    response = first.post('/access/lock', headers={'Accept': 'application/json'})
    assert response.status_code == 200 and response.json()['expires_at'] == 0
    assert access.ADMIN_COOKIE not in first.cookies
    assert first.cookies.get(access.SESSION_COOKIE) == first_reading
    assert first.get('/').status_code == 200
    assert first.post('/admin/run/rank').status_code == 401
    assert 'id="model-settings"' not in first.get('/settings/services').text
    assert second.cookies.get(access.ADMIN_COOKIE) == second_grant
    assert second.get('/access/status').json()['expires_at'] > 0
    assert second.post('/admin/run/rank').status_code == 200
    # Unlocking A does not unlock B, even with the same address/user-agent/password.
    second.post('/access/lock', headers={'Accept': 'application/json'})
    assert first.post('/access/unlock', data={'password': 'test-password'},
                      headers={'Accept': 'application/json'}).status_code == 200
    assert second.get('/access/status').json()['expires_at'] == 0
    assert second.post('/admin/run/rank').status_code == 401
    assert first.post('/admin/run/rank').status_code == 200


def test_operation_exit_requires_same_origin_and_supports_native_form(owner):
    client, _ = owner
    grant = client.cookies.get(access.ADMIN_COOKIE)
    assert client.post('/access/lock', headers={'Origin': 'https://elsewhere.invalid'}).status_code == 403
    assert client.cookies.get(access.ADMIN_COOKIE) == grant
    response = client.post('/access/lock', data={'next': '/stats?g=default'}, follow_redirects=False)
    assert response.status_code == 303 and response.headers['location'] == '/stats?g=default'
    assert access.ADMIN_COOKIE not in client.cookies
    assert client.get('/stats').status_code == 200


def test_background_progress_survives_expiry_and_prevents_duplicate_runs(owner, monkeypatch):
    client, cfg = owner
    entered, release = threading.Event(), threading.Event()
    calls = []
    def run(*args, **kwargs):
        calls.append(kwargs['group'].slug)
        progress.report('已处理 5 篇，正在评分下一批', 5, 12)
        entered.set()
        assert release.wait(5)
        return {'scored': 12}
    monkeypatch.setattr(web.rank, 'run', run)
    run_id = uuid.uuid4().hex
    try:
        response = post_run(client, run_id=run_id)
        assert response.status_code == 202
        assert entered.wait(2)
        state = client.get('/admin/jobs/current').json()
        assert state['id'] == run_id and state['status'] == 'running'
        assert state['current'] == 5 and state['total'] == 12
        assert state['steps'][0]['status'] == 'running'
        assert post_run(client, run_id=run_id).json()['id'] == run_id
        assert post_run(client).status_code == 409
        assert post_run(client, stage='summarize', run_id=run_id).status_code == 409
        client.cookies.delete(access.ADMIN_COOKIE)
        assert client.get(f'/admin/jobs/{run_id}').json()['status'] == 'running'
        assert post_run(client).status_code == 401
        assert TestClient(web.app).get('/admin/jobs/current').status_code == 401
        assert calls == ['default']
    finally:
        release.set()
        with single_instance(cfg.db_file.parent / 'litradar.lock', blocking=True):
            pass
    state = client.get(f'/admin/jobs/{run_id}').json()
    assert state['status'] == 'completed' and state['finished_at']
    assert state['steps'][0]['message'] == '12 篇评分'
    # Reads use the same durable store after creating a new app client/store.
    assert JobStore(cfg).snapshot()['id'] == run_id


@pytest.mark.parametrize('fail', [False, True])
def test_partial_and_failed_jobs_are_reported_without_raw_provider_errors(owner, monkeypatch, fail):
    client, cfg = owner
    def run(*a, **kw):
        if fail:
            raise RuntimeError('SECRET_PROVIDER_PAYLOAD')
        return {'scored': 3, 'groups': {'default': {'llm_failed': 2, 'error': 'SECRET_PROVIDER_PAYLOAD'}}}
    monkeypatch.setattr(web.rank, 'run', run)
    response = post_run(client)
    assert response.status_code == 202
    with single_instance(cfg.db_file.parent / 'litradar.lock', blocking=True):
        pass
    state = JobStore(cfg).snapshot()
    assert state['status'] == ('failed' if fail else 'partial')
    assert state['steps'][0]['status'] == state['status']
    assert 'SECRET_PROVIDER_PAYLOAD' not in json.dumps(state)


def test_interrupted_worker_is_not_automatically_restarted(owner, monkeypatch):
    client, cfg = owner
    monkeypatch.setattr(web.rank, 'run', lambda *a, **kw: {'scored': 1})
    response = post_run(client)
    with single_instance(cfg.db_file.parent / 'litradar.lock', blocking=True):
        store = JobStore(cfg)
        store._change(response.json()['id'], lambda state: state.update(status='running', finished_at=None))
        assert store.snapshot()['status'] == 'running'
    state = JobStore(cfg).snapshot()
    assert state['status'] == 'interrupted'
    assert state['finished_at'] and '中断' in state['message']


def test_limits_and_cli_lock_apply_before_background_work(owner, monkeypatch):
    client, cfg = owner
    calls = []
    monkeypatch.setattr(web.rank, 'run', lambda *a, **kw: calls.append(True))
    with single_instance(cfg.db_file.parent / 'litradar.lock'):
        assert post_run(client).status_code == 409
    def limited(*args):
        raise web.HTTPException(429, '今天已达到上限')
    monkeypatch.setattr(web, 'require_stage_limits', limited)
    assert post_run(client).status_code == 429
    assert JobStore(cfg).snapshot() is None and not calls


def test_browser_grant_expiry_retains_drafts_and_retries_only_rejected_writes():
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for the frontend regression')
    result = subprocess.run([node, str(Path(__file__).parent / 'helpers/authorization.cjs')],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_failed_model_batch_still_advances_processed_counts():
    from litradar import rank
    from litradar.config import Config
    from litradar.llm import LLMError
    cfg = Config()
    cfg.llm.rerank_batch_size = 5
    cfg.interests_data = {'name': 'Research', 'direction': 'Chemistry'}
    rows = [({'id': i, 'title': f'Paper {i}', 'abstract': 'Example abstract',
              'journal': 'Journal', 'published_at': '2026-01-01'}, 0, {}) for i in range(10)]
    class Model:
        calls = 0
        def json(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise LLMError('Temporary failure')
            return {'scores': [{'id': i, 'score': 70, 'reason': 'Relevant'} for i in range(1, 6)]}
    events = []
    with progress.observe(events.append):
        scores = progress.run_stage('rank', lambda: rank.llm_rerank(rows, rank.load_interests(cfg), cfg, Model()))
    counts = [(event['current'], event['total']) for event in events if event['kind'] == 'progress']
    assert counts == [(0, 10), (5, 10), (5, 10), (10, 10)]
    assert len(scores) == 5
