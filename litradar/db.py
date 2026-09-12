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
    enriched_at    TEXT
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
    seed_doi   TEXT NOT NULL,        -- 引用方引用了哪个种子
    citing_doi TEXT NOT NULL,        -- 引用方
    item_json  TEXT,                 -- 引用方元数据,凑够票时直接入库,不再查一次
    first_seen TEXT,
    PRIMARY KEY (seed_doi, citing_doi)
);
CREATE INDEX IF NOT EXISTS idx_seed_cite_citing ON seed_cite(citing_doi);

-- 每个种子上次刷新时间。挑最久没查的先查,轮着来。
CREATE TABLE IF NOT EXISTS seed_query (
    seed_doi   TEXT PRIMARY KEY,
    queried_at TEXT,
    n_cites    INTEGER DEFAULT 0,
    status     TEXT                  -- ok / failed
);

CREATE TABLE IF NOT EXISTS score (
    item_id      INTEGER PRIMARY KEY REFERENCES item(id) ON DELETE CASCADE,
    rule_score   REAL,
    coarse_score REAL,
    llm_score    REAL,
    llm_reason   TEXT,
    llm_model    TEXT,
    final_score  REAL,
    ranked_at    TEXT
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
    error       TEXT
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
            conn.executescript(SCHEMA)
            conn.executescript(FTS_TRIGGERS)
            self._migrate(conn)
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """轻量迁移:已存在的库就地升级,不动老数据。"""
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


# ---------------------------------------------------------------- item 读写
ITEM_FIELDS = (
    "kind", "dedup_key", "doi", "title", "title_norm", "abstract", "authors",
    "journal", "issn", "published_at", "url", "impact_factor", "xmol_url",
    "matched_keywords", "source", "source_ref",
)


def upsert_item(conn: sqlite3.Connection, data: dict[str, Any]) -> tuple[int, bool]:
    """按 dedup_key 插入或补全。返回 (item_id, 是否新建)。

    已存在的条目只补空字段,不覆盖已有内容(避免低质量源盖掉高质量源)。
    """
    row = conn.execute(
        "SELECT * FROM item WHERE dedup_key = ?", (data["dedup_key"],)
    ).fetchone()

    payload = {k: data.get(k) for k in ITEM_FIELDS}
    # 刊名在入库这个唯一入口统一洗干净(HTML 实体 / 换行),见 clean_journal
    payload["journal"] = clean_journal(payload.get("journal"))
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
    """时间窗条件,占用**一个** ``?`` 参数(形如 ``"-200 days"``),自带括号。

    除了按字面日期比较,还要捞回"只知道年份"的条目:这类记录入库时写成
    ``YYYY-01-01`` 占位(见 `upsert_item` 里对 published_at 的特例处理),
    按字面比会被窗口起点切掉 —— 明明是今年的论文,却永远拿不到分数,
    在收件箱里显示成"未评分"。实测有 2 条卡在这里。

    宁可多捞这一点,也不要让条目静默地永远不被评价。
    """
    p = f"{alias}.published_at" if alias else "published_at"
    return (f"(COALESCE({p},'') >= date('now', ?) "
            f"OR ({p} LIKE '____-01-01' AND substr({p},1,4) = strftime('%Y','now')))")


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
        where.append("COALESCE(s.ignored,0) = 1")
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
        where.append("COALESCE(s.ignored,0) = 0")
        where.append("COALESCE(s.excluded,0) = 0")
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
                since: str | None = None) -> int:
    """与 get_items 条件一致的计数,供分页算总页数。"""
    where, params = _item_filters(kind=kind, state=state,
                                  min_score=min_score, since=since)
    sql = f"""
        SELECT COUNT(*) FROM item i
        LEFT JOIN score      sc ON sc.item_id = i.id
        LEFT JOIN item_state s  ON s.item_id  = i.id
        WHERE {' AND '.join(where)}
    """
    return int(conn.execute(sql, params).fetchone()[0])


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
) -> list[sqlite3.Row]:
    where, params = _item_filters(kind=kind, state=state,
                                  min_score=min_score, since=since)

    order_sql = {
        "score": "COALESCE(sc.final_score,-1) DESC, i.published_at DESC",
        "date": "i.published_at DESC",
    }.get(order, "COALESCE(sc.final_score,-1) DESC")

    sql = f"""
        SELECT i.*, sc.final_score, sc.llm_score, sc.llm_reason,
               su.title_zh, su.one_liner, su.relevance, su.method, su.key_results,
               su.problem, su.limitation,
               COALESCE(s.state,'new') AS state,
               COALESCE(s.starred,0) AS starred,
               COALESCE(s.ignored,0) AS ignored,
               e.cited_by_count, e.is_oa, e.oa_url
        FROM item i
        LEFT JOIN score        sc ON sc.item_id = i.id
        LEFT JOIN summary      su ON su.item_id = i.id
        LEFT JOIN item_state   s  ON s.item_id  = i.id
        LEFT JOIN item_enrichment e ON e.item_id = i.id
        WHERE {' AND '.join(where)}
        ORDER BY {order_sql}
        LIMIT ? OFFSET ?
    """
    return conn.execute(sql, [*params, limit, offset]).fetchall()


