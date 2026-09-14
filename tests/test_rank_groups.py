"""按订阅组排序:各算各的分、各记各的排除、LLM 按组开关。

分组真正落地的地方在这一层:同一篇文献在两个方向里可以拿到完全不同的分数,
因为规则、BM25 与 LLM 都是拿**那个组**的画像在评。同时钉住三件事:
  · 候选只取本组成员(否则花钱的精排会随组数翻倍);
  · 规则排除是按组的(对 A 无关的可能是 B 的核心);
  · llm_rank: false 真的不调 LLM,而分数仍然可用。
"""
from __future__ import annotations

import datetime

import pytest

from litradar import db, rank
from litradar.config import Config

TODAY = datetime.date.today().isoformat()


def _cfg(tmp_path, groups) -> Config:
    cfg = Config()
    cfg.app.db_path = str(tmp_path / "rank.db")
    cfg.interests_data = {"groups": groups}
    return cfg


def _seed(conn, title: str, *, groups: list[int] | None = None) -> int:
    iid, _ = db.upsert_item(conn, {
        "kind": "paper", "dedup_key": f"doi:10.1/{title}", "doi": f"10.1/{title}",
        "title": title, "title_norm": title.lower(), "source": "test",
        "published_at": TODAY,
    })
    for gid in groups or []:
        db.add_to_group(conn, gid, iid)
    return iid


class _NoLLM:
    """没有任何 LLM 可用的场景(没配 key):规则与 BM25 仍要照常工作。"""

    available = False

    def __init__(self, *a, **kw):
        pass


@pytest.fixture
def no_llm(monkeypatch):
    monkeypatch.setattr(rank, "LLMClient", _NoLLM)


def _scores(cfg) -> dict[tuple[str, str], float]:
    """{(slug, title): final_score}"""
    conn = db.Database(cfg.db_file).connect()
    try:
        return {(r[0], r[1]): r[2] for r in conn.execute(
            """SELECT g.slug, i.title, sc.final_score FROM score sc
                 JOIN interest_group g ON g.id = sc.group_id
                 JOIN item i ON i.id = sc.item_id""")}
    finally:
        conn.close()


# ─────────────────────────────────────── 两组各算各的分
def test_同一篇在两个组里的分数可以不同(tmp_path, no_llm):
    """这是分组的核心承诺:同一个方向库,不同的规则给不同的分。"""
    # 两组的 core 词个数不同 → 规则分不同;query 不同 → BM25 也不同。
    cfg = _cfg(tmp_path, [
        {"slug": "org", "name": "有机",
         "keywords": {"core": ["sensor", "hybrid"]}},
        {"slug": "mat", "name": "材料", "keywords": {"core": ["perovskite"]}},
    ])
    conn = db.Database(cfg.db_file).connect()
    ids = db.sync_groups(conn, rank.load_groups(cfg))
    _seed(conn, "Sensor and perovskite hybrid", groups=[ids["org"], ids["mat"]])
    conn.commit()
    conn.close()

    rank.run(cfg, days=30, verbose=False)

    got = _scores(cfg)
    org = got[("org", "Sensor and perovskite hybrid")]
    mat = got[("mat", "Sensor and perovskite hybrid")]
    # 同一篇、同一次运行,两组拿到不同的分 —— 这正是分组的承诺
    assert org != mat, "两组的关键词不同,分数不该一样"
    assert org > mat, "命中两个 core 词的组该给更高分"


