"""用户实际填写的排序权重必须被加载,错误值要在发起调用前被拒绝。"""
from pathlib import Path

import pytest
import yaml

from litradar.config import load_config


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


def _load_wos(tmp_path, wos):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"wos": wos}), encoding="utf-8")
    return load_config(path).wos


def test_wos_max_attempts_parsed_and_validated(tmp_path):
    assert _load_wos(tmp_path, {"max_attempts": 3}).max_attempts == 3

    for bad in (0, -1, True, "many"):
        with pytest.raises(ValueError, match="max_attempts"):
            _load_wos(tmp_path, {"max_attempts": bad})


def test_unknown_wos_key_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="未知 wos 配置项"):
        _load_wos(tmp_path, {"max_attemptz": 3})
