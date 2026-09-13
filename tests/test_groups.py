"""订阅分组的存储语义。

这一层保护的是分组功能的地基,四条不能破:
  1. 同一篇在两个组里的分值互不影响,而且**不会按组复制成多行**;
  2. 「忽略 / 被规则排除」按组隔离,「收藏 / 已读」全局;
  3. 组按 slug 对账(改名不改 id),并且**不删除**库里已有的组;
  4. 单组用户不带 group_slug 时的行为与分组前完全一致。
"""
from __future__ import annotations

import pytest

from litradar import db

ITEM = {"kind": "paper", "dedup_key": "doi:10.1/x", "doi": "10.1/x",
        "title": "Catalysis", "title_norm": "catalysis", "source": "test"}


@pytest.fixture
def conn(tmp_path):
    conn = db.Database(tmp_path / "groups.db").connect()
    yield conn
    conn.close()


def _item(conn, key="10.1/x", title="Catalysis"):
    iid, _ = db.upsert_item(conn, {**ITEM, "dedup_key": f"doi:{key}",
                                   "doi": key, "title": title,
                                   "title_norm": title.lower()})
    return iid


# --------------------------------------------------------------- 分值按组
def test_同一篇在两个组里分值互不影响(conn):
    iid = _item(conn)
    a = db.ensure_group(conn, "a", name="A")
    b = db.ensure_group(conn, "b", name="B")
    db.save_score(conn, iid, group_id=a, final_score=10.0)
    db.save_score(conn, iid, group_id=b, final_score=90.0)
    conn.commit()

    rows_a = db.get_items(conn, group_slug="a")
    rows_b = db.get_items(conn, group_slug="b")

    # 复合主键的坑:按 item 关联而不带 group 条件,一条 item 会被复制成多行
    assert len(rows_a) == len(rows_b) == 1
    assert (rows_a[0]["final_score"], rows_b[0]["final_score"]) == (10.0, 90.0)


def test_组内排序与条数按组(conn):
    iid1, iid2 = _item(conn, "10.1/a", "Alpha"), _item(conn, "10.1/b", "Beta")
    a = db.ensure_group(conn, "a")
    db.save_score(conn, iid1, group_id=a, final_score=1.0)
    db.save_score(conn, iid2, group_id=a, final_score=99.0)
    conn.commit()

    assert [r["title"] for r in db.get_items(conn, group_slug="a")] == ["Beta", "Alpha"]
    assert db.count_items(conn, group_slug="a") == 2


def test_不带组时落到默认组(conn):
    iid = _item(conn)
    db.save_score(conn, iid, final_score=42.0)          # 单组用户的写法
    conn.commit()

    assert db.group_id(conn, db.DEFAULT_GROUP_SLUG) is not None
    assert db.get_items(conn)[0]["final_score"] == 42.0


def test_未知组不会报错只是查不到分(conn):
    _item(conn)
    assert len(db.get_items(conn, group_slug="nope")) == 1
    assert db.get_items(conn, group_slug="nope")[0]["final_score"] is None


# ------------------------------------------------- 忽略按组、收藏全局
def test_忽略按组隔离(conn):
    iid = _item(conn)
    db.ensure_group(conn, "a")
    db.ensure_group(conn, "b")
    db.set_action(conn, iid, "ignore", group_slug="a")
    conn.commit()

    assert db.get_items(conn, group_slug="a") == []          # A 组隐藏
    assert len(db.get_items(conn, group_slug="b")) == 1      # B 组照常
    assert db.count_items(conn, state="ignored", group_slug="a") == 1
    assert db.count_items(conn, state="ignored", group_slug="b") == 0


def test_取消忽略只影响本组(conn):
    iid = _item(conn)
    db.ensure_group(conn, "a")
    db.ensure_group(conn, "b")
    db.set_action(conn, iid, "ignore", group_slug="a")
    db.set_action(conn, iid, "ignore", group_slug="b")
    db.set_action(conn, iid, "unignore", group_slug="a")
    conn.commit()

    assert len(db.get_items(conn, group_slug="a")) == 1
    assert db.get_items(conn, group_slug="b") == []


def test_收藏是全局的(conn):
    iid = _item(conn)
    db.ensure_group(conn, "a")
    db.ensure_group(conn, "b")
    db.set_action(conn, iid, "star", group_slug="a")
    conn.commit()

    # 在 B 组看"收藏"页签同样能看到 —— 收藏是对这篇文献的判断
    assert db.count_items(conn, state="starred", group_slug="b") == 1
    assert db.count_items(conn, state="starred", group_slug="a") == 1


def test_规则排除按组(conn):
    iid = _item(conn)
    db.ensure_group(conn, "a")
    db.ensure_group(conn, "b")
    db.set_excluded(conn, [iid], True, group_slug="a")
    conn.commit()

    assert db.get_items(conn, group_slug="a") == []
    assert len(db.get_items(conn, group_slug="b")) == 1


# ----------------------------------------------------------------- 组对账
def test_ensure_group_幂等(conn):
    first = db.ensure_group(conn, "chem", name="化学")
    again = db.ensure_group(conn, "chem", name="改个名字")
    conn.commit()

    assert first == again
    assert len(db.groups(conn)) == 1


def test_sync_groups_按_slug_更新而不新建(conn):
    from types import SimpleNamespace

    def prof(slug, name, **kw):
        base = dict(slug=slug, name=name, direction="", enabled=True, llm_rank=True)
        return SimpleNamespace(**{**base, **kw})

    first = db.sync_groups(conn, [prof("a", "A"), prof("b", "B")])
    second = db.sync_groups(conn, [prof("a", "A renamed", direction="d", llm_rank=False)])
    conn.commit()

    assert first["a"] == second["a"]
    row = [g for g in db.groups(conn) if g["slug"] == "a"][0]
    assert row["name"] == "A renamed" and row["direction"] == "d"
    assert row["llm_rank"] == 0


def test_sync_groups_不删除库里已有的组(conn):
    """临时把某个组从 interests.yaml 里注释掉,不该连带删掉它的历史分值。"""
    from types import SimpleNamespace

    def prof(slug):
        return SimpleNamespace(slug=slug, name=slug, direction="",
                               enabled=True, llm_rank=True)

    db.sync_groups(conn, [prof("a"), prof("b")])
    db.sync_groups(conn, [prof("a")])
    conn.commit()

    assert [g["slug"] for g in db.groups(conn)] == ["a", "b"]


def test_组顺序按_position(conn):
    from types import SimpleNamespace

    def prof(slug):
        return SimpleNamespace(slug=slug, name=slug, direction="",
                               enabled=True, llm_rank=True)

    db.sync_groups(conn, [prof("a"), prof("b"), prof("c")])
    db.sync_groups(conn, [prof("c"), prof("a")])
    conn.commit()

    assert [g["slug"] for g in db.groups(conn)] == ["c", "a", "b"]


def test_写给不存在的组会明确报错(conn):
    """组名的权威来源是 interests.yaml;拼错的 slug 不该悄悄攒出一堆空组。"""
    iid = _item(conn)

    with pytest.raises(ValueError, match="未知订阅组"):
        db.save_score(conn, iid, group_slug="typo", final_score=1.0)
    with pytest.raises(ValueError, match="未知订阅组"):
        db.set_excluded(conn, [iid], True, group_slug="typo")
    with pytest.raises(ValueError, match="未知订阅组"):
        db.set_action(conn, iid, "ignore", group_slug="typo")