def test_只给本组成员打分(tmp_path, no_llm):
    """B 组的成员不该被 A 组评分 —— 否则精排花费随组数线性翻倍。"""
    cfg = _cfg(tmp_path, [
        {"slug": "org", "name": "有机", "keywords": {"core": ["sensor"]}},
        {"slug": "mat", "name": "材料", "keywords": {"core": ["perovskite"]}},
    ])
    conn = db.Database(cfg.db_file).connect()
    ids = db.sync_groups(conn, rank.load_groups(cfg))
    _seed(conn, "Sensor chemistry", groups=[ids["org"]])
    _seed(conn, "Perovskite solar cell", groups=[ids["mat"]])
    conn.commit()
    conn.close()

    out = rank.run(cfg, days=30, verbose=False)

    got = _scores(cfg)
    assert ("org", "Sensor chemistry") in got
    assert ("mat", "Sensor chemistry") not in got
    assert ("mat", "Perovskite solar cell") in got
    assert out["groups"]["org"]["candidates"] == 1
    assert out["groups"]["mat"]["candidates"] == 1


# ─────────────────────────────────────── 排除按组
def test_规则排除是按组的(tmp_path, no_llm):
    """被 A 组规则否掉的文献,在 B 组照常评分 —— 它可能是 B 的核心方向。"""
    cfg = _cfg(tmp_path, [
        {"slug": "org", "name": "有机", "keywords": {"core": ["sensor"]},
         "negative": ["perovskite"]},
        {"slug": "mat", "name": "材料", "keywords": {"core": ["perovskite"]}},
    ])
    conn = db.Database(cfg.db_file).connect()
    ids = db.sync_groups(conn, rank.load_groups(cfg))
    iid = _seed(conn, "Perovskite solar cell", groups=[ids["org"], ids["mat"]])
    conn.commit()
    conn.close()

    rank.run(cfg, days=30, verbose=False)

    conn = db.Database(cfg.db_file).connect()
    rows = {r[0]: r[1] for r in conn.execute(
        """SELECT g.slug, gs.excluded FROM group_state gs
             JOIN interest_group g ON g.id = gs.group_id
            WHERE gs.item_id = ?""", (iid,))}
    conn.close()
    assert rows.get("org") == 1, "A 组的 negative 命中了它"
    assert rows.get("mat", 0) == 0, "B 组不该跟着被排除"
    assert ("mat", "Perovskite solar cell") in _scores(cfg)


def test_收藏的条目不会被规则排除(tmp_path, no_llm):
    """沿用既有语义:收藏是用户明确说过的"我要留着",按组也一样。"""
    cfg = _cfg(tmp_path, [{"slug": "org", "name": "有机",
                           "keywords": {"core": ["sensor"]},
                           "negative": ["perovskite"]}])
    conn = db.Database(cfg.db_file).connect()
    ids = db.sync_groups(conn, rank.load_groups(cfg))
    iid = _seed(conn, "Perovskite solar cell", groups=[ids["org"]])
    db.set_action(conn, iid, "star")
    conn.commit()
    conn.close()

    rank.run(cfg, days=30, verbose=False)

    conn = db.Database(cfg.db_file).connect()
    excluded = conn.execute(
        "SELECT excluded FROM group_state WHERE item_id=?", (iid,)).fetchone()
    conn.close()
    assert excluded is None or excluded[0] == 0, \
        "收藏的条目不该被标成排除,否则它就从收藏夹里消失了"
    # 注意:这层保护管的是**可见性**(收藏夹里还在),而不是"照常参与本轮评分"——
    # 被规则丢掉的条目本来就不进精排,这与分组无关,沿用既有语义。


# ─────────────────────────────────────── LLM 按组开关
def test_llm_rank_false_不调_LLM_但仍有分数(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, [
        {"slug": "free", "name": "不花钱的组", "keywords": {"core": ["sensor"]},
         "llm_rank": False},
    ])
    calls = []

    class _FakeLLM:
        available = True

        def __init__(self, *a, **kw):
            pass

    def fake_rerank(*a, **kw):
        calls.append(1)
        return {}

    monkeypatch.setattr(rank, "LLMClient", _FakeLLM)
    monkeypatch.setattr(rank, "llm_rerank", fake_rerank)
    conn = db.Database(cfg.db_file).connect()
    ids = db.sync_groups(conn, rank.load_groups(cfg))
    _seed(conn, "Sensor chemistry", groups=[ids["free"]])
    conn.commit()
    conn.close()

    out = rank.run(cfg, days=30, verbose=False)

    assert calls == [], "llm_rank: false 的组绝不能花钱"
    stat = out["groups"]["free"]
    assert "关闭" in stat["llm_skipped"]
    assert stat["scored"] == 1
    assert _scores(cfg)[("free", "Sensor chemistry")] is not None


