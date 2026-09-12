"""真实 SDK 异常类型的离线回归,验证失败批次不影响已成功的结果。"""
import datetime
import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import openai
import pytest

from litradar import db, rank, summarize
from litradar.config import Config
from litradar.llm import DeepSeek, LLMError


def _error(kind):
    request = httpx.Request("POST", "https://example.invalid")
    if kind == "connection":
        return openai.APIConnectionError(request=request)
    if kind == "timeout":
        return openai.APITimeoutError(request=request)
    status = 429 if kind == "rate_limit" else 503
    cls = openai.RateLimitError if status == 429 else openai.InternalServerError
    return cls("unavailable", response=httpx.Response(status, request=request), body=None)


def _response(data):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=json.dumps(data)))])


def _client(monkeypatch, effects):
    create = Mock(side_effect=effects)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(DeepSeek, "_client_or_raise", lambda *_: client)
    return create


@pytest.mark.parametrize("kind", ["connection", "timeout", "rate_limit", "server"])
def test_sdk_errors_use_application_exception(monkeypatch, kind):
    error = _error(kind)
    _client(monkeypatch, [error])
    with pytest.raises(LLMError) as caught:
        DeepSeek(Config().llm).json("system", "user")
    assert caught.value.__cause__ is error


def _seed(cfg, count):
    conn = db.Database(cfg.db_file).connect()
    for i in range(count):
        db.upsert_item(conn, {
            "kind": "paper", "dedup_key": f"doi:10.9999/{i}", "doi": f"10.9999/{i}",
            "title": f"Chemistry paper {i}", "title_norm": f"chemistry paper {i}",
            "published_at": datetime.date.today().isoformat(), "source": "test",
        })
    conn.commit()
    return conn


def test_rank_preserves_successful_batch_and_continues(tmp_path, monkeypatch):
    cfg = Config()
    cfg.app.db_path = str(tmp_path / "t.db")
    cfg.llm.api_key_env = "LITRADAR_TEST_LLM_KEY"
    cfg.llm.rerank_batch_size = 5
    monkeypatch.setenv(cfg.llm.api_key_env, "fake-key")
    conn = _seed(cfg, 11)
    success = _response({"scores": [{"id": i, "score": 80, "reason": "relevant"}
                                     for i in range(1, 6)]})
    last = _response({"scores": [{"id": 1, "score": 90, "reason": "last"}]})
    create = _client(monkeypatch, [success, _error("connection"), last])

    result = rank.run(cfg, verbose=False)
    assert result["llm_scored"] == 6
    assert result["scored"] == 11
    assert create.call_count == 3
    rows = conn.execute("SELECT llm_score FROM score").fetchall()
    assert sum(r[0] == 80 for r in rows) == 5
    assert sum(r[0] == 90 for r in rows) == 1
    assert sum(r[0] is None for r in rows) == 5
    conn.close()


def test_brief_summary_returns_no_results_on_timeout(tmp_path, monkeypatch):
    cfg = Config()
    cfg.app.db_path = str(tmp_path / "t.db")
    conn = _seed(cfg, 1)
    rows = conn.execute("SELECT * FROM item").fetchall()
    _client(monkeypatch, [_error("timeout")])
    assert summarize.summarize_brief(rows, DeepSeek(cfg.llm)) == {}
    conn.close()


def test_summary_continues_after_timeout_and_retries_failed_item(tmp_path, monkeypatch):
    cfg = Config()
    cfg.app.db_path = str(tmp_path / "t.db")
    cfg.llm.api_key_env = "LITRADAR_TEST_LLM_KEY"
    cfg.llm.deep_summary_top_n = 2
    monkeypatch.setenv(cfg.llm.api_key_env, "fake-key")
    conn = _seed(cfg, 3)
    ids = [r[0] for r in conn.execute("SELECT id FROM item ORDER BY id")]
    for index, iid in enumerate(ids):
        db.save_score(conn, iid, final_score=90 - index)
    conn.commit()
    deep = _response({"title_zh": "深度摘要", "one_liner": "结论"})
    brief = _response({"items": [{"id": 1, "title_zh": "简要摘要", "one_liner": "结论"}]})
    create = _client(monkeypatch, [_error("timeout"), deep, brief, deep])

    first = summarize.run(cfg, verbose=False)
    assert (first["deep"], first["brief"], first["skipped"]) == (1, 1, 1)
    assert conn.execute("SELECT COUNT(*) FROM summary WHERE item_id=?", (ids[0],)).fetchone()[0] == 0
    second = summarize.run(cfg, verbose=False)
    assert (second["deep"], second["brief"], second["skipped"]) == (1, 0, 0)
    assert conn.execute("SELECT COUNT(*) FROM summary").fetchone()[0] == 3
    assert create.call_count == 4
    conn.close()
