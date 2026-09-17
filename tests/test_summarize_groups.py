"""按组摘要:中性字段共享,只有"对研究的用处"按组各一份。

分组的省钱承诺就在这一层:整条摘要按组重算会把 LLM 成本乘上组数,而摘要里
**只有 relevance 与方向有关**。所以第二个组不该重做问题/方法/结果/局限,
只补那一行。
"""
from __future__ import annotations

import datetime
import re
from copy import deepcopy

import pytest

from litradar import db, summarize
from litradar.config import Config

TODAY = datetime.date.today().isoformat()
TWO_GROUPS = {"groups": [
    {"slug": "org", "name": "有机", "direction": "示例方向 A"},
    {"slug": "mat", "name": "材料", "direction": "钙钛矿材料"},
]}


def _cfg(tmp_path, *, groups=TWO_GROUPS) -> Config:
    cfg = Config()
    cfg.app.db_path = str(tmp_path / "s.db")
    cfg.interests_data = deepcopy(groups)
    return cfg


def _seed(conn, title: str, *, groups: list[int]) -> int:
    iid, _ = db.upsert_item(conn, {
        "kind": "paper", "dedup_key": f"doi:10.1/{title}", "doi": f"10.1/{title}",
        "title": title, "title_norm": title.lower(), "source": "test",
        "abstract": f"abstract of {title}", "published_at": TODAY,
    })
    for gid in groups:
        db.add_to_group(conn, gid, iid)
    return iid


class FakeLLM:
    """把"当前是哪个方向"写进返回值,这样能直接断言两组拿到不同的 relevance。"""

    available = True
    calls: list[str] = []

    def __init__(self, *a, **kw):
        pass

    def json(self, system, user, *, max_tokens=2048):
        m = re.search(r"【用户研究方向】\n(.+)", user)
        direction = (m.group(1).strip() if m else "")
        if "只输出 JSON,一个字段" in user:          # relevance-only 那条 prompt
            FakeLLM.calls.append(f"relevance:{direction}")
            return {"relevance": f"relevance-only for {direction}"}
        FakeLLM.calls.append(f"full:{direction}")
        if "items" in user:                         # 批量简要
            return {"items": [{"id": 1, "title_zh": "标题", "one_liner": "结论"}]}
        return {"title_zh": "标题", "one_liner": "结论", "problem": "p",
                "method": "m", "key_results": "k", "limitation": "l",
                "relevance": f"full summary for {direction}"}


@pytest.fixture(autouse=True)
def _reset_calls(monkeypatch):
    FakeLLM.calls = []
    monkeypatch.setattr(summarize, "LLMClient", FakeLLM)


def _relevances(cfg) -> dict[tuple[str, str], str]:
    conn = db.Database(cfg.db_file).connect()
    try:
        return {(r[0], r[1]): r[2] for r in conn.execute(
            """SELECT g.slug, i.title, sg.relevance FROM summary_group sg
                 JOIN interest_group g ON g.id = sg.group_id
                 JOIN item i ON i.id = sg.item_id""")}
    finally:
        conn.close()


def _ids(cfg) -> dict[str, int]:
    """把 interests.yaml 里的组对账进库并返回 slug → id(真实流程里由流水线做)。"""
    from litradar.rank import load_groups

    conn = db.Database(cfg.db_file).connect()
    try:
        mapping = db.sync_groups(conn, load_groups(cfg))
        conn.commit()
        return mapping
    finally:
        conn.close()


# ───────────────────────────────────── 一组一份 relevance,中性字段共享
def test_两组各拿一份_relevance_而中性摘要只算一次(tmp_path):
    cfg = _cfg(tmp_path)
    conn = db.Database(cfg.db_file).connect()
    ids = _ids(cfg)
    iid = _seed(conn, "Shared paper", groups=[ids["org"], ids["mat"]])
    # 真实流程先排序再摘要。复合 score 主键下漏 group JOIN 会复制同一条 item。
    db.save_score(conn, iid, group_id=ids["org"], final_score=60)
    db.save_score(conn, iid, group_id=ids["mat"], final_score=90)
    conn.commit()
    conn.close()

    summarize.run(cfg, days=30, verbose=False)

    got = _relevances(cfg)
    assert got[("org", "Shared paper")] != got[("mat", "Shared paper")], \
        "两组方向不同,relevance 不该一样"
    conn = db.Database(cfg.db_file).connect()
    # 中性摘要只有一份 —— 这是省钱的关键
    assert conn.execute("SELECT COUNT(*) FROM summary").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM summary_group").fetchone()[0] == 2
    assert [tuple(r) for r in conn.execute("SELECT stage,group_slug FROM run_log ORDER BY id")] == [
        ("summarize", "org"), ("summarize", "mat")]
    conn.close()
    # 一次整条 + 一次只补 relevance:第二个组没有重算问题/方法/结果/局限
    assert len([c for c in FakeLLM.calls if c.startswith("full:")]) == 1
    assert len([c for c in FakeLLM.calls if c.startswith("relevance:")]) == 1
    before = len(FakeLLM.calls)
    out = summarize.run(cfg, days=30, verbose=False)
    assert len(FakeLLM.calls) == before
    assert out["deep"] == out["brief"] == out["relevance"] == 0


