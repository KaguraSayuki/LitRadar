"""Offline tests for Outlook IMAP OAuth2 and the optional MSAL dependency."""
from __future__ import annotations

import json
import stat
import sys
import types
from pathlib import Path

import pytest

from litradar.config import MailConfig
from litradar.sources import mail, mail_oauth


class FakeCache:
    def __init__(self):
        self.data: dict = {}
        self.has_state_changed = False

    def deserialize(self, text):
        self.data = json.loads(text)

    def serialize(self):
        return json.dumps(self.data)


class FakeMSALApp:
    apps: list["FakeMSALApp"] = []
    silent_result = None

    def __init__(self, client_id, *, authority, token_cache, timeout=None):
        self.client_id = client_id
        self.authority = authority
        self.token_cache = token_cache
        self.timeout = timeout
        self.device_scopes = None
        self.device_flow = None
        self.silent_scopes = None
        self.silent_account = None
        self.device_result = {
            "access_token": "device-token",
            "account": {"username": "me@outlook.com"},
        }
        type(self).apps.append(self)

    def get_accounts(self):
        accounts = self.token_cache.data.get("accounts")
        if accounts is not None:
            return accounts
        account = self.token_cache.data.get("account")
        return [account] if account else []

    def acquire_token_silent(self, scopes, *, account):
        self.silent_scopes = scopes
        self.silent_account = account
        if type(self).silent_result is not None:
            return type(self).silent_result
        return self.token_cache.data.get("silent_result", {"error": "none"})

    def initiate_device_flow(self, *, scopes):
        self.device_scopes = scopes
        self.device_flow = {
            "user_code": "ABCD-EFGH",
            "message": "Open https://microsoft.example/device and enter the code.",
        }
        return self.device_flow

    def acquire_token_by_device_flow(self, flow):
        self.token_cache.data = {
            "account": {"username": "me@outlook.com"},
            "silent_result": self.device_result,
        }
        self.token_cache.has_state_changed = True
        return self.device_result


@pytest.fixture
def fake_msal(monkeypatch):
    FakeMSALApp.apps = []
    FakeMSALApp.silent_result = None
    module = types.SimpleNamespace(
        SerializableTokenCache=FakeCache,
        PublicClientApplication=FakeMSALApp,
    )
    monkeypatch.setitem(sys.modules, "msal", module)
    monkeypatch.setenv("OUTLOOK_CLIENT_ID", "client-id")
    return module


def _cfg(tmp_path: Path, **kwargs) -> MailConfig:
    values = dict(
        mode="imap",
        imap_host="outlook.office365.com",
        imap_user="me@outlook.com",
        imap_auth="oauth2",
        oauth_token_cache=str(tmp_path / "outlook-token-cache.json"),
    )
    values.update(kwargs)
    return MailConfig(**values)


def test_device_authorize_writes_private_cache_and_requests_only_imap_scope(
    fake_msal, tmp_path
):
    cfg = _cfg(tmp_path)
    shown = []

    token = mail_oauth.device_authorize(cfg, on_device_code=shown.append)

    assert token == "device-token"
    assert shown == [FakeMSALApp.apps[0].device_flow]
    assert FakeMSALApp.apps[0].authority == "https://login.microsoftonline.com/consumers"
    assert FakeMSALApp.apps[0].timeout == mail_oauth.MSAL_TIMEOUT == 30
    assert FakeMSALApp.apps[0].device_scopes == [mail_oauth.IMAP_SCOPE]
    cache = Path(cfg.oauth_token_cache)
    assert cache.exists()
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    assert not list(cache.parent.glob(f".{cache.name}.*.tmp"))


def test_silent_cache_refresh_is_used_without_device_flow(fake_msal, tmp_path):
    cfg = _cfg(tmp_path)
    mail_oauth.device_authorize(cfg)
    FakeMSALApp.silent_result = {"access_token": "refreshed-token"}

    assert mail_oauth.acquire_token(cfg) == "refreshed-token"
    app = FakeMSALApp.apps[-1]
    assert app.device_scopes is None
    assert app.silent_scopes == [mail_oauth.IMAP_SCOPE]


def test_missing_cache_fails_explicitly_without_interactive_login(fake_msal, tmp_path):
    cfg = _cfg(tmp_path)

    with pytest.raises(mail_oauth.OAuthAuthorizationRequired, match="设备码授权"):
        mail_oauth.acquire_token(cfg)

    assert FakeMSALApp.apps[0].device_scopes is None
    assert not Path(cfg.oauth_token_cache).exists()


def test_corrupt_cache_silent_fails_but_device_flow_can_replace_it(
    fake_msal, tmp_path
):
    cfg = _cfg(tmp_path)
    cache = Path(cfg.oauth_token_cache)
    cache.write_bytes(b"not-json")
    cache.chmod(0o600)

    with pytest.raises(mail_oauth.OAuthAuthorizationRequired, match="缓存无法读取"):
        mail_oauth.acquire_token(cfg)
    assert cache.read_bytes() == b"not-json"

    token = mail_oauth.device_authorize(cfg)
    assert token == "device-token"
    assert cache.read_bytes() != b"not-json"
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600


