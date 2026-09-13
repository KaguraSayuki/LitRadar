"""easyScholar 密钥解析与"没密钥也不报错"的保证。

两件事必须锁住:
  · ``journal_rank.api_key_env`` 改的名字要真的生效(以前写死,配置项是摆设);
  · 没有密钥时 ``fetch_rank`` 直接返回 None 且**不发任何请求**,让整条流水线
    在没有 easyScholar 的机器上照常跑完。
"""
from __future__ import annotations

import pytest

from litradar.sources import easyscholar


@pytest.fixture(autouse=True)
def _clear_keys(monkeypatch):
    monkeypatch.delenv(easyscholar.DEFAULT_KEY_ENV, raising=False)
    monkeypatch.delenv("LITRADAR_TEST_EASYSCHOLAR_KEY", raising=False)


def test_default_env_name_is_used(monkeypatch):
    monkeypatch.setenv(easyscholar.DEFAULT_KEY_ENV, "abc")

    assert easyscholar.api_key() == "abc"
    assert easyscholar.available() is True


def test_configured_env_name_is_honoured(monkeypatch):
    """config.yaml 里改成别的变量名时,必须读那个名字。"""
    monkeypatch.setenv("LITRADAR_TEST_EASYSCHOLAR_KEY", "custom")

    assert easyscholar.api_key("LITRADAR_TEST_EASYSCHOLAR_KEY") == "custom"
    assert easyscholar.available("LITRADAR_TEST_EASYSCHOLAR_KEY") is True
    # 默认名字没设置,所以默认调用仍视为不可用
    assert easyscholar.available() is False


def test_blank_env_name_falls_back_to_default(monkeypatch):
    """api_key_env 配成空串不该变成"读环境里那个空名字的变量"。"""
    monkeypatch.setenv(easyscholar.DEFAULT_KEY_ENV, "abc")

    assert easyscholar.api_key("") == "abc"
    assert easyscholar.available("") is True


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_key_value_counts_as_missing(monkeypatch, blank):
    """空串/纯空格不该让 available() 为真然后拿着垃圾密钥去请求。"""
    monkeypatch.setenv(easyscholar.DEFAULT_KEY_ENV, blank)

    assert easyscholar.api_key() is None
    assert easyscholar.available() is False


def test_missing_key_makes_no_request(monkeypatch):
    """核心保证:没密钥时一个请求都不发,也不抛异常。"""
    calls = []
    monkeypatch.setattr(easyscholar.requests, "get",
                        lambda *a, **k: calls.append((a, k)))

    assert easyscholar.fetch_rank("Angewandte Chemie") is None
    assert easyscholar.fetch_rank("Angewandte Chemie", "LITRADAR_TEST_EASYSCHOLAR_KEY") is None
    assert calls == []


def test_empty_journal_name_makes_no_request(monkeypatch):
    monkeypatch.setenv(easyscholar.DEFAULT_KEY_ENV, "abc")
    calls = []
    monkeypatch.setattr(easyscholar.requests, "get",
                        lambda *a, **k: calls.append((a, k)))

    assert easyscholar.fetch_rank("   ") is None
    assert calls == []