def search_items(conn: sqlite3.Connection, q: str, limit: int = 100,
                 offset: int = 0) -> list[sqlite3.Row]:
    # 必须与 get_items 选出同一组列 —— 卡片宏会用到 cited_by_count / is_oa / oa_url
    sql = """
        SELECT i.*, sc.final_score, sc.llm_reason, su.title_zh, su.one_liner,
               COALESCE(s.state,'new') AS state, COALESCE(s.starred,0) AS starred,
               COALESCE(s.ignored,0) AS ignored,
               e.cited_by_count, e.is_oa, e.oa_url
        FROM item_fts f
        JOIN item i ON i.id = f.rowid
        LEFT JOIN score            sc ON sc.item_id = i.id
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
        return conn.execute(sql, (" ".join(terms), limit, offset)).fetchall()
    except sqlite3.OperationalError:
        return []








def save_score(conn: sqlite3.Connection, item_id: int, **kw) -> None:
    conn.execute(
        """INSERT INTO score (item_id, rule_score, coarse_score, llm_score,
                              llm_reason, llm_model, final_score, ranked_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(item_id) DO UPDATE SET
             rule_score=excluded.rule_score, coarse_score=excluded.coarse_score,
             llm_score=excluded.llm_score, llm_reason=excluded.llm_reason,
             llm_model=excluded.llm_model, final_score=excluded.final_score,
             ranked_at=excluded.ranked_at""",
        (item_id, kw.get("rule_score"), kw.get("coarse_score"),
         kw.get("llm_score"), kw.get("llm_reason"), kw.get("llm_model"),
         kw.get("final_score"), now()),
    )


def save_summary(conn: sqlite3.Connection, item_id: int, data: dict, depth: str, model: str) -> None:
    conn.execute(
        """INSERT INTO summary (item_id, title_zh, one_liner, problem, method, key_results,
                                limitation, relevance, depth, model, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(item_id) DO UPDATE SET
             title_zh=COALESCE(excluded.title_zh, summary.title_zh),
             one_liner=COALESCE(excluded.one_liner, summary.one_liner),
             problem=excluded.problem, method=excluded.method,
             key_results=excluded.key_results, limitation=excluded.limitation,
             relevance=excluded.relevance, depth=excluded.depth,
             model=excluded.model, created_at=excluded.created_at""",
        (item_id, data.get("title_zh"), data.get("one_liner"), data.get("problem"),
         data.get("method"), data.get("key_results"), data.get("limitation"),
         data.get("relevance"), depth, model, now()),
    )


def save_enrichment(conn: sqlite3.Connection, item_id: int, data: dict) -> None:
    conn.execute(
        """INSERT INTO item_enrichment (item_id, cited_by_count, is_oa, oa_url,
                                        openalex_id, openalex_json, crossref_json, enriched_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(item_id) DO UPDATE SET
             cited_by_count=excluded.cited_by_count, is_oa=excluded.is_oa,
             oa_url=excluded.oa_url, openalex_id=excluded.openalex_id,
             openalex_json=excluded.openalex_json, crossref_json=excluded.crossref_json,
             enriched_at=excluded.enriched_at""",
        (item_id, data.get("cited_by_count"), data.get("is_oa"), data.get("oa_url"),
         data.get("openalex_id"), data.get("openalex_json"), data.get("crossref_json"), now()),
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
                        k: int) -> list[str]:
    """挑最久没刷新的 k 个种子。从没查过的排最前。

    为什么轮着查而不是一次全查:S2 免费 key 名义上 1 req/s,实际突发很容易
    429(实测连打十几个种子,一半失败)。一轮只查几个,既避开限流,
    又让每个种子隔几天被刷新一次 —— 日报场景完全够。
    """
    if k <= 0 or not seeds:
        return []
    order = {s: i for i, s in enumerate(seeds)}
    rows = {r["seed_doi"]: r["queried_at"] for r in conn.execute(
        "SELECT seed_doi, queried_at FROM seed_query")}
    # 没查过的排最前(空串最小),其次按时间从早到晚
    return sorted(seeds, key=lambda s: (rows.get(s) or "", order[s]))[:k]


def save_seed_cites(conn: sqlite3.Connection, seed_doi: str,
                    items: list[dict]) -> int:
    """记录"这些论文引用了这个种子"。返回写入条数。"""
    stamp = now()
    n = 0
    for it in items:
        doi = (it.get("doi") or "").lower()
        if not doi:
            continue
        cur = conn.execute(
            """INSERT OR IGNORE INTO seed_cite (seed_doi, citing_doi, item_json, first_seen)
               VALUES (?,?,?,?)""",
            (seed_doi, doi, json.dumps(it, ensure_ascii=False), stamp))
        n += cur.rowcount
    return n


def mark_seed_queried(conn: sqlite3.Connection, seed_doi: str,
                      n_cites: int, status: str = "ok") -> None:
    conn.execute(
        """INSERT INTO seed_query (seed_doi, queried_at, n_cites, status)
           VALUES (?,?,?,?)
           ON CONFLICT(seed_doi) DO UPDATE SET
             queried_at=excluded.queried_at, n_cites=excluded.n_cites,
             status=excluded.status""",
        (seed_doi, now(), n_cites, status))


def cocited_items(conn: sqlite3.Connection, min_seeds: int) -> list[dict]:
    """共被引达到门槛、且**还不在库里**的候选。

    过滤在库的:这些论文每轮都会被重新算出来,不去掉就会反复走一遍入库路径。
    """
    out: list[dict] = []
    for r in conn.execute(
            """SELECT citing_doi, COUNT(DISTINCT seed_doi) n,
                      MAX(item_json) item_json
               FROM seed_cite
               WHERE citing_doi NOT IN
                     (SELECT LOWER(doi) FROM item WHERE doi IS NOT NULL)
               GROUP BY citing_doi
               HAVING n >= ?""", (max(1, min_seeds),)):
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


def set_excluded(conn: sqlite3.Connection, item_ids: list[int], excluded: bool = True) -> None:
    """标记/取消标记被规则过滤的条目。收件箱据此隐藏它们。"""
    if not item_ids:
        return
    for iid in item_ids:
        conn.execute("INSERT OR IGNORE INTO item_state (item_id) VALUES (?)", (iid,))
        conn.execute("UPDATE item_state SET excluded=? WHERE item_id=?",
                     (1 if excluded else 0, iid))


def set_action(conn: sqlite3.Connection, item_id: int, action: str) -> None:
    conn.execute("INSERT OR IGNORE INTO item_state (item_id) VALUES (?)", (item_id,))
    if action in ("star", "unstar"):
        conn.execute("UPDATE item_state SET starred=? WHERE item_id=?",
                     (1 if action == "star" else 0, item_id))
    elif action == "ignore":
        conn.execute("UPDATE item_state SET ignored=1 WHERE item_id=?", (item_id,))
    elif action == "unignore":
        conn.execute("UPDATE item_state SET ignored=0 WHERE item_id=?", (item_id,))
    elif action in ("read", "unread", "archive"):
        val = {"read": "read", "unread": "new", "archive": "archived"}[action]
        conn.execute("UPDATE item_state SET state=? WHERE item_id=?", (val, item_id))
    conn.execute(
        "INSERT INTO feedback (item_id, action, created_at) VALUES (?,?,?)",
        (item_id, action, now()),
    )


def log_run(conn: sqlite3.Connection, stage: str, status: str, stats: Any = None,
            error: str | None = None, started_at: str | None = None) -> None:
    conn.execute(
        """INSERT INTO run_log (stage, started_at, finished_at, status, stats, error)
           VALUES (?,?,?,?,?,?)""",
        (stage, started_at or now(), now(), status,
         json.dumps(stats, ensure_ascii=False) if stats is not None else None, error),
    )


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    def one(sql: str, *a):
        r = conn.execute(sql, a).fetchone()
        return r[0] if r else 0

    return {
        "items": one("SELECT COUNT(*) FROM item"),
        "papers": one("SELECT COUNT(*) FROM item WHERE kind='paper'"),
        "new": one("SELECT COUNT(*) FROM item_state WHERE state='new' AND ignored=0"),
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
