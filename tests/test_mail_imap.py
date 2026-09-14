"""IMAP 接入测试:用假连接替代 imaplib,验证 UID 路径、连接复用与搜索条件。"""
from __future__ import annotations

import sys
import imaplib
import ssl
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from litradar import cli  # noqa: E402
from litradar.config import Config, MailConfig  # noqa: E402
from litradar.sources import mail  # noqa: E402


class FakeIMAP:
    """只实现 iter_imap 用到的命令。故意**不提供** search/fetch/store ——
    一旦代码退回序列号命令,测试会直接 AttributeError。"""

    created: list["FakeIMAP"] = []

    def __init__(self, host, port, timeout=None, ssl_context=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.ssl_context = ssl_context
        self.calls: list[tuple] = []
        self.logged_out = False
        FakeIMAP.created.append(self)

    def login(self, user, pwd):
        self.calls.append(("login", user, pwd))

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))

    def uid(self, command, *args):
        self.calls.append(("uid", command, *args))
        if command == "SEARCH":
            return "OK", [b"1001 1002"]
        if command == "FETCH":
            uid = args[0]
            return "OK", [(b"7 (UID " + uid + b" BODY[] {5})", b"raw-" + uid), b")"]
        return "OK", [b""]

    def close(self):
        self.calls.append(("close",))

    def logout(self):
        self.logged_out = True


@pytest.fixture
def fake_imap(monkeypatch):
    FakeIMAP.created = []
    monkeypatch.setenv("IMAP_PASSWORD", "app-specific-pw")
    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", FakeIMAP)
    return FakeIMAP


def _cfg(**kw) -> MailConfig:
    base = dict(mode="imap", imap_host="imap.qq.com", imap_user="me@qq.com")
    base.update(kw)
    return MailConfig(**base)


def _drain(cfg) -> list:
    """模拟 pipeline:在两次 yield 之间调 acknowledge。"""
    out = []
    for msg in mail.iter_messages(cfg):
        out.append(msg)
        mail.acknowledge(cfg, msg)
    return out


# --------------------------------------------------------------- 搜索条件
def test_标已读时搜索条件加UNSEEN():
    """标了已读,UNSEEN 就等于"还没处理过",流量不再随邮箱体积增长。"""
    assert mail.imap_criteria(_cfg(imap_mark_seen=True)) == [
        "UNSEEN", 'FROM "newsletter.x-mol.com"']


def test_不标已读时保持全量():
    """没人标已读还加 UNSEEN,用户在网页上瞄一眼邮件就会让它永远进不来。"""
    assert mail.imap_criteria(_cfg(imap_mark_seen=False)) == [
        'FROM "newsletter.x-mol.com"']


def test_搜索条件原样传给UID_SEARCH(fake_imap):
    _drain(_cfg(imap_mark_seen=True))
    search = [c for c in fake_imap.created[0].calls if c[:2] == ("uid", "SEARCH")]
    assert search == [("uid", "SEARCH", None, "UNSEEN", 'FROM "newsletter.x-mol.com"')]


# ------------------------------------------------------------------- UID
def test_source_ref用真实UID(fake_imap):
    msgs = _drain(_cfg())
    assert [m.source_ref for m in msgs] == ["imap:1001", "imap:1002"]
    assert [m.origin for m in msgs] == ["1001", "1002"]
    assert [m.raw for m in msgs] == [b"raw-1001", b"raw-1002"]


def test_回执按UID标已读且复用同一条连接(fake_imap):
    """回归:以前 mark_seen 另开连接、拿 SEARCH 的**序列号**去 STORE ——
    两次会话之间来一封新邮件,已读就打到不相干的邮件上了。"""
    _drain(_cfg(imap_mark_seen=True))
    assert len(fake_imap.created) == 1, "一次 ingest 不该每封邮件登录一次"
    conn = fake_imap.created[0]
    assert [c for c in conn.calls if c[:2] == ("uid", "STORE")] == [
        ("uid", "STORE", b"1001", "+FLAGS", "\\Seen"),
        ("uid", "STORE", b"1002", "+FLAGS", "\\Seen"),
    ]
    # STORE 必须发生在下一封 FETCH 之前 —— 即生成器空闲的那个间隙
    order = [c[1] for c in conn.calls if c[0] == "uid"]
    assert order == ["SEARCH", "FETCH", "STORE", "FETCH", "STORE"]
    assert conn.logged_out is True


def test_不标已读时不发STORE(fake_imap):
    _drain(_cfg(imap_mark_seen=False))
    assert [c for c in fake_imap.created[0].calls
            if c[:2] == ("uid", "STORE")] == []


def test_连接带超时(fake_imap):
    _drain(_cfg())
    assert fake_imap.created[0].timeout == mail.IMAP_TIMEOUT


