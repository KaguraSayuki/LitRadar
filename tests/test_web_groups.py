"""Web 层的订阅组:收件箱按组、可切换、详情显示归属。

分组之后"这条从哪来"不再显而易见,所以三件事都要在界面上成立:收件箱只放
本组的条目、能切到别的组、详情页说清它属于哪些组。
"""
from __future__ import annotations

import datetime
import json
import shutil
import subprocess
from copy import deepcopy
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import yaml
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

    # 无组参数的新导航仍使用 Cookie 作为默认视图。
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


# ───────────────────────────── 反馈按钮的按组语义
@pytest.fixture
def both_client(tmp_path, monkeypatch):
    """一条**同时属于两个组**的 item。

    组级状态只有在这种条目上才看得见差异:只在一个组里的条目,写错组的后果
    是"另一个组凭空多了一条",而不是"当前组纹丝不动"。
    """
    cfg = Config()
    cfg.app.db_path = str(tmp_path / "both.db")
    cfg.app.interests = str(tmp_path / "i.yaml")
    cfg.interests_data = deepcopy(TWO_GROUPS)
    monkeypatch.setattr(webapp, "get_cfg", lambda: cfg)

    conn = db.Database(cfg.db_file).connect()
    ids = db.sync_groups(conn, load_groups(cfg))
    iid, _ = db.upsert_item(conn, {
        "kind": "paper", "dedup_key": "doi:both-groups", "doi": "both-groups",
        "title": "Both groups", "title_norm": "both groups", "source": "test",
        "published_at": TODAY})
    for slug in ("org", "mat"):
        db.add_to_group(conn, ids[slug], iid)
    conn.commit()
    conn.close()
    return TestClient(webapp.app), cfg, iid, ids


def test_点不感兴趣后按钮不回弹(client):
    """回归:回读读的是 ``item_state.ignored`` —— v5 起废弃、迁移时已清零的列。

    读死列的后果是按钮立刻弹回「不感兴趣」,看起来像没生效。用户于是反复点,
    feedback 里因此堆出一串重复的 ignore(真实库里 217 条就是这么来的)。
    """
    r = client.post("/item/1/action", data={"action": "ignore"})

    assert r.status_code == 200
    assert "litradarAction(1, 'unignore')" in r.text, "点完该变成「恢复」"


def test_再点恢复又回到不感兴趣(client):
    """往返:两个方向都要按库里的真实状态回显,而不是都停在默认。"""
    first = client.post("/item/1/action", data={"action": "ignore"})
    assert "litradarAction(1, 'unignore')" in first.text

    r = client.post("/item/1/action", data={"action": "unignore"})

    assert "litradarAction(1, 'ignore')" in r.text


def test_不感兴趣只落在当前组(both_client):
    """回归:action 没带 group_slug,忽略永远写进默认组。

    后果不是报错,而是"在 mat 组点一下改掉了 org 组的视图",而 mat 自己
    的视图纹丝不动 —— 按钮照旧弹回默认,两边的账都错。
    """
    c, cfg, iid, ids = both_client

    r = c.post(f"/item/{iid}/action?g=mat", data={"action": "ignore"})
    assert r.status_code == 200
    assert f"litradarAction({iid}, 'unignore')" in r.text, "按钮该按 mat 组回显"

    conn = db.Database(cfg.db_file).connect()
    rows = dict(conn.execute(
        "SELECT group_id, ignored FROM group_state WHERE item_id=?", (iid,)).fetchall())
    conn.close()
    assert rows.get(ids["mat"]) == 1, "mat 组该记下这次忽略"
    assert rows.get(ids["org"], 0) == 0, "org 组不该被牵连"


def test_已读与收藏仍是全局的(both_client):
    """两轴的分界:star / 已读是"我对这篇文献"的判断,不该被组切碎。"""
    c, cfg, iid, ids = both_client

    c.post(f"/item/{iid}/action?g=mat", data={"action": "read"})
    c.post(f"/item/{iid}/action?g=org", data={"action": "star"})

    conn = db.Database(cfg.db_file).connect()
    row = conn.execute("SELECT state, starred FROM item_state WHERE item_id=?",
                       (iid,)).fetchone()
    conn.close()
    assert row["state"] == "read"
    assert row["starred"] == 1


