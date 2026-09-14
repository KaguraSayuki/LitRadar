"""数据库迁移的版本化。

以前每个进程第一次 connect() 都把全部迁移步骤跑一遍(全表扫 item 洗刊名、
三次 PRAGMA table_info、试探 score 表重建),每条 CLI 命令启动都白付这笔钱。
这里测三件事:老库能升上来、新库直接是最新版、最新版什么都不再做。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

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
    # SCHEMA 现在建的是 v5 的复合主键 score(item_id 不再是主键)。要模拟一个
    # v3 的库,得先把 score 换回旧结构,否则下面的历史写法会撞 NOT NULL。
    conn.executescript("""
        DROP TABLE score;
        CREATE TABLE score (
            item_id      INTEGER PRIMARY KEY REFERENCES item(id) ON DELETE CASCADE,
            rule_score   REAL, coarse_score REAL, llm_score REAL,
            llm_reason   TEXT, llm_model    TEXT,
            final_score  REAL, ranked_at    TEXT
        );
    """)
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
    # 最新 unstar/unignore/unread 生效；starred/state 仍是全局的。
    assert tuple(conn.execute("SELECT starred, ignored, excluded, state FROM item_state").fetchone()) == \
        (0, 0, 0, "new")
    # v5 起 ignored/excluded 按组存放:默认组里应保留旧副本的 excluded=1。
    # 旧的 item_state 那两列同时被清空,避免出现两份真相。
    assert tuple(conn.execute(
        "SELECT ignored, excluded FROM group_state").fetchone()) == (0, 1)
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


# ─────────────────────────────── v4 → v5:分组上线后老库不能丢也不能空
LEGACY_SCORE = """
DROP TABLE score;
CREATE TABLE score (
    item_id      INTEGER PRIMARY KEY REFERENCES item(id) ON DELETE CASCADE,
    rule_score   REAL, coarse_score REAL, llm_score REAL,
    llm_reason   TEXT, llm_model    TEXT,
    final_score  REAL, ranked_at    TEXT
);
"""


def test_v4库升级到分组后条目不丢也不空(tmp_path):
    """这是分组功能最关键的一条回归:升级后收件箱必须还是满的。

    老库没有 item_group,如果迁移不把既有条目归入默认组,收件箱会整个空掉
    —— 数据没丢,但用户看到的是"东西全没了"。
    """
    database = db.Database(tmp_path / "v4.db")
    conn = database.connect()
    conn.executescript(LEGACY_SCORE)          # 换回 v4 的 score 形态
    for n, (title, doi) in enumerate([("Kept", "10.1/kept"), ("Excluded", "10.1/ex")]):
        conn.execute(
            """INSERT INTO item(kind, dedup_key, doi, title, title_norm, source,
                                created_at, updated_at)
               VALUES ('paper', ?, ?, ?, ?, 'test', '2026-01-01', '2026-01-01')""",
            (f"doi:{doi}", doi, title, title.lower()))
    kept, excluded = [r[0] for r in conn.execute("SELECT id FROM item ORDER BY id")]
    conn.execute("INSERT INTO item_state(item_id, starred, ignored, excluded) VALUES (?,1,0,0)", (kept,))
    conn.execute("INSERT INTO item_state(item_id, starred, ignored, excluded) VALUES (?,0,1,1)", (excluded,))
    conn.execute("INSERT INTO score(item_id, final_score, llm_score, ranked_at) VALUES (?,?,?,?)",
                 (kept, 77.0, 60.0, "2026-01-02"))
    conn.execute("PRAGMA user_version=4")
    conn.commit()

    db.Database._migrate(conn)
    conn.commit()

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    # 1) 默认组存在,且所有历史条目都归入其中 —— 收件箱不会空
    default_id = db.group_id(conn, db.DEFAULT_GROUP_SLUG)
    assert default_id is not None
    assert conn.execute("SELECT COUNT(*) FROM item_group WHERE group_id=?",
                        (default_id,)).fetchone()[0] == 2
    # 2) 分值原值搬到 (默认组, item),没丢也没串组
    assert tuple(conn.execute("SELECT group_id, final_score, llm_score FROM score").fetchone()) == \
        (default_id, 77.0, 60.0)
    # 3) 全局的 ignored/excluded 搬进默认组的 group_state,并就地清空旧列
    assert tuple(conn.execute(
        "SELECT item_id, ignored, excluded FROM group_state").fetchone()) == (excluded, 1, 1)
    assert conn.execute(
        "SELECT COUNT(*) FROM item_state WHERE COALESCE(ignored,0)<>0 "
        "OR COALESCE(excluded,0)<>0").fetchone()[0] == 0
    # 4) star 保持全局,且收件箱里还剩那条没被排除的
    assert db.count_items(conn, state="starred") == 1
    visible = db.get_items(conn, group_slug=db.DEFAULT_GROUP_SLUG)
    assert [r["title"] for r in visible] == ["Kept"]
    assert visible[0]["final_score"] == 77.0
    # 5) 重复跑迁移必须幂等(每次 connect 都可能触发)
    db.Database._migrate(conn)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM item_group").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM group_state").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_v4库升级后能直接跑第二次迁移不会重复计分(tmp_path):
    """幂等性:迁移只跑一次由 user_version 保证,重复调用也不该产生重复行。"""
    database = db.Database(tmp_path / "twice.db")
    conn = database.connect()
    conn.executescript(LEGACY_SCORE)
    conn.execute(
        """INSERT INTO item(kind, dedup_key, doi, title, title_norm, source,
                            created_at, updated_at)
           VALUES ('paper','doi:10.2/a','10.2/a','A','a','test','2026-01-01','2026-01-01')""")
    conn.execute("INSERT INTO score(item_id, final_score) VALUES (1, 5.0)")
    conn.execute("PRAGMA user_version=4")
    conn.commit()

    for _ in range(3):
        db.Database._migrate(conn)
        conn.commit()

    assert conn.execute("SELECT COUNT(*) FROM item_group").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM score").fetchone()[0] == 1
    conn.close()


# 固定迁移涉及的 v6 旧表定义,不能借用当前 SCHEMA 的方向基线列。
LEGACY_GROUP = """
DROP TABLE interest_group;
CREATE TABLE interest_group (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    direction TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    llm_rank INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _legacy_relevance_db(path, *, version=6, source="legacy", direction="organic"):
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA)  # 其它表未变;迁移涉及的表在下面还原。
    conn.executescript(LEGACY_GROUP)
    if version == 4:
        conn.executescript(LEGACY_SCORE)
        conn.executescript("""
            DROP TABLE summary_group;
            DROP TABLE group_state;
            DROP TABLE item_group;
            DROP TABLE interest_group;
        """)
    else:
        for gid, slug in [(1, "default"), (2, "mat")]:
            conn.execute("""INSERT INTO interest_group
                (id,slug,name,direction,created_at,updated_at) VALUES (?,?,?,?,?,?)""",
                (gid, slug, slug, direction if gid == 1 else "materials", "old", "old"))
    conn.execute("""INSERT INTO item
        (id,kind,dedup_key,title,title_norm,abstract,published_at,source,created_at,updated_at)
        VALUES (1,'paper','doi:10.1/legacy','Legacy paper','legacy paper','Original abstract',
                '2000-01-01','test','2000-01-01','2000-01-01')""")
    conn.execute("INSERT INTO item_state(item_id) VALUES (1)")
    conn.execute("""INSERT INTO summary
        (item_id,one_liner,relevance,depth,model,created_at)
        VALUES (1,'Neutral result','Legacy default relevance','deep','old-model',NULL)""")
    if version == 6:
        for gid in ([2] if source == "nonmember" else [1, 2]):
            conn.execute("INSERT INTO item_group(group_id,item_id,first_seen) VALUES (?,1,'old')", (gid,))
        if source in ("mat", "default"):
            gid = 2 if source == "mat" else 1
            conn.execute("""INSERT INTO summary_group(group_id,item_id,relevance,model,created_at)
                VALUES (?,1,?,'new-model','new')""", (gid, f"Current {source} relevance"))
    conn.execute(f"PRAGMA user_version={version}")
    conn.commit()
    conn.close()


