"""收藏夹筛选 + "手动反馈优先于自动规则"的测试。

这两件事是绑在一起的:收藏如果只是一面旗子而进不去任何视图,用户就
永远看不到自己收藏了什么;而如果收藏的条目会被后续的规则过滤悄悄标成
excluded,那这个视图本身也会漏掉东西。所以两处一起测。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from litradar import db  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    database = db.Database(tmp_path / "t.db")
    database.init()
    c = database.connect()
    yield c
    c.close()


def _add(conn, title: str, *, kind: str = "paper") -> int:
    iid, _ = db.upsert_item(conn, {
        "kind": kind, "dedup_key": f"doi:10.1/{title}", "doi": f"10.1/{title}",
        "title": title, "title_norm": title.lower(), "published_at": "2026-09-01",
        "source": "test",
    })
    # 收件箱只放本组成员;真实链路里由 pipeline._store(group_id=...) 记
    db.add_to_group(conn, db.ensure_group(conn, db.DEFAULT_GROUP_SLUG,
                                         name=db.DEFAULT_GROUP_NAME), iid)
    return iid


def test_starred_is_a_view_of_its_own(conn):
    """收藏过的条目要能在 state='starred' 下取到 —— 而且不看它读没读过。"""
    a, b = _add(conn, "a"), _add(conn, "b")
    db.set_action(conn, a, "star")
    db.set_action(conn, b, "star")
    db.set_action(conn, b, "read")      # 收藏同时已读,仍然算收藏

    assert db.count_items(conn, state="starred") == 2
    ids = {r["id"] for r in db.get_items(conn, state="starred")}
    assert ids == {a, b}
    # 未读视图不该出现被标已读的那条
    assert {r["id"] for r in db.get_items(conn, state="new")} == {a}
    db.set_action(conn, a, "unstar")
    assert [r["id"] for r in db.get_items(conn, state="starred")] == [b]
    assert db.count_items(conn, state="starred") == 1


def test_starred_survives_exclusion(conn):
    """关键回归:规则把条目标成 excluded 之后,收藏夹里仍然要有它。

    收藏是用户亲手挑的,不能被自动规则"吃掉"。实测踩过:收紧检索词后
    收藏的条目直接消失,而用户收不到任何提示。
    """
    a, b = _add(conn, "a"), _add(conn, "b")
    db.set_action(conn, a, "star")
    db.set_excluded(conn, [a, b], True)

    assert db.count_items(conn, state="starred") == 1
    assert [r["id"] for r in db.get_items(conn, state="starred")] == [a]
    # 其余视图照旧隐藏 excluded(state=None 就是网页上的"全部")
    assert db.count_items(conn, state=None) == 0
    assert db.count_items(conn, state="new") == 0


def test_starred_count_respects_kind(conn):
    """收藏夹按 kind 分开数。

    专利页已经砍了,但 item.kind 这个维度还在(count_items / get_items 都收它),
    所以这条测试验的是参数作用域本身,不是那个已删的页面。"""
    p = _add(conn, "p", kind="paper")
    q = _add(conn, "q", kind="patent")
    db.set_action(conn, p, "star")
    db.set_action(conn, q, "star")
    assert db.count_items(conn, kind="paper", state="starred") == 1
    assert db.count_items(conn, kind="patent", state="starred") == 1
    assert db.count_items(conn, kind=None, state="starred") == 2


# ------------------------------------------------- "不感兴趣"收纳(单独页签)

def test_ignored_leaves_every_normal_view(conn):
    """点了"不感兴趣"就不该再出现在 未读 / 已读 / 全部 里。

    之前的实现只在"未读"过滤 ignored,于是"已读"和"全部"里还混着一堆
    自己明确否决过的东西,卡片上又没有任何标记 —— 看不出它为什么在那儿。
    """
    a, b = _add(conn, "a"), _add(conn, "b")
    db.set_action(conn, a, "ignore")
    db.set_action(conn, b, "read")
    db.set_action(conn, a, "star")
    assert db.starred_ids(conn) == {a}
    assert db.count_items(conn, state="starred") == 1
    assert db.count_items(conn, state="ignored") == 1
    assert [r["id"] for r in db.get_items(conn, state="ignored")] == [a]

    for view in ("new", "read", None):
        ids = {r["id"] for r in db.get_items(conn, state=view)}
        assert a not in ids, f"被否决的条目不该出现在 state={view}"
    db.set_action(conn, a, "unignore")
    assert db.count_items(conn, state="ignored") == 0
    assert [r["id"] for r in db.get_items(conn, state="new")] == [a]


def test_ignored_view_ignores_score_threshold_only_when_asked(conn):
    """"不感兴趣"页签同样支持分数筛选 —— 回看时想只看高分的未采纳候选。"""
    a, b = _add(conn, "a"), _add(conn, "b")
    db.set_action(conn, a, "ignore")
    db.set_action(conn, b, "ignore")
    db.save_score(conn, a, final_score=80.0)
    db.save_score(conn, b, final_score=10.0)
    assert db.count_items(conn, state="ignored") == 2
    assert db.count_items(conn, state="ignored", min_score=50) == 1


# ------------------------------------------------------- 反馈 action 白名单

def test_unknown_action_is_rejected_and_not_logged(conn):
    """回归:未知 action 以前不改状态,却照样原样写进 feedback 表。"""
    a = _add(conn, "a")
    with pytest.raises(ValueError):
        db.set_action(conn, a, "drop table")
    assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0
    assert db.count_items(conn, state="new") == 1


# ------------------------------------------------------- 统计页口径

def test_stats_new_matches_inbox_tab(conn):
    """回归:统计页"未读"直接数 item_state,没排除 excluded、没限定 kind,
    和首页"未读"页签(count_items)对不上。"""
    a, b, c, d = _add(conn, "a"), _add(conn, "b"), _add(conn, "c"), _add(conn, "d")
    db.set_action(conn, b, "ignore")
    db.set_excluded(conn, [c], True)
    db.set_action(conn, d, "read")
    _add(conn, "p", kind="patent")
    assert db.stats(conn)["new"] == db.count_items(conn, kind="paper", state="new") == 1
