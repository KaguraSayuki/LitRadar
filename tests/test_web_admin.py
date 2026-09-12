"""``/admin/run/*`` 的准入测试。

这个端点是整个应用里唯一会真的花钱的地方(DeepSeek 额度),所以它的闸门
要单独测:口令、跨源。其余页面只读,漏了最多是泄露标题。
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
