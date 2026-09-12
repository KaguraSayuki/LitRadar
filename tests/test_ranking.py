"""排序漏斗与数据库层测试(不依赖网络与 LLM)。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from litradar import db  # noqa: E402
from litradar.rank import (Profile, coarse_rank, feedback_examples,  # noqa: E402
                          rule_filter)


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
        "direction": "optical sensor Sample detection",
        "keywords": {
            "core": ["optical sensor", "sample detection"],
            "bonus": ["selective", "optical"],
            "current_challenges": ["示例信号较弱"],
            "boost_topics": ["信号增强"],
        },
        "negative": ["polymerization", "retraction"],
        "journals": {"core": ["Organic Letters"],
                     "ok": ["Tetrahedron Letters"],
                     "issn": {"Organic Letters": ["1523-7060"]}},
        "authors_watch": ["Alex Example"],
    })


# ------------------------------------------------------------- 规则过滤
def test_命中核心关键词得分(profile):
    kept, dropped = rule_filter(
        [_row(title="Optical sensor Sample detection into samples")], profile)
    assert dropped == 0
    assert kept[0][1] > 0
    assert "optical sensor" in kept[0][2]["core_hits"]


def test_命中排除方向被丢弃(profile):
    kept, dropped = rule_filter(
        [_row(title="Optical sensor in polymerization of styrene")], profile)
    assert dropped == 1
    assert kept == []


def test_撤稿被丢弃(profile):
    _, dropped = rule_filter([_row(title="Retraction: optical sensor study")], profile)
    assert dropped == 1


def test_期刊缩写命中保送(profile):
    """X-MOL 给缩写 'Org. Lett.',必须能命中白名单里的 'Organic Letters'。"""
    kept, _ = rule_filter(
        [_row(title="Some unrelated title", journal="Org. Lett.")], profile)
    assert kept[0][2].get("journal_core") == "Organic Letters"


def test_xmol高亮词加分(profile):
    plain, _ = rule_filter([_row(title="A study", matched_keywords="[]")], profile)
    marked, _ = rule_filter([_row(title="A study", matched_keywords='["Sensors"]')], profile)
    assert marked[0][1] > plain[0][1]


def test_关注作者加分(profile):
    plain, _ = rule_filter([_row(title="A study")], profile)
    auth, _ = rule_filter([_row(title="A study", authors='["Alex Example"]')], profile)
    assert auth[0][1] > plain[0][1]


# ------------------------------------------------------------- BM25 粗排
def test_粗排把相关项排前(profile):
    rows = [
        _row(id=1, title="Total synthesis of a natural product"),
        _row(id=2, title="Optical sensor Sample detection of optical compounds"),
        _row(id=3, title="Polymer surface modification"),
    ]
    kept, _ = rule_filter(rows, profile)
    picked = coarse_rank(kept, profile, top_k=3)
    assert picked, "粗排不应返回空"
    assert picked[0][0]["id"] == 2, "最相关的那篇应排第一"


def test_粗排默认不截断(profile):
    """top_k=0(默认)必须原样返回全部 —— 截断会让 BM25 拿到"一票否决权",
    被截掉的条目永远拿不到 LLM 判断,在收件箱里长成一片"未评分"。"""
    rows = [_row(id=i, title=f"Optical sensor study number {i}") for i in range(1, 21)]
    kept, _ = rule_filter(rows, profile)
    assert len(coarse_rank(kept, profile)) == len(kept) == 20
    assert len(coarse_rank(kept, profile, top_k=0)) == 20


def test_粗排显式topk才截断(profile):
    """仍保留截断能力:显式给了正数才生效(给限量试跑用)。"""
    rows = [_row(id=i, title=f"Optical sensor study number {i}") for i in range(1, 21)]
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
    got = {r[0].split("/")[-1] for r in conn.execute(sql, ("-200 days", "-200 days"))}
    assert "placeholder-now" in got, "今年只有年份的条目必须进窗"
    assert "placeholder-old" not in got, "往年的占位日期不该被捞回来"
    assert "old-exact" not in got
    assert db.in_window("").startswith("(COALESCE(published_at"), "无别名也要能用"


def test_时间窗按created_at捞回无日期条目(tmp_path):
    """回归:published_at 为 NULL/空时任何日期比较都不成立,这类条目既不打分
    也不会被规则标 excluded,只能永远以"未评分"挂在列表尾部。
    改用 created_at 兜底,并跟着 created_at 自然老化退出窗口。"""
    database = db.Database(tmp_path / "t.db")
    database.init()
    conn = database.connect()

    def add(key, pub):
        iid, _ = db.upsert_item(conn, {
            "kind": "paper", "dedup_key": f"doi:10.1/{key}", "doi": f"10.1/{key}",
            "title": key, "title_norm": key, "published_at": pub, "source": "test"})
        return iid

    add("no-date-fresh", None)
    add("no-date-empty", "")
    stale = add("no-date-stale", None)
    conn.execute("UPDATE item SET created_at = '2019-01-01T00:00:00+08:00' WHERE id = ?",
                 (stale,))

    sql = f"SELECT i.doi FROM item i WHERE i.kind='paper' AND {db.in_window('i')}"
    got = {r[0].split("/")[-1] for r in conn.execute(sql, ("-30 days", "-30 days"))}
    assert "no-date-fresh" in got, "刚入库的无日期条目必须能被评一轮分"
    assert "no-date-empty" in got, "空字符串和 NULL 一样要兜底"
    assert "no-date-stale" not in got, "老条目应随 created_at 自然退出窗口"


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
                          "title": "Photochemical reaction of nanoparticle esters",
                          "title_norm": "photochemical reaction", "abstract": "azides",
                          "source": "a"})
    db.upsert_item(conn, {"kind": "paper", "dedup_key": "doi:10.1/b", "doi": "10.1/b",
                          "title": "Polymer chemistry", "title_norm": "polymer",
                          "source": "a"})
    assert len(db.search_items(conn, "nanoparticle")) == 1
    assert len(db.search_items(conn, "polymer")) == 1
    assert db.search_items(conn, "不存在的词xyz") == []


# ------------------------------------------------------------- 反馈闭环
def _seed(conn, title: str) -> int:
    """插一条落在时间窗内的论文。"""
    import datetime

    iid, _ = db.upsert_item(conn, {
        "kind": "paper", "dedup_key": f"doi:10.1/{title}", "doi": f"10.1/{title}",
        "title": title, "title_norm": title.lower(), "source": "test",
        "published_at": datetime.date.today().isoformat(),
    })
    return iid


def test_反馈样本只把有分数的否决当负例(tmp_path):
    """被规则挡掉的条目用户根本没看见,拿它当负例会污染判断 ——
    所以负例必须 JOIN score,只留"LLM 说相关、用户却否掉"的那些。"""
    conn = db.Database(tmp_path / "t.db").connect()
    liked_id = _seed(conn, "Sensor insertion A")
    scored_id = _seed(conn, "Sensor insertion B")
    bare_id = _seed(conn, "Sensor insertion C")
    db.set_action(conn, liked_id, "star")
    db.set_action(conn, scored_id, "ignore")
    db.set_action(conn, bare_id, "ignore")
    db.save_score(conn, scored_id, final_score=80.0)

    liked, disliked = feedback_examples(conn)
    assert liked == ["Sensor insertion A"]
    assert disliked == ["Sensor insertion B"], "没分数的否决不该进负例"


def test_精排拿到反馈样本(tmp_path, monkeypatch):
    """回归:feedback_examples 写好了却没有任何调用点,精排 prompt 里的
    正负例永远是"(暂无)"—— 收藏/否决只落库,从不影响下一轮打分。"""
    from litradar import rank
    from litradar.config import Config

    cfg = Config()
    cfg.app.db_path = str(tmp_path / "t.db")
    cfg.interests_data = {"direction": "optical sensor",
                          "keywords": {"core": ["optical sensor"]}}

    conn = db.Database(cfg.db_file).connect()
    _seed(conn, "Optical sensor Sample detection")
    star_id = _seed(conn, "Optical sensor sensing methods")
    drop_id = _seed(conn, "Optical sensor polymer coating")
    db.set_action(conn, star_id, "star")
    db.set_action(conn, drop_id, "ignore")
    db.save_score(conn, drop_id, final_score=70.0)
    conn.commit()
    conn.close()

    seen = {}

    def fake_rerank(rows, prof, cfg_, llm, liked=None, disliked=None):
        seen["liked"], seen["disliked"] = liked, disliked
        return {}

    class FakeLLM:
        available = True

        def __init__(self, *a, **kw):
            pass

    monkeypatch.setattr(rank, "llm_rerank", fake_rerank)
    monkeypatch.setattr(rank, "DeepSeek", FakeLLM)

    stat = rank.run(cfg, days=30, verbose=False)
    assert seen["liked"] == ["Optical sensor sensing methods"]
    assert seen["disliked"] == ["Optical sensor polymer coating"]
    # 统计页据此判断闭环有没有在工作
    assert stat["feedback_liked"] == 1
    assert stat["feedback_disliked"] == 1


# ------------------------------------------------------- LLM 部分失败的兜底
class _FakeLLM:
    available = True

    def __init__(self, *a, **kw):
        pass


def _rank_cfg(tmp_path):
    from litradar.config import Config

    cfg = Config()
    cfg.app.db_path = str(tmp_path / "t.db")
    cfg.interests_data = {"direction": "optical sensor",
                          "keywords": {"core": ["optical sensor"]}}
    return cfg


def test_精排部分失败的条目不反超(tmp_path, monkeypatch):
    """回归:某个精排批次失败时,该批条目的 final 被 /0.15 归一化回满量程,
    一个只有粗排分的条目能冲到 100 反超真被 LLM 评过的 —— 而界面上看不出
    它压根没被评过。LLM 跑过的那轮里,缺分的条目必须封顶在 w_coarse+w_rule。"""
    from litradar import rank

    cfg = _rank_cfg(tmp_path)
    conn = db.Database(cfg.db_file).connect()
    # 让**没拿到 LLM 分**的那条恰好是粗排第一(coarse=100):归一化的老写法
    # 会把它抬到 80 分以上,反超真被 LLM 评过的条目。
    failed_id = _seed(conn, "Optical sensor chemistry optical sensor insertion")
    ok_id = _seed(conn, "Optical sensor overview")
    for filler in ("Polymer coating survey", "Total synthesis of a terpene",
                   "Surface analysis of thin films"):
        _seed(conn, filler)
    conn.commit()
    conn.close()

    monkeypatch.setattr(rank, "DeepSeek", _FakeLLM)
    monkeypatch.setattr(rank, "llm_rerank",
                        lambda *a, **kw: {ok_id: (60.0, "相关")})

    rank.run(cfg, days=30, verbose=False)

    conn = db.Database(cfg.db_file).connect()
    got = {r["item_id"]: r for r in conn.execute(
        "SELECT item_id, coarse_score, final_score FROM score")}
    assert got[failed_id]["coarse_score"] == 100.0, "前提:它粗排第一"
    cap = (cfg.ranking.w_coarse + cfg.ranking.w_rule) * 100
    assert got[failed_id]["final_score"] <= cap, "缺 LLM 分的条目不该被归一化放大"
    assert got[failed_id]["final_score"] < got[ok_id]["final_score"], \
        "它绝不能反超真被 LLM 评过的条目"
    conn.close()


def test_完全没有LLM分时仍然归一化(tmp_path, monkeypatch):
    """纯 BM25+规则模式(没配 key)下谁也不会反超谁,分数要铺满 0-100,
    否则界面按分数阈值筛选就没有意义了。"""
    from litradar import rank

    cfg = _rank_cfg(tmp_path)
    conn = db.Database(cfg.db_file).connect()
    _seed(conn, "Optical sensor Sample detection study")
    for filler in ("Polymer coating survey", "Total synthesis of a terpene",
                   "Surface analysis of thin films"):
        _seed(conn, filler)
    conn.commit()
    conn.close()

    class _NoLLM(_FakeLLM):
        available = False

    monkeypatch.setattr(rank, "DeepSeek", _NoLLM)
    rank.run(cfg, days=30, verbose=False)

    conn = db.Database(cfg.db_file).connect()
    top = conn.execute("SELECT MAX(final_score) FROM score").fetchone()[0]
    assert top > (cfg.ranking.w_coarse + cfg.ranking.w_rule) * 100
    conn.close()