@pytest.mark.parametrize("deep_n", [0, 1])
def test_共享论文的待处理队列和深度名额只按本组评分(tmp_path, monkeypatch, deep_n):
    from litradar.rank import load_groups

    cfg = _cfg(tmp_path)
    cfg.llm.deep_summary_top_n = deep_n
    ids = _ids(cfg)
    conn = db.Database(cfg.db_file).connect()
    low = _seed(conn, "High in materials", groups=list(ids.values()))
    high = _seed(conn, "High in organic", groups=list(ids.values()))
    for iid, org, mat in [(low, 1, 100), (high, 90, 2)]:
        db.save_score(conn, iid, group_id=ids["org"], final_score=org)
        db.save_score(conn, iid, group_id=ids["mat"], final_score=mat)
    conn.commit()
    conn.close()
    deep_calls, brief_calls = [], []

    def deep(row, prof, llm):
        deep_calls.append(row["id"])
        return {"one_liner": "deep", "relevance": prof.slug}

    def brief(rows, llm):
        brief_calls.extend(r["id"] for r in rows)
        return {r["id"]: {"one_liner": "brief"} for r in rows}

    monkeypatch.setattr(summarize, "summarize_one", deep)
    monkeypatch.setattr(summarize, "summarize_brief", brief)
    out = summarize.run(cfg, limit=2, group=load_groups(cfg)[0], verbose=False)
    assert out["errors"] == 0
    assert out["groups"]["org"]["pending"] == 2
    assert deep_calls == ([high] if deep_n else [])
    assert brief_calls == ([low] if deep_n else [high, low])
    conn = db.Database(cfg.db_file).connect()
    assert conn.execute("SELECT COUNT(*) FROM summary").fetchone()[0] == 2
    conn.close()


# ───────────────────────────────────── 原文变了要一起作废
def test_原文更新后其它组的_relevance_会重算(tmp_path):
    """别的组那一行是基于旧原文写的;不清掉它就会一直停着,而读者看不出过期。"""
    cfg = _cfg(tmp_path)
    conn = db.Database(cfg.db_file).connect()
    ids = _ids(cfg)
    iid = _seed(conn, "Shared paper", groups=[ids["org"], ids["mat"]])
    conn.commit()
    conn.close()
    summarize.run(cfg, days=30, verbose=False)

    conn = db.Database(cfg.db_file).connect()
    conn.execute("UPDATE item SET abstract='a much longer abstract' WHERE id=?", (iid,))
    conn.commit()
    conn.close()
    FakeLLM.calls = []

    summarize.run(cfg, days=30, verbose=False)

    # 一个组整条重做,另一个组只补 relevance
    assert len([c for c in FakeLLM.calls if c.startswith("full:")]) == 1
    assert len([c for c in FakeLLM.calls if c.startswith("relevance:")]) == 1
    got = _relevances(cfg)
    assert len(got) == 2


# ───────────────────────────────────── 只跑一个组 / 失败隔离
def test_只跑指定的组(tmp_path):
    cfg = _cfg(tmp_path)
    conn = db.Database(cfg.db_file).connect()
    ids = _ids(cfg)
    _seed(conn, "Shared paper", groups=[ids["org"], ids["mat"]])
    conn.commit()
    conn.close()

    from litradar.rank import load_groups

    out = summarize.run(cfg, days=30, verbose=False, group=load_groups(cfg)[1])

    assert set(out["groups"]) == {"mat"}
    got = _relevances(cfg)
    assert set(got) == {("mat", "Shared paper")}


