"""用户实际填写的排序权重必须被加载,错误值要在发起调用前被拒绝。"""
from pathlib import Path

import pytest
import yaml

from litradar.config import Config, load_config, read_secret


def _load(tmp_path, ranking):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"ranking": ranking}), encoding="utf-8")
    return load_config(path).ranking


def test_sample_weights_change_ranking(tmp_path):
    cfg = _load(tmp_path, {"weights": {"llm": 0, "coarse": 1, "rule": 0}})
    assert (cfg.w_llm, cfg.w_coarse, cfg.w_rule) == (0, 1, 0)


def test_example_and_legacy_flat_weights(tmp_path):
    example = load_config(Path(__file__).resolve().parents[1] / "config.example.yaml")
    assert (example.ranking.w_llm, example.ranking.w_coarse, example.ranking.w_rule) == (0.85, 0.10, 0.05)
    legacy = _load(tmp_path, {"w_llm": 0.6, "w_coarse": 0.25, "w_rule": 0.15})
    assert (legacy.w_llm, legacy.w_coarse, legacy.w_rule) == (0.6, 0.25, 0.15)


@pytest.mark.parametrize("ranking", [
    {"weights": {"llm": -1}},
    {"weights": {"llm": float("nan")}},
    {"weights": {"coarse": float("inf")}},
    {"weights": {"llm": 1e308, "coarse": 1e308}},
    {"weights": {"rule": True}},
    {"weights": {"llm": "0.85"}},
    {"weights": {"llm": 0, "coarse": 0, "rule": 0}},
    {"weights": {"coars": 0.1}},
    {"weight": {"llm": 0.8}},
    {"weights": [0.85, 0.10, 0.05]},
    {"weights": {"llm": 0.7}, "w_llm": 0.8},
    [0.85, 0.10, 0.05],
])
def test_invalid_weights_fail_during_load(tmp_path, ranking):
    with pytest.raises(ValueError, match="ranking"):
        _load(tmp_path, ranking)


def test_empty_ranking_and_partial_weights_keep_defaults(tmp_path):
    assert _load(tmp_path, None).w_llm == 0.85
    cfg = _load(tmp_path, {"weights": {"llm": 0.7}})
    assert (cfg.w_llm, cfg.w_coarse, cfg.w_rule) == (0.7, 0.10, 0.05)


# ------------------------------------------------------- 密钥读取(统一入口)
def test_read_secret_strips_and_treats_blank_as_unset(monkeypatch):
    monkeypatch.setenv("LITRADAR_TEST_SECRET", "  s3cr3t\n")
    assert read_secret("LITRADAR_TEST_SECRET") == "s3cr3t"

    for blank in ("", "   ", "\t", "\n"):
        monkeypatch.setenv("LITRADAR_TEST_SECRET", blank)
        assert read_secret("LITRADAR_TEST_SECRET") is None

    monkeypatch.delenv("LITRADAR_TEST_SECRET", raising=False)
    assert read_secret("LITRADAR_TEST_SECRET") is None
    # 变量名本身为空/未配置时不能去读一个空名字的变量
    assert read_secret("") is None
    assert read_secret(None) is None


def test_every_secret_property_has_the_same_semantics(monkeypatch):
    """DeepSeek / IMAP / 接口口令走同一个入口:去空白,空白值=没配置。

    以前各处自己 ``os.environ.get``,尾随空格会让"已配置"的判断成真,而真正
    发出去的凭据带着空格 —— 表现为"体检说没问题,请求却被拒"。
    """
    cfg = Config()
    cases = [
        (cfg.llm.api_key_env, lambda: cfg.llm.api_key),
        (cfg.mail.imap_password_env, lambda: cfg.mail.imap_password),
        (cfg.app.token_env, lambda: cfg.app.token),
    ]
    for env_name, get in cases:
        monkeypatch.setenv(env_name, "  value  ")
        assert get() == "value", env_name
        monkeypatch.setenv(env_name, "   ")
        assert get() is None, env_name


def test_whitespace_only_llm_key_counts_as_unavailable(monkeypatch):
    """只有空格时不该让 DeepSeek 认为自己可用,否则会拿坏密钥去请求。"""
    from litradar.llm import DeepSeek

    cfg = Config()
    monkeypatch.setenv(cfg.llm.api_key_env, "   ")
    assert DeepSeek(cfg.llm).available is False

    monkeypatch.setenv(cfg.llm.api_key_env, " sk-test \n")
    assert cfg.llm.api_key == "sk-test"
    assert DeepSeek(cfg.llm).available is True


def test_semanticscholar_uses_the_same_key_semantics(monkeypatch):
    """S2 的 9 处 key 判断也必须和真正发出去的密钥一致。"""
    from litradar.sources import semanticscholar

    monkeypatch.setenv("S2_API_KEY", "   ")
    assert read_secret("S2_API_KEY") is None
    assert "x-api-key" not in semanticscholar._headers()

    monkeypatch.setenv("S2_API_KEY", " s2-key ")
    assert semanticscholar._headers()["x-api-key"] == "s2-key"
