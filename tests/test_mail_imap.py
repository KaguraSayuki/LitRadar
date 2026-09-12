"""IMAP 接入测试:用假连接替代 imaplib,验证 UID 路径、连接复用与搜索条件。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from litradar.config import MailConfig  # noqa: E402
from litradar.sources import mail  # noqa: E402


class FakeIMAP:
    """只实现 iter_imap 用到的命令。故意**不提供** search/fetch/store ——
    一旦代码退回序列号命令,测试会直接 AttributeError。"""

    created: list["FakeIMAP"] = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
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