# ───────────────────────────── 花费护栏按组记账(阶段 4)
def _log(cfg, stage, slug=None):
    from datetime import datetime, timezone

    from litradar import db as _db

    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    conn = _db.Database(cfg.db_file).connect()
    conn.execute(
        "INSERT INTO run_log (stage, started_at, finished_at, status, group_slug) "
        "VALUES (?,?,?,?,?)",
        (stage, ts, ts, "ok", slug))
    conn.commit()
    conn.close()


def test_额度按组各算各的(tmp_path, monkeypatch):
    """在 A 组点三次,不该把 B 组的额度也吃掉。"""
    cfg = Config()
    cfg.app.db_path = str(tmp_path / "g.db")
    cfg.app.interests = str(tmp_path / "i.yaml")
    cfg.interests_data = TWO_GROUPS
    cfg.admin.cooldown_seconds = 0
    cfg.admin.daily_limit = 3
    monkeypatch.setattr(webapp, "get_cfg", lambda: cfg)
    monkeypatch.setattr(webapp.rank, "run", lambda *a, **kw: {"scored": 0})
    for _ in range(3):
        _log(cfg, "rank", "org")
    c = TestClient(webapp.app)

    blocked = c.post("/admin/run/rank?g=org")
    allowed = c.post("/admin/run/rank?g=mat")

    assert blocked.status_code == 429, "org 组自己已经跑满"
    assert allowed.status_code == 200, "mat 组不该被 org 的额度牵连"


def test_全局工作算进每个组的账(tmp_path, monkeypatch):
    """mail / enrich 是全局的(不按方向跑),它们的记录不该在按组算账时消失。"""
    cfg = Config()
    cfg.app.db_path = str(tmp_path / "g.db")
    cfg.app.interests = str(tmp_path / "i.yaml")
    cfg.interests_data = TWO_GROUPS
    cfg.admin.cooldown_seconds = 0
    cfg.admin.daily_limit = 1
    monkeypatch.setattr(webapp, "get_cfg", lambda: cfg)
    monkeypatch.setattr(webapp.rank, "run", lambda *a, **kw: {"scored": 0})
    _log(cfg, "rank", None)          # 不带组的记录(旧版 / 全局入口)
    c = TestClient(webapp.app)

    assert c.post("/admin/run/rank?g=org").status_code == 429


class PageElements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def attrs(self, tag):
        return [attrs for name, attrs in self.elements if name == tag]


@pytest.mark.parametrize("path", ["/", "/week", "/search?q=Both", "/item/1", "/stats", "/interests"])
def test_所有页面导航和表单固定渲染时的组(both_client, path):
    c, cfg, iid, ids = both_client
    # 页面 URL 本身不含组,但渲染时从 Cookie 选中的组仍必须写入 DOM/链接。
    c.get("/?g=org")
    page = PageElements(c.get(path).text)
    assert page.attrs("body")[0]["data-group"] == "org"
    links = [a["href"] for a in page.attrs("a") if "href" in a
             and "groupbar__item" not in a.get("class", "")]
    for href in links:
        if href.startswith("/") or href.startswith("?"):
            assert parse_qs(urlsplit(href).query).get("g") == ["org"], href
    for form in page.attrs("form"):
        if form.get("method") == "get":
            assert any(i.get("name") == "g" and i.get("value") == "org"
                       for i in page.attrs("input"))
        else:
            assert parse_qs(urlsplit(form["action"]).query)["g"] == ["org"]
    c.get("/?g=mat")
    # 从旧标签页导航到详情,分数/反馈上下文仍然是 org。
    details = [href for href in links if href.startswith("/item/")]
    if details:
        assert PageElements(c.get(details[0]).text).attrs("body")[0]["data-group"] == "org"


