"""网页写入口的测试:``/admin/run/*``、``/item/{id}/action`` 与 ``/interests``。

第一个是整个应用里唯一会真的花钱的地方(DeepSeek 额度),所以它的闸门
要单独测:口令、跨源。第二个的输入来自表单,得把白名单之外的值挡在库外。
第三个会覆盖用户的配置文件,备份与原子写入不能出错。
其余页面只读,漏了最多是泄露标题。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from litradar.config import Config  # noqa: E402
from litradar.web import app as webapp  # noqa: E402

TOKEN = "s3cret"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """把配置指向 tmp,并把真正会跑流水线的 rank.run 换成桩。

    不这么做的话,一次"成功"的用例会去读真实数据库、真的发请求。
    """
    cfg = Config()
    cfg.app.db_path = str(tmp_path / "t.db")
    cfg.app.interests = str(tmp_path / "interests.yaml")   # 别碰仓库里的真实文件
    monkeypatch.setattr(webapp, "get_cfg", lambda: cfg)
    monkeypatch.setattr(webapp.rank, "run", lambda *a, **kw: {"scored": 0})
    monkeypatch.setenv("LITRADAR_TOKEN", TOKEN)
    return TestClient(webapp.app)


def test_没带口令被拒(client):
    """回归:这个 handler 以前根本没调 require_token —— 设了口令也拦不住任何人。"""
    assert client.post("/admin/run/rank").status_code == 401


def test_口令错误被拒(client):
    assert client.post("/admin/run/rank", headers={"X-Token": "wrong"}).status_code == 401


def test_带对口令放行(client):
    r = client.post("/admin/run/rank", headers={"X-Token": TOKEN})
    assert r.status_code == 200
    assert r.json() == {"scored": 0}


def test_cookie_也算数(client):
    """页面上的按钮靠 cookie 认证(middleware 写的),不能只认 ?k= 和头。"""
    client.cookies.set(webapp.COOKIE_NAME, TOKEN)
    assert client.post("/admin/run/rank").status_code == 200


def test_跨源请求被拒(client):
    """口令对也不行 —— 挡的是"别的网页借你的浏览器发请求"。"""
    r = client.post("/admin/run/rank",
                    headers={"X-Token": TOKEN, "Origin": "http://evil.example"})
    assert r.status_code == 403


def test_同源请求放行(client):
    r = client.post("/admin/run/rank",
                    headers={"X-Token": TOKEN, "Origin": "http://testserver"})
    assert r.status_code == 200


def test_没有origin头的照常放行(client, monkeypatch):
    """curl / 定时脚本不带 Origin,不能被误伤(未设口令时也一样)。"""
    monkeypatch.delenv("LITRADAR_TOKEN", raising=False)
    assert client.post("/admin/run/rank").status_code == 200


# ------------------------------------------------------------- 互斥锁(与 CLI 共用)

def test_锁被占时返回409(client, tmp_path):
    """回归:网页按钮直调 pipeline,曾经完全绕过 CLI 那把锁。

    定时任务在跑时点一下,两份 enrich 会互抢 Semantic Scholar 的限流;
    双击按钮也一样。锁是同一个文件,所以这里直接把它占住来模拟。
    """
    from litradar.lock import single_instance

    with single_instance(tmp_path / "litradar.lock"):
        r = client.post("/admin/run/rank", headers={"X-Token": TOKEN})
    assert r.status_code == 409


def test_锁用完就放开(client, tmp_path):
    """跑完一轮后还能再跑 —— 别把锁文件留成永久的墓碑。"""
    for _ in range(2):
        assert client.post("/admin/run/rank",
                           headers={"X-Token": TOKEN}).status_code == 200


def test_未知阶段仍是400(client):
    """加锁用的 try 不能把 HTTPException 一起吞掉。"""
    assert client.post("/admin/run/nope", headers={"X-Token": TOKEN}).status_code == 400


# ------------------------------------------------------------- 反馈 action
def _add_item(cfg, title: str = "T") -> int:
    from litradar import db

    conn = db.Database(cfg.db_file).connect()
    iid, _ = db.upsert_item(conn, {
        "kind": "paper", "dedup_key": f"doi:10.1/{title}", "doi": f"10.1/{title}",
        "title": title, "title_norm": title.lower(), "source": "test",
    })
    conn.commit()
    conn.close()
    return iid


def test_未知action返回400(client):
    """回归:白名单之外的 action 以前会原样写进 feedback 表。"""
    iid = _add_item(webapp.get_cfg())
    r = client.post(f"/item/{iid}/action", data={"action": "nope"},
                    headers={"X-Token": TOKEN})
    assert r.status_code == 400


def test_合法action照常放行(client):
    iid = _add_item(webapp.get_cfg())
    r = client.post(f"/item/{iid}/action", data={"action": "star"},
                    headers={"X-Token": TOKEN})
    assert r.status_code == 200


# ------------------------------------------------------------- interests 保存
GOOD = "direction: x\nsearch_queries: [a]\nkeywords: {core: [k]}\njournals: {core: []}\n"


def _save(client, raw: str):
    return client.post("/interests", data={"raw": raw}, headers={"X-Token": TOKEN},
                       follow_redirects=False)


def _backups(target):
    return sorted(target.parent.glob(f"{target.name}.*.bak"))


def test_保存成功并留下带时间戳的备份(client):
    target = webapp.get_cfg().interests_file
    target.write_text("direction: old\n", encoding="utf-8")

    assert _save(client, GOOD).status_code == 303
    assert target.read_text(encoding="utf-8") == GOOD
    baks = _backups(target)
    assert len(baks) == 1 and baks[0].read_text(encoding="utf-8") == "direction: old\n"
    assert not target.with_name(target.name + ".tmp").exists(), "临时文件必须被 rename 掉"


def test_备份只留最近五份(client, monkeypatch):
    """回归:只有一层 .bak 时,连续两次坏保存会把好配置的备份也盖掉。"""
    target = webapp.get_cfg().interests_file
    target.write_text("direction: v0\n", encoding="utf-8")
    stamps = iter(f"20260912-1500{n:02d}" for n in range(1, 20))
    monkeypatch.setattr(webapp, "_backup_stamp", lambda: next(stamps))

    for n in range(1, 8):
        assert _save(client, GOOD.replace("x", f"v{n}")).status_code == 303
    baks = _backups(target)
    assert len(baks) == webapp.INTERESTS_BACKUPS
    assert [b.name for b in baks] == [f"interests.yaml.20260912-1500{n:02d}.bak"
                                      for n in range(3, 8)]
    # 最新那份备份是保存前的内容
    assert "v6" in baks[-1].read_text(encoding="utf-8")


def test_文件过大直接拒绝(client):
    target = webapp.get_cfg().interests_file
    target.write_text("direction: old\n", encoding="utf-8")
    huge = GOOD + "# " + "x" * webapp.MAX_INTERESTS_BYTES + "\n"
    r = _save(client, huge)
    assert r.status_code == 400 and "文件过大" in r.text
    assert target.read_text(encoding="utf-8") == "direction: old\n"
    assert _backups(target) == []


def test_写坏的内容不覆盖原文件(client):
    target = webapp.get_cfg().interests_file
    target.write_text("direction: old\n", encoding="utf-8")
    assert _save(client, "search_queries:\n  - a\n").status_code == 400
    assert _save(client, "direction: [unclosed\n").status_code == 400
    assert target.read_text(encoding="utf-8") == "direction: old\n"
    assert _backups(target) == []


# ─────────────────────────────── 兜底分的红色警告标签

def test_兜底分只在_LLM_可用时才标(client, monkeypatch):
    """有分数、却没有 LLM 分 = 精排批次失败,分数不是 LLM 判断的结果,
    必须显式标出来,否则和"真的低分"在列表上长得一模一样。

    但没配 key 时全场都没有 LLM 分,逐条标注只是噪音(顶部已有全局说明)。
    """
    from litradar import db
    from litradar.web import app as webapp

    database = db.Database(webapp.get_cfg().db_file)
    conn = database.connect()
    iid, _ = db.upsert_item(conn, {
        "kind": "paper", "dedup_key": "test:no-llm", "doi": "10.9999/nollm",
        "title": "No LLM score", "title_norm": "no llm score",
        "published_at": "2026-09-01", "source": "test"})
    db.save_score(conn, iid, rule_score=133.0, coarse_score=100.0,
                  final_score=12.4)          # llm_score 刻意留空
    conn.commit()

    def _flag(rows, _conn):
        return [dict(r) for r in rows]

    try:
        # LLM 可用 → 标
        monkeypatch.setattr(webapp, "_rank_renderer", lambda cfg: None)
        cfg = webapp.get_cfg()
        monkeypatch.setattr(type(cfg.llm), "api_key",
                            property(lambda self: "sk-test"), raising=False)
        got = webapp._decorate(db.get_items(conn, state=None, limit=999), conn)
        row = [d for d in got if d["id"] == iid][0]
        assert row["score_partial"] is True

        # LLM 不可用 → 不标
        monkeypatch.setattr(type(cfg.llm), "api_key",
                            property(lambda self: None), raising=False)
        got = webapp._decorate(db.get_items(conn, state=None, limit=999), conn)
        row = [d for d in got if d["id"] == iid][0]
        assert row["score_partial"] is False
    finally:
        conn.execute("DELETE FROM score WHERE item_id=?", (iid,))
        conn.execute("DELETE FROM item WHERE id=?", (iid,))
        conn.commit()
        conn.close()