@pytest.mark.parametrize("source,expected_default", [
    ("legacy", "Legacy default relevance"),
    ("mat", None),
    ("default", "Current default relevance"),
    ("nonmember", None),
])
def test_v6迁移只将可信旧说明归入默认组(tmp_path, source, expected_default):
    path = tmp_path / "legacy.db"
    _legacy_relevance_db(path, source=source)
    conn = db.Database(path).connect()
    got = dict(conn.execute("""SELECT g.slug,sg.relevance FROM summary_group sg
        JOIN interest_group g ON g.id=sg.group_id"""))
    assert got.get("default") == expected_default
    assert got.get("mat") == ("Current mat relevance" if source == "mat" else None)
    assert tuple(conn.execute("SELECT one_liner,relevance,model FROM summary").fetchone()) == \
        ("Neutral result", None, "old-model")
    if source == "legacy":
        row = conn.execute("SELECT model,created_at FROM summary_group").fetchone()
        assert row[0] == "old-model" and row[1]  # 旧的 NULL 时间不阻止升级。
    snapshot = list(map(tuple, conn.execute("SELECT * FROM summary_group")))
    db.Database._migrate(conn)
    assert list(map(tuple, conn.execute("SELECT * FROM summary_group"))) == snapshot
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


@pytest.mark.parametrize("version", [4, 6])
def test_旧库首次同步方向保留窗口外说明而以后改方向会失效(tmp_path, version):
    from litradar.config import Config
    from litradar.rank import load_groups

    path = tmp_path / "first-sync.db"
    _legacy_relevance_db(path, version=version, direction="")
    conn = db.Database(path).connect()
    cfg = Config()
    cfg.interests_data = {"direction": "organic"}
    db.sync_groups(conn, load_groups(cfg))
    conn.commit()
    row = db.get_items(conn, group_slug="default")[0]
    assert row["relevance"] == "Legacy default relevance"
    assert row["published_at"] == "2000-01-01"  # 不靠摘要窗口内的 LLM 重算挽回。

    cfg.interests_data = {"direction": "enzymes"}
    db.sync_groups(conn, load_groups(cfg))
    assert db.get_items(conn, group_slug="default")[0]["relevance"] is None
    assert conn.execute("SELECT one_liner FROM summary").fetchone()[0] == "Neutral result"
    conn.close()


def test_v6已知方向升级后仍能检测方向变化(tmp_path):
    from litradar.config import Config
    from litradar.rank import load_groups

    path = tmp_path / "known-direction.db"
    _legacy_relevance_db(path, direction="organic")
    conn = db.Database(path).connect()
    cfg = Config()
    cfg.interests_data = {"direction": "enzymes"}
    db.sync_groups(conn, load_groups(cfg))
    assert db.get_items(conn, group_slug="default")[0]["relevance"] is None
    conn.close()