@pytest.mark.parametrize("path", ["/?state=all&min_score=30&g=org", "/search?q=Both&g=org"])
def test_分页保留组和筛选条件(both_client, monkeypatch, path):
    c, cfg, iid, ids = both_client
    monkeypatch.setattr(webapp, "PER_PAGE", 1)
    conn = db.Database(cfg.db_file).connect()
    for n in range(30):
        new_id, _ = db.upsert_item(conn, {
            "kind": "paper", "dedup_key": f"page:{n}", "title": f"Both {n}",
            "title_norm": f"both {n}", "source": "test", "published_at": TODAY})
        db.add_to_group(conn, ids["org"], new_id)
        db.save_score(conn, new_id, group_id=ids["org"], final_score=50)
    conn.commit()
    conn.close()
    page = PageElements(c.get(path).text)
    pagers = [a["href"] for a in page.attrs("a") if "page=" in a.get("href", "")]
    assert pagers
    expected = parse_qs(urlsplit(path).query)
    for href in pagers:
        query = parse_qs(urlsplit(href).query)
        assert all(query.get(k) == v for k, v in expected.items())


@pytest.mark.parametrize("referer", ["http://testserver/?g=org", "http://testserver/search?q=Both&g=org"])
def test_旧页面写请求可由同源Referer恢复组(both_client, referer):
    c, cfg, iid, ids = both_client
    c.get("/?g=mat")
    r = c.post(f"/item/{iid}/action", data={"action": "ignore"}, headers={"Referer": referer})
    assert r.status_code == 200
    conn = db.Database(cfg.db_file).connect()
    states = dict(conn.execute("SELECT group_id, ignored FROM group_state WHERE item_id=?", (iid,)))
    conn.close()
    assert states.get(ids["org"]) == 1
    assert states.get(ids["mat"], 0) == 0


def test_跨源Referer不能覆盖默认组(both_client):
    c, cfg, iid, ids = both_client
    c.get("/?g=mat")
    r = c.post(f"/item/{iid}/action", data={"action": "ignore"},
               headers={"Referer": "https://other.example/?g=org"})
    assert r.status_code == 200
    conn = db.Database(cfg.db_file).connect()
    assert dict(conn.execute("SELECT group_id, ignored FROM group_state")) == {ids["mat"]: 1}
    conn.close()


@pytest.mark.parametrize("path", ["/item/1/action", "/admin/run/rank"])
def test_写请求中的失效组不会静默改投其他组(both_client, monkeypatch, path):
    c, cfg, iid, ids = both_client
    calls = []
    monkeypatch.setattr(webapp.rank, "run", lambda *a, **kw: calls.append(kw))
    c.get("/?g=mat")
    r = c.post(path + "?g=removed", data={"action": "ignore"})
    assert r.status_code == 400
    assert calls == []
    conn = db.Database(cfg.db_file).connect()
    assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize("path", ["/", "/week", "/search?q=Both"])
def test_新配置组同步前页面为空且读请求不建组(both_client, path):
    c, cfg, iid, ids = both_client
    cfg.interests_data = deepcopy(TWO_GROUPS)
    cfg.interests_data["groups"].append({"slug": "new", "name": "New"})
    c.get("/?g=new")
    page = c.get(path)
    assert page.status_code == 200
    assert "Both groups" not in page.text
    conn = db.Database(cfg.db_file).connect()
    assert db.group_id(conn, "new") is None
    assert db.count_items(conn, group_slug="new") == 0
    conn.close()


