"""邮件确认、全量验证、崩溃恢复与跨来源去重的整条 WoS 流程。"""
from email.message import EmailMessage
from unittest.mock import Mock

import pytest

from litradar import db, pipeline, wos_sync
from litradar.config import Config
from litradar.sources import mail, wos_browser


ALERT_ID = "11111111-2222-4333-8444-555555555555"
URL = f"https://www.webofscience.com/wos/alldb/alert-execution-summary/{ALERT_ID}"


def message(total=2, *, mid="<wos-test@example.invalid>", url=URL):
    msg = EmailMessage()
    msg["From"] = "alerts-noreply@clarivate.com"
    msg["Subject"] = f"Web of Science Alert - Sample chemistry - {total} results"
    msg["Message-ID"] = mid
    msg["Date"] = "Fri, 11 Sep 2026 10:29:00 +0000"
    msg.set_content(f'<a href="{url}">View all {total} records</a>', subtype="html")
    return mail.MailMessage(raw=msg.as_bytes(), source_ref="fixture:"+mid,
                            origin="fixture", ack=Mock())


def ris(n=2):
    return "".join(
        f"TY  - JOUR\nTI  - Chemistry article {i}\nAU  - Example, A\n"
        f"T2  - Test Journal\nPY  - 2003\nDO  - 10.1234/example.{i}\n"
        f"AN  - WOS:00000000000000{i}\nAB  - Complete abstract {i}.\nER  -\n\n"
        for i in range(1, n+1)
    ).encode()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    cfg = Config()
    cfg.app.db_path = str(tmp_path / "wos.db")
    cfg.mail.folder = str(tmp_path / "inbox")
    cfg.wos.browser_profile_dir = str(tmp_path / "browser")
    fetch = Mock(return_value=ris())
    monkeypatch.setattr(wos_browser, "fetch_ris", fetch)
    return cfg, fetch


def rows(cfg, sql):
    conn = db.Database(cfg.db_file).connect()
    try:
        return [dict(row) for row in conn.execute(sql)]
    finally:
        conn.close()


def test_complete_alert_imported_and_duplicate_delivery_does_not_download_again(setup, monkeypatch):
    cfg, fetch = setup
    msg = message()
    monkeypatch.setattr(mail, "iter_messages", lambda *_: iter([msg]))
    out = pipeline.ingest_mail(cfg, verbose=False)
    assert out["new"] == out["records"] == 2
    assert out["wos_queued"] == 1 and out["errors"] == 0
    msg.ack.assert_called_once()
    assert rows(cfg, "SELECT status,records_imported FROM wos_alert") == [
        {"status": "complete", "records_imported": 2}]
    assert rows(cfg, "SELECT processed_at FROM raw_email")[0]["processed_at"]
    assert len(rows(cfg, "SELECT * FROM wos_alert_item")) == 2
    assert all(row["published_at"].startswith("2003")
               for row in rows(cfg, "SELECT published_at FROM item"))

    again = pipeline.ingest_mail(cfg, verbose=False)
    assert again["new"] == again["records"] == again["wos_queued"] == 0
    fetch.assert_called_once()


def test_partial_download_is_retried_from_durable_queue_after_mail_is_gone(setup, monkeypatch):
    cfg, fetch = setup
    fetch.return_value = ris(1)
    msg = message()
    monkeypatch.setattr(mail, "iter_messages", lambda *_: iter([msg]))
    out = pipeline.ingest_mail(cfg, verbose=False)
    assert out["errors"] == 1 and out["new"] == 0
    # 邮件已经安全入队可确认，但完整处理标记仍未写入。
    msg.ack.assert_called_once()
    assert rows(cfg, "SELECT processed_at FROM raw_email") == [{"processed_at": None}]
    assert rows(cfg, "SELECT * FROM item") == []
    assert rows(cfg, "SELECT status,ris FROM wos_alert") == [{"status": "retry", "ris": None}]

    monkeypatch.setattr(mail, "iter_messages", lambda *_: iter([]))
    assert pipeline.ingest_mail(cfg, verbose=False)["wos"]["alerts"] == 0  # backoff
    fetch.return_value = ris()
    done = wos_sync.run(cfg, force=True, verbose=False)
    assert done["new"] == 2 and done["remaining"] == 0
    assert fetch.call_count == 2


