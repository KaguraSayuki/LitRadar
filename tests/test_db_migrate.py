"""数据库迁移的版本化。

以前每个进程第一次 connect() 都把全部迁移步骤跑一遍(全表扫 item 洗刊名、
三次 PRAGMA table_info、试探 score 表重建),每条 CLI 命令启动都白付这笔钱。
这里测三件事:老库能升上来、新库直接是最新版、最新版什么都不再做。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from litradar import db  # noqa: E402


def _old_db(path: Path) -> None:
    """造一个版本化之前的老库:user_version=0,缺 excluded / title_zh 列,
    score 还带 profile 列,刊名里留着 HTML 实体。"""
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA)          # item 等表结构没变过,直接借用
    conn.executescript(db.FTS_TRIGGERS)    # 老库也有 FTS 触发器,少了它 UPDATE item 会炸
    conn.executescript("""
        DROP TABLE item_state;
        CREATE TABLE item_state (
            item_id INTEGER PRIMARY KEY REFERENCES item(id) ON DELETE CASCADE,
            state TEXT NOT NULL DEFAULT 'new',
            starred INTEGER NOT NULL DEFAULT 0,
            ignored INTEGER NOT NULL DEFAULT 0,
            notified_at TEXT
        );
        DROP TABLE summary;
        CREATE TABLE summary (
            item_id INTEGER PRIMARY KEY REFERENCES item(id) ON DELETE CASCADE,
            one_liner TEXT, problem TEXT, method TEXT, key_results TEXT,
            limitation TEXT, relevance TEXT, depth TEXT, model TEXT, created_at TEXT
        );
        DROP TABLE score;
        CREATE TABLE score (
            profile TEXT NOT NULL DEFAULT 'default',
            item_id INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
            rule_score REAL, coarse_score REAL, llm_score REAL,
            llm_reason TEXT, llm_model TEXT, final_score REAL, ranked_at TEXT,
            PRIMARY KEY (profile, item_id)
        );
        INSERT INTO item (id, kind, dedup_key, title, title_norm, journal, source,
                          created_at, updated_at)
        VALUES (1, 'paper', 'doi:10.1/a', 'T', 't',
                'Organic &amp; Biomolecular Chemistry', 'crossref', 'x', 'x');
        INSERT INTO score (profile, item_id, final_score) VALUES ('default', 1, 77.5);
    """)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    conn.commit()
    conn.close()


def _cols(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_老库升级到最新版(tmp_path):
    path = tmp_path / "old.db"
    _old_db(path)

    database = db.Database(path)
    database.init()
    conn = database.connect()

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert "excluded" in _cols(conn, "item_state")
    assert "title_zh" in _cols(conn, "summary")
    assert "profile" not in _cols(conn, "score")
    assert conn.execute("SELECT final_score FROM score WHERE item_id=1").fetchone()[0] == 77.5
    assert conn.execute("SELECT journal FROM item").fetchone()[0] == \
        "Organic & Biomolecular Chemistry"
    conn.close()


def test_新库直接打最新版本号不跑历史步骤(tmp_path, monkeypatch):
    """SCHEMA 建出来的就是最新结构,没必要再扫一遍空表、探测一遍列。"""
    def boom(conn):
        raise AssertionError("新库不该跑历史迁移步骤")

    monkeypatch.setattr(db, "_MIGRATIONS", [boom] * db.SCHEMA_VERSION)
    database = db.Database(tmp_path / "new.db")
    database.init()
    conn = database.connect()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()


def test_已是最新版时迁移不再碰数据(tmp_path):
    """回归:以前每次启动都全表 UPDATE item 洗刊名。现在只读一次版本号就退出。"""
    database = db.Database(tmp_path / "t.db")
    database.init()
    conn = database.connect()
    db.upsert_item(conn, {"kind": "paper", "dedup_key": "doi:10.1/a", "doi": "10.1/a",
                          "title": "T", "title_norm": "t", "journal": "JACS",
                          "source": "crossref"})
    conn.commit()

    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    database._migrate(conn)
    conn.set_trace_callback(None)

    assert seen == ["PRAGMA user_version"], seen
    conn.close()
