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


# ------------------------------------------------------- 花钱阶段的护栏配置
def test_admin_defaults_are_the_documented_ones(tmp_path):
    cfg = load_config(tmp_path / "missing.yaml").admin

    assert cfg.guarded_stages == ["rank", "summarize", "all"]
    assert cfg.cooldown_seconds == 60
    assert cfg.daily_limit == 3


def test_example_config_documents_the_same_defaults():
    """示例配置里的 admin 段必须与代码默认值一致,否则又是一处漂移。"""
    example = load_config(Path(__file__).resolve().parents[1] / "config.example.yaml")

    assert example.admin == Config().admin


def test_admin_stage_typo_is_rejected(tmp_path):
    """阶段名拼错会让人以为有护栏、其实没有 —— 必须直接报错。"""
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(
        {"admin": {"guarded_stages": ["rank", "summarise"]}}), encoding="utf-8")

    with pytest.raises(ValueError, match="guarded_stages"):
        load_config(path)


@pytest.mark.parametrize("data", [
    {"daily_limit": -1},
    {"cooldown_seconds": -5},
    {"daily_limit": "three"},
    {"cooldown_seconds": True},
])
def test_admin_limits_must_be_non_negative_ints(tmp_path, data):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"admin": data}), encoding="utf-8")

    with pytest.raises(ValueError, match="非负整数"):
        load_config(path)


def test_unknown_admin_key_is_rejected(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"admin": {"dailly_limit": 3}}), encoding="utf-8")

    with pytest.raises(ValueError, match="未知 admin 配置项"):
        load_config(path)


def test_admin_overrides_are_loaded(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"admin": {
        "guarded_stages": ["Rank", "enrich"],
        "cooldown_seconds": 0,
        "daily_limit": 5,
    }}), encoding="utf-8")

    cfg = load_config(path).admin

    assert cfg.guarded_stages == ["rank", "enrich"]   # 大小写被归一
    assert cfg.cooldown_seconds == 0 and cfg.daily_limit == 5


def test_is_loopback():
    from litradar.config import AppConfig

    assert AppConfig(host="127.0.0.1").is_loopback
    assert AppConfig(host="localhost").is_loopback
    assert AppConfig(host="::1").is_loopback
    # 空串在 uvicorn 里等于绑全部网卡,不能算本机
    assert not AppConfig(host="").is_loopback
    assert not AppConfig(host="0.0.0.0").is_loopback
    assert not AppConfig(host="192.168.1.5").is_loopback