def test_item_failure_rolls_back_whole_alert_and_reuses_validated_download(setup, monkeypatch):
    cfg, fetch = setup
    msg = message()
    monkeypatch.setattr(mail, "iter_messages", lambda *_: iter([msg]))
    store = pipeline._store
    calls = 0

    def fail_second(conn, data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated database write failure")
        return store(conn, data)

    monkeypatch.setattr(pipeline, "_store", fail_second)
    out = pipeline.ingest_mail(cfg, verbose=False)
    assert out["errors"] == 1 and out["new"] == out["updated"] == 0
    assert rows(cfg, "SELECT * FROM item") == []
    assert rows(cfg, "SELECT * FROM wos_alert_item") == []
    assert rows(cfg, "SELECT processed_at FROM raw_email") == [{"processed_at": None}]
    assert rows(cfg, "SELECT ris FROM wos_alert")[0]["ris"] == ris()
    monkeypatch.setattr(pipeline, "_store", store)
    assert wos_sync.run(cfg, force=True, verbose=False)["new"] == 2
    fetch.assert_called_once()


def test_same_alert_in_two_messages_is_fetched_once_and_both_marked_complete(setup, monkeypatch):
    cfg, fetch = setup
    first, second = message(), message(mid="<forwarded@example.invalid>")
    monkeypatch.setattr(mail, "iter_messages", lambda *_: iter([first, second]))
    out = pipeline.ingest_mail(cfg, verbose=False)
    assert out["wos_queued"] == 1 and out["new"] == 2
    assert len(rows(cfg, "SELECT * FROM raw_email WHERE processed_at IS NOT NULL")) == 2
    assert len(rows(cfg, "SELECT * FROM wos_alert_email")) == 2
    fetch.assert_called_once()


def test_empty_alert_completes_without_browser(setup, monkeypatch):
    cfg, fetch = setup
    monkeypatch.setattr(mail, "iter_messages", lambda *_: iter([message(0)]))
    out = pipeline.ingest_mail(cfg, verbose=False)
    assert out["wos"]["completed"] == 1 and out["records"] == 0
    fetch.assert_not_called()


def test_access_failure_is_visible_and_does_not_acknowledge_complete_results(setup, monkeypatch):
    cfg, fetch = setup
    fetch.side_effect = wos_browser.WosAccessError("机构访问失效，请重新登录")
    monkeypatch.setattr(mail, "iter_messages", lambda *_: iter([message()]))
    out = pipeline.ingest_mail(cfg, verbose=False)
    assert out["wos"]["needs_login"] == 1
    assert wos_sync.status(cfg)[0]["status"] == "needs_login"
    assert rows(cfg, "SELECT processed_at FROM raw_email") == [{"processed_at": None}]


def test_invalid_email_link_never_opens_browser_and_original_is_retained(setup, monkeypatch):
    cfg, fetch = setup
    msg = message(url="https://example.invalid/alert-execution-summary/"+ALERT_ID)
    monkeypatch.setattr(mail, "iter_messages", lambda *_: iter([msg]))
    out = pipeline.ingest_mail(cfg, verbose=False)
    assert out["errors"] == 1
    msg.ack.assert_not_called()
    fetch.assert_not_called()
    assert len(rows(cfg, "SELECT raw FROM raw_email")) == 1


def test_existing_doi_keeps_user_state_and_gains_wos_membership(setup, monkeypatch):
    cfg, fetch = setup
    conn = db.Database(cfg.db_file).connect()
    iid, _ = pipeline._store(conn, {"kind": "paper", "source": "crossref", "doi": "10.1234/EXAMPLE.1",
                                   "title": "Chemistry article 1"})
    conn.execute("UPDATE item_state SET starred=1,state='read' WHERE item_id=?", (iid,))
    conn.commit()
    conn.close()
    monkeypatch.setattr(mail, "iter_messages", lambda *_: iter([message()]))
    out = pipeline.ingest_mail(cfg, verbose=False)
    assert out["new"] == out["updated"] == 1
    assert rows(cfg, f"SELECT starred,state FROM item_state WHERE item_id={iid}") == [
        {"starred": 1, "state": "read"}]
    assert len(rows(cfg, f"SELECT * FROM wos_alert_item WHERE item_id={iid}")) == 1


def test_pending_queue_runs_even_if_mailbox_is_unavailable(setup, monkeypatch):
    cfg, fetch = setup
    fetch.side_effect = RuntimeError("first download unavailable")
    monkeypatch.setattr(mail, "iter_messages", lambda *_: iter([message()]))
    pipeline.ingest_mail(cfg, verbose=False)
    conn = db.Database(cfg.db_file).connect()
    conn.execute("UPDATE wos_alert SET next_attempt_at=NULL")
    conn.commit()
    conn.close()
    fetch.side_effect = None

    def offline(*_):
        raise RuntimeError("mailbox offline")
    monkeypatch.setattr(mail, "iter_messages", offline)
    with pytest.raises(RuntimeError, match="mailbox offline"):
        pipeline.ingest_mail(cfg, verbose=False)
    assert wos_sync.status(cfg)[0]["status"] == "complete"


def test_v4_database_acquires_queue_without_losing_existing_data(tmp_path):
    path = tmp_path / "old.db"
    conn = db.Database(path).connect()
    pipeline._store(conn, {"kind": "paper", "source": "crossref", "doi": "10.1234/old", "title": "Existing"})
    for table in ("wos_alert_item", "wos_alert_email", "wos_alert"):
        conn.execute(f"DROP TABLE {table}")
    conn.execute("PRAGMA user_version=4")
    conn.commit()
    conn.close()
    db.Database(path).init()
    conn = db.Database(path).connect()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("SELECT title FROM item").fetchone()[0] == "Existing"
    assert conn.execute("SELECT count(*) FROM wos_alert").fetchone()[0] == 0
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()
