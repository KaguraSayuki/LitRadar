"""排序漏斗与数据库层测试(不依赖网络与 LLM)。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from litradar import db  # noqa: E402
from litradar.rank import Profile, coarse_rank, rule_filter  # noqa: E402


def _row(**kw) -> sqlite3.Row:
    """构造一个字段齐全的假 item 行。"""
    base = {
        "id": 1, "kind": "paper", "doi": None, "title": "", "title_norm": "",
        "abstract": None, "authors": "[]", "journal": None, "issn": None,
        "published_at": "2026-09-01", "url": None, "impact_factor": None,
        "xmol_url": None, "matched_keywords": "[]", "source": "test",
        "source_ref": None, "created_at": "", "updated_at": "",
    }
    base.update(kw)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cols = ", ".join(base)
    ph = ", ".join("?" for _ in base)
    conn.execute(f"CREATE TABLE t ({cols})")
    conn.execute(f"INSERT INTO t ({cols}) VALUES ({ph})", list(base.values()))
    return conn.execute("SELECT * FROM t").fetchone()


@pytest.fixture
def profile() -> Profile:
    return Profile.from_dict({
        "name": "test",
        "direction": "metal carbene N-H insertion",
        "keywords": {
            "core": ["metal carbene", "n-h insertion"],
            "bonus": ["enantioselective", "diazo"],
            "current_challenges": ["手性传递效率低"],
            "boost_topics": ["手性重排"],
        },
        "negative": ["polymerization", "retraction"],
        "journals": {"core": ["Organic Letters"],
                     "ok": ["Tetrahedron Letters"],
                     "issn": {"Organic Letters": ["1523-7060"]}},
        "authors_watch": ["Huw M. L. Davies"],
    })


# ------------------------------------------------------------- 规则过滤
def test_命中核心关键词得分(profile):
    kept, dropped = rule_filter(
        [_row(title="Metal carbene N-H insertion into anilines")], profile)
    assert dropped == 0
    assert kept[0][1] > 0
    assert "metal carbene" in kept[0][2]["core_hits"]


def test_命中排除方向被丢弃(profile):
    kept, dropped = rule_filter(
        [_row(title="Metal carbene in polymerization of styrene")], profile)
    assert dropped == 1
    assert kept == []


def test_撤稿被丢弃(profile):
    _, dropped = rule_filter([_row(title="Retraction: metal carbene study")], profile)
    assert dropped == 1


def test_期刊缩写命中保送(profile):
    """X-MOL 给缩写 'Org. Lett.',必须能命中白名单里的 'Organic Letters'。"""
    kept, _ = rule_filter(
        [_row(title="Some unrelated title", journal="Org. Lett.")], profile)
    assert kept[0][2].get("journal_core") == "Organic Letters"


def test_xmol高亮词加分(profile):
    plain, _ = rule_filter([_row(title="A study", matched_keywords="[]")], profile)
    marked, _ = rule_filter([_row(title="A study", matched_keywords='["Diazos"]')], profile)
    assert marked[0][1] > plain[0][1]


def test_关注作者加分(profile):
    plain, _ = rule_filter([_row(title="A study")], profile)
    auth, _ = rule_filter([_row(title="A study", authors='["Huw M. L. Davies"]')], profile)
    assert auth[0][1] > plain[0][1]


# ------------------------------------------------------------- BM25 粗排
def test_粗排把相关项排前(profile):
    rows = [
        _row(id=1, title="Total synthesis of a natural product"),
        _row(id=2, title="Metal carbene N-H insertion of diazo compounds"),
        _row(id=3, title="Polymer surface modification"),
    ]
    kept, _ = rule_filter(rows, profile)
    picked = coarse_rank(kept, profile, top_k=3)
    assert picked, "粗排不应返回空"
    assert picked[0][0]["id"] == 2, "最相关的那篇应排第一"


def test_粗排默认不截断(profile):
    """top_k=0(默认)必须原样返回全部 —— 截断会让 BM25 拿到"一票否决权",
    被截掉的条目永远拿不到 LLM 判断,在收件箱里长成一片"未评分"。"""
    rows = [_row(id=i, title=f"Metal carbene study number {i}") for i in range(1, 21)]
    kept, _ = rule_filter(rows, profile)
    assert len(coarse_rank(kept, profile)) == len(kept) == 20
    assert len(coarse_rank(kept, profile, top_k=0)) == 20


def test_粗排显式topk才截断(profile):
    """仍保留截断能力:显式给了正数才生效(给限量试跑用)。"""
    rows = [_row(id=i, title=f"Metal carbene study number {i}") for i in range(1, 21)]
    kept, _ = rule_filter(rows, profile)
    assert len(coarse_rank(kept, profile, top_k=5)) == 5


# ------------------------------------------------------------- 数据库
def test_去重同一DOI只入库一次(tmp_path):
    database = db.Database(tmp_path / "t.db")
    database.init()
    conn = database.connect()
    data = {"kind": "paper", "dedup_key": "doi:10.1/x", "doi": "10.1/x",
            "title": "T", "title_norm": "t", "source": "a"}
    iid1, created1 = db.upsert_item(conn, data)
    iid2, created2 = db.upsert_item(conn, dict(data, source="b"))
    assert created1 is True and created2 is False
    assert iid1 == iid2
    assert conn.execute("SELECT COUNT(*) FROM item").fetchone()[0] == 1


def test_已有字段不被空值覆盖(tmp_path):
    database = db.Database(tmp_path / "t.db")
    database.init()
    conn = database.connect()
    db.upsert_item(conn, {"kind": "paper", "dedup_key": "doi:10.1/y", "doi": "10.1/y",
                          "title": "T", "title_norm": "t", "abstract": "好摘要",
                          "source": "xmol"})
    db.upsert_item(conn, {"kind": "paper", "dedup_key": "doi:10.1/y", "doi": "10.1/y",
                          "title": "T", "title_norm": "t", "abstract": None,
                          "source": "crossref"})
    assert conn.execute("SELECT abstract FROM item").fetchone()[0] == "好摘要"


def test_时间窗捞回只有年份的条目(tmp_path):
    """只知道年份的记录入库时写成 YYYY-01-01 占位,按字面比会被窗口切掉,
    于是今年的论文永远拿不到分数、在收件箱里显示成"未评分"。
    `db.in_window` 要对这类占位单独按年份放行。"""
    import datetime

    database = db.Database(tmp_path / "t.db")
    database.init()
    conn = database.connect()
    this_year = datetime.date.today().year
    old_year = this_year - 3

    def add(key, pub):
        db.upsert_item(conn, {"kind": "paper", "dedup_key": f"doi:10.1/{key}",
                              "doi": f"10.1/{key}", "title": key, "title_norm": key,
                              "published_at": pub, "source": "test"})

    add("placeholder-now", f"{this_year}-01-01")
    add("placeholder-old", f"{old_year}-01-01")
    add("old-exact", f"{old_year}-06-15")

    sql = f"SELECT i.doi FROM item i WHERE i.kind='paper' AND {db.in_window('i')}"
    got = {r[0].split("/")[-1] for r in conn.execute(sql, ("-200 days",))}
    assert "placeholder-now" in got, "今年只有年份的条目必须进窗"
    assert "placeholder-old" not in got, "往年的占位日期不该被捞回来"
    assert "old-exact" not in got
    assert db.in_window("").startswith("(COALESCE(published_at"), "无别名也要能用"


def test_反馈记录(tmp_path):
    database = db.Database(tmp_path / "t.db")
    database.init()
    conn = database.connect()
    iid, _ = db.upsert_item(conn, {"kind": "paper", "dedup_key": "doi:10.1/z",
                                   "doi": "10.1/z", "title": "T", "title_norm": "t",
                                   "source": "a"})
    db.set_action(conn, iid, "star")
    db.set_action(conn, iid, "read")
    assert conn.execute("SELECT starred FROM item_state").fetchone()[0] == 1
    assert conn.execute("SELECT state FROM item_state").fetchone()[0] == "read"
    assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 2


def test_全文检索(tmp_path):
    database = db.Database(tmp_path / "t.db")
    database.init()
    conn = database.connect()
    db.upsert_item(conn, {"kind": "paper", "dedup_key": "doi:10.1/a", "doi": "10.1/a",
                          "title": "Photochemical reaction of vinyldiazo esters",
                          "title_norm": "photochemical reaction", "abstract": "azides",
                          "source": "a"})
    db.upsert_item(conn, {"kind": "paper", "dedup_key": "doi:10.1/b", "doi": "10.1/b",
                          "title": "Polymer chemistry", "title_norm": "polymer",
                          "source": "a"})
    assert len(db.search_items(conn, "vinyldiazo")) == 1
    assert len(db.search_items(conn, "polymer")) == 1
    assert db.search_items(conn, "不存在的词xyz") == []
