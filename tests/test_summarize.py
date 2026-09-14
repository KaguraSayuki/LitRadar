"""深度摘要的选取:跟着排名走,不跟着"本轮 pending 的前 N"走(不联网)。"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from litradar import db, summarize  # noqa: E402
from litradar.config import Config  # noqa: E402


class _FakeLLM:
    available = True

    def __init__(self, *a, **kw):
        pass


def _seed(conn, title: str) -> int:
    iid, _ = db.upsert_item(conn, {
        "kind": "paper", "dedup_key": f"doi:10.1/{title}", "doi": f"10.1/{title}",
        "title": title, "title_norm": title.lower(), "source": "test",
        "abstract": f"abstract of {title}",
        "published_at": datetime.date.today().isoformat(),
    })
    # 摘要只处理**本组成员**;真实链路里由 pipeline._store(group_id=...) 记
    db.add_to_group(conn, db.ensure_group(conn, db.DEFAULT_GROUP_SLUG,
                                         name=db.DEFAULT_GROUP_NAME), iid)
    return iid


def _cfg(tmp_path, top_n: int = 1) -> Config:
    cfg = Config()
    cfg.app.db_path = str(tmp_path / "t.db")
    cfg.interests_data = {"direction": "metal carbene"}
    cfg.llm.deep_summary_top_n = top_n
    return cfg


def _patch(monkeypatch) -> tuple[list, list]:
    """把两条 LLM 路径换成记录调用的假实现。"""
    deep_ids: list[int] = []
    brief_ids: list[int] = []

    def fake_one(row, prof, llm):
        deep_ids.append(int(row["id"]))
        return {"title_zh": "深", "one_liner": "深", "problem": "p", "method": "m",
                "key_results": "k", "limitation": "l", "relevance": "r"}

    def fake_brief(rows, llm):
        brief_ids.extend(int(r["id"]) for r in rows)
        return {int(r["id"]): {"title_zh": "浅", "one_liner": "浅"} for r in rows}

    monkeypatch.setattr(summarize, "LLMClient", _FakeLLM)
    monkeypatch.setattr(summarize, "summarize_one", fake_one)
    monkeypatch.setattr(summarize, "summarize_brief", fake_brief)
    return deep_ids, brief_ids


def test_排名上升的brief条目会升级成deep(tmp_path, monkeypatch):
    """回归:条目排名后来上升时,它已经有 brief 摘要、不再是 pending,
    于是永远升不成深度摘要 —— "前 N 篇深度摘要"名不副实。"""
    cfg = _cfg(tmp_path, top_n=1)
    conn = db.Database(cfg.db_file).connect()
    risen = _seed(conn, "risen")
    other = _seed(conn, "other")
    db.save_score(conn, risen, final_score=90.0)
    db.save_score(conn, other, final_score=10.0)
    # 上一轮它排名靠后,只拿到了 brief
    db.save_summary(conn, risen, {"title_zh": "旧", "one_liner": "旧"}, "brief", "m")
    conn.commit()
    conn.close()

    deep_ids, brief_ids = _patch(monkeypatch)
    summarize.run(cfg, verbose=False)

    assert deep_ids == [risen], "排名第一的条目必须补上深度摘要"
    assert risen not in brief_ids, "同一轮不该又给它做一遍 brief"

    conn = db.Database(cfg.db_file).connect()
    depth = conn.execute("SELECT depth FROM summary WHERE item_id = ?",
                         (risen,)).fetchone()[0]
    assert depth == "deep"
    conn.close()


def test_已有深度摘要的前N名不重做(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, top_n=1)
    conn = db.Database(cfg.db_file).connect()
    top = _seed(conn, "top")
    tail = _seed(conn, "tail")
    db.save_score(conn, top, final_score=90.0)
    db.save_score(conn, tail, final_score=10.0)
    db.save_summary(conn, top, {"title_zh": "深", "one_liner": "深"}, "deep", "m")
    conn.commit()
    conn.close()

    deep_ids, brief_ids = _patch(monkeypatch)
    summarize.run(cfg, verbose=False)

    assert deep_ids == []
    assert brief_ids == [tail], "深度摘要名额被占住,其余仍走 brief"


def test_force仍然重做前N名(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, top_n=1)
    conn = db.Database(cfg.db_file).connect()
    top = _seed(conn, "top")
    db.save_score(conn, top, final_score=90.0)
    db.save_summary(conn, top, {"title_zh": "深", "one_liner": "深"}, "deep", "m")
    conn.commit()
    conn.close()

    deep_ids, brief_ids = _patch(monkeypatch)
    summarize.run(cfg, verbose=False, force=True)

    assert deep_ids == [top]
    assert brief_ids == []


def test_无原文的deep在同秒补齐后刷新且只刷新一次(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(db, "now", lambda: "2026-09-12T12:00:00+00:00")
    conn = db.Database(cfg.db_file).connect()
    iid = _seed(conn, "missing")
    conn.execute("UPDATE item SET abstract=NULL WHERE id=?", (iid,))
    db.save_score(conn, iid, final_score=90)
    conn.commit()
    deep_ids, _ = _patch(monkeypatch)
    summarize.run(cfg, verbose=False)
    assert deep_ids == [iid]

    conn.execute("UPDATE item SET abstract='原文已补齐' WHERE id=?", (iid,))
    db.save_enrichment(conn, iid, {"cited_by_count": 1})
    conn.commit()
    summarize.run(cfg, verbose=False)
    summarize.run(cfg, verbose=False)
    assert deep_ids == [iid, iid]
    saved = conn.execute("SELECT depth, abstract_hash FROM summary WHERE item_id=?", (iid,)).fetchone()
    assert tuple(saved) == ("deep", summarize._abstract_hash("原文已补齐"))
    conn.close()


def test_仅富化元数据不重做原文未变的摘要(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    conn = db.Database(cfg.db_file).connect()
    iid = _seed(conn, "unchanged")
    db.save_score(conn, iid, final_score=90)
    conn.commit()
    deep_ids, _ = _patch(monkeypatch)
    summarize.run(cfg, verbose=False)

    db.save_enrichment(conn, iid, {"cited_by_count": 10})
    conn.execute("UPDATE item_enrichment SET enriched_at='2099-01-01' WHERE item_id=?", (iid,))
    conn.commit()
    summarize.run(cfg, verbose=False)
    assert deep_ids == [iid]
    conn.close()


@pytest.mark.parametrize("depth", ["deep", "brief"])
def test_历史摘要富化后刷新并保留深度(tmp_path, monkeypatch, depth):
    cfg = _cfg(tmp_path, top_n=0)
    conn = db.Database(cfg.db_file).connect()
    iid = _seed(conn, "legacy")
    db.save_summary(conn, iid, {"one_liner": "旧"}, depth, "m")
    conn.execute("UPDATE summary SET created_at='2000-01-01' WHERE item_id=?", (iid,))
    db.save_enrichment(conn, iid, {"cited_by_count": 1})
    conn.commit()
    deep_ids, brief_ids = _patch(monkeypatch)
    summarize.run(cfg, verbose=False)
    summarize.run(cfg, verbose=False)
    assert deep_ids == ([iid] if depth == "deep" else [])
    assert brief_ids == ([iid] if depth == "brief" else [])
    assert conn.execute("SELECT depth FROM summary WHERE item_id=?", (iid,)).fetchone()[0] == depth
    conn.close()


def test_旧无原文占位内容没有富化时间也会恢复(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    conn = db.Database(cfg.db_file).connect()
    iid = _seed(conn, "placeholder")
    data = {key: "摘要未提及" for key in ("problem", "method", "key_results", "limitation")}
    db.save_summary(conn, iid, data, "deep", "m")
    conn.commit()
    deep_ids, _ = _patch(monkeypatch)
    summarize.run(cfg, verbose=False)
    assert deep_ids == [iid]
    conn.close()


def test_原文更新后深度摘要掉出前N仍保留深度(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, top_n=0)
    conn = db.Database(cfg.db_file).connect()
    iid = _seed(conn, "former-top")
    db.save_summary(conn, iid, {"method": "旧方法"}, "deep", "m",
                    abstract_hash=summarize._abstract_hash("原文旧版"))
    conn.commit()
    deep_ids, brief_ids = _patch(monkeypatch)
    summarize.run(cfg, verbose=False)
    assert deep_ids == [iid]
    assert brief_ids == []
    assert conn.execute("SELECT method FROM summary WHERE item_id=?", (iid,)).fetchone()[0] == "m"
    conn.close()
