"""入库计数:"新增 / 更新 / 重复命中"必须是三件不同的事。

逐日跑的关键词检索会把同一批论文一遍遍捞回来。以前"库里已存在"一律记成
"更新",运行记录里于是天天挂着一条"N 篇更新",而用户点进列表找不到任何变化。
这里钉住新的口径:只有 item 行真的被补了字段才算更新,重复命中单独计数。
"""
from __future__ import annotations

import json

from litradar import db, pipeline, progress
from litradar.config import Config

ONE_GROUP = {"groups": [{"slug": "org", "name": "有机", "search_queries": ["organic"]}]}


def _cfg(tmp_path) -> Config:
    cfg = Config()
    cfg.app.db_path = str(tmp_path / "counting.db")
    cfg.app.interests = str(tmp_path / "interests.yaml")
    cfg.mail.mode = "folder"
    cfg.sources.crossref_search_enabled = True
    cfg.sources.s2_search_enabled = False          # 免得真去打 S2
    cfg.sources.snowball_enabled = False
    cfg.sources.openalex_enabled = False
    cfg.interests_data = ONE_GROUP
    return cfg


def _hit(doi: str, title: str, **extra) -> dict:
    return {"kind": "paper", "doi": doi, "title": title, "source": "crossref",
            "published_at": "2026-06-01", **extra}


def _stub_search(monkeypatch, hits) -> None:
    monkeypatch.setattr(pipeline.crossref_search, "search",
                        lambda q, **_kw: list(hits))


def _latest_stats(cfg) -> dict:
    conn = db.Database(cfg.db_file).connect()
    try:
        raw = conn.execute("SELECT stats FROM run_log ORDER BY id DESC LIMIT 1").fetchone()[0]
    finally:
        conn.close()
    return json.loads(raw)


def test_upsert_status_区分新增更新与重复(tmp_path):
    conn = db.Database(tmp_path / "t.db").connect()
    data = {"kind": "paper", "dedup_key": "title:paper", "title": "Paper",
            "title_norm": "paper", "source": "manual"}

    iid, status = db.upsert_item_status(conn, data)
    assert (status, iid > 0) == ("created", True)

    # 一模一样的第二次:库没动
    assert db.upsert_item_status(conn, data) == (iid, "duplicate")

    # 补上以前为空的字段才算更新
    assert db.upsert_item_status(conn, {**data, "journal": "Organic Letters"}) \
        == (iid, "updated")

    # 旧的二元 API 语义不变,调用方(来源、脚本、其它测试)不受影响
    assert db.upsert_item(conn, {**data, "journal": "Organic Letters"}) == (iid, False)
    conn.close()


def test_重复命中在摘要里单独成项():
    assert progress.summary({"new": 0, "updated": 0, "duplicate": 7}) == "7 篇重复命中"
    assert progress.summary({"new": 1, "duplicate": 2}) == "1 篇新增 · 2 篇重复命中"


def test_重复检索同一批论文只记重复命中(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _stub_search(monkeypatch, [_hit("10.1/dup", "Same paper")])

    first = pipeline.ingest_keyword_search(cfg, verbose=False)
    second = pipeline.ingest_keyword_search(cfg, verbose=False)

    assert (first["new"], first["updated"], first["duplicate"]) == (1, 0, 0)
    assert (second["new"], second["updated"], second["duplicate"]) == (0, 0, 1)
    # 运行记录(网页"最近运行"读的就是它)也要带上这一项
    assert _latest_stats(cfg)["duplicate"] == 1


def test_补上缺失字段算更新而不是重复(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _stub_search(monkeypatch, [_hit("10.1/fill", "Needs a journal")])
    pipeline.ingest_keyword_search(cfg, verbose=False)

    _stub_search(monkeypatch, [_hit("10.1/fill", "Needs a journal",
                                    journal="Organic Letters")])
    out = pipeline.ingest_keyword_search(cfg, verbose=False)

    assert (out["new"], out["updated"], out["duplicate"]) == (0, 1, 0)