def test_failed_reauthorization_keeps_corrupt_cache_for_manual_recovery(
    fake_msal, monkeypatch, tmp_path
):
    cfg = _cfg(tmp_path)
    cache = Path(cfg.oauth_token_cache)
    cache.write_bytes(b"not-json")
    cache.chmod(0o600)

    def denied(self, flow):
        return {"error": "access_denied"}

    monkeypatch.setattr(FakeMSALApp, "acquire_token_by_device_flow", denied)
    with pytest.raises(mail_oauth.OAuthAuthorizationRequired, match="未返回有效访问令牌"):
        mail_oauth.device_authorize(cfg)
    assert cache.read_bytes() == b"not-json"
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600


def test_target_mismatch_can_explicitly_reauthorize_but_silent_mode_fails(
    fake_msal, tmp_path
):
    cfg = _cfg(tmp_path)
    cache = Path(cfg.oauth_token_cache)
    cache.write_text(json.dumps({
        "accounts": [{"username": "someone-else@outlook.com"}],
    }), encoding="utf-8")
    cache.chmod(0o600)

    with pytest.raises(mail_oauth.OAuthAuthorizationRequired, match="不匹配"):
        mail_oauth.acquire_token(cfg)

    token = mail_oauth.device_authorize(cfg)
    assert token == "device-token"
    assert FakeMSALApp.apps[-1].device_scopes == [mail_oauth.IMAP_SCOPE]


def test_authenticate_sends_raw_xoauth2_payload_and_checks_server_result(
    monkeypatch, tmp_path
):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(mail_oauth, "acquire_token", lambda *_a, **_kw: "access-token")

    class Conn:
        def __init__(self, result=("OK", [b"authenticated"])):
            self.result = result
            self.mechanism = None
            self.payload = None
            self.callback = None

        def authenticate(self, mechanism, callback):
            self.mechanism = mechanism
            self.callback = callback
            self.payload = callback(b"")
            return self.result

    conn = Conn()
    assert mail_oauth.authenticate(conn, cfg) == ("OK", [b"authenticated"])
    assert conn.mechanism == "XOAUTH2"
    # A non-empty or repeated challenge must never receive the bearer token.
    assert conn.payload == b"user=me@outlook.com\x01auth=Bearer access-token\x01\x01"
    assert conn.callback(b"") == b""
    assert conn.callback(b"error") == b""

    with pytest.raises(mail_oauth.OAuthAuthenticationError):
        mail_oauth.authenticate(Conn(("NO", [b"denied"])), cfg)


def test_authenticate_rejects_wrong_host_and_control_character_username(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(mail_oauth, "acquire_token", lambda *_a, **_kw: "access-token")

    with pytest.raises(mail_oauth.OAuthConfigurationError, match="outlook.office365.com"):
        mail_oauth.authenticate(object(), _cfg(tmp_path, imap_host="imap.qq.com"))

    with pytest.raises(mail_oauth.OAuthConfigurationError, match="控制字符"):
        mail_oauth.authenticate(object(), _cfg(tmp_path, imap_user="me\x01@outlook.com"))


def test_authority_rejects_non_microsoft_host(fake_msal, tmp_path):
    cfg = _cfg(tmp_path, oauth_tenant="https://evil.example/tenant")
    with pytest.raises(mail_oauth.OAuthConfigurationError):
        mail_oauth.acquire_token(cfg)


def test_oauth_connect_authenticates_then_checks_select(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    events = []

    class Conn:
        def __init__(self, host, port, timeout=None):
            events.append(("connect", host, port, timeout))

        def select(self, folder, readonly=False):
            events.append(("select", folder, readonly))
            return "OK", [b"0"]

        def close(self):
            events.append(("close",))

        def logout(self):
            events.append(("logout",))

    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", Conn)
    monkeypatch.setattr(
        mail.mail_oauth,
        "authenticate",
        lambda conn, cfg: events.append(("authenticate", conn, cfg)),
    )

    conn = mail._connect(cfg)
    assert isinstance(conn, Conn)
    assert [event[0] for event in events] == ["connect", "authenticate", "select"]


def test_oauth_connect_rejects_bad_select_and_closes(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    events = []

    class Conn:
        def __init__(self, *_args, **_kwargs):
            pass

        def select(self, *_args, **_kwargs):
            return "NO", [b"bad folder"]

        def unselect(self):
            events.append("unselect")

        def logout(self):
            events.append("logout")

    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", Conn)
    monkeypatch.setattr(mail.mail_oauth, "authenticate", lambda *_args: None)

    with pytest.raises(ConnectionError, match="SELECT"):
        mail._connect(cfg)
    assert events == ["unselect", "logout"]
