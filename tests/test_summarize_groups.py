"""按组摘要:中性字段共享,只有"对研究的用处"按组各一份。

分组的省钱承诺就在这一层:整条摘要按组重算会把 LLM 成本乘上组数,而摘要里
**只有 relevance 与方向有关**。所以第二个组不该重做问题/方法/结果/局限,
只补那一行。
"""
from __future__ import annotations

import datetime
import re

import pytest

from litradar import db, summarize
from litradar.config import Config

TODAY = datetime.date.today().isoformat()
TWO_GROUPS = {"groups": [
    {"slug": "org", "name": "有机", "direction": "有机合成方法学"},
    {"slug": "mat", "name": "材料", "direction": "钙钛矿材料"},
]}


def _cfg(tmp_path, *, groups=TWO_GROUPS) -> Config:
    cfg = Config()
    cfg.app.db_path = str(tmp_path / "s.db")
    cfg.interests_data = groups
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
    monkeypatch.setattr(summarize, "DeepSeek", FakeLLM)


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
    _seed(conn, "Shared paper", groups=[ids["org"], ids["mat"]])
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
    conn.close()
    # 一次整条 + 一次只补 relevance:第二个组没有重算问题/方法/结果/局限
    assert len([c for c in FakeLLM.calls if c.startswith("full:")]) == 1
    assert len([c for c in FakeLLM.calls if c.startswith("relevance:")]) == 1


def test_第二次运行不再重复花钱(tmp_path):
    cfg = _cfg(tmp_path)
    conn = db.Database(cfg.db_file).connect()
    ids = _ids(cfg)
    _seed(conn, "Shared paper", groups=[ids["org"], ids["mat"]])
    conn.commit()
    conn.close()

    summarize.run(cfg, days=30, verbose=False)
    before = len(FakeLLM.calls)
    out = summarize.run(cfg, days=30, verbose=False)

    assert len(FakeLLM.calls) == before, "都做过了,不该再调 LLM"
    assert out["deep"] == out["brief"] == out["relevance"] == 0


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


def test_运行记录按组记账(tmp_path):
    cfg = _cfg(tmp_path)
    summarize.run(cfg, days=30, verbose=False)

    conn = db.Database(cfg.db_file).connect()
    rows = [(r[0], r[1]) for r in conn.execute(
        "SELECT stage, group_slug FROM run_log ORDER BY id")]
    conn.close()
    assert rows == [("summarize", "org"), ("summarize", "mat")]