def test_缺密码时报错(fake_imap, monkeypatch):
    monkeypatch.delenv("IMAP_PASSWORD", raising=False)
    with pytest.raises(ValueError):
        list(mail.iter_imap(_cfg()))


@pytest.mark.parametrize("entry", ["ingest", "ack", "mail-test"])
def test_所有IMAP入口验证证书和主机名(fake_imap, monkeypatch, entry):
    # 上下文来自真实 ssl 库,FakeIMAP 只替换网络和协议 I/O。
    if entry == "ingest":
        _drain(_cfg())
    elif entry == "ack":
        mail.mark_seen(_cfg(imap_mark_seen=True), mail.MailMessage(b"", "imap:1", "1"))
    else:
        monkeypatch.setattr(FakeIMAP, "search", lambda *a: ("OK", [b""]), raising=False)
        cfg = Config()
        cfg.mail = _cfg()
        assert cli.cmd_mail_test(cfg, SimpleNamespace()) == 1  # 空邮箱诊断失败
    conn = fake_imap.created[0]
    assert conn.ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert conn.ssl_context.check_hostname is True
    assert conn.timeout == mail.IMAP_TIMEOUT
    assert ("close",) not in conn.calls
    assert conn.logged_out
    if entry == "mail-test":
        assert ("select", "INBOX", True) in conn.calls


@pytest.mark.parametrize("entry", ["ingest", "mail-test"])
@pytest.mark.parametrize("reason", ["untrusted issuer", "hostname mismatch"])
def test_证书验证失败时不发送登录凭据(monkeypatch, entry, reason):
    # 保留 IMAP4_SSL 的真实构造/上下文,仅在建立 socket 的边界注入握手失败。
    # 若上下文没有验证,假握手会成功,接着 LOGIN 将使本用例失败。
    def handshake(conn, *a, **kw):
        if (conn.ssl_context.verify_mode == ssl.CERT_REQUIRED
                and conn.ssl_context.check_hostname):
            raise ssl.SSLCertVerificationError(reason)

    def login(*a, **kw):
        pytest.fail("证书不可信时不能发送 LOGIN")

    monkeypatch.setenv("IMAP_PASSWORD", "test-only")
    monkeypatch.setattr(imaplib.IMAP4, "__init__", handshake)
    monkeypatch.setattr(imaplib.IMAP4, "login", login)
    if entry == "ingest":
        with pytest.raises(ssl.SSLCertVerificationError, match=reason):
            list(mail.iter_imap(_cfg()))
    else:
        cfg = Config()
        cfg.mail = _cfg()
        assert cli.cmd_mail_test(cfg, SimpleNamespace()) == 1


@pytest.mark.parametrize("mark_seen", [False, True])
@pytest.mark.parametrize("finish", ["normal", "empty", "fetch-error", "early-close"])
def test_IMAP退出路径不清除其他客户端标删除的邮件(fake_imap, monkeypatch, mark_seen, finish):
    original_uid = FakeIMAP.uid

    def uid(conn, command, *args):
        if command == "SEARCH" and finish == "empty":
            return "OK", [b""]
        if command == "FETCH" and finish == "fetch-error":
            raise imaplib.IMAP4.error("test fetch error")
        return original_uid(conn, command, *args)

    monkeypatch.setattr(FakeIMAP, "uid", uid)
    cfg = _cfg(imap_mark_seen=mark_seen)
    if finish == "early-close":
        messages = mail.iter_imap(cfg)
        next(messages)
        messages.close()
    elif finish == "fetch-error":
        with pytest.raises(imaplib.IMAP4.error):
            _drain(cfg)
    else:
        _drain(cfg)
    conn = fake_imap.created[0]
    assert ("select", "INBOX", not mark_seen) in conn.calls
    assert ("close",) not in conn.calls
    assert conn.logged_out


def test_断连不发送标准库的CLOSE或EXPUNGE命令():
    class OfflineIMAP(imaplib.IMAP4):
        def __init__(self):
            self.state = "SELECTED"
            self.commands = []

        def _simple_command(self, command, *args):
            self.commands.append(command)
            return "OK", []

        def logout(self):
            self.commands.append("LOGOUT")

    conn = OfflineIMAP()
    mail._disconnect(conn)
    assert conn.commands == ["LOGOUT"]


# ---------------------------------------------------------------- folder
def test_folder模式的回执仍会归档(tmp_path):
    inbox, done = tmp_path / "inbox", tmp_path / "done"
    inbox.mkdir()
    (inbox / "a.eml").write_bytes(b"hello")
    cfg = MailConfig(mode="folder", folder=str(inbox), move_processed_to=str(done))
    msgs = _drain(cfg)
    assert [m.raw for m in msgs] == [b"hello"]
    assert not (inbox / "a.eml").exists()
    assert (done / "a.eml").read_bytes() == b"hello"