def test_一个组失败不影响其它组(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    conn = db.Database(cfg.db_file).connect()
    ids = _ids(cfg)
    _seed(conn, "Shared paper", groups=[ids["org"], ids["mat"]])
    conn.commit()
    conn.close()

    original = summarize.summarize_one

    def flaky(row, prof, llm):
        if prof.slug == "org":
            raise RuntimeError("画像写坏了")
        return original(row, prof, llm)

    monkeypatch.setattr(summarize, "summarize_one", flaky)

    out = summarize.run(cfg, days=30, verbose=False)

    assert out["errors"] == 1
    assert "画像写坏了" in out["groups"]["org"]["error"]
    assert out["groups"]["mat"]["deep"] == 1
    assert set(_relevances(cfg)) == {("mat", "Shared paper")}


@pytest.mark.parametrize("depth,reverse", [(None, False), (None, True),
                                          ("brief", False), ("deep", True)])
def test_force共享摘要只刷新一次且保留两组说明(tmp_path, depth, reverse):
    cfg = _cfg(tmp_path)
    if reverse:
        cfg.interests_data["groups"].reverse()
    ids = _ids(cfg)
    conn = db.Database(cfg.db_file).connect()
    iid = _seed(conn, "Shared paper", groups=list(ids.values()))
    for gid in ids.values():
        db.save_score(conn, iid, group_id=gid, final_score=80)
        if depth == "deep":
            db.save_group_relevance(conn, gid, iid, "old relevance", "old")
    if depth:
        db.save_summary(conn, iid, {"one_liner": "old"}, depth, "old",
                        abstract_hash=summarize._abstract_hash("abstract of Shared paper"))
    conn.commit()
    conn.close()

    out = summarize.run(cfg, force=True, verbose=False)

    assert out["errors"] == 0
    assert out["deep"] == out["relevance"] == 1
    assert out["brief"] == 0
    assert len(FakeLLM.calls) == 2
    got = _relevances(cfg)
    assert "示例方向 A" in got[("org", "Shared paper")]
    assert "钙钛矿材料" in got[("mat", "Shared paper")]
    conn = db.Database(cfg.db_file).connect()
    # 共享表不再写入某个方向的说明,避免后续调用方误用。
    assert conn.execute("SELECT relevance FROM summary WHERE item_id=?", (iid,)).fetchone()[0] is None
    conn.close()
    summarize.run(cfg, verbose=False)
    assert len(FakeLLM.calls) == 2


# Cover every pair of independent options without the full Cartesian product.
@pytest.mark.parametrize("force,limit,existing_brief", [
    (False, 1, False), (False, 20, True), (True, 1, True), (True, 20, False),
])
def test_后处理组的深度需求不会漏掉前组说明或重复生成简要摘要(
        tmp_path, monkeypatch, force, limit, existing_brief):
    cfg = _cfg(tmp_path)
    cfg.llm.deep_summary_top_n = 1
    ids = _ids(cfg)
    conn = db.Database(cfg.db_file).connect()
    shared = _seed(conn, "Shared paper", groups=list(ids.values()))
    high = _seed(conn, "Organic winner", groups=[ids["org"]])
    for iid, slug, score in [(shared, "org", 10), (shared, "mat", 99), (high, "org", 99)]:
        db.save_score(conn, iid, group_id=ids[slug], final_score=score)
    if existing_brief:
        db.save_summary(conn, shared, {"one_liner": "brief"}, "brief", "old",
                        abstract_hash=summarize._abstract_hash("abstract of Shared paper"))
    conn.commit()
    conn.close()
    full, brief = [], []
    original = summarize.summarize_one

    def deep(row, prof, llm):
        full.append(row["id"])
        return original(row, prof, llm)

    def batch(rows, llm):
        brief.extend(r["id"] for r in rows)
        return {r["id"]: {"one_liner": "brief"} for r in rows}

    monkeypatch.setattr(summarize, "summarize_one", deep)
    monkeypatch.setattr(summarize, "summarize_brief", batch)
    out = summarize.run(cfg, limit=limit, force=force, verbose=False)

    assert out["errors"] == 0
    assert sorted(full) == sorted([shared, high])
    assert brief == []
    got = _relevances(cfg)
    assert "示例方向 A" in got[("org", "Shared paper")]
    assert "钙钛矿材料" in got[("mat", "Shared paper")]


def test_force指定组保留未参与组的有效说明(tmp_path):
    from litradar.rank import load_groups

    cfg = _cfg(tmp_path)
    ids = _ids(cfg)
    conn = db.Database(cfg.db_file).connect()
    _seed(conn, "Shared paper", groups=list(ids.values()))
    conn.commit()
    conn.close()
    summarize.run(cfg, verbose=False)
    before = _relevances(cfg)[("mat", "Shared paper")]
    FakeLLM.calls = []

    summarize.run(cfg, group=load_groups(cfg)[0], force=True, verbose=False)

    assert FakeLLM.calls == ["full:示例方向 A"]
    assert _relevances(cfg)[("mat", "Shared paper")] == before


@pytest.mark.parametrize("fields,created,enriched", [
    ({"problem": "p", "method": "m", "key_results": "k", "limitation": "l"}, None, "absent"),
    ({"method": "m"}, None, None),
    ({"problem": "p"}, "2026-01-01", "absent"),
    ({"method": "m"}, "2026-01-01", "2025-01-01"),
])
def test_无指纹的旧摘要能补新组说明且不重算中性内容(tmp_path, fields, created, enriched):
    cfg = _cfg(tmp_path)
    ids = _ids(cfg)
    conn = db.Database(cfg.db_file).connect()
    iid = _seed(conn, "Legacy paper", groups=list(ids.values()))
    db.save_summary(conn, iid, fields | {"one_liner": "legacy neutral"}, "deep", "old")
    conn.execute("UPDATE summary SET created_at=? WHERE item_id=?", (created, iid))
    db.save_group_relevance(conn, ids["org"], iid, "legacy organic", "old")
    if enriched != "absent":
        conn.execute("INSERT INTO item_enrichment(item_id,enriched_at) VALUES (?,?)", (iid, enriched))
    conn.commit()
    conn.close()

    out = summarize.run(cfg, verbose=False)

    assert out["errors"] == 0
    assert out["deep"] == 0 and out["relevance"] == 1
    assert FakeLLM.calls == ["relevance:钙钛矿材料"]
    assert _relevances(cfg)[("org", "Legacy paper")] == "legacy organic"
    conn = db.Database(cfg.db_file).connect()
    assert conn.execute("SELECT one_liner FROM summary WHERE item_id=?", (iid,)).fetchone()[0] == "legacy neutral"
    conn.close()


@pytest.mark.parametrize("direction,rename,refresh", [
    ("示例方向 A", True, False),
    ("  示例方向 A  ", False, False),
    ("酶工程", False, True),
    ("", False, True),
])
def test_仅有效方向变化才补本组说明(tmp_path, direction, rename, refresh):
    from litradar.rank import load_groups

    cfg = _cfg(tmp_path)
    ids = _ids(cfg)
    conn = db.Database(cfg.db_file).connect()
    iid = _seed(conn, "Shared paper", groups=list(ids.values()))
    conn.commit()
    conn.close()
    summarize.run(cfg, verbose=False)
    before = _relevances(cfg)
    FakeLLM.calls = []
    cfg.interests_data["groups"][0]["direction"] = direction
    if rename:
        cfg.interests_data["groups"][0]["name"] = "新名字"
    conn = db.Database(cfg.db_file).connect()
    neutral = tuple(conn.execute("SELECT * FROM summary WHERE item_id=?", (iid,)).fetchone())
    db.sync_groups(conn, load_groups(cfg))
    conn.commit()
    got = _relevances(cfg)
    assert (("org", "Shared paper") not in got) == refresh
    assert got[("mat", "Shared paper")] == before[("mat", "Shared paper")]

    out = summarize.run(cfg, verbose=False)

    assert out["deep"] == 0 and out["relevance"] == int(refresh)
    assert len(FakeLLM.calls) == int(refresh)
    assert tuple(conn.execute("SELECT * FROM summary WHERE item_id=?", (iid,)).fetchone()) == neutral
    conn.close()


def test_方向只有提示词实际使用的前300字影响说明缓存(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.interests_data["groups"][0]["direction"] = "有" * 300 + "旧后缀"
    ids = _ids(cfg)
    conn = db.Database(cfg.db_file).connect()
    _seed(conn, "Shared paper", groups=list(ids.values()))
    conn.commit()
    conn.close()
    summarize.run(cfg, verbose=False)
    FakeLLM.calls = []

    cfg.interests_data["groups"][0]["direction"] = "有" * 300 + "新后缀"
    summarize.run(cfg, verbose=False)
    assert FakeLLM.calls == []

    cfg.interests_data["groups"][0]["direction"] = "新" + "有" * 299 + "新后缀"
    summarize.run(cfg, verbose=False)
    assert FakeLLM.calls == ["relevance:" + "新" + "有" * 299]


def test_新组说明失败后下轮只重试说明(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    ids = _ids(cfg)
    conn = db.Database(cfg.db_file).connect()
    _seed(conn, "Shared paper", groups=list(ids.values()))
    conn.commit()
    conn.close()
    original = summarize.summarize_relevance

    def fail(*args):
        raise RuntimeError("relevance service unavailable")

    monkeypatch.setattr(summarize, "summarize_relevance", fail)
    out = summarize.run(cfg, verbose=False)
    assert out["groups"]["mat"]["relevance_failed"] == 1
    assert set(_relevances(cfg)) == {("org", "Shared paper")}
    monkeypatch.setattr(summarize, "summarize_relevance", original)
    FakeLLM.calls = []
    summarize.run(cfg, verbose=False)
    assert FakeLLM.calls == ["relevance:钙钛矿材料"]
