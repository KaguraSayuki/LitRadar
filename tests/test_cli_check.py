"""``litradar check`` 对 easyScholar 密钥的结论。

以前这一项压根不检查:缺密钥时体检全绿,而期刊等级永远是空的。这里锁住
三种状态,保证"静默降级"至少会有一行明确的告警。
"""
from __future__ import annotations

import pytest

from litradar import cli
from litradar.config import Config
from litradar.sources import easyscholar


@pytest.fixture
def cfg():
    return Config()


def test_missing_key_is_warned_not_silently_green(cfg, monkeypatch):
    monkeypatch.delenv("EASYSCHOLAR_SECRET_KEY", raising=False)
    cfg.journal_rank.enabled = True

    mark, label, detail = cli._journal_rank_line(cfg)

    assert mark == cli.WARN
    assert "EASYSCHOLAR_SECRET_KEY" in label
    assert "不会显示" in detail


def test_configured_key_is_reported_ok(cfg, monkeypatch):
    monkeypatch.setenv("EASYSCHOLAR_SECRET_KEY", "abc")
    cfg.journal_rank.enabled = True

    mark, label, _ = cli._journal_rank_line(cfg)

    assert mark == cli.OK
    assert label.endswith("已设置")


def test_custom_env_name_is_what_gets_reported(cfg, monkeypatch):
    """改过 api_key_env 的用户,提示里必须是他们自己那个变量名。"""
    monkeypatch.delenv("EASYSCHOLAR_SECRET_KEY", raising=False)
    monkeypatch.setenv("LITRADAR_MY_EASYSCHOLAR", "abc")
    cfg.journal_rank.api_key_env = "LITRADAR_MY_EASYSCHOLAR"

    mark, label, _ = cli._journal_rank_line(cfg)

    assert mark == cli.OK
    assert "LITRADAR_MY_EASYSCHOLAR" in label


def test_disabled_journal_rank_is_not_a_warning(cfg, monkeypatch):
    monkeypatch.delenv("EASYSCHOLAR_SECRET_KEY", raising=False)
    cfg.journal_rank.enabled = False

    mark, _, detail = cli._journal_rank_line(cfg)

    assert mark == cli.OK
    assert "enabled" in detail


def test_blank_env_name_falls_back_to_the_default_name(cfg, monkeypatch):
    monkeypatch.setenv(easyscholar.DEFAULT_KEY_ENV, "abc")
    cfg.journal_rank.api_key_env = ""

    mark, label, _ = cli._journal_rank_line(cfg)

    assert mark == cli.OK
    assert easyscholar.DEFAULT_KEY_ENV in label


def test_whitespace_only_key_is_not_reported_as_set(cfg, monkeypatch):
    """check 的结论必须和客户端一致,否则又会变成"体检全绿但功能是空的"。"""
    monkeypatch.setenv("EASYSCHOLAR_SECRET_KEY", "   ")
    cfg.journal_rank.enabled = True

    mark, _, _ = cli._journal_rank_line(cfg)

    assert mark == cli.WARN
    assert easyscholar.available("EASYSCHOLAR_SECRET_KEY") is False


# ------------------------------------------------- 整条 check 跑通(含网络段)
def test_check_command_runs_to_completion(tmp_path, monkeypatch, capsys):
    """把网络与 LLM 打桩后跑完整条 check。

    这条用例是为一个真实事故加的:我删掉 cmd_check 里一处局部 ``import os as
    _os`` 时,只改到前半段,后半段(【6b】S2 配额)仍在用 ``_os`` —— 单元测试
    全绿,但真跑 ``litradar check`` 会 NameError。这里走完整函数体,专门堵住
    这类"函数后半段没人碰"的漏洞。
    """
    from types import SimpleNamespace

    import requests

    from litradar.config import Config

    cfg = Config()
    cfg.app.db_path = str(tmp_path / "check.db")
    monkeypatch.delenv("EASYSCHOLAR_SECRET_KEY", raising=False)
    monkeypatch.delenv("LITRADAR_ADMIN_PASSWORD_HASH", raising=False)
    monkeypatch.setenv("S2_API_KEY", "fake-s2-key")   # 走到 6b 的 else 分支

    class FakeResponse:
        status_code = 200
        text = "{}"
        headers: dict = {}

        def json(self):
            return {}

    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse())

    class FakeLLM:
        def __init__(self, cfg):
            self.cfg = cfg

        @property
        def available(self):
            return False

    monkeypatch.setattr("litradar.llm.DeepSeek", FakeLLM)

    cli.cmd_check(cfg, SimpleNamespace())

    out = capsys.readouterr().out
    assert "EASYSCHOLAR_SECRET_KEY 未设置" in out      # 新增的那一项确实打印了
    assert "【6b】" in out and "【7】" in out            # 后半段真的跑到了
    # 花钱阶段的护栏也要如实报出来:没设密码就得提醒,别让它默默无护栏
    assert "LITRADAR_ADMIN_PASSWORD_HASH 未设置" in out
    assert "每阶段每日上限 3 次" in out


# ------------------------------------------------- 管理员密码的设置与清除
def test_admin_password_set_then_clear(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from litradar import cli, config, passwords

    monkeypatch.setattr(config, "ROOT", tmp_path)
    env_path = tmp_path / ".env"
    env_path.write_text("DEEPSEEK_API_KEY=keep-me\n", encoding="utf-8")

    answers = iter(["s3cret-password", "s3cret-password"])
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: next(answers))
    cfg = config.Config()

    assert cli.cmd_admin_password(cfg, SimpleNamespace(clear=False)) == 0

    from litradar import credentials
    text = credentials.store_path(cfg).read_text(encoding="utf-8")
    assert "DEEPSEEK_API_KEY=keep-me" in env_path.read_text(), "不能修改旧密钥文件"
    stored = credentials.read_store(cfg)['values'][cfg.app.admin_password_env]
    assert passwords.verify_password("s3cret-password", stored)
    assert "s3cret-password" not in text, "只存哈希,绝不写明文"

    assert cli.cmd_admin_password(cfg, SimpleNamespace(clear=True)) == 0
    assert credentials.read_store(cfg)['values'][cfg.app.admin_password_env] is None


def test_admin_password_rejects_short_or_mismatched(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    from litradar import cli, config

    monkeypatch.setattr(config, "ROOT", tmp_path)
    cfg = config.Config()

    monkeypatch.setattr("getpass.getpass", lambda *a, **k: "short")
    assert cli.cmd_admin_password(cfg, SimpleNamespace(clear=False)) == 1

    answers = iter(["long-enough-password", "different-password"])
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: next(answers))
    assert cli.cmd_admin_password(cfg, SimpleNamespace(clear=False)) == 1

    assert not (tmp_path / ".env").exists(), "校验失败不该写出任何东西"
    assert "不一致" in capsys.readouterr().err
