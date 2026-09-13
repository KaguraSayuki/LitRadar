"""SQLite 数据层:连接、建表、常用读写。单用户场景,不引入 ORM。"""
from __future__ import annotations

import html
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .normalize import normalize_doi


def clean_journal(name: str | None) -> str | None:
    """把来源给的刊名洗干净。

    Crossref 的 container-title 有两个坑,实测都踩到了:
      · HTML 实体没解码 —— 存进来是 "Organic &amp; Biomolecular Chemistry"
      · 名字里带换行 —— "Journal of the American\\nChemical Society"
    显示上靠模板的 |unesc 和 HTML 折叠还能糊弄过去,但会污染一切按刊名做的
    功能:统计页按刊名分组时 JACS 裂成两行;按刊名查期刊分区(如 easyScholar)
    更是直接查不到。所以在入库这个唯一入口上洗干净。
    """
    if name is None:
        return None
    s = html.unescape(str(name))
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def _clean_url(url: Any) -> str | None:
    """外链只放行 http(s)。

    item.url / xmol_url / oa_url 直接进模板的 href,来源元数据里一条
    ``javascript:`` URL 就会变成可点的 XSS。在入库口统一挡掉,模板不用逐处防。
    """
    if not url:
        return None
    s = str(url).strip()
    if s.lower().startswith(("http://", "https://")):
        return s
    return None


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 原始邮件留档:解析器升级后可重跑
CREATE TABLE IF NOT EXISTS raw_email (
    id            INTEGER PRIMARY KEY,
    message_id    TEXT UNIQUE,
    received_at   TEXT,
    subject       TEXT,
    raw           BLOB NOT NULL,
    parsed_at     TEXT,
    processed_at  TEXT,              -- 与该邮件条目同一事务写入的完成标记
    parse_version INTEGER DEFAULT 0,
    items_found   INTEGER DEFAULT 0
);

