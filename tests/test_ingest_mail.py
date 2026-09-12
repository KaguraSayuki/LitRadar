"""邮件入库事务与历史 raw_email 重放。"""
from pathlib import Path
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