@pytest.mark.parametrize("stage", ["search", "rank", "summarize", "all"])
def test_真实前端脚本的反馈撤销及流水线重试都沿用页面组(both_client, monkeypatch, stage):
    node = shutil.which("node")
    if not node:
        pytest.skip("前端执行回归需要 Node.js 18+;其余 Web 测试不依赖 Node")
    c, cfg, iid, ids = both_client
    c.get("/?g=org")
    # 有意使用不含 g 的页面 URL,保证脚本读取渲染上下文而不是猜 URL/Cookie。
    body = PageElements(c.get("/").text).attrs("body")[0]
    c.get("/?g=mat")
    result = subprocess.run(
        [node, str(Path(__file__).parent / "helpers" / "web_context.cjs")],
        input=json.dumps({"itemId": iid, "stage": stage, "group": body["data-group"],
                          "state": body["data-state"]}),
        text=True, capture_output=True, check=True,
    )
    requests = json.loads(result.stdout)
    assert len(requests) == 4  # ignore, undo, 第一次 run, 密码重试
    calls, limits = [], []
    cfg.admin.guarded_stages = (stage,)

    def password(request, cfg):
        if not request.headers.get("X-Admin-Password"):
            raise webapp.HTTPException(401, "password", headers={"X-Admin-Password-Required": "1"})

    def run(*a, **kw):
        calls.append(kw["group"].slug)
        return {"errors": 0}

    monkeypatch.setattr(webapp, "require_admin_password", password)
    monkeypatch.setattr(webapp, "require_stage_limits", lambda cfg, stage, slug: limits.append(slug))
    monkeypatch.setattr(webapp.pipeline, "ingest_keyword_search", run)
    monkeypatch.setattr(webapp.rank, "run", run)
    monkeypatch.setattr(webapp.summarize, "run", run)
    monkeypatch.setattr(webapp.pipeline, "run_all", run)
    for n, req in enumerate(requests):
        assert parse_qs(urlsplit(req["url"]).query)["g"] == ["org"]
        response = c.post(req["url"], data=req["data"], headers=req["headers"])
        assert response.status_code == (401 if n == 2 else 200)
        if n < 2:
            conn = db.Database(cfg.db_file).connect()
            states = dict(conn.execute("SELECT group_id, ignored FROM group_state"))
            conn.close()
            assert states.get(ids["org"]) == 1 - n
            assert states.get(ids["mat"], 0) == 0
    assert calls == limits == ["org"]
    assert c.cookies.get(webapp.GROUP_COOKIE) == "mat"  # 写请求不切换默认视图


@pytest.mark.parametrize("path", ["/", "/week", "/search?q=Both", "/item/1"])
def test_当前组说明缺失时页面和查询都不回退到其他方向(both_client, path):
    c, cfg, iid, ids = both_client
    conn = db.Database(cfg.db_file).connect()
    db.save_summary(conn, iid, {"one_liner": "Shared neutral result"}, "deep", "fake")
    # 模拟旧进程遗留的共享列污染;不能只依赖新写入路径不再保存它。
    conn.execute("UPDATE summary SET relevance='Only organic relevance' WHERE item_id=?", (iid,))
    db.save_group_relevance(conn, ids["org"], iid, "Only organic relevance", "fake")
    conn.commit()
    assert db.get_items(conn, group_slug="mat")[0]["relevance"] is None
    assert db.search_items(conn, "Both", group_slug="mat")[0]["relevance"] is None
    conn.close()
    page = c.get(path, params={"g": "mat", "q": "Both"})
    assert page.status_code == 200
    assert "Shared neutral result" in page.text
    assert "Only organic relevance" not in page.text


@pytest.mark.parametrize("slug", ["材料", "mat&chem", "mat+chem", "mat%26chem",
                                  "mat/chem?#", 'mat "chem"'])
def test_合法特殊slug在保存切换Cookie导航和反馈中保持身份(both_client, slug, monkeypatch):
    c, cfg, iid, ids = both_client
    monkeypatch.setenv('LITRADAR_TOKEN','legacy-test-token')
    c.headers['X-Token']='legacy-test-token'
    cfg.interests_data["groups"].append({"slug": slug, "name": "特殊方向"})
    raw = yaml.safe_dump(cfg.interests_data, allow_unicode=True)
    from litradar.settings import SettingsStore
    saved = c.post("/interests", data={"raw": raw,'version':SettingsStore(cfg).version()}, follow_redirects=False)
    assert saved.status_code == 303
    cfg.interests_data = yaml.safe_load(cfg.interests_file.read_text(encoding="utf-8"))
    conn = db.Database(cfg.db_file).connect()
    gid = db.sync_groups(conn, load_groups(cfg))[slug]
    db.add_to_group(conn, gid, iid)
    conn.commit()
    conn.close()

    links = PageElements(c.get("/").text).attrs("a")
    switch = next(a["href"] for a in links if a.get("title") == "特殊方向")
    assert parse_qs(urlsplit(switch).query) == {"g": [slug]}
    selected = c.get(switch)
    assert selected.status_code == 200
    assert unquote(c.cookies.get(webapp.GROUP_COOKIE)) == slug
    # Cookie 默认页和显式导航都必须回到同一组,不能被 &、+、% 或 # 拆开。
    for path in ["/", "/week", "/search?q=Both", f"/item/{iid}", "/interests"]:
        page = PageElements(c.get(path).text)
        assert page.attrs("body")[0]["data-group"] == slug
        for attrs in page.attrs("a"):
            href = attrs.get("href", "")
            if href.startswith("/") and "groupbar__item" not in attrs.get("class", ""):
                assert parse_qs(urlsplit(href).query)["g"] == [slug]
    c.get("/?g=mat")
    posted = c.post(f"/item/{iid}/action", params={"g": slug}, data={"action": "ignore"})
    assert posted.status_code == 200
    conn = db.Database(cfg.db_file).connect()
    assert dict(conn.execute("SELECT group_id,ignored FROM group_state")) == {gid: 1}
    conn.close()


