"""WoS 邮件触发的持久采集队列。

邮件原文和队列写入成功后即可确认收信。只有完整 RIS 的全部记录入库成功，
才给 raw_email 写 processed_at；下载或入库失败都能脱离邮箱重试。
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone

from . import db
from .config import Config
from .sources import wos_email, wos_ris


def enqueue(conn: sqlite3.Connection, alert: wos_email.WosAlert,
            meta: dict, raw: bytes, *, message_id: str) -> bool:
    """在调用方事务中留存邮件及任务；重复投递复用同一个 alert。"""
    previous = conn.execute(
        "SELECT expected_count, status FROM wos_alert WHERE alert_id=?",
        (alert.alert_id,),
    ).fetchone()
    if previous and previous["expected_count"] != alert.total:
        raise ValueError("同一个 WoS 提醒出现不同的结果总数，需要核对邮件")
    stamp = db.now()
    conn.execute(
        """INSERT OR IGNORE INTO raw_email
           (message_id, received_at, subject, raw, parsed_at, parse_version, items_found)
           VALUES (?,?,?,?,?,?,0)""",
        (message_id, meta.get("received_at"), meta.get("subject"), raw, stamp,
         getattr(wos_email, "PARSE_VERSION", 1)),
    )
    conn.execute(
        """INSERT OR IGNORE INTO wos_alert
           (alert_id, url, query, expected_count, created_at, updated_at)
           VALUES (?,?,?,?,?,?)""",
        (alert.alert_id, alert.url, alert.query, alert.total, stamp, stamp),
    )
    mapped = conn.execute(
        "SELECT alert_id FROM wos_alert_email WHERE message_id=?", (message_id,),
    ).fetchone()
    if mapped and mapped[0] != alert.alert_id:
        raise ValueError("同一个 Message-ID 对应不同的 WoS 提醒")
    conn.execute(
        "INSERT OR IGNORE INTO wos_alert_email(message_id, alert_id) VALUES (?,?)",
        (message_id, alert.alert_id),
    )
    if previous and previous["status"] == "complete":
        conn.execute(
            "UPDATE raw_email SET processed_at=?, items_found=? WHERE message_id=?",
            (stamp, alert.total, message_id),
        )
    return previous is None


def _error_text(error: Exception) -> str:
    # Playwright 异常可能带页面 URL/会话参数。日志只保留可操作的错误信息。
    return re.sub(r"https?://\S+", "[URL]", str(error))[:800]


def run(cfg: Config, *, verbose: bool = True, force: bool = False,
        limit: int | None = None) -> dict:
    """运行到期任务，失败时退避，已校验的下载可在入库重试时复用。"""
    stat = {"alerts": 0, "completed": 0, "records": 0, "new": 0,
            "updated": 0, "errors": 0, "needs_login": 0, "remaining": 0}
    if not cfg.wos.enabled:
        return {**stat, "disabled": True}
    from .pipeline import _store
    from .sources import wos_browser

    maximum = cfg.wos.max_alerts_per_run if limit is None else limit
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
        raise ValueError("WoS 单轮任务数量必须是正整数")
    conn = db.Database(cfg.db_file).connect()
    started = db.now()
    try:
        jobs = conn.execute(
            """SELECT * FROM wos_alert WHERE status <> 'complete'
               AND (? OR next_attempt_at IS NULL OR julianday(next_attempt_at)<=julianday(?))
               ORDER BY COALESCE(next_attempt_at, created_at), created_at LIMIT ?""",
            (int(force), started, maximum),
        ).fetchall()
        for job in jobs:
            aid = job["alert_id"]
            attempts = int(job["attempts"]) + 1
            stat["alerts"] += 1
            conn.execute("UPDATE wos_alert SET attempts=?, updated_at=? WHERE alert_id=?",
                         (attempts, db.now(), aid))
            conn.commit()
            try:
                if job["expected_count"] > cfg.wos.max_records_per_alert:
                    raise ValueError(
                        f"提醒有 {job['expected_count']} 条，超过 wos.max_records_per_alert="
                        f"{cfg.wos.max_records_per_alert}；调大上限后重试")
                if job["expected_count"] == 0:
                    records = []
                else:
                    raw = job["ris"]
                    if raw is None:
                        raw = wos_browser.fetch_ris(job["url"], job["expected_count"], cfg.wos)
                    try:
                        records = wos_ris.parse_bytes(raw)
                        if len(records) != job["expected_count"]:
                            raise ValueError(
                                f"WoS 完整性校验失败：期望 {job['expected_count']} 条，"
                                f"实际 {len(records)} 条")
                        refs = [r.get("source_ref") for r in records]
                        if not all(refs) or len(set(refs)) != len(records):
                            raise ValueError("WoS 完整性校验失败：文献标识缺失或重复")
                    except Exception:
                        # 旧缓存损坏时允许下一次重新下载。
                        conn.execute("UPDATE wos_alert SET ris=NULL WHERE alert_id=?", (aid,))
                        conn.commit()
                        raise
                    if job["ris"] is None:
                        conn.execute("UPDATE wos_alert SET ris=? WHERE alert_id=?", (raw, aid))
                        conn.commit()

                added = updated = 0
                conn.execute("SAVEPOINT store_wos_alert")
                try:
                    for record in records:
                        wos_id = record["source_ref"]
                        iid, created = _store(conn, dict(record))
                        conn.execute(
                            """INSERT INTO wos_alert_item(alert_id,wos_id,item_id) VALUES (?,?,?)
                               ON CONFLICT(alert_id,wos_id) DO UPDATE SET item_id=excluded.item_id""",
                            (aid, wos_id, iid),
                        )
                        added += int(created)
                        updated += int(not created)
                    stamp = db.now()
                    conn.execute(
                        """UPDATE wos_alert SET status='complete', records_imported=?,
                           next_attempt_at=NULL, last_error=NULL, updated_at=? WHERE alert_id=?""",
                        (len(records), stamp, aid),
                    )
                    conn.execute(
                        """UPDATE raw_email SET processed_at=?, items_found=?
                           WHERE message_id IN
                           (SELECT message_id FROM wos_alert_email WHERE alert_id=?)""",
                        (stamp, len(records), aid),
                    )
                    conn.execute("RELEASE SAVEPOINT store_wos_alert")
                except Exception:
                    conn.execute("ROLLBACK TO SAVEPOINT store_wos_alert")
                    conn.execute("RELEASE SAVEPOINT store_wos_alert")
                    raise
                conn.commit()
                stat["completed"] += 1
                stat["records"] += len(records)
                stat["new"] += added
                stat["updated"] += updated
                if verbose:
                    print(f"  WoS {job['query'][:60]} → {len(records)}/{job['expected_count']} 条")
            except Exception as error:  # 单个提醒失败不影响其它队列任务
                conn.rollback()
                needs_login = isinstance(error, wos_browser.WosAccessError)
                status = "needs_login" if needs_login else "retry"
                minutes = min(cfg.wos.retry_minutes * 2 ** min(attempts - 1, 10), 1440)
                retry_at = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
                message = _error_text(error)
                conn.execute(
                    """UPDATE wos_alert SET status=?, next_attempt_at=?, last_error=?,
                       updated_at=? WHERE alert_id=?""",
                    (status, retry_at, message, db.now(), aid),
                )
                conn.commit()
                stat["errors"] += 1
                stat["needs_login"] += int(needs_login)
                if verbose:
                    print(f"  [warn] WoS {job['query'][:60]}: {message}")
        stat["remaining"] = conn.execute(
            "SELECT COUNT(*) FROM wos_alert WHERE status <> 'complete'",
        ).fetchone()[0]
        db.log_run(conn, "ingest_wos", "partial" if stat["errors"] else "ok",
                   stat, started_at=started)
        conn.commit()
        return stat
    finally:
        conn.close()


def status(cfg: Config, *, limit: int = 20) -> list[dict]:
    conn = db.Database(cfg.db_file).connect()
    try:
        return [dict(row) for row in conn.execute(
            """SELECT query, expected_count, records_imported, status, attempts,
                      next_attempt_at, last_error, updated_at FROM wos_alert
               ORDER BY status='complete', updated_at DESC LIMIT ?""", (limit,),
        )]
    finally:
        conn.close()
