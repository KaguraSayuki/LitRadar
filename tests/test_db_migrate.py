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
    """造一个版本化之前的老库:user_version=0,缺 excluded / title_zh /
    abstract_attempts 列,score 还带 profile 列,刊名里留着 HTML 实体。"""
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
        DROP TABLE item_enrichment;
        CREATE TABLE item_enrichment (
            item_id INTEGER PRIMARY KEY REFERENCES item(id) ON DELETE CASCADE,
            cited_by_count INTEGER, is_oa INTEGER, oa_url TEXT, openalex_id TEXT,
            openalex_json TEXT, crossref_json TEXT, enriched_at TEXT
        );
        DROP TABLE raw_email;
        CREATE TABLE raw_email (
            id INTEGER PRIMARY KEY, message_id TEXT UNIQUE, received_at TEXT,
            subject TEXT, raw BLOB NOT NULL, parsed_at TEXT,
            parse_version INTEGER DEFAULT 0, items_found INTEGER DEFAULT 0
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
    assert "abstract_hash" in _cols(conn, "summary")
    assert "profile" not in _cols(conn, "score")
    assert "abstract_attempts" in _cols(conn, "item_enrichment")
    assert "processed_at" in _cols(conn, "raw_email")
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


def test_混合大小写DOI迁移合并关联数据(tmp_path):
    """合并重复条目时，用户反馈/评分/摘要/富化都跟到保留项。"""
    database = db.Database(tmp_path / "dupe.db")
    conn = database.connect()
    # 直接写入历史形态，绕过当前 upsert 的 canonicalization。
    for doi, title in (("10.9/ABC", "A"), ("10.9/abc", "B")):
        conn.execute(
            """INSERT INTO item(kind, dedup_key, doi, title, title_norm, abstract,
               source, created_at, updated_at)
               VALUES ('paper', ?, ?, ?, ?, ?, 'test', '2026-01-01', '2026-01-01')""",
            (f"doi:{doi}", doi, title, title.lower(), "longer abstract" if title == "B" else None),
        )
    first, second = [r[0] for r in conn.execute("SELECT id FROM item ORDER BY id")]
    conn.execute("INSERT INTO item_state(item_id) VALUES (?)", (first,))
    conn.execute("INSERT INTO item_state(item_id) VALUES (?)", (second,))
    conn.execute("UPDATE item_state SET starred=1 WHERE item_id=?", (first,))
    conn.execute("UPDATE item_state SET ignored=1, state='read' WHERE item_id=?", (first,))
    conn.execute("UPDATE item_state SET excluded=1 WHERE item_id=?", (second,))
    conn.execute("INSERT INTO feedback(item_id, action, created_at) VALUES (?,?,?)",
                 (first, "star", "2026-01-01T00:00:00"))
    conn.execute("INSERT INTO feedback(item_id, action, created_at) VALUES (?,?,?)",
                 (second, "unstar", "2026-01-02T00:00:00"))
    conn.execute("INSERT INTO feedback(item_id, action, created_at) VALUES (?,?,?)",
                 (first, "ignore", "2026-01-01T00:00:00"))
    conn.execute("INSERT INTO feedback(item_id, action, created_at) VALUES (?,?,?)",
                 (second, "unignore", "2026-01-02T00:00:01"))
    conn.execute("INSERT INTO feedback(item_id, action, created_at) VALUES (?,?,?)",
                 (first, "read", "2026-01-01T00:00:01"))
    conn.execute("INSERT INTO feedback(item_id, action, created_at) VALUES (?,?,?)",
                 (second, "unread", "2026-01-02T00:00:02"))
    conn.execute("INSERT INTO score(item_id, final_score, ranked_at) VALUES (?,?,?)",
                 (second, 88, "2026-01-02"))
    conn.execute(
        """INSERT INTO summary(item_id, problem, depth, model, abstract_hash, created_at)
           VALUES (?,?,?,?,?,?)""",
        (second, "problem", "deep", "model", "hash-b", "2026-01-02"),
    )
    conn.execute("INSERT INTO item_enrichment(item_id, crossref_json) VALUES (?,?)",
                 (first, '{"source":"crossref"}'))
    conn.execute("INSERT INTO journal_rank(journal_norm, journal, ranks_json, hit) "
                 "VALUES ('old', 'Old', '{}', 0)")
    conn.execute("PRAGMA user_version=3")
    db.Database._migrate(conn)
    conn.commit()

    assert conn.execute("SELECT COUNT(*) FROM item").fetchone()[0] == 1
    item = conn.execute("SELECT doi, dedup_key, abstract FROM item").fetchone()
    assert tuple(item) == ("10.9/abc", "doi:10.9/abc", "longer abstract")
    assert conn.execute("SELECT final_score FROM score").fetchone()[0] == 88
    assert conn.execute("SELECT abstract_hash FROM summary").fetchone()[0] == "hash-b"
    assert conn.execute("SELECT crossref_json FROM item_enrichment").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 6
    # 最新 unstar/unignore/unread 生效；旧副本的 excluded 仍保留。
    assert tuple(conn.execute("SELECT starred, ignored, excluded, state FROM item_state").fetchone()) == \
        (0, 0, 1, "new")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("SELECT COUNT(*) FROM item_fts WHERE item_fts MATCH 'longer'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM journal_rank").fetchone()[0] == 0
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    # v4 的历史清理只执行一次；新写入的负缓存交给新版查询策略处理。
    conn.execute("INSERT INTO journal_rank(journal_norm, journal, ranks_json, hit) "
                 "VALUES ('new', 'New', '{}', 0)")
    db.Database._migrate(conn)
    assert conn.execute("SELECT COUNT(*) FROM journal_rank").fetchone()[0] == 1
    conn.close()


def test_DOI迁移保留不同标识符的末尾标点(tmp_path):
    conn = db.Database(tmp_path / "suffix.db").connect()
    suffixes = ["ABC", "ABC)", "ABC.", "ABC;", "ABC(D)"]
    for suffix in suffixes:
        for variant in (suffix, suffix.lower()):
            doi = f"10.1234/{variant}"
            conn.execute(
                "INSERT INTO item(kind, dedup_key, doi, title, title_norm, source, "
                "created_at, updated_at) VALUES ('paper', ?, ?, 'Paper', 'paper', "
                "'test', '2026-01-01', '2026-01-01')", (f"doi:{doi}", doi),
            )
    conn.execute("PRAGMA user_version=3")
    db.Database._migrate(conn)
    conn.commit()
    assert {r[0] for r in conn.execute("SELECT doi FROM item")} == {
        f"10.1234/{s.lower()}" for s in suffixes}
    assert conn.execute("SELECT COUNT(*) FROM item").fetchone()[0] == len(suffixes)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()
