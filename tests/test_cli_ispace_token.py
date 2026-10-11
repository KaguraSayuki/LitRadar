"""``litradar ispace-token`` 的设置、清除与验证。

网页表单是推荐入口，命令行这条通路要满足两件事:令牌只经过管道（不进命令行
参数、不进 shell 历史），以及形状不对时在写入**之前**就拒绝 —— 令牌格式错了
在请求时表现为"会话过期",极难排查。
"""
from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from litradar import cli, config, credentials  # noqa: E402
from litradar.sources import xmol_ispace  # noqa: E402


VALID = "atk0210=fake-access-token; rtk0210=fake-refresh-token"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """把 ROOT 指到 tmp_path,凭据文件就落在临时目录里,绝不碰真实实例。"""
    monkeypatch.setattr(config, "ROOT", tmp_path)
    return config.Config()


def _args(**over):
    base = {"clear": False, "stdin": False, "check": False}
    base.update(over)
    return SimpleNamespace(**base)


def _stdin(monkeypatch, text: str) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))


def test_stdin_saves_token_without_echoing_it(cfg, monkeypatch, capsys):
    _stdin(monkeypatch, VALID + "\n")
    assert cli.cmd_ispace_token(cfg, _args(stdin=True)) == 0

    stored = credentials.read_store(cfg)["values"][cfg.sources.ispace_token_env]
    assert stored == VALID

    out = capsys.readouterr().out
    assert "已保存" in out
    # 令牌绝不能出现在输出里 —— 否则会留在终端回滚和 CI 日志中。
    assert "fake-access-token" not in out


def test_令牌首尾空白被去掉(cfg, monkeypatch):
    _stdin(monkeypatch, f"  {VALID}  \n\n")
    assert cli.cmd_ispace_token(cfg, _args(stdin=True)) == 0
    assert credentials.read_store(cfg)["values"][cfg.sources.ispace_token_env] == VALID


@pytest.mark.parametrize("bad,missing", [
    ("atk0210=only-access", "rtk0210="),
    ("rtk0210=only-refresh", "atk0210="),
    ("some-random-string", "atk0210="),
])
def test_缺少任一_cookie_都拒绝写入(cfg, monkeypatch, capsys, bad, missing):
    _stdin(monkeypatch, bad)
    assert cli.cmd_ispace_token(cfg, _args(stdin=True)) == 1
    assert missing in capsys.readouterr().err
    assert cfg.sources.ispace_token_env not in credentials.read_store(cfg).get("values", {})


def test_空输入被拒绝(cfg, monkeypatch, capsys):
    _stdin(monkeypatch, "   \n")
    assert cli.cmd_ispace_token(cfg, _args(stdin=True)) == 1
    assert "没有读到令牌" in capsys.readouterr().err


def test_多行令牌被拒绝(cfg, monkeypatch, capsys):
    """整串 Cookie 必须是一行 —— 粘成多行时要在写入前就报错。

    ``credentials.write`` 同样禁止换行,但那时错误信息是通用的一句
    "凭据过长或包含换行",不如在这里直接说清该粘成什么样。
    """
    _stdin(monkeypatch, VALID + "\nrtk0210=extra")
    assert cli.cmd_ispace_token(cfg, _args(stdin=True)) == 1
    assert "单行" in capsys.readouterr().err
    assert cfg.sources.ispace_token_env not in credentials.read_store(cfg).get("values", {})


def test_清除令牌(cfg, monkeypatch):
    _stdin(monkeypatch, VALID)
    cli.cmd_ispace_token(cfg, _args(stdin=True))
    assert cli.cmd_ispace_token(cfg, _args(clear=True)) == 0
    assert credentials.read_store(cfg)["values"][cfg.sources.ispace_token_env] is None


def test_check_在未设置时返回失败而不是崩溃(cfg, capsys):
    assert cli.cmd_ispace_token(cfg, _args(check=True)) == 1
    assert "尚未设置" in capsys.readouterr().err


def test_check_验证有效会话(cfg, monkeypatch, capsys):
    _stdin(monkeypatch, VALID)
    cli.cmd_ispace_token(cfg, _args(stdin=True))
    capsys.readouterr()

    monkeypatch.setattr(xmol_ispace, "load_subscriptions",
                        lambda *a, **k: {1: "a", 2: "b", 3: "c"})
    assert cli.cmd_ispace_token(cfg, _args(check=True)) == 0
    assert "读到 3 个订阅关键词" in capsys.readouterr().out


def test_check_把会话过期报成失败(cfg, monkeypatch, capsys):
    _stdin(monkeypatch, VALID)
    cli.cmd_ispace_token(cfg, _args(stdin=True))
    capsys.readouterr()

    def expired(*a, **k):
        raise xmol_ispace.IspaceAuthError("X-MOL 会话已过期或被拒绝。")

    monkeypatch.setattr(xmol_ispace, "load_subscriptions", expired)
    assert cli.cmd_ispace_token(cfg, _args(check=True)) == 1
    assert "过期" in capsys.readouterr().err


def test_check_不改动已保存的令牌(cfg, monkeypatch):
    _stdin(monkeypatch, VALID)
    cli.cmd_ispace_token(cfg, _args(stdin=True))
    monkeypatch.setattr(xmol_ispace, "load_subscriptions", lambda *a, **k: {})
    cli.cmd_ispace_token(cfg, _args(check=True))
    assert credentials.read_store(cfg)["values"][cfg.sources.ispace_token_env] == VALID


# ------------------------------------------------- 形状校验（写盘之前）
@pytest.mark.parametrize("bad,match", [
    # 文档里的省略号示例被直接粘进来 —— 真实踩过一次
    ("atk0210=…; rtk0210=…", "非 ASCII"),
    ("atk0210=中文; rtk0210=中文", "非 ASCII"),
    ("atk0210=x; rtk0210=y", "太短"),
    ("atk0210=" + "a" * 40, "缺少 rtk0210="),
    ("rtk0210=" + "b" * 40, "缺少 atk0210="),
    ("atk0210=" + "a" * 40 + "\nrtk0210=" + "b" * 40, "单行"),
    ("a" * 9000, "过长"),
    ("", "没有读到令牌"),
])
def test_坏令牌在写入前被拒(cfg, monkeypatch, capsys, bad, match):
    _stdin(monkeypatch, bad)
    assert cli.cmd_ispace_token(cfg, _args(stdin=True)) == 1
    assert match in capsys.readouterr().err
    assert cfg.sources.ispace_token_env not in credentials.read_store(cfg).get("values", {})


def test_形状校验函数可被网页复用(cfg):
    """校验放在适配器里,网页那条路才能用同一套判断。"""
    assert xmol_ispace.check_token_shape(VALID) is None
    assert "非 ASCII" in xmol_ispace.check_token_shape("atk0210=…; rtk0210=…")
    assert xmol_ispace.check_token_shape(None) == "没有读到令牌。"


def test_非ASCII令牌在请求时报成可为用户理解的错误(cfg, monkeypatch):
    """即使绕过写入校验,请求阶段也不该抛原始 UnicodeEncodeError。"""
    import requests

    class _S:
        def get(self, *a, **k):
            # requests 在编码 header 时抛的就是这个异常
            raise UnicodeEncodeError("latin-1", "…", 0, 1, "ordinal not in range")

    with pytest.raises(xmol_ispace.IspaceAuthError) as err:
        xmol_ispace._get_json(_S(), "/x", None, "atk0210=…", timeout=5)
    assert "非 ASCII" in str(err.value)
