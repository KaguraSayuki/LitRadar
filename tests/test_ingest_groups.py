"""按订阅组采集:各自的检索词、各自的成员关系、互不拖垮。

分组的意义就在这里 —— 两组用不同的检索式各捞各的,命中的条目只记进对应的组;
而 X-MOL 是全局来源,它的条目要进所有启用的组。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from litradar import db, pipeline
from litradar.config import Config
from litradar.rank import load_groups
from litradar.sources import mail

ROOT = Path(__file__).resolve().parent.parent
MAIL_RAW = (ROOT / "fixtures/xmol_sample.eml").read_bytes()

TWO_GROUPS = {"groups": [
    {"slug": "org", "name": "有机", "search_queries": ["organic"]},
    {"slug": "mat", "name": "材料", "search_queries": ["materials"]},
]}


def _cfg(tmp_path, *, groups=TWO_GROUPS) -> Config:
    cfg = Config()
    cfg.app.db_path = str(tmp_path / "groups.db")
    cfg.app.interests = str(tmp_path / "interests.yaml")
    cfg.mail.mode = "folder"
    cfg.sources.crossref_search_enabled = True
    cfg.sources.s2_search_enabled = False          # 免得真去打 S2
    cfg.sources.snowball_enabled = False
    cfg.sources.openalex_enabled = False
    cfg.interests_data = groups
    return cfg


def _hit(doi: str, title: str) -> dict:
    # kind 是 NOT NULL,真实数据源都会带上(upsert_item 会显式传 None,
    # 所以 DB 的 DEFAULT 兜不住漏掉它的来源)
    return {"kind": "paper", "doi": doi, "title": title, "source": "crossref",
            "published_at": "2026-06-01"}


def _membership(cfg) -> list[tuple[str, str]]:
    conn = db.Database(cfg.db_file).connect()
    try:
        return [(r[0], r[1]) for r in conn.execute(
            """SELECT g.slug, i.doi FROM item_group ig
                 JOIN interest_group g ON g.id = ig.group_id
                 JOIN item i ON i.id = ig.item_id
                ORDER BY g.slug""")]
    finally:
        conn.close()


def _stub_search(monkeypatch, by_query: dict):
    def search(q, **_kw):
        got = by_query.get(q)
        if isinstance(got, Exception):
            raise got
        return got or []
    monkeypatch.setattr(pipeline.crossref_search, "search", search)


# ───────────────────────────────────────────── 各组只收自己的命中
def test_每个组只收自己检索词命中的条目(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _stub_search(monkeypatch, {
        "organic": [_hit("10.1/org", "Organic paper")],
        "materials": [_hit("10.1/mat", "Materials paper")],
    })

    out = pipeline.ingest_keyword_search(cfg, verbose=False)

    assert set(out["groups"]) == {"org", "mat"}
    assert _membership(cfg) == [("mat", "10.1/mat"), ("org", "10.1/org")]
    # 条目池是共享的:两篇都在 item 里,只是分属不同的组
    conn = db.Database(cfg.db_file).connect()
    assert conn.execute("SELECT COUNT(*) FROM item").fetchone()[0] == 2
    conn.close()


def test_同一篇被两组都命中时两条成员关系都记(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _stub_search(monkeypatch, {
        "organic": [_hit("10.1/both", "Shared")],
        "materials": [_hit("10.1/both", "Shared")],
    })

    pipeline.ingest_keyword_search(cfg, verbose=False)

    assert _membership(cfg) == [("mat", "10.1/both"), ("org", "10.1/both")]
    conn = db.Database(cfg.db_file).connect()
    assert conn.execute("SELECT COUNT(*) FROM item").fetchone()[0] == 1   # DOI 去重
    conn.close()


def test_每组的运行记录带自己的_slug(tmp_path, monkeypatch):
    """分组后 run_log 要能按组算账(花费护栏按组计数就靠它)。"""
    cfg = _cfg(tmp_path)
    _stub_search(monkeypatch, {"organic": [], "materials": []})

    pipeline.ingest_keyword_search(cfg, verbose=False)

    conn = db.Database(cfg.db_file).connect()
    rows = [(r[0], r[1]) for r in conn.execute(
        "SELECT stage, group_slug FROM run_log ORDER BY id")]
    conn.close()
    assert rows == [("ingest_search", "org"), ("ingest_search", "mat")]


def test_只跑指定的组(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _stub_search(monkeypatch, {
        "organic": [_hit("10.1/org", "O")],
        "materials": [_hit("10.1/mat", "M")],
    })
    only = load_groups(cfg)[1]

    out = pipeline.ingest_keyword_search(cfg, verbose=False, group=only)

    assert set(out["groups"]) == {"mat"}
    assert _membership(cfg) == [("mat", "10.1/mat")]


def test_停用的组不参与采集(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, groups={"groups": [
        {"slug": "on", "name": "On", "search_queries": ["organic"]},
        {"slug": "off", "name": "Off", "search_queries": ["materials"],
         "enabled": False},
    ]})
    _stub_search(monkeypatch, {"organic": [_hit("10.1/org", "O")],
                               "materials": [_hit("10.1/mat", "M")]})

    out = pipeline.ingest_keyword_search(cfg, verbose=False)

    assert set(out["groups"]) == {"on"}
    assert _membership(cfg) == [("on", "10.1/org")]


# ───────────────────────────────────────────── 一个组坏了不拖垮别的组
def test_一个组失败不影响其它组(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _stub_search(monkeypatch, {
        "organic": RuntimeError("boom"),
        "materials": [_hit("10.1/mat", "Materials paper")],
    })

    out = pipeline.ingest_keyword_search(cfg, verbose=False)

    assert out["errors"] == 1
    assert "boom" in out["groups"]["org"]["error"]
    assert out["groups"]["mat"]["new"] == 1
    assert _membership(cfg) == [("mat", "10.1/mat")]


# ───────────────────────────────────────────── X-MOL 是全局来源
def test_邮件条目进所有启用的组(tmp_path, monkeypatch):
    """X-MOL 推什么由它网站上的订阅决定,本项目按方向驱动不了它,所以它的
    条目属于所有启用的组,再由各组的规则与关键词决定相关性。"""
    cfg = _cfg(tmp_path, groups={"groups": [
        {"slug": "a", "name": "A"},
        {"slug": "b", "name": "B"},
        {"slug": "off", "name": "Off", "enabled": False},
    ]})
    message = mail.MailMessage(raw=MAIL_RAW, source_ref="t", origin="t", ack=Mock())
    monkeypatch.setattr(mail, "iter_messages", lambda *_: iter([message]))

    out = pipeline.ingest_mail(cfg, verbose=False)

    assert out["new"] > 0
    conn = db.Database(cfg.db_file).connect()
    slugs = {r[0] for r in conn.execute(
        """SELECT DISTINCT g.slug FROM item_group ig
             JOIN interest_group g ON g.id = ig.group_id""")}
    conn.close()
    assert slugs == {"a", "b"}, "停用的组不该收到条目"
