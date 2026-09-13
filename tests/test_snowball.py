"""引用滚雪球:增量累积 + 共被引闸门。

设计要点都值得钉住,因为每一条都是踩出来的:
  · 种子要轮着刷新(连打十几个必撞 429,失败一半)
  · 刷新失败也要记时间戳(否则死种子每轮都排最前,永远刷不到别人)
  · 共被引在全量历史上统计(否则一次失败就静默漏判)
  · 已在库的不再重复返回
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


def _cite(doi: str, title: str = "T") -> dict:
    return {"doi": doi, "title": title, "source": "semanticscholar",
            "published_at": "2026-06-01"}


# --------------------------------------------------------- 种子轮换
def test_没查过的种子排最前(conn):
    seeds = ["s1", "s2", "s3", "s4"]
    db.mark_seed_queried(conn, "s2", 3)
    db.mark_seed_queried(conn, "s3", 3)
    got = db.pick_seeds_to_query(conn, seeds, 2)
    assert set(got) == {"s1", "s4"}


def test_查过的按时间从早到晚轮(conn):
    seeds = ["s1", "s2", "s3"]
    for s in seeds:
        db.mark_seed_queried(conn, s, 1)
    conn.execute("UPDATE seed_query SET queried_at='2026-01-01' WHERE seed_doi='s3'")
    conn.execute("UPDATE seed_query SET queried_at='2026-05-01' WHERE seed_doi='s2'")
    conn.execute("UPDATE seed_query SET queried_at='2026-09-01' WHERE seed_doi='s1'")
    assert db.pick_seeds_to_query(conn, seeds, 2) == ["s3", "s2"]


def test_刷新失败也要记时间戳(conn):
    """否则一个 404 的死种子永远排最前,把额度全吃掉,别的种子永远轮不到。"""
    seeds = ["dead", "live"]
    db.mark_seed_queried(conn, "dead", 0, status="failed")
    db.mark_seed_queried(conn, "live", 0, status="failed")
    # 两个都查过(都失败),此时顺序应由时间决定,而不是永远卡在 dead
    conn.execute("UPDATE seed_query SET queried_at='2026-09-01' WHERE seed_doi='dead'")
    conn.execute("UPDATE seed_query SET queried_at='2026-01-01' WHERE seed_doi='live'")
    assert db.pick_seeds_to_query(conn, seeds, 1) == ["live"]


def test_每轮取几个由上限决定(conn):
    seeds = [f"s{i}" for i in range(10)]
    assert len(db.pick_seeds_to_query(conn, seeds, 3)) == 3
    assert db.pick_seeds_to_query(conn, seeds, 0) == []


# --------------------------------------------------------- 引用关系累积
def test_同一关系重复写只算一次(conn):
    db.save_seed_cites(conn, "s1", [_cite("10.1/a"), _cite("10.1/b")])
    db.save_seed_cites(conn, "s1", [_cite("10.1/a")])       # 再见到一次
    assert conn.execute("SELECT COUNT(*) FROM seed_cite").fetchone()[0] == 2


def test_没有DOI的引用方被丢掉(conn):
    db.save_seed_cites(conn, "s1", [{"title": "无 DOI"}, _cite("10.1/a")])
    assert conn.execute("SELECT COUNT(*) FROM seed_cite").fetchone()[0] == 1


# --------------------------------------------------------- 共被引闸门
def test_共被引不到门槛不返回(conn):
    db.save_seed_cites(conn, "s1", [_cite("10.1/only-one")])
    assert db.cocited_items(conn, min_seeds=2) == []


def test_两个种子都引用才返回(conn):
    db.save_seed_cites(conn, "s1", [_cite("10.1/two", "Two")])
    db.save_seed_cites(conn, "s2", [_cite("10.1/two", "Two")])
    got = db.cocited_items(conn, min_seeds=2)
    assert [g["doi"] for g in got] == ["10.1/two"]
    assert got[0]["source"] == "snowball"
    assert got[0]["source_ref"] == "cocite:2"


def test_跨轮次累积也能凑够票(conn):
    """这正是落表的目的:两轮各查一个种子,第三个种子是别轮查的,
    拼起来仍然算共同引用 —— 一轮查不完全部种子不影响判断。"""
    db.save_seed_cites(conn, "s1", [_cite("10.1/x")])
    db.save_seed_cites(conn, "s9", [_cite("10.1/x")])       # 另一轮才刷到的种子
    assert len(db.cocited_items(conn, min_seeds=2)) == 1


def test_已在库的不再返回(conn):
    """"已在库"从分组起是**已在这个组里** —— 单组场景两者等价。"""
    db.save_seed_cites(conn, "s1", [_cite("10.1/have")])
    db.save_seed_cites(conn, "s2", [_cite("10.1/have")])
    iid, _ = db.upsert_item(conn, {"kind": "paper", "dedup_key": "doi:10.1/have",
                                   "doi": "10.1/have", "title": "H",
                                   "title_norm": "h", "source": "snowball"})
    db.add_to_group(conn, db.group_id(conn, db.DEFAULT_GROUP_SLUG), iid)
    conn.commit()
    assert db.cocited_items(conn, min_seeds=2) == []


def test_别的组捞到过的仍算本组候选(conn):
    """A 组先入库的论文,对 B 组仍是合法候选 —— 否则第二个方向永远捞不到它。"""
    a = db.ensure_group(conn, "a")
    b = db.ensure_group(conn, "b")
    db.save_seed_cites(conn, "s1", [_cite("10.1/shared")], group_id=a)
    db.save_seed_cites(conn, "s2", [_cite("10.1/shared")], group_id=a)
    iid, _ = db.upsert_item(conn, {"kind": "paper", "dedup_key": "doi:10.1/shared",
                                   "doi": "10.1/shared", "title": "S",
                                   "title_norm": "s", "source": "snowball"})
    db.add_to_group(conn, a, iid)
    conn.commit()

    # A 组已经收了它 → 不再是 A 的候选
    assert db.cocited_items(conn, min_seeds=2, group_id=a) == []
    # B 组自己的种子引用了它 → 仍是 B 的候选(哪怕它已在全局库里)
    db.save_seed_cites(conn, "t1", [_cite("10.1/shared")], group_id=b)
    db.save_seed_cites(conn, "t2", [_cite("10.1/shared")], group_id=b)
    assert len(db.cocited_items(conn, min_seeds=2, group_id=b)) == 1


def test_两个组的共被引票数不互相借(conn):
    """A 的一个种子 + B 的一个种子,不该凑成"被两个种子引用"。"""
    a = db.ensure_group(conn, "a")
    b = db.ensure_group(conn, "b")
    db.save_seed_cites(conn, "s1", [_cite("10.1/mix")], group_id=a)
    db.save_seed_cites(conn, "s2", [_cite("10.1/mix")], group_id=b)

    assert db.cocited_items(conn, min_seeds=2, group_id=a) == []
    assert db.cocited_items(conn, min_seeds=2, group_id=b) == []


def test_大小写不同的DOI算同一条(conn):
    db.save_seed_cites(conn, "s1", [_cite("10.1/ABC")])
    db.save_seed_cites(conn, "s2", [_cite("10.1/abc")])
    assert len(db.cocited_items(conn, min_seeds=2)) == 1


def test_门槛可调(conn):
    db.save_seed_cites(conn, "s1", [_cite("10.1/y")])
    db.save_seed_cites(conn, "s2", [_cite("10.1/y")])
    db.save_seed_cites(conn, "s3", [_cite("10.1/y")])
    assert len(db.cocited_items(conn, min_seeds=3)) == 1
    assert db.cocited_items(conn, min_seeds=4) == []