def test_开着的组照常调_LLM(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, [
        {"slug": "paid", "name": "花钱的组", "keywords": {"core": ["sensor"]},
         "llm_rank": True},
    ])
    calls = []

    class _FakeLLM:
        available = True

        def __init__(self, *a, **kw):
            pass

    def fake_rerank(rows, prof, *a, **kw):
        calls.append(len(rows))
        return {}

    monkeypatch.setattr(rank, "LLMClient", _FakeLLM)
    monkeypatch.setattr(rank, "llm_rerank", fake_rerank)
    conn = db.Database(cfg.db_file).connect()
    ids = db.sync_groups(conn, rank.load_groups(cfg))
    _seed(conn, "Sensor chemistry", groups=[ids["paid"]])
    conn.commit()
    conn.close()

    rank.run(cfg, days=30, verbose=False)

    assert calls == [1]


# ─────────────────────────────────────── 只跑一个组 / 失败隔离
def test_只跑指定的组(tmp_path, no_llm):
    cfg = _cfg(tmp_path, [
        {"slug": "org", "name": "有机", "keywords": {"core": ["sensor"]}},
        {"slug": "mat", "name": "材料", "keywords": {"core": ["perovskite"]}},
    ])
    conn = db.Database(cfg.db_file).connect()
    ids = db.sync_groups(conn, rank.load_groups(cfg))
    _seed(conn, "Sensor chemistry", groups=[ids["org"]])
    _seed(conn, "Perovskite cell", groups=[ids["mat"]])
    conn.commit()
    conn.close()

    out = rank.run(cfg, days=30, verbose=False, group=rank.load_groups(cfg)[1])

    assert set(out["groups"]) == {"mat"}
    got = _scores(cfg)
    assert ("mat", "Perovskite cell") in got
    assert not any(slug == "org" for slug, _ in got)


def test_一个组排序失败不影响其它组(tmp_path, monkeypatch, no_llm):
    cfg = _cfg(tmp_path, [
        {"slug": "bad", "name": "坏组", "keywords": {"core": ["sensor"]}},
        {"slug": "good", "name": "好组", "keywords": {"core": ["sensor"]}},
    ])
    conn = db.Database(cfg.db_file).connect()
    ids = db.sync_groups(conn, rank.load_groups(cfg))
    _seed(conn, "Sensor chemistry", groups=[ids["bad"], ids["good"]])
    conn.commit()
    conn.close()

    original = rank.rule_filter

    def flaky(rows, prof):
        if prof.slug == "bad":
            raise RuntimeError("画像写坏了")
        return original(rows, prof)

    monkeypatch.setattr(rank, "rule_filter", flaky)

    out = rank.run(cfg, days=30, verbose=False)

    assert out["errors"] == 1
    assert "画像写坏了" in out["groups"]["bad"]["error"]
    assert ("good", "Sensor chemistry") in _scores(cfg)


def test_运行记录按组记账(tmp_path, no_llm):
    """花费护栏按 (阶段, 组) 计数,靠的就是这两行 run_log。"""
    cfg = _cfg(tmp_path, [
        {"slug": "org", "name": "有机"}, {"slug": "mat", "name": "材料"}])
    rank.run(cfg, days=30, verbose=False)

    conn = db.Database(cfg.db_file).connect()
    rows = [(r[0], r[1]) for r in conn.execute(
        "SELECT stage, group_slug FROM run_log ORDER BY id")]
    conn.close()
    assert rows == [("rank", "org"), ("rank", "mat")]
