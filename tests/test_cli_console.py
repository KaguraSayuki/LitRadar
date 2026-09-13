"""Windows 上输出被重定向时的中文编码兜底。

Windows 控制台本身走 UTF-8,但重定向到文件/管道时会退回 ANSI 代码页
(英文系统 cp1252),打印中文直接 UnicodeEncodeError。这里锁定
``_configure_console`` 的行为:强制 UTF-8、编码错误降级为替换字符,
并且任何异常都必须被吞掉 —— 诊断命令不能因为改不了流编码而失败。
"""
from __future__ import annotations

import sys

import pytest

from litradar import cli


class FakeStream:
    def __init__(self, *, error: Exception | None = None):
        self.calls: list[dict] = []
        self.error = error

    def reconfigure(self, **kwargs) -> None:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error


def test_console_is_forced_to_utf8_with_replacement(monkeypatch):
    out, err = FakeStream(), FakeStream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    cli._configure_console()

    assert out.calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert err.calls == [{"encoding": "utf-8", "errors": "replace"}]


@pytest.mark.parametrize("error", [OSError("no"), ValueError("closed")])
def test_reconfigure_failure_is_swallowed(monkeypatch, error):
    out, err = FakeStream(error=error), FakeStream(error=error)
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    cli._configure_console()          # 不能抛

    assert len(out.calls) == 1


def test_streams_without_reconfigure_are_skipped(monkeypatch):
    """被替换成普通对象的流(pytest 捕获、pythonw 的 None)不能导致崩溃。"""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", object())

    cli._configure_console()          # 不能抛


def test_main_configures_the_console_before_running(monkeypatch, tmp_path):
    """main() 必须先兜住编码,否则第一条中文错误信息就会崩掉。"""
    calls: list[str] = []
    monkeypatch.setattr(cli, "_configure_console", lambda: calls.append("console"))

    class Stop(Exception):
        pass

    def boom(*_args, **_kwargs):
        calls.append("run")
        raise Stop

    monkeypatch.setattr(cli, "build_parser", boom)
    with pytest.raises(Stop):
        cli.main([])

    assert calls == ["console", "run"]
