"""邮件入库事务与历史 raw_email 重放。"""
from pathlib import Path
import sqlite3
from unittest.mock import Mock

import pytest

from litradar import db, pipeline
from litradar.config import Config
from litradar.sources import mail, xmol_email


ROOT = Path(__file__).resolve().parent.parent
RAW = (ROOT / "fixtures/xmol_sample.eml").read_bytes()


def _cfg(tmp_path) -> Config:
    cfg = Config()
    cfg.app.db_path = str(tmp_path / "mail.db")
    cfg.mail.mode = "folder"
    return cfg


@pytest.mark.parametrize("fail_at", [1, 2])
def test_一封邮件任一条失败则整封回滚并可重试(tmp_path, monkeypatch, fail_at):
    cfg = _cfg(tmp_path)
    message = mail.MailMessage(raw=RAW, source_ref="test:mail", origin="test", ack=Mock())
    monkeypatch.setattr(mail, "iter_messages", lambda *_: iter([message]))

    original = pipeline._store
    calls = 0

    def fail_once(conn, data):
        nonlocal calls
        calls += 1
        if calls == fail_at:
            raise RuntimeError("temporary write failure")
        return original(conn, data)

    monkeypatch.setattr(pipeline, "_store", fail_once)
    first = pipeline.ingest_mail(cfg, verbose=False)
    conn = db.Database(cfg.db_file).connect()
    assert first["errors"] == 1
    assert first["new"] == first["updated"] == 0
    assert conn.execute("SELECT COUNT(*) FROM item").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM raw_email").fetchone()[0] == 0
    assert message.ack.call_count == 0
    conn.close()

    monkeypatch.setattr(pipeline, "_store", original)
    second = pipeline.ingest_mail(cfg, verbose=False)
    conn = db.Database(cfg.db_file).connect()
    assert second["new"] == second["records"] == 2
    assert conn.execute("SELECT COUNT(*) FROM item").fetchone()[0] == 2
    assert conn.execute("SELECT processed_at FROM raw_email").fetchone()[0]
    assert message.ack.call_count == 1
    conn.close()


def test_迁移前未完成raw邮件可离线重放(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    parsed, _ = xmol_email.parse_bytes(RAW)
    conn = db.Database(cfg.db_file).connect()
    # 第一条已由其他来源进入，重放时必须补齐邮件第二条而不能靠
    # source_ref 计数误判整封邮件已完成。
    pipeline._store(conn, xmol_email.to_item_data(parsed[0], source_ref="other:source"))
    conn.execute(
        """INSERT INTO raw_email(message_id, raw, parsed_at, processed_at, items_found)
           VALUES (?,?,?,?,?)""",
        ("legacy:mail", RAW, db.now(), None, 2),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(mail, "iter_messages", lambda *_: iter([]))

    out = pipeline.ingest_mail(cfg, verbose=False)
    conn = db.Database(cfg.db_file).connect()
    assert out["messages"] == 1 and out["records"] == 2
    assert out["new"] == 1 and out["updated"] == 1
    assert conn.execute("SELECT COUNT(*) FROM item").fetchone()[0] == 2
    assert conn.execute("SELECT processed_at FROM raw_email").fetchone()[0]
    conn.close()

    # 完成标记已写入后，下一轮不再重放同一 raw。
    out = pipeline.ingest_mail(cfg, verbose=False)
    assert out["messages"] == out["records"] == 0


@pytest.mark.parametrize("scenario", ["empty", "unrelated", "success", "write-failure", "replay-failure"])
def test_收信与确认邮件时不持有数据库写锁(tmp_path, monkeypatch, scenario):
    cfg = _cfg(tmp_path)
    cfg.interests_data = {"groups": [{"slug": "org"}, {"slug": "mat"}]}
    conn = db.Database(cfg.db_file).connect()
    iid, _ = pipeline._store(conn, {"kind": "paper", "title": "Existing paper", "source": "test"})
    if scenario == "replay-failure":
        conn.execute("INSERT INTO raw_email(message_id, raw) VALUES (?,?)", ("old", RAW))
    conn.commit()
    conn.close()
    probes = []

    def browser_write():
        other = sqlite3.connect(cfg.db_file, timeout=0)
        try:
            # 独立连接必须已能读到组同步,且立即写入反馈,不等待 IMAP 超时。
            assert other.execute("SELECT COUNT(*) FROM interest_group WHERE slug IN ('org','mat')").fetchone()[0] == 2
            other.execute("UPDATE item_state SET starred=1 WHERE item_id=?", (iid,))
            other.commit()
            probes.append("write")
        finally:
            other.close()

    def messages(_):
        browser_write()  # connect/search
        if scenario not in ("empty", "replay-failure"):
            yield mail.MailMessage(
                raw=b"unrelated" if scenario == "unrelated" else RAW,
                source_ref="test:mail", origin="test", ack=browser_write)
            browser_write()  # 下一封 FETCH/断连之前

    monkeypatch.setattr(mail, "iter_messages", messages)
    if scenario in ("write-failure", "replay-failure"):
        monkeypatch.setattr(pipeline, "_store", Mock(side_effect=RuntimeError("failed record")))
    out = pipeline.ingest_mail(cfg, verbose=False)
    assert out["errors"] == int(scenario in ("write-failure", "replay-failure"))
    assert len(probes) == (3 if scenario == "success" else
                           1 if scenario in ("empty", "replay-failure") else 2)


@pytest.mark.parametrize("mode", ["imap", "folder", "maildir"])
def test_XMOL关闭时邮件采集入口跳过且不触碰来源或数据库(tmp_path, monkeypatch, mode):
    cfg = _cfg(tmp_path)
    cfg.sources.xmol_enabled = False
    cfg.mail.mode = mode
    # IMAP 配置无效也不能拦住后续阶段;其它模式不能意外读取/归档文件。
    monkeypatch.setattr(mail, "iter_messages", Mock(side_effect=AssertionError("disabled source")))
    assert pipeline.ingest_mail(cfg, verbose=False) == {"skipped": "sources.xmol_enabled=false"}
    assert not cfg.db_file.exists()


def test_完整流水线关闭XMOL后继续其余阶段(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.sources.xmol_enabled = False
    cfg.mail.mode = "imap"
    monkeypatch.setattr(pipeline, "ingest_mail", Mock(side_effect=AssertionError("disabled mail")))
    calls = []

    def stage(name):
        def run(*a, **kw):
            calls.append(name)
            return {"errors": 0}
        return run

    monkeypatch.setattr(pipeline, "ingest_keyword_search", stage("search"))
    monkeypatch.setattr(pipeline.enrich, "run", stage("enrich"))
    monkeypatch.setattr(pipeline.rank, "run", stage("rank"))
    monkeypatch.setattr(pipeline.summarize, "run", stage("summarize"))
    out = pipeline.run_all(cfg, verbose=False)
    assert out["ingest_mail"] == {"skipped": "sources.xmol_enabled=false"}
    assert calls == ["search", "enrich", "rank", "summarize"]
