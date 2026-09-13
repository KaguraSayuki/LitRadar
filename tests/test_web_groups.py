"""Web 层的订阅组:收件箱按组、可切换、详情显示归属。

分组之后"这条从哪来"不再显而易见,所以三件事都要在界面上成立:收件箱只放
本组的条目、能切到别的组、详情页说清它属于哪些组。
"""
from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient

from litradar import db
from litradar.config import Config
from litradar.rank import load_groups
from litradar.web import app as webapp

TODAY = datetime.date.today().isoformat()
TWO_GROUPS = {"groups": [
    {"slug": "org", "name": "有机", "search_queries": ["a"]},
    {"slug": "mat", "name": "材料", "search_queries": ["b"]},
]}


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = Config()
    cfg.app.db_path = str(tmp_path / "w.db")
    cfg.app.interests = str(tmp_path / "interests.yaml")
    cfg.interests_data = TWO_GROUPS
    monkeypatch.setattr(webapp, "get_cfg", lambda: cfg)

    conn = db.Database(cfg.db_file).connect()
    ids = db.sync_groups(conn, load_groups(cfg))
    for slug, title in (("org", "Carbene paper"), ("mat", "Perovskite paper")):
        iid, _ = db.upsert_item(conn, {
            "kind": "paper", "dedup_key": f"doi:{title}", "doi": title,
            "title": title, "title_norm": title.lower(), "source": "test",
            "journal": "J", "published_at": TODAY})
        db.add_to_group(conn, ids[slug], iid)
        db.save_score(conn, iid, group_id=ids[slug],
                      final_score=50.0 if slug == "org" else 90.0)
    conn.commit()
    conn.close()
    return TestClient(webapp.app)


def test_收件箱只显示当前组的条目(client):
    page = client.get("/")

    assert "Carbene" in page.text
    assert "Perovskite" not in page.text, "别的组的条目不该出现在这一组"


def test_切换组看到另一组(client):
    page = client.get("/?g=mat")

    assert "Perovskite" in page.text
    assert "Carbene" not in page.text


def test_切换器列出所有组(client):
    page = client.get("/")

    assert "g=org" in page.text and "g=mat" in page.text
    assert "有机" in page.text and "材料" in page.text


def test_只有一个组时不显示切换器(tmp_path, monkeypatch):
    cfg = Config()
    cfg.app.db_path = str(tmp_path / "one.db")
    cfg.app.interests = str(tmp_path / "i.yaml")
    cfg.interests_data = {"direction": "单方向", "search_queries": ["a"]}
    monkeypatch.setattr(webapp, "get_cfg", lambda: cfg)

    page = TestClient(webapp.app).get("/")

    assert "groupbar" not in page.text


def test_切组后记住选择(client):
    client.get("/?g=mat")

    # 页面内的链接不带 ?g=,靠 cookie 才不会点进详情就跳回第一组
    assert client.cookies.get(webapp.GROUP_COOKIE) == "mat"
    assert "Perovskite" in client.get("/").text


def test_详情页显示所属订阅组(client):
    page = client.get("/item/1")

    assert "订阅组" in page.text
    assert "org" in page.text


def test_未知组名退回第一个启用的组(client):
    page = client.get("/?g=nope")

    assert page.status_code == 200
    assert "Carbene" in page.text


def test_统计页按当前组(client):
    page = client.get("/?g=mat")

    assert page.status_code == 200
    assert "材料" in page.text


def test_停用的组在下拉里标出来(tmp_path, monkeypatch):
    cfg = Config()
    cfg.app.db_path = str(tmp_path / "off.db")
    cfg.app.interests = str(tmp_path / "i.yaml")
    cfg.interests_data = {"groups": [
        {"slug": "on", "name": "在跑", "search_queries": ["a"]},
        {"slug": "off", "name": "停了", "enabled": False, "search_queries": ["b"]},
    ]}
    monkeypatch.setattr(webapp, "get_cfg", lambda: cfg)

    page = TestClient(webapp.app).get("/")

    assert "is-off" in page.text


def test_配置坏了不该让页面_500(tmp_path, monkeypatch):
    """画像写坏时页面要能用(退回不分组),不是白屏。"""
    cfg = Config()
    cfg.app.db_path = str(tmp_path / "bad.db")
    cfg.app.interests = str(tmp_path / "i.yaml")
    cfg.interests_data = {"groups": "这不是列表"}
    monkeypatch.setattr(webapp, "get_cfg", lambda: cfg)

    assert TestClient(webapp.app).get("/").status_code == 200


def test_详情页显示当前组的分与状态(tmp_path, monkeypatch):
    """回归:详情页的 score/summary_group/group_state 三处 join 都漏了组条件。

    漏掉的后果不是报错,而是同一条 item 关联出多行、fetchone() 取到哪一组的
    分全凭运气;而且 ignored 读的是 v5 起废弃的 item_state.ignored。
    """
    from litradar import db as _db

    cfg = Config()
    cfg.app.db_path = str(tmp_path / "d.db")
    cfg.app.interests = str(tmp_path / "i.yaml")
    cfg.interests_data = TWO_GROUPS
    monkeypatch.setattr(webapp, "get_cfg", lambda: cfg)
    conn = _db.Database(cfg.db_file).connect()
    ids = _db.sync_groups(conn, load_groups(cfg))
    iid, _ = _db.upsert_item(conn, {
        "kind": "paper", "dedup_key": "doi:both", "doi": "both",
        "title": "Both groups", "title_norm": "both groups", "source": "t",
        "published_at": TODAY})
    for slug in ("org", "mat"):
        _db.add_to_group(conn, ids[slug], iid)
    _db.save_score(conn, iid, group_id=ids["org"], final_score=11.0)
    _db.save_score(conn, iid, group_id=ids["mat"], final_score=88.0)
    _db.save_group_relevance(conn, ids["org"], iid, "对有机的用处", "m")
    _db.save_group_relevance(conn, ids["mat"], iid, "对材料的用处", "m")
    _db.set_action(conn, iid, "ignore", group_slug="mat")
    conn.commit()
    conn.close()

    c = TestClient(webapp.app)
    org_page = c.get("/item/1")
    mat_page = c.get("/item/1?g=mat")

    assert "11.0" in org_page.text, "该显示 org 组的分,而不是运气好的那组"
    assert "88.0" in mat_page.text
    assert "对有机的用处" in org_page.text
    assert "对材料的用处" in mat_page.text
    # ignored 的按组语义在数据层已由 test_groups.py 钉住;详情页上它是按钮,
    # 没有可见的状态文案,不该在这里猜措辞