def test_未知Unicode组和损坏Cookie安全回退到实际组(both_client):
    c, cfg, iid, ids = both_client
    page = c.get("/", params={"g": "不存在的方向"})
    assert page.status_code == 200
    assert c.cookies.get(webapp.GROUP_COOKIE) == "org"
    page = c.get("/", headers={"Cookie": webapp.GROUP_COOKIE + "=%FF"})
    assert page.status_code == 200
    assert PageElements(page.text).attrs("body")[0]["data-group"] == "org"


@pytest.mark.parametrize("slug,needle", [("bad\nslug", "控制字符"),
                                        ("材" * 86, "256"),
                                        ("\ud800", "Unicode")])
def test_无效slug在编辑页报错而不覆盖已有配置(both_client, slug, needle, monkeypatch):
    c, cfg, iid, ids = both_client
    monkeypatch.setenv('LITRADAR_TOKEN','legacy-test-token')
    c.headers['X-Token']='legacy-test-token'
    before = yaml.safe_dump(cfg.interests_data)
    cfg.interests_file.write_text(before, encoding="utf-8")
    raw = yaml.safe_dump({"groups": [{"slug": slug, "name": "Bad group"}]})
    page = c.post("/interests", data={"raw": raw})
    assert page.status_code == 400
    assert needle in page.text
    assert cfg.interests_file.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("path", ["/", "/week", "/search?q=Both", "/item/1"])
@pytest.mark.parametrize("mode,notice,label", [
    ("group_disabled", "本组已关闭 AI 精排", "关键词分"),
    ("disabled", "已关闭 AI", "关键词分"),
    ("no_key", "尚未设置模型 API 密钥", "关键词分"),
    ("failed", "", "关键词分"),
    ("scored", "", "相关度"),
    ("unscored", "", "未评分"),
])
def test_评分界面区分主动关闭真实失败和成功(both_client, monkeypatch, path, mode, notice, label):
    c, cfg, iid, ids = both_client
    cfg.interests_data["groups"][0]["llm_rank"] = mode != "group_disabled"
    cfg.llm.enabled = mode != "disabled"
    cfg.llm.api_key_env = "LITRADAR_TEST_SCORE_KEY"
    monkeypatch.setenv(cfg.llm.api_key_env, "" if mode == "no_key" else "fake-no-network")
    conn = db.Database(cfg.db_file).connect()
    if mode != "unscored":
        db.save_score(conn, iid, group_id=ids["org"], final_score=70,
                      llm_score=70 if mode == "scored" else None)
    conn.commit()
    conn.close()

    page = c.get(path, params={"g": "org", "q": "Both"})

    assert page.status_code == 200
    assert "Both groups" in page.text
    assert ('class="score__l">' + label + "</span>") in page.text
    assert ("精排失败" in page.text) == (mode == "failed")
    assert ("score--partial" in page.text) == (mode == "failed")
    notices = [p for p in PageElements(page.text).attrs("p") if p.get("class") == "notice"]
    assert bool(notices) == bool(notice)
    if notice:
        assert notice in page.text
    if mode != "no_key":
        assert "尚未设置模型 API 密钥" not in page.text
