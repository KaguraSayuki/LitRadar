"""http.get_json:重试耗尽时必须留痕,别把"网络挂了"伪装成"命中 0 条"。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from litradar import http  # noqa: E402


class _Resp:
    def __init__(self, status: int):
        self.status_code = status

    def raise_for_status(self):
        raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return {}


def test_放弃时打印host和最后状态码(monkeypatch, capsys):
    """回归:429/5xx 分支以前不记 last_err,放弃时连状态码都没有。"""
    monkeypatch.setattr(http.time, "sleep", lambda *_: None)
    monkeypatch.setattr(http.SESSION, "get", lambda *a, **kw: _Resp(503))
    assert http.get_json("https://api.crossref.org/works", retries=2) is None
    out = capsys.readouterr().out
    assert "[warn]" in out and "api.crossref.org" in out and "HTTP 503" in out


def test_放弃时打印异常类型(monkeypatch, capsys):
    monkeypatch.setattr(http.time, "sleep", lambda *_: None)

    def boom(*a, **kw):
        raise ConnectionError("unreachable")

    monkeypatch.setattr(http.SESSION, "get", boom)
    assert http.get_json("https://api.crossref.org/works", retries=2) is None
    assert "ConnectionError" in capsys.readouterr().out


def test_404不算失败不告警(monkeypatch, capsys):
    monkeypatch.setattr(http.time, "sleep", lambda *_: None)
    monkeypatch.setattr(http.SESSION, "get", lambda *a, **kw: _Resp(404))
    assert http.get_json("https://api.crossref.org/works/10.1/x") is None
    assert "[warn]" not in capsys.readouterr().out