-- 条目池。kind 目前只有 'paper' 一种取值 —— 专利源没有接,
-- 留着这个维度是为了将来真要接时不用改表结构。
CREATE TABLE IF NOT EXISTS item (
    id               INTEGER PRIMARY KEY,
    kind             TEXT NOT NULL DEFAULT 'paper' CHECK (kind IN ('paper','patent')),
    dedup_key        TEXT NOT NULL UNIQUE,
    doi              TEXT,
    title            TEXT NOT NULL,
    title_norm       TEXT NOT NULL,
    abstract         TEXT,
    authors          TEXT,          -- JSON array
    journal          TEXT,
    issn             TEXT,
    published_at     TEXT,          -- ISO8601
    url              TEXT,
    impact_factor    REAL,          -- X-MOL 邮件自带
    xmol_url         TEXT,
    matched_keywords TEXT,          -- JSON array,X-MOL 高亮命中的订阅词
    source           TEXT NOT NULL, -- 'xmol' | 'openalex' | 'crossref' | 'manual'
    source_ref       TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_item_pub     ON item(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_item_journal ON item(journal);
CREATE INDEX IF NOT EXISTS idx_item_tnorm   ON item(title_norm);

CREATE TABLE IF NOT EXISTS item_enrichment (
    item_id        INTEGER PRIMARY KEY REFERENCES item(id) ON DELETE CASCADE,
    cited_by_count INTEGER,
    is_oa          INTEGER,
    oa_url         TEXT,
    openalex_id    TEXT,
    openalex_json  TEXT,
    crossref_json  TEXT,
    enriched_at    TEXT,
    -- 富化过但没拿到摘要的次数。攒够上限就不再进"缺摘要重试"队列(见 enrich._pending),
    -- 否则来源里真没有摘要的条目会每轮都陪跑 S2 + Crossref 批量,永远烧请求。
    abstract_attempts INTEGER NOT NULL DEFAULT 0
);

-- 期刊等级缓存(easyScholar)。按刊名缓存,同一本刊只查一次 ——
-- 开放接口是按次计额的,36 本刊 36 次就够,之后全走本地。
CREATE TABLE IF NOT EXISTS journal_rank (
    journal_norm TEXT PRIMARY KEY,   -- 归一化后的刊名(小写、去空白)
    journal      TEXT,               -- 原始刊名,便于核对
    ranks_json   TEXT,               -- {"sci": "Q1", "sciUp": "化学1区", ...}
    source       TEXT DEFAULT 'easyscholar',
    fetched_at   TEXT,
    hit          INTEGER DEFAULT 1   -- 0 = 接口明确说查不到,别再反复问
);

-- 引用滚雪球的引用关系。**每次只刷新几个种子,关系累积在这里** ——
-- 一轮里连打十几个种子必然撞限流,而且部分失败会把共被引计数打散:
-- 只被那失败种子引用的论文就永远凑不够票。落表之后,共被引是在
-- 全部种子、全部历史轮次上统计的,单次失败不再影响判断。
CREATE TABLE IF NOT EXISTS seed_cite (
    group_id   INTEGER NOT NULL REFERENCES interest_group(id) ON DELETE CASCADE,
    seed_doi   TEXT NOT NULL,        -- 引用方引用了哪个种子
    citing_doi TEXT NOT NULL,        -- 引用方
    item_json  TEXT,                 -- 引用方元数据,凑够票时直接入库,不再查一次
    first_seen TEXT,
    PRIMARY KEY (group_id, seed_doi, citing_doi)
);

-- 每个种子上次刷新时间。挑最久没查的先查,轮着来。
CREATE TABLE IF NOT EXISTS seed_query (
    group_id   INTEGER NOT NULL REFERENCES interest_group(id) ON DELETE CASCADE,
    seed_doi   TEXT NOT NULL,
    queried_at TEXT,
    n_cites    INTEGER DEFAULT 0,
    status     TEXT,                 -- ok / failed
    PRIMARY KEY (group_id, seed_doi)
);

-- 订阅组:一个研究方向一组。slug 是稳定标识(改名/改方向不动它),id 在库内
-- 稳定,所有按组的表都用 group_id 关联。组本身来自 interests.yaml,
-- 每次加载时对账(sync_groups),所以删掉一个组不会连带删掉历史分值。
CREATE TABLE IF NOT EXISTS interest_group (
    id         INTEGER PRIMARY KEY,
    slug       TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL DEFAULT '',
    direction  TEXT NOT NULL DEFAULT '',
    enabled    INTEGER NOT NULL DEFAULT 1,
    llm_rank   INTEGER NOT NULL DEFAULT 1,   -- 本组是否跑 LLM 精排(花钱开关)
    position   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 这篇文献属于哪些组 —— 由采集时命中的查询决定,是"该组收件箱"的权威来源。
-- 同一篇可以被多个组命中(DOI 去重后只有一行 item)。
CREATE TABLE IF NOT EXISTS item_group (
    group_id   INTEGER NOT NULL REFERENCES interest_group(id) ON DELETE CASCADE,
    item_id    INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
    first_seen TEXT NOT NULL,
    PRIMARY KEY (group_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_item_group_item ON item_group(item_id);

-- 每组的"被规则排除 / 用户忽略"。**刻意与 item_state 分开**:
-- starred / state(已读) 是对"这篇文献"的判断,全局一份;
-- ignored / excluded 是对"它与某个方向的关系"的判断,按组各一份 ——
-- 同一篇完全可能对 A 组无关、对 B 组是核心。
CREATE TABLE IF NOT EXISTS group_state (
    group_id INTEGER NOT NULL REFERENCES interest_group(id) ON DELETE CASCADE,
    item_id  INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
    ignored  INTEGER NOT NULL DEFAULT 0,
    excluded INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, item_id)
);

-- 摘要里**唯一**与方向有关的一行(relevance = "对研究的用处")。其余字段
-- (problem / method / key_results / …)与方向无关,留在 summary 里共享 ——
-- 否则每多一个组就要把整条摘要重算一遍。
CREATE TABLE IF NOT EXISTS summary_group (
    group_id   INTEGER NOT NULL REFERENCES interest_group(id) ON DELETE CASCADE,
    item_id    INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
    relevance  TEXT,
    model      TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (group_id, item_id)
);

-- 分值按组存:同一篇在不同组里的规则 / BM25 / LLM 分可以完全不同。
CREATE TABLE IF NOT EXISTS score (
    group_id     INTEGER NOT NULL REFERENCES interest_group(id) ON DELETE CASCADE,
    item_id      INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
    rule_score   REAL,
    coarse_score REAL,
    llm_score    REAL,
    llm_reason   TEXT,
    llm_model    TEXT,
    final_score  REAL,
    ranked_at    TEXT,
    PRIMARY KEY (group_id, item_id)
);


CREATE TABLE IF NOT EXISTS summary (
    item_id     INTEGER PRIMARY KEY REFERENCES item(id) ON DELETE CASCADE,
    title_zh    TEXT,               -- 中文标题翻译
    one_liner   TEXT,
    problem     TEXT,
    method      TEXT,
    key_results TEXT,
    limitation  TEXT,
    relevance   TEXT,
    depth       TEXT,
    model       TEXT,
    abstract_hash TEXT,             -- 原文摘要版本指纹(摘要刷新判断)
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS item_state (
    item_id     INTEGER PRIMARY KEY REFERENCES item(id) ON DELETE CASCADE,
    state       TEXT NOT NULL DEFAULT 'new',  -- new | read | archived
    starred     INTEGER NOT NULL DEFAULT 0,
    ignored     INTEGER NOT NULL DEFAULT 0,
    -- 被规则过滤掉的条目标记为 1,收件箱不再展示。
    -- 实测教训:这些条目原来只是"没有分数",仍会排在列表末尾逼用户手动忽略 ——
    -- 139 条忽略反馈里有 119 条属于这种,纯属浪费用户时间。
    excluded    INTEGER NOT NULL DEFAULT 0,
    notified_at TEXT
);

CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY,
    item_id    INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
    action     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_item ON feedback(item_id);

CREATE TABLE IF NOT EXISTS run_log (
    id          INTEGER PRIMARY KEY,
    stage       TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT,
    stats       TEXT,
    error       TEXT,
    group_slug  TEXT              -- 按组计花费护栏的账本(空 = 全部组)
);

-- 全文检索
CREATE VIRTUAL TABLE IF NOT EXISTS item_fts USING fts5(
    title, abstract, journal, authors,
    content='item', content_rowid='id', tokenize='unicode61'
);
"""

FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS item_ai AFTER INSERT ON item BEGIN
  INSERT INTO item_fts(rowid,title,abstract,journal,authors)
  VALUES (new.id,new.title,COALESCE(new.abstract,''),COALESCE(new.journal,''),COALESCE(new.authors,''));
END;
CREATE TRIGGER IF NOT EXISTS item_ad AFTER DELETE ON item BEGIN
  INSERT INTO item_fts(item_fts,rowid,title,abstract,journal,authors)
  VALUES ('delete',old.id,old.title,COALESCE(old.abstract,''),COALESCE(old.journal,''),COALESCE(old.authors,''));
END;
CREATE TRIGGER IF NOT EXISTS item_au AFTER UPDATE ON item BEGIN
  INSERT INTO item_fts(item_fts,rowid,title,abstract,journal,authors)
  VALUES ('delete',old.id,old.title,COALESCE(old.abstract,''),COALESCE(old.journal,''),COALESCE(old.authors,''));
  INSERT INTO item_fts(rowid,title,abstract,journal,authors)
  VALUES (new.id,new.title,COALESCE(new.abstract,''),COALESCE(new.journal,''),COALESCE(new.authors,''));
END;
"""


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ------------------------------------------------------------------ 迁移
def _migrate_v1(conn: sqlite3.Connection) -> None:
    """版本化之前积累的全部历史步骤。每步先探测再动手(幂等):
    老库缺的表已经由 SCHEMA 按最新结构建好,不能对它们再加一次列。"""
    import contextlib

    # journal:洗掉历史遗留的 HTML 实体与换行(见 clean_journal)
    for rid, j in conn.execute(
            "SELECT id, journal FROM item WHERE journal IS NOT NULL").fetchall():
        cj = clean_journal(j)
        if cj != j:
            conn.execute("UPDATE item SET journal=? WHERE id=?", (cj, rid))

    # summary.title_zh
    cols = {r[1] for r in conn.execute("PRAGMA table_info(summary)")}
    if cols and "title_zh" not in cols:
        conn.execute("ALTER TABLE summary ADD COLUMN title_zh TEXT")

    # item_state.excluded:规则过滤标记
    cols = {r[1] for r in conn.execute("PRAGMA table_info(item_state)")}
    if cols and "excluded" not in cols:
        conn.execute("ALTER TABLE item_state ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0")

    # score:去掉 profile 列(单用户,这个维度是过度设计)。
    # SQLite 改主键要重建表,所以走 建新表 -> 拷数据 -> 换名。
    cols = {r[1] for r in conn.execute("PRAGMA table_info(score)")}
    if "profile" in cols:
        with contextlib.suppress(sqlite3.OperationalError):
            conn.executescript("""
                CREATE TABLE score_migrated (
                    item_id      INTEGER PRIMARY KEY
                                 REFERENCES item(id) ON DELETE CASCADE,
                    rule_score   REAL, coarse_score REAL, llm_score REAL,
                    llm_reason   TEXT, llm_model    TEXT,
                    final_score  REAL, ranked_at    TEXT
                );
                INSERT OR REPLACE INTO score_migrated
                    SELECT item_id, rule_score, coarse_score, llm_score,
                           llm_reason, llm_model, final_score, ranked_at
                    FROM score;
                DROP TABLE score;
                ALTER TABLE score_migrated RENAME TO score;
            """)


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """item_enrichment.abstract_attempts:缺摘要重试的计数。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(item_enrichment)")}
    if cols and "abstract_attempts" not in cols:
        conn.execute("ALTER TABLE item_enrichment "
                     "ADD COLUMN abstract_attempts INTEGER NOT NULL DEFAULT 0")


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """增加摘要原文指纹及邮件完成标记，无法可靠回填的历史值保持 NULL。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(summary)")}
    if cols and "abstract_hash" not in cols:
        conn.execute("ALTER TABLE summary ADD COLUMN abstract_hash TEXT")
    raw_cols = {r[1] for r in conn.execute("PRAGMA table_info(raw_email)")}
    if raw_cols and "processed_at" not in raw_cols:
        conn.execute("ALTER TABLE raw_email ADD COLUMN processed_at TEXT")


def _present(value: Any) -> bool:
    """判断一个数据库值是否实际提供了元数据。"""
    return value not in (None, "", "[]", "{}")


def _merge_item_state(conn: sqlite3.Connection, keep: int, drop: int) -> None:
    keep_row = conn.execute(
        "SELECT state, starred, ignored, excluded, notified_at "
        "FROM item_state WHERE item_id = ?", (keep,)).fetchone()
    drop_row = conn.execute(
        "SELECT state, starred, ignored, excluded, notified_at "
        "FROM item_state WHERE item_id = ?", (drop,)).fetchone()
    if keep_row is None:
        conn.execute("INSERT OR IGNORE INTO item_state (item_id) VALUES (?)", (keep,))
        keep_row = conn.execute(
            "SELECT state, starred, ignored, excluded, notified_at "
            "FROM item_state WHERE item_id = ?", (keep,)).fetchone()
    if drop_row is None:
        # The old database could contain items inserted directly without the
        # companion state row; treat that side as the schema defaults while
        # still applying any feedback events below.
        drop_row = {"state": "new", "starred": 0, "ignored": 0,
                    "excluded": 0, "notified_at": None}

    # 用户反馈是事件流。若同一轴上有明确的最新操作，应按时间决定最终
    # 状态；否则才对两条旧 item_state 做保守并集。这样 duplicate 上的
    # unstar/unignore/unread 不会被另一个旧副本的 star/ignore/read 抵消。
    def latest(actions: tuple[str, ...]):
        return conn.execute(
            """SELECT action FROM feedback
               WHERE item_id IN (?,?) AND action IN ({})
               ORDER BY created_at DESC, id DESC LIMIT 1""".format(
                   ",".join("?" for _ in actions)),
            (keep, drop, *actions),
        ).fetchone()

    star_event = latest(("star", "unstar"))
    ignore_event = latest(("ignore", "unignore"))
    state_event = latest(("read", "unread", "archive"))

    # 无事件时取较进阶状态，避免迁移把已有状态降级。
    state_rank = {"new": 0, "read": 1, "archived": 2}
    states = [keep_row["state"], drop_row["state"]]
    if state_event:
        state = {"read": "read", "unread": "new", "archive": "archived"}[state_event["action"]]
    else:
        state = max(states, key=lambda s: state_rank.get(s, -1))
    if star_event:
        starred = int(star_event["action"] == "star")
    else:
        starred = int(bool(keep_row["starred"] or drop_row["starred"]))
    if ignore_event:
        ignored = int(ignore_event["action"] == "ignore")
    else:
        ignored = int(bool(keep_row["ignored"] or drop_row["ignored"]))
    # excluded 是规则结果，不应遮住用户明确收藏的条目；收藏迁移后给它
    # 让路，下一轮规则可按最新 profile 重新计算。
    excluded = int(bool(keep_row["excluded"] or drop_row["excluded"]))
    if starred:
        excluded = 0
    notified = max((v for v in (keep_row["notified_at"], drop_row["notified_at"]) if v),
                   default=None)
    conn.execute(
        """UPDATE item_state SET state=?, starred=?, ignored=?, excluded=?, notified_at=?
           WHERE item_id=?""",
        (state, starred, ignored, excluded, notified, keep),
    )
    conn.execute("DELETE FROM item_state WHERE item_id=?", (drop,))


def _merge_score(conn: sqlite3.Connection, keep: int, drop: int) -> None:
    keep_row = conn.execute("SELECT * FROM score WHERE item_id=?", (keep,)).fetchone()
    drop_row = conn.execute("SELECT * FROM score WHERE item_id=?", (drop,)).fetchone()
    if keep_row is None and drop_row is not None:
        conn.execute("UPDATE score SET item_id=? WHERE item_id=?", (keep, drop))
        return
    if keep_row is None or drop_row is None:
        return

    # 对两份历史排序结果保留最新的一份；同一时刻则保留最终分更高者。
    # 这只合并同一 DOI 的记录，不会影响正常的精排结果。
    kr = keep_row["ranked_at"] or ""
    dr = drop_row["ranked_at"] or ""
    kscore = keep_row["final_score"]
    dscore = drop_row["final_score"]
    if (dr, dscore if dscore is not None else float("-inf")) > \
            (kr, kscore if kscore is not None else float("-inf")):
        source = drop_row
    else:
        source = keep_row
    conn.execute(
        """UPDATE score SET rule_score=?, coarse_score=?, llm_score=?, llm_reason=?,
           llm_model=?, final_score=?, ranked_at=? WHERE item_id=?""",
        (source["rule_score"], source["coarse_score"], source["llm_score"],
         source["llm_reason"], source["llm_model"], source["final_score"],
         source["ranked_at"], keep),
    )
    conn.execute("DELETE FROM score WHERE item_id=?", (drop,))


def _merge_summary(conn: sqlite3.Connection, keep: int, drop: int) -> None:
    columns = ("title_zh", "one_liner", "problem", "method", "key_results",
               "limitation", "relevance", "depth", "model", "abstract_hash",
               "created_at")
    keep_row = conn.execute(
        "SELECT " + ",".join(columns) + " FROM summary WHERE item_id=?", (keep,)
    ).fetchone()
    drop_row = conn.execute(
        "SELECT " + ",".join(columns) + " FROM summary WHERE item_id=?", (drop,)
    ).fetchone()
    if keep_row is None and drop_row is not None:
        conn.execute("UPDATE summary SET item_id=? WHERE item_id=?", (keep, drop))
        return
    if keep_row is None or drop_row is None:
        return

    depth_rank = {"brief": 1, "deep": 2}
    kd, dd = keep_row["depth"], drop_row["depth"]
    if (depth_rank.get(dd, 0), drop_row["created_at"] or "") > \
            (depth_rank.get(kd, 0), keep_row["created_at"] or ""):
        source = drop_row
    else:
        source = keep_row
    # source 的 abstract_hash 必须跟着所选摘要一起保留；历史摘要为 NULL
    # 时不猜测其原文版本。
    conn.execute(
        """UPDATE summary SET title_zh=?, one_liner=?, problem=?, method=?,
           key_results=?, limitation=?, relevance=?, depth=?, model=?,
           abstract_hash=?, created_at=? WHERE item_id=?""",
        tuple(source[c] for c in columns) + (keep,),
    )
    conn.execute("DELETE FROM summary WHERE item_id=?", (drop,))


def _merge_enrichment(conn: sqlite3.Connection, keep: int, drop: int) -> None:
    columns = ("cited_by_count", "is_oa", "oa_url", "openalex_id",
               "openalex_json", "crossref_json", "enriched_at", "abstract_attempts")
    keep_row = conn.execute(
        "SELECT " + ",".join(columns) + " FROM item_enrichment WHERE item_id=?", (keep,)
    ).fetchone()
    drop_row = conn.execute(
        "SELECT " + ",".join(columns) + " FROM item_enrichment WHERE item_id=?", (drop,)
    ).fetchone()
    if keep_row is None and drop_row is not None:
        conn.execute("UPDATE item_enrichment SET item_id=? WHERE item_id=?", (keep, drop))
        return
    if keep_row is None or drop_row is None:
        return

    # 富化列按“已有值优先”，计数取较新的/较大的可用值，重试次数取最大值。
    merged = {}
    for c in columns:
        old, new = keep_row[c], drop_row[c]
        if c == "abstract_attempts":
            merged[c] = max(int(old or 0), int(new or 0))
        elif c == "cited_by_count":
            merged[c] = max((v for v in (old, new) if v is not None), default=None)
        elif c == "enriched_at":
            merged[c] = max((v for v in (old, new) if v), default=None)
        elif _present(old):
            merged[c] = old
        else:
            merged[c] = new
    conn.execute(
        "UPDATE item_enrichment SET " + ", ".join(f"{c}=?" for c in columns) +
        " WHERE item_id=?",
        tuple(merged[c] for c in columns) + (keep,),
    )
    conn.execute("DELETE FROM item_enrichment WHERE item_id=?", (drop,))


def _item_priority(conn: sqlite3.Connection, row: sqlite3.Row) -> tuple:
    state = conn.execute(
        "SELECT starred, ignored FROM item_state WHERE item_id=?", (row["id"],)
    ).fetchone()
    feedback = conn.execute(
        "SELECT COUNT(*) FROM feedback WHERE item_id=?", (row["id"],)
    ).fetchone()[0]
    values = sum(_present(row[c]) for c in (
        "abstract", "authors", "journal", "issn", "published_at", "url",
        "xmol_url", "matched_keywords", "source_ref"))
    # 用户明确操作过的条目优先；其余默认保留较早的 id，便于稳定迁移。
    return (-int(bool(state and state["starred"])),
            -int(feedback),
            -int(bool(state and state["ignored"])),
            -values, int(row["id"]))


def _merge_duplicate_items(conn: sqlite3.Connection) -> None:
    """把历史上仅大小写不同的 DOI 条目合并到一个 canonical key。"""
    candidates = conn.execute(
        """SELECT * FROM item
           WHERE (doi IS NOT NULL AND doi <> '')
              OR lower(dedup_key) LIKE 'doi:%'
           ORDER BY id"""
    ).fetchall()
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in candidates:
        doi = normalize_doi(row["doi"]) or normalize_doi(row["dedup_key"])
        if doi:
            groups.setdefault(doi, []).append(row)

    item_columns = ("kind", "title", "title_norm", "abstract", "authors", "journal",
                    "issn", "published_at", "url", "impact_factor", "xmol_url",
                    "matched_keywords", "source", "source_ref", "created_at", "updated_at")
    for doi, rows in groups.items():
        loaded = [conn.execute("SELECT * FROM item WHERE id=?", (r["id"],)).fetchone()
                  for r in rows]
        loaded = [r for r in loaded if r is not None]
        if not loaded:
            continue
        keep_row = min(loaded, key=lambda r: _item_priority(conn, r))
        keep = int(keep_row["id"])

        for row in loaded:
            drop = int(row["id"])
            if drop == keep:
                continue
            # 先补齐 item 的空元数据；摘要优先保留更完整的一份。
            current = conn.execute("SELECT * FROM item WHERE id=?", (keep,)).fetchone()
            updates, values = [], []
            for c in item_columns:
                old, new = current[c], row[c]
                if c == "abstract" and _present(new) and \
                        (not _present(old) or len(str(new)) > len(str(old))):
                    updates.append(f"{c}=?")
                    values.append(new)
                elif not _present(old) and _present(new):
                    updates.append(f"{c}=?")
                    values.append(new)
            if updates:
                updates.append("updated_at=?")
                values.extend([now(), keep])
                conn.execute("UPDATE item SET " + ",".join(updates) + " WHERE id=?", values)

            _merge_item_state(conn, keep, drop)
            _merge_score(conn, keep, drop)
            _merge_summary(conn, keep, drop)
            _merge_enrichment(conn, keep, drop)
            conn.execute("UPDATE feedback SET item_id=? WHERE item_id=?", (keep, drop))
            conn.execute("DELETE FROM item WHERE id=?", (drop,))

        # Do this after deleting all duplicate rows so the UNIQUE constraint on
        # dedup_key cannot transiently collide with one of them.
        conn.execute("UPDATE item SET doi=?, dedup_key=? WHERE id=?",
                     (doi, f"doi:{doi}", keep))


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """统一历史 DOI，并合并重复记录及其关联数据。"""
    _merge_duplicate_items(conn)
    # 旧版本把网络/密钥临时失败也写成 hit=0，无法从缓存区分“明确无
    # 结果”。新版接口仍会缓存真正的空结果，但历史负缓存统一重查一次。
    conn.execute("DELETE FROM journal_rank WHERE hit=0")


def _migrate_v5(conn: sqlite3.Connection) -> None:
    """订阅分组:分值按组存,并补齐成员关系与组级状态。

    老库必须**无损且不空**:所有既有条目归入默认组,既有的 excluded/ignored
    搬进该组的 group_state,score 原值搬到 (默认组, item)。否则升级后收件箱
    会因为"没有成员关系"而整个空掉。
    """
    default_id = ensure_group(conn, DEFAULT_GROUP_SLUG, name=DEFAULT_GROUP_NAME)

    run_cols = {r[1] for r in conn.execute("PRAGMA table_info(run_log)")}
    if run_cols and "group_slug" not in run_cols:
        conn.execute("ALTER TABLE run_log ADD COLUMN group_slug TEXT")

    stamp = now()
    conn.execute(
        """INSERT OR IGNORE INTO item_group (group_id, item_id, first_seen)
           SELECT ?, id, COALESCE(created_at, ?) FROM item""",
        (default_id, stamp),
    )

    # 组级状态:老库只有全局一份,原样搬给默认组。
    # 搬完**就地清空**这两列 —— item_state.ignored/excluded 从 v5 起是废弃
    # 列,留着会和 group_state 变成两份真相(set_action 之后只更新 group_state,
    # 旧列会停在迁移那一刻的值,读它的人看到的是错的而不是空的)。
    state_cols = {r[1] for r in conn.execute("PRAGMA table_info(item_state)")}
    if {"ignored", "excluded"} <= state_cols:
        # JOIN item:老库里可能残留 item 已删、item_state 还在的孤儿行,直接搬
        # 会连孤儿一起复制进 group_state,凭空造出新的外键违规。
        conn.execute(
            """INSERT OR IGNORE INTO group_state (group_id, item_id, ignored, excluded)
               SELECT ?, s.item_id, COALESCE(s.ignored,0), COALESCE(s.excluded,0)
                 FROM item_state s
                 JOIN item i ON i.id = s.item_id
                WHERE COALESCE(s.ignored,0) <> 0 OR COALESCE(s.excluded,0) <> 0""",
            (default_id,),
        )
        conn.execute(
            """UPDATE item_state SET ignored=0, excluded=0
                WHERE COALESCE(ignored,0) <> 0 OR COALESCE(excluded,0) <> 0""")

    # score 改复合主键。SQLite 改主键只能重建表,走 建新表 → 拷数据 → 换名。
    score_cols = {r[1] for r in conn.execute("PRAGMA table_info(score)")}
    if score_cols and "group_id" not in score_cols:
        conn.executescript(f"""
            CREATE TABLE score_v5 (
                group_id     INTEGER NOT NULL
                             REFERENCES interest_group(id) ON DELETE CASCADE,
                item_id      INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
                rule_score   REAL, coarse_score REAL, llm_score REAL,
                llm_reason   TEXT, llm_model    TEXT,
                final_score  REAL, ranked_at    TEXT,
                PRIMARY KEY (group_id, item_id)
            );
            INSERT INTO score_v5
                (group_id, item_id, rule_score, coarse_score, llm_score,
                 llm_reason, llm_model, final_score, ranked_at)
                SELECT {default_id}, item_id, rule_score, coarse_score, llm_score,
                       llm_reason, llm_model, final_score, ranked_at
                  FROM score;
            DROP TABLE score;
            ALTER TABLE score_v5 RENAME TO score;
        """)


def ensure_group(conn: sqlite3.Connection, slug: str, *, name: str = "",
                 direction: str = "", enabled: bool = True,
                 llm_rank: bool = True, position: int = 0) -> int:
    """按 slug 取组,不存在就建。返回稳定的 group_id。"""
    slug = (slug or DEFAULT_GROUP_SLUG).strip() or DEFAULT_GROUP_SLUG
    row = conn.execute("SELECT id FROM interest_group WHERE slug=?", (slug,)).fetchone()
    if row:
        return int(row["id"])
    stamp = now()
    cur = conn.execute(
        """INSERT INTO interest_group
           (slug, name, direction, enabled, llm_rank, position, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (slug, name or slug, direction, int(bool(enabled)), int(bool(llm_rank)),
         int(position), stamp, stamp),
    )
    return int(cur.lastrowid)


def sync_groups(conn: sqlite3.Connection, groups) -> dict[str, int]:
    """把 interests.yaml 里的组对账进库,返回 {slug: group_id}。

    只增改不删除:删掉一个组不该连带删掉它的历史分值(想清干净就自己
    删库里的行)。这样"临时注释掉一个组"是可逆的。
    """
    mapping: dict[str, int] = {}
    for position, group in enumerate(groups):
        gid = ensure_group(
            conn, group.slug, name=group.name, direction=group.direction,
            enabled=group.enabled, llm_rank=group.llm_rank, position=position,
        )
        conn.execute(
            """UPDATE interest_group
                  SET name=?, direction=?, enabled=?, llm_rank=?, position=?, updated_at=?
                WHERE id=?""",
            (group.name or group.slug, group.direction, int(bool(group.enabled)),
             int(bool(group.llm_rank)), position, now(), gid),
        )
        mapping[group.slug] = gid
    return mapping


def groups(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """库里已知的组,按展示顺序。"""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM interest_group ORDER BY position, id")]


def group_id(conn: sqlite3.Connection, slug: str | None) -> int | None:
    """slug → id;查不到返回 None(调用方自己决定是建还是忽略)。"""
    row = conn.execute("SELECT id FROM interest_group WHERE slug=?",
                       ((slug or DEFAULT_GROUP_SLUG),)).fetchone()
    return int(row["id"]) if row else None


def _migrate_v6(conn: sqlite3.Connection) -> None:
    """滚雪球的引用关系按组存放。

    以前 seed_cite / seed_query 是全局的:两个方向各自的种子混在一起统计
    共被引,于是"A 方向的候选"会被算成"也被 B 的种子引过",票数虚高。
    老数据全部归默认组 —— 它们本来就来自那份单方向配置。
    """
    default_id = ensure_group(conn, DEFAULT_GROUP_SLUG, name=DEFAULT_GROUP_NAME)

    cols = {r[1] for r in conn.execute("PRAGMA table_info(seed_cite)")}
    if cols and "group_id" not in cols:
        conn.executescript(f"""
            CREATE TABLE seed_cite_v6 (
                group_id   INTEGER NOT NULL
                           REFERENCES interest_group(id) ON DELETE CASCADE,
                seed_doi   TEXT NOT NULL,
                citing_doi TEXT NOT NULL,
                item_json  TEXT,
                first_seen TEXT,
                PRIMARY KEY (group_id, seed_doi, citing_doi)
            );
            INSERT INTO seed_cite_v6
                (group_id, seed_doi, citing_doi, item_json, first_seen)
                SELECT {default_id}, seed_doi, citing_doi, item_json, first_seen
                  FROM seed_cite;
            DROP TABLE seed_cite;
            ALTER TABLE seed_cite_v6 RENAME TO seed_cite;
        """)

    cols = {r[1] for r in conn.execute("PRAGMA table_info(seed_query)")}
    if cols and "group_id" not in cols:
        conn.executescript(f"""
            CREATE TABLE seed_query_v6 (
                group_id   INTEGER NOT NULL
                           REFERENCES interest_group(id) ON DELETE CASCADE,
                seed_doi   TEXT NOT NULL,
                queried_at TEXT,
                n_cites    INTEGER DEFAULT 0,
                status     TEXT,
                PRIMARY KEY (group_id, seed_doi)
            );
            INSERT INTO seed_query_v6
                (group_id, seed_doi, queried_at, n_cites, status)
                SELECT {default_id}, seed_doi, queried_at, n_cites, status
                  FROM seed_query;
            DROP TABLE seed_query;
            ALTER TABLE seed_query_v6 RENAME TO seed_query;
        """)


# 迁移步骤按版本排列:下标 + 1 = 跑完这步之后的 user_version。
# 加新迁移就在末尾追加一个函数,**同时把 SCHEMA 改成最新结构** ——
# 新库只建 SCHEMA、不走这里。
_MIGRATIONS = [_migrate_v1, _migrate_v2, _migrate_v3, _migrate_v4, _migrate_v5,
               _migrate_v6]
SCHEMA_VERSION = len(_MIGRATIONS)

# 只能在迁移之后建的索引(见 Database.init 的注释)。
POST_MIGRATION_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_score_final ON score(group_id, final_score DESC);
CREATE INDEX IF NOT EXISTS idx_seed_cite_citing ON seed_cite(group_id, citing_doi);
"""

# 老库升级时,所有历史条目都归到这个组名下 —— 这样收件箱不会因为分组功能
# 上线而突然变空。旧的 interests.yaml(没有 groups:)也映射到这个 slug。
DEFAULT_GROUP_SLUG = "default"
DEFAULT_GROUP_NAME = "默认订阅组"

# 已经跑过建表 + 迁移的库路径(进程级)。见 connect()
_READY: set[str] = set()


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        # 每个库在每个进程里自动建表 + 跑一次迁移。
        #
        # 以前只有 `litradar init-db` 会走 init(),于是任何新增的表/字段在
        # 别的命令里都是 "no such table" —— 实测加 journal_rank 表时,
        # `enrich` 直接报错,而且那次连 journal 清洗迁移也没跑过。
        # 让 connect() 兜住这件事,省掉"记得先 init-db"这个隐性前提。
        key = str(self.path)
        if key not in _READY:
            _READY.add(key)                # 先标记:init() 内部会回调 connect()
            try:
                self.init()
            except Exception:
                _READY.discard(key)        # 失败就允许下次重试
                raise
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        conn = self.connect()
        try:
            yield conn.cursor()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self) -> None:
        conn = self.connect()
        try:
            # 全新库(还没有 item 表)由 SCHEMA 建出来就是最新结构:直接打上
            # 最新版本号,历史迁移一步都不用跑。老库才按缺的版本逐步补。
            fresh = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='item'"
            ).fetchone()[0] == 0
            conn.executescript(SCHEMA)
            conn.executescript(FTS_TRIGGERS)
            if fresh:
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            else:
                self._migrate(conn)
            # 依赖"迁移后结构"的索引必须在这里建:SCHEMA 先于迁移执行,那时
            # 老库的 score 表还没有 group_id,把索引写进 SCHEMA 会让老库直接
            # 启动失败(executescript 报 no such column)。
            conn.executescript(POST_MIGRATION_INDEXES)
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """已存在的库就地升级，执行各版本的数据修复并保留关联记录。

        用 ``PRAGMA user_version`` 记着"已经升到哪一版",只补跑缺的步骤。
        以前每个进程第一次 connect() 都把全部步骤跑一遍 —— 全表扫 item 洗刊名、
        三次 PRAGMA table_info、试探 score 表重建 —— 每条 CLI 命令启动都白付
        这笔钱,库越大越慢。现在已是最新版就一步不跑。
        """
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version >= SCHEMA_VERSION:
            return
        for target, step in enumerate(_MIGRATIONS, start=1):
            if version < target:
                step(conn)
                # 每一步各自盖章:步骤里的 executescript 会先提交前面的语句,
                # 中途失败也不会让做完的步骤下次重跑
                conn.execute(f"PRAGMA user_version = {target}")


# ---------------------------------------------------------------- item 读写
ITEM_FIELDS = (
    "kind", "dedup_key", "doi", "title", "title_norm", "abstract", "authors",
    "journal", "issn", "published_at", "url", "impact_factor", "xmol_url",
    "matched_keywords", "source", "source_ref",
)


def upsert_item(conn: sqlite3.Connection, data: dict[str, Any]) -> tuple[int, bool]:
    """按 dedup_key 插入或补全。返回 (item_id, 是否新建)。

    已存在的条目只补空字段,不覆盖已有内容(避免低质量源盖掉高质量源)。

    **这里不管订阅组的成员关系。** 入库但没记 ``item_group`` 的条目不会出现在
    任何组的收件箱与排序里(排序与列表都只认成员)。采集路径统一走
    ``pipeline._store(group_id=...)``;手动插数据时记得自己调 ``add_to_group``。
    """
    # ``upsert_item`` 也是若干来源和管理脚本直接使用的共同边界，不能只
    # 依赖 pipeline._prepare。DOI 与 dedup_key 必须始终使用同一个 canonical
    # 小写值，避免一个来源写 ``doi:10.../ABC``、另一个写 ``.../abc``。
    data = dict(data)
    doi = normalize_doi(data.get("doi"))
    if doi:
        data["doi"] = doi
        data["dedup_key"] = f"doi:{doi}"
    elif isinstance(data.get("dedup_key"), str) \
            and data["dedup_key"].lower().startswith("doi:"):
        doi = normalize_doi(data["dedup_key"])
        if doi:
            data["doi"] = doi
            data["dedup_key"] = f"doi:{doi}"

    row = conn.execute(
        "SELECT * FROM item WHERE dedup_key = ?", (data["dedup_key"],)
    ).fetchone()

    payload = {k: data.get(k) for k in ITEM_FIELDS}
    # 刊名在入库这个唯一入口统一洗干净(HTML 实体 / 换行),见 clean_journal
    payload["journal"] = clean_journal(payload.get("journal"))
    for k in ("url", "xmol_url"):
        payload[k] = _clean_url(payload.get(k))
    for k in ("authors", "matched_keywords"):
        if isinstance(payload.get(k), (list, tuple)):
            payload[k] = json.dumps(list(payload[k]), ensure_ascii=False)

    if row is None:
        payload["created_at"] = payload["updated_at"] = now()
        cols = ", ".join(payload)
        ph = ", ".join("?" for _ in payload)
        cur = conn.execute(
            f"INSERT INTO item ({cols}) VALUES ({ph})", list(payload.values())
        )
        iid = int(cur.lastrowid)
        conn.execute("INSERT OR IGNORE INTO item_state (item_id) VALUES (?)", (iid,))
        return iid, True

    iid = int(row["id"])
    updates, values = [], []
    for k, v in payload.items():
        if v in (None, "", [], "[]"):
            continue
        if row[k] in (None, "", "[]"):
            updates.append(f"{k} = ?")
            values.append(v)

    # published_at 特例:允许更精确的日期覆盖"只有年份"的占位值
    # (Crossref 有些条目 issued 只给年份,早先会写成 YYYY-01-01,
    #  导致这些其实很新的论文被时间窗误伤)
    new_pub, old_pub = payload.get("published_at"), row["published_at"]
    if new_pub and old_pub and new_pub != old_pub:
        if old_pub.endswith("-01-01") and not new_pub.endswith("-01-01"):
            if "published_at = ?" not in updates:
                updates.append("published_at = ?")
                values.append(new_pub)

    if updates:
        updates.append("updated_at = ?")
        values.extend([now(), iid])
        conn.execute(f"UPDATE item SET {', '.join(updates)} WHERE id = ?", values)
    conn.execute("INSERT OR IGNORE INTO item_state (item_id) VALUES (?)", (iid,))
    return iid, False


def in_window(alias: str = "i") -> str:
    """时间窗条件,占用**两个** ``?`` 参数(同一个值传两遍,形如
    ``("-200 days", "-200 days")``),自带括号。

    除了按字面日期比较,还要捞回两类条目:

    1. "只知道年份"的:入库时写成 ``YYYY-01-01`` 占位(见 `upsert_item` 里对
       published_at 的特例处理),按字面比会被窗口起点切掉 —— 明明是今年的
       论文,却永远拿不到分数,在收件箱里显示成"未评分"。实测有 2 条卡在这里。
    2. **压根没有日期**的:published_at 为 NULL/空时任何日期比较都不成立,
       这类条目既进不了排序窗口(不打分)也进不了规则过滤(不会被标 excluded),
       于是永远以"未评分"挂在列表尾部。改用 created_at 兜底:新条目先被评
       一轮分,随后跟着 created_at 自然老化退出窗口,旧分数照旧保留。

    宁可多捞这一点,也不要让条目静默地永远不被评价。
    """
    p = f"{alias}.published_at" if alias else "published_at"
    c = f"{alias}.created_at" if alias else "created_at"
    return (f"(COALESCE({p},'') >= date('now', ?) "
            f"OR ({p} LIKE '____-01-01' AND substr({p},1,4) = strftime('%Y','now')) "
            f"OR (COALESCE({p},'') = '' AND COALESCE({c},'') >= date('now', ?)))")


def _read_group_id(conn: sqlite3.Connection, slug: str | None) -> int:
    """读路径用的 group_id:查不到就返回 -1(什么都关联不上)。

    读路径**不建组** —— 建组是写路径(pipeline.sync_groups / save_score)的事,
    在读事务里偷偷 INSERT 只会在回滚时白做。默认组不存在时,条目照常列出,
    只是没有分数与组级状态,这正是空库该有的样子。
    """
    gid = group_id(conn, slug)
    return gid if gid is not None else -1


def _write_group_id(conn: sqlite3.Connection, slug: str | None = None) -> int:
    """写路径用的 group_id。

    默认组不存在时会**建出来** —— 单组用户的调用点(save_score /
    set_excluded / set_action)不该先手动 sync 一次组。

    但**具名组**不存在时直接报错,不顺手新建:组的权威来源是 interests.yaml
    (sync_groups),写一个不存在的 slug 只可能是调用方拿了过期的组名。
    要是悄悄建出来,拼错的 slug 就会在库里攒出一堆空组。
    """
    gid = group_id(conn, slug)
    if gid is not None:
        return gid
    if slug in (None, "", DEFAULT_GROUP_SLUG):
        return ensure_group(conn, DEFAULT_GROUP_SLUG, name=DEFAULT_GROUP_NAME)
    raise ValueError(f"未知订阅组 {slug!r}:请先 sync_groups / ensure_group")


def _group_joins() -> str:
    """按组关联的三张表。三处 SELECT 共用,避免写法漂移。

    三个 ``?`` 都要传**同一个** group_id(顺序即 sc / gs / sg)。分值表是
    ``PRIMARY KEY (group_id, item_id)``,不加这个条件会让一条 item 按组数
    复制成多行 —— 列表里就会出现重复条目。
    """
    return """
        LEFT JOIN score         sc ON sc.item_id = i.id AND sc.group_id = ?
        LEFT JOIN group_state   gs ON gs.item_id = i.id AND gs.group_id = ?
        LEFT JOIN summary_group sg ON sg.item_id = i.id AND sg.group_id = ?"""


def _item_filters(*, kind: str | None, state: str | None,
                  min_score: float | None, since: str | None) -> tuple[list[str], list]:
    """get_items 与 count_items 共用同一套筛选条件,防止两处写法漂移
    (漂移会导致分页总数和实际列表对不上)。"""
    where, params = ["1=1"], []
    if kind:
        where.append("i.kind = ?")
        params.append(kind)

    # "收藏"和"不感兴趣"是两个**伪状态**:它们不是 item_state.state 的取值,
    # 而是横切阅读状态的另外两条轴(收藏的条目可能未读也可能已读)。
    # 它们各有自己的页签,是用户回看/撤销手动决定的地方,所以单独处理。
    pseudo = state in ("starred", "ignored")
    if state == "starred":
        where.append("COALESCE(s.starred,0) = 1")
    elif state == "ignored":
        # ignored 是**按组**的:同一篇可能对 A 组无关、对 B 组是核心
        where.append("COALESCE(gs.ignored,0) = 1")
    elif state == "new":
        where.append("COALESCE(s.state,'new') = 'new'")
    elif state:
        where.append("s.state = ?")
        params.append(state)

    if not pseudo:
        # 默认视图只显示"还在考虑范围内"的条目,两类被排除的都不出现:
        #
        # 1) ignored —— 用户已经明确否决。之前它只在"未读"里被过滤掉,
        #    于是"已读"和"全部"里还混着一堆自己说过不要的东西,而且卡片上
        #    没有任何标记,看不出为什么它在这儿。现在它们统一收进
        #    "不感兴趣"页签,那里可以逐条恢复。
        #
        # 2) excluded —— 被规则过滤(实测 139 条忽略反馈里 119 条属于这种),
        #    本来就不该出现在列表里逼用户手动处理。
        where.append("COALESCE(gs.ignored,0) = 0")
        where.append("COALESCE(gs.excluded,0) = 0")
    # 两个伪状态视图刻意**免疫**上面这两条:收藏是用户亲手挑的清单,
    # 不能因为之后收紧了检索词就被"吃掉";不感兴趣页签本来就是收容所,
    # 进去的东西必然带着 ignored=1。
    if min_score is not None:
        where.append("COALESCE(sc.final_score,0) >= ?")
        params.append(min_score)
    if since:
        where.append("i.published_at >= ?")
        params.append(since)
    return where, params


def count_items(conn: sqlite3.Connection, *, kind: str | None = "paper",
                state: str | None = None, min_score: float | None = None,
                since: str | None = None, group_slug: str | None = None) -> int:
    """与 get_items 条件一致的计数,供分页算总页数。"""
    where, params = _item_filters(kind=kind, state=state,
                                  min_score=min_score, since=since)
    gid = _read_group_id(conn, group_slug)
    sql = f"""
        SELECT COUNT(*) FROM item i
        {_group_joins()}
        LEFT JOIN item_state s ON s.item_id = i.id
        WHERE {' AND '.join(where)}
    """
    return int(conn.execute(sql, [gid, gid, gid, *params]).fetchone()[0])


def get_items(
    conn: sqlite3.Connection,
    *,
    kind: str | None = "paper",
    state: str | None = None,
    min_score: float | None = None,
    since: str | None = None,
    limit: int = 200,
    offset: int = 0,
    order: str = "score",
    group_slug: str | None = None,
) -> list[sqlite3.Row]:
    where, params = _item_filters(kind=kind, state=state,
                                  min_score=min_score, since=since)
    gid = _read_group_id(conn, group_slug)

    order_sql = {
        "score": "COALESCE(sc.final_score,-1) DESC, i.published_at DESC",
        "date": "i.published_at DESC",
    }.get(order, "COALESCE(sc.final_score,-1) DESC")

    sql = f"""
        SELECT i.*, sc.final_score, sc.llm_score, sc.llm_reason,
               su.title_zh, su.one_liner, su.method, su.key_results,
               su.problem, su.limitation,
               COALESCE(sg.relevance, su.relevance) AS relevance,
               COALESCE(s.state,'new') AS state,
               COALESCE(s.starred,0) AS starred,
               COALESCE(gs.ignored,0) AS ignored,
               e.cited_by_count, e.is_oa, e.oa_url
        FROM item i
        {_group_joins()}
        LEFT JOIN summary      su ON su.item_id = i.id
        LEFT JOIN item_state   s  ON s.item_id  = i.id
        LEFT JOIN item_enrichment e ON e.item_id = i.id
        WHERE {' AND '.join(where)}
        ORDER BY {order_sql}
        LIMIT ? OFFSET ?
    """
    return conn.execute(sql, [gid, gid, gid, *params, limit, offset]).fetchall()


def search_items(conn: sqlite3.Connection, q: str, limit: int = 100,
                 offset: int = 0, group_slug: str | None = None) -> list[sqlite3.Row]:
    # 必须与 get_items 选出同一组列 —— 卡片宏会用到 cited_by_count / is_oa / oa_url
    gid = _read_group_id(conn, group_slug)
    sql = f"""
        SELECT i.*, sc.final_score, sc.llm_reason, su.title_zh, su.one_liner,
               COALESCE(sg.relevance, su.relevance) AS relevance,
               COALESCE(s.state,'new') AS state, COALESCE(s.starred,0) AS starred,
               COALESCE(gs.ignored,0) AS ignored,
               e.cited_by_count, e.is_oa, e.oa_url
        FROM item_fts f
        JOIN item i ON i.id = f.rowid
        {_group_joins()}
        LEFT JOIN summary          su ON su.item_id = i.id
        LEFT JOIN item_state       s  ON s.item_id  = i.id
        LEFT JOIN item_enrichment  e  ON e.item_id  = i.id
        WHERE item_fts MATCH ?
        ORDER BY rank LIMIT ? OFFSET ?
    """
    # FTS5 语法:把裸词包装成前缀查询,避免用户输入特殊字符报错
    terms = [f'"{t}"*' for t in q.replace('"', " ").split() if t.strip()]
    if not terms:
        return []
    try:
        return conn.execute(sql, (gid, gid, gid, " ".join(terms),
                                  limit, offset)).fetchall()
    except sqlite3.OperationalError:
        return []








def save_score(conn: sqlite3.Connection, item_id: int, *,
               group_id: int | None = None, group_slug: str | None = None,
               **kw) -> None:
    """写某个组里这篇的分值。

    ``group_id`` / ``group_slug`` 都不给时落到默认组 —— 单组用户的写法不用改。
    """
    gid = group_id if group_id is not None else _write_group_id(conn, group_slug)
    conn.execute(
        """INSERT INTO score (group_id, item_id, rule_score, coarse_score,
                              llm_score, llm_reason, llm_model, final_score, ranked_at)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(group_id, item_id) DO UPDATE SET
             rule_score=excluded.rule_score, coarse_score=excluded.coarse_score,
             llm_score=excluded.llm_score, llm_reason=excluded.llm_reason,
             llm_model=excluded.llm_model, final_score=excluded.final_score,
             ranked_at=excluded.ranked_at""",
        (gid, item_id, kw.get("rule_score"), kw.get("coarse_score"),
         kw.get("llm_score"), kw.get("llm_reason"), kw.get("llm_model"),
         kw.get("final_score"), now()),
    )


def save_summary(conn: sqlite3.Connection, item_id: int, data: dict, depth: str,
                 model: str, *, abstract_hash: str | None = None) -> None:
    conn.execute(
        """INSERT INTO summary (item_id, title_zh, one_liner, problem, method, key_results,
                                limitation, relevance, depth, model, abstract_hash, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(item_id) DO UPDATE SET
             title_zh=COALESCE(excluded.title_zh, summary.title_zh),
             one_liner=COALESCE(excluded.one_liner, summary.one_liner),
             problem=excluded.problem, method=excluded.method,
             key_results=excluded.key_results, limitation=excluded.limitation,
             relevance=excluded.relevance, depth=excluded.depth,
             model=excluded.model,
             abstract_hash=excluded.abstract_hash,
             created_at=excluded.created_at""",
        (item_id, data.get("title_zh"), data.get("one_liner"), data.get("problem"),
         data.get("method"), data.get("key_results"), data.get("limitation"),
         data.get("relevance"), depth, model, abstract_hash, now()),
    )


def save_enrichment(conn: sqlite3.Connection, item_id: int, data: dict,
                    *, partial: bool = False) -> None:
    # abstract_attempts 刻意不在这里:UPSERT 只动列出的列,重新富化不会把
    # 已经攒下的"没拿到摘要"计数归零。
    conn.execute(
        """INSERT INTO item_enrichment (item_id, cited_by_count, is_oa, oa_url,
                                        openalex_id, openalex_json, crossref_json, enriched_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(item_id) DO UPDATE SET
             cited_by_count=COALESCE(excluded.cited_by_count, item_enrichment.cited_by_count),
             is_oa=COALESCE(excluded.is_oa, item_enrichment.is_oa),
             oa_url=COALESCE(excluded.oa_url, item_enrichment.oa_url),
             openalex_id=COALESCE(excluded.openalex_id, item_enrichment.openalex_id),
             openalex_json=COALESCE(excluded.openalex_json, item_enrichment.openalex_json),
             crossref_json=COALESCE(excluded.crossref_json, item_enrichment.crossref_json),
             enriched_at=COALESCE(excluded.enriched_at, item_enrichment.enriched_at)""",
        (item_id, data.get("cited_by_count"), data.get("is_oa"), _clean_url(data.get("oa_url")),
         data.get("openalex_id"), data.get("openalex_json"), data.get("crossref_json"),
         None if partial else now()),
    )


def norm_journal(name: str | None) -> str:
    """期刊缓存键:小写 + 折叠空白。

    不能更激进(比如去掉标点)—— "Organic & Biomolecular Chemistry" 和
    "Organic and Biomolecular Chemistry" 是同一本刊,但去标点也救不了,
    而误合并两本不同的刊更糟。easyScholar 自己会做模糊匹配。
    """
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def journal_ranks(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    """一次读全缓存,避免在模板渲染里逐条查库。"""
    out: dict[str, dict[str, str]] = {}
    for r in conn.execute(
            "SELECT journal_norm, ranks_json FROM journal_rank WHERE hit=1"):
        try:
            out[r["journal_norm"]] = json.loads(r["ranks_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def journal_ranks_by_issn(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    """ISSN → 期刊等级。

    有些来源只给刊名的**短形式**("Angewandte Chemie"、"Org. Lett."),
    easyScholar 按全名查不到,但同一 ISSN 下往往已经有别的条目查到了全名。
    实测 "Angewandte Chemie" 与 "Angewandte Chemie International Edition"
    共用 ISSN 1433-7851 —— 用 ISSN 兜一道,4 条里能救回 2 条。
    """
    out: dict[str, dict[str, str]] = {}
    for r in conn.execute(
            """SELECT DISTINCT i.issn, jr.ranks_json
               FROM item i
               JOIN journal_rank jr ON jr.journal_norm = lower(trim(i.journal))
               WHERE i.issn IS NOT NULL AND i.issn <> '' AND jr.hit = 1"""):
        if r["issn"] in out:
            continue
        try:
            ranks = json.loads(r["ranks_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if ranks:
            out[r["issn"]] = ranks
    return out


def save_journal_rank(conn: sqlite3.Connection, journal: str,
                      ranks: dict[str, str] | None) -> None:
    """写缓存。ranks=None 表示接口明确说查不到,记 hit=0 免得反复消耗额度。"""
    conn.execute(
        """INSERT INTO journal_rank (journal_norm, journal, ranks_json, source,
                                     fetched_at, hit)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(journal_norm) DO UPDATE SET
             journal=excluded.journal, ranks_json=excluded.ranks_json,
             fetched_at=excluded.fetched_at, hit=excluded.hit""",
        (norm_journal(journal), journal,
         json.dumps(ranks or {}, ensure_ascii=False), "easyscholar", now(),
         1 if ranks else 0),
    )


def pick_seeds_to_query(conn: sqlite3.Connection, seeds: list[str],
                        k: int, *, group_id: int | None = None) -> list[str]:
    """挑最久没刷新的 k 个种子。从没查过的排最前。

    为什么轮着查而不是一次全查:S2 免费 key 名义上 1 req/s,实际突发很容易
    429(实测连打十几个种子,一半失败)。一轮只查几个,既避开限流,
    又让每个种子隔几天被刷新一次 —— 日报场景完全够。
    """
    if k <= 0 or not seeds:
        return []
    gid = _read_group_id(conn, None) if group_id is None else group_id
    order = {s: i for i, s in enumerate(seeds)}
    rows = {r["seed_doi"]: r["queried_at"] for r in conn.execute(
        "SELECT seed_doi, queried_at FROM seed_query WHERE group_id=?", (gid,))}
    # 没查过的排最前(空串最小),其次按时间从早到晚
    return sorted(seeds, key=lambda s: (rows.get(s) or "", order[s]))[:k]


def save_seed_cites(conn: sqlite3.Connection, seed_doi: str,
                    items: list[dict], *, group_id: int | None = None) -> int:
    """记录"这些论文引用了这个种子"(属于某个组)。返回写入条数。

    ``group_id`` 缺省落默认组 —— 与 save_score / set_excluded 同一套语义,
    单组调用点不必显式传。
    """
    group_id = _write_group_id(conn) if group_id is None else group_id
    seed_doi = normalize_doi(seed_doi) or seed_doi
    stamp = now()
    n = 0
    for it in items:
        doi = normalize_doi(it.get("doi"))
        if not doi:
            continue
        cur = conn.execute(
            """INSERT OR IGNORE INTO seed_cite
               (group_id, seed_doi, citing_doi, item_json, first_seen)
               VALUES (?,?,?,?,?)""",
            (group_id, seed_doi, doi, json.dumps(it, ensure_ascii=False), stamp))
        n += cur.rowcount
    return n


def mark_seed_queried(conn: sqlite3.Connection, seed_doi: str,
                      n_cites: int, *, group_id: int | None = None,
                      status: str = "ok") -> None:
    group_id = _write_group_id(conn) if group_id is None else group_id
    conn.execute(
        """INSERT INTO seed_query (group_id, seed_doi, queried_at, n_cites, status)
           VALUES (?,?,?,?,?)
           ON CONFLICT(group_id, seed_doi) DO UPDATE SET
             queried_at=excluded.queried_at, n_cites=excluded.n_cites,
             status=excluded.status""",
        (group_id, seed_doi, now(), n_cites, status))


def cocited_items(conn: sqlite3.Connection, min_seeds: int, *,
                  group_id: int | None = None) -> list[dict]:
    """共被引达到门槛、且**还没进这个组**的候选。

    只统计**本组的种子**:两个方向混在一起算,A 的候选会因为"也被 B 的种子
    引过"而票数虚高。过滤的是"已在本组"而不是"已在库里" —— 被 A 组先捞到
    的论文,对 B 组仍然是合法候选,只是要再记一次 B 组的成员关系。
    """
    gid = _read_group_id(conn, None) if group_id is None else group_id
    out: list[dict] = []
    for r in conn.execute(
            """SELECT citing_doi, COUNT(DISTINCT seed_doi) n,
                      MAX(item_json) item_json
               FROM seed_cite
               WHERE group_id = ?
                 AND citing_doi NOT IN
                     (SELECT LOWER(i.doi) FROM item i
                        JOIN item_group g ON g.item_id = i.id AND g.group_id = ?
                       WHERE i.doi IS NOT NULL)
               GROUP BY citing_doi
               HAVING n >= ?""", (gid, gid, max(1, min_seeds))):
        try:
            item = json.loads(r["item_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not item.get("title"):
            continue
        item["source"] = "snowball"
        item["source_ref"] = f"cocite:{r['n']}"
        out.append(item)
    return out


def add_to_group(conn: sqlite3.Connection, group_id: int, item_id: int) -> None:
    """把条目记进某个组。已存在就什么都不做(保留最早一次的 first_seen)。"""
    conn.execute(
        """INSERT OR IGNORE INTO item_group (group_id, item_id, first_seen)
           VALUES (?,?,?)""",
        (group_id, item_id, now()))


def item_group_ids(conn: sqlite3.Connection, item_id: int) -> list[str]:
    """这篇属于哪些组(slug)。详情页要显示"它从哪来"。"""
    return [r[0] for r in conn.execute(
        """SELECT g.slug FROM item_group ig
             JOIN interest_group g ON g.id = ig.group_id
            WHERE ig.item_id = ? ORDER BY g.position, g.id""", (item_id,))]


def starred_ids(conn: sqlite3.Connection) -> set[int]:
    """被收藏的条目 id。

    这些 id 的"收藏"是用户明确说过的"我要留着",规则过滤必须给它让路:
    "收藏一篇 → 某天收紧了检索词 → 它被标成 excluded"这种无声的数据丢失
    是收藏功能最不该有的行为。

    范围刻意**只限收藏**,不扩大到"所有有过反馈的条目":
    已读/不感兴趣的条目本来就不在未读视图里,保护它们只会让被规则丢掉的
    噪音滞留在"已读/全部"里,而且它们没有分数 —— 正是"未评分"观感的来源。
    """
    return {int(r[0]) for r in
            conn.execute("SELECT item_id FROM item_state WHERE starred=1")}


def set_excluded(conn: sqlite3.Connection, item_ids: list[int],
                 excluded: bool = True, *, group_slug: str | None = None) -> None:
    """标记/取消标记**某个组里**被规则过滤的条目。收件箱据此隐藏它们。

    按组:组 A 的规则排除的文献,可能是组 B 的核心方向。
    """
    if not item_ids:
        return
    gid = _write_group_id(conn, group_slug)
    for iid in item_ids:
        # 不用 upsert:group_state 有个列就叫 excluded,而 upsert 的伪表也叫
        # excluded,"excluded=excluded.excluded" 读起来等于自欺,拆成两步清楚。
        conn.execute("INSERT OR IGNORE INTO group_state (group_id, item_id) VALUES (?,?)",
                     (gid, iid))
        conn.execute("UPDATE group_state SET excluded=? WHERE group_id=? AND item_id=?",
                     (1 if excluded else 0, gid, iid))


# 用户反馈的全部合法动作。set_action 之外没有别的写入口。
ACTIONS = frozenset({"star", "unstar", "read", "unread", "archive", "ignore", "unignore"})


def set_action(conn: sqlite3.Connection, item_id: int, action: str, *,
               group_slug: str | None = None) -> None:
    """记录一次用户反馈。

    状态归属分两轴:**star 与已读/归档是全局的**(这是对"这篇文献"的判断:
    我看过、我要留着),而 **ignore / unignore 是按组的**(这是对"它与某个
    方向的关系"的判断 —— 同一篇可能对 A 组无关、对 B 组是核心)。
    不带 group_slug 时落在默认组,单组用户的写法不用改。
    """
    # action 来自表单的任意字符串。以前未知值不改状态,却照样原样写进
    # feedback 表(统计页按 action 分组、精排 prompt 取反馈样本都读它),
    # 所以在这里就拒绝,别让垃圾落库。
    if action not in ACTIONS:
        raise ValueError(f"未知操作: {action!r}")
    conn.execute("INSERT OR IGNORE INTO item_state (item_id) VALUES (?)", (item_id,))
    if action in ("star", "unstar"):
        conn.execute("UPDATE item_state SET starred=? WHERE item_id=?",
                     (1 if action == "star" else 0, item_id))
    elif action in ("ignore", "unignore"):
        gid = _write_group_id(conn, group_slug)
        conn.execute("INSERT OR IGNORE INTO group_state (group_id, item_id) VALUES (?,?)",
                     (gid, item_id))
        conn.execute("UPDATE group_state SET ignored=? WHERE group_id=? AND item_id=?",
                     (1 if action == "ignore" else 0, gid, item_id))
    elif action in ("read", "unread", "archive"):
        val = {"read": "read", "unread": "new", "archive": "archived"}[action]
        conn.execute("UPDATE item_state SET state=? WHERE item_id=?", (val, item_id))
    conn.execute(
        "INSERT INTO feedback (item_id, action, created_at) VALUES (?,?,?)",
        (item_id, action, now()),
    )


def log_run(conn: sqlite3.Connection, stage: str, status: str, stats: Any = None,
            error: str | None = None, started_at: str | None = None,
            group_slug: str | None = None) -> None:
    conn.execute(
        """INSERT INTO run_log
           (stage, started_at, finished_at, status, stats, error, group_slug)
           VALUES (?,?,?,?,?,?,?)""",
        (stage, started_at or now(), now(), status,
         json.dumps(stats, ensure_ascii=False) if stats is not None else None, error,
         group_slug),
    )


def recent_stage_runs(conn: sqlite3.Connection, stages: Iterable[str],
                      *, limit: int = 500) -> list[tuple[str, str]]:
    """最近若干条运行记录的 ``(stage, 结束时间)``,供"冷却 + 每日上限"当账本。

    刻意用 run_log 而不是内存计数:进程重启不会把额度计数清零,而且 CLI 与
    定时任务跑过的同样算数 —— 上限约束的是"这一天这个阶段一共跑了几次"。
    返回阶段名是因为 ``all`` 要按**每个子阶段各自**的次数算,不能求和。
    """
    names = [s for s in stages if s]
    if not names:
        return []
    marks = ", ".join("?" for _ in names)
    rows = conn.execute(
        f"""SELECT stage, COALESCE(finished_at, started_at) AS at FROM run_log
            WHERE stage IN ({marks})
              AND COALESCE(finished_at, started_at) IS NOT NULL
            ORDER BY id DESC LIMIT ?""",
        (*names, limit),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    def one(sql: str, *a):
        r = conn.execute(sql, a).fetchone()
        return r[0] if r else 0

    return {
        "items": one("SELECT COUNT(*) FROM item"),
        "papers": one("SELECT COUNT(*) FROM item WHERE kind='paper'"),
        # "未读"必须和首页页签同一口径(排除 ignored / excluded、只数 paper),
        # 否则统计页和首页给出两个对不上的数字。
        "new": count_items(conn, kind="paper", state="new"),
        "starred": one("SELECT COUNT(*) FROM item_state WHERE starred=1"),
        "ignored": one("SELECT COUNT(*) FROM item_state WHERE ignored=1"),
        "summaries": one("SELECT COUNT(*) FROM summary"),
        "scored": one("SELECT COUNT(*) FROM score"),
        "by_source": [dict(r) for r in conn.execute(
            "SELECT source, COUNT(*) n FROM item GROUP BY source ORDER BY n DESC")],
        "by_journal": [dict(r) for r in conn.execute(
            """SELECT journal, COUNT(*) n, ROUND(AVG(sc.final_score),1) avg_score
               FROM item i LEFT JOIN score sc ON sc.item_id=i.id
               WHERE journal IS NOT NULL GROUP BY journal ORDER BY n DESC LIMIT 15""")],
        "feedback": [dict(r) for r in conn.execute(
            "SELECT action, COUNT(*) n FROM feedback GROUP BY action ORDER BY n DESC")],
        "last_runs": [dict(r) for r in conn.execute(
            """SELECT stage, status, finished_at, stats FROM run_log
               ORDER BY id DESC LIMIT 8""")],
    }
