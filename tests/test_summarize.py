"""深度摘要的选取:跟着排名走,不跟着"本轮 pending 的前 N"走(不联网)。"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

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

    monkeypatch.setattr(summarize, "DeepSeek", _FakeLLM)
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
