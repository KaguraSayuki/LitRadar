"""Real file regressions for versioned settings and credential snapshots."""
import copy

import pytest
import yaml

from litradar.config import load_config
from litradar.settings import SettingsError, SettingsStore, group_patch


@pytest.fixture
def settings_env(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    interests = tmp_path / "interests.yaml"
    path.write_text(yaml.safe_dump({"app": {"interests": str(interests),
        "db_path": str(tmp_path / "test.db")}, "llm": {"enabled": False},
        "extension": {"keep": 12}}), encoding="utf-8")
    return path, interests, None


def test_empty_instance_can_add_and_rename_without_changing_identity(settings_env):
    path, interests, client = settings_env
    store = SettingsStore(load_config(path))
    slug = store.update_group(None, group_patch({"name": "材料化学", "enabled": "on"}), store.version())
    assert slug == "default"
    store.update_group(slug, {"name": "光催化"}, store.version())
    entries = store.group_entries(store.snapshot().interests)
    assert [(g["slug"], g["name"]) for g in entries] == [("default", "光催化")]


def test_migrate_legacy_copy_disable_reorder_preserves_unedited_data(settings_env):
    path, interests, _ = settings_env
    original = {"name": "旧方向", "direction": "organic", "keywords": {"core": ["old"],
        "future": ["retain"]}, "s2_queries": ['(a | b) + -c'], "extension": {"private": 1}}
    interests.write_text(yaml.safe_dump(original), encoding="utf-8")
    store = SettingsStore(load_config(path))
    store.update_group("default", {"name": "新名字", "keywords.core": ["new"]}, store.version())
    copied = store.update_group("default", {}, store.version(), action="copy")
    store.update_group("default", {}, store.version(), action="disable")
    store.update_group(copied, {}, store.version(), action="up")
    entries = store.snapshot().interests["groups"]
    assert [g["slug"] for g in entries] == [copied, "default"]
    assert entries[1]["enabled"] is False
    assert entries[0]["extension"] == entries[1]["extension"] == original["extension"]
    assert entries[1]["keywords"]["future"] == ["retain"]
    assert entries[1]["s2_queries"] == original["s2_queries"]
    assert len(list(interests.parent.glob("interests.yaml.*.bak"))) == 4


def test_stale_form_cannot_overwrite_other_page_or_cli(settings_env):
    path, interests, _ = settings_env
    store = SettingsStore(load_config(path))
    before = store.version()
    store.update_group(None, {"name": "A"}, before)
    with pytest.raises(SettingsError, match="另一页面"):
        store.update_group("default", {"name": "stale"}, before)
    before = store.version()
    path.write_text(path.read_text() + "cli_extension: 42\n")
    with pytest.raises(SettingsError):
        store.update_group("default", {"name": "stale"}, before)
    assert store.snapshot().interests["groups"][0]["name"] == "A"


def test_failed_write_retains_file_and_only_five_backups(settings_env, monkeypatch):
    from litradar import settings
    path, interests, _ = settings_env
    store = SettingsStore(load_config(path))
    store.update_group(None, {"name": "first"}, store.version())
    for i in range(7):
        store.update_group("default", {"name": str(i)}, store.version())
    assert len(list(interests.parent.glob("interests.yaml.*.bak"))) == 5
    before = interests.read_bytes()
    def fail(*args):
        raise OSError("simulated disk failure")
    monkeypatch.setattr(settings.os, "replace", fail)
    with pytest.raises(SettingsError, match='磁盘空间'):
        store.update_group("default", {"name": "lost"}, store.version())
    assert interests.read_bytes() == before
    assert not list(interests.parent.glob(".interests.yaml.*"))


def test_partial_config_save_preserves_extensions(settings_env):
    path, _, _ = settings_env
    store = SettingsStore(load_config(path))
    original = copy.deepcopy(store.snapshot().config)
    store.update_config({"llm.deep_summary_top_n": 3}, store.version())
    data = store.snapshot().config
    assert data["extension"] == original["extension"]
    assert data["app"] == original["app"]
    assert data["llm"]["enabled"] is False
    assert data["llm"]["deep_summary_top_n"] == 3


def test_credentials_rotate_clear_mask_legacy_and_preserve_running_snapshot(settings_env, monkeypatch):
    from litradar import credentials, config
    path, _, _ = settings_env
    cfg = load_config(path)
    monkeypatch.setattr(config, '_file_values', {'DEEPSEEK_API_KEY':'old-file-value'})
    credentials.write(cfg,cfg.llm.api_key_env,'first-key')
    running = load_config(path)
    with credentials.snapshot(running):
        assert config.read_secret(cfg.llm.api_key_env) == 'first-key'
        credentials.write(cfg,cfg.llm.api_key_env,'second-key')
        assert config.read_secret(cfg.llm.api_key_env) == 'first-key'
        assert running.llm.api_key == 'first-key'
    assert load_config(path).llm.api_key == 'second-key'
    credentials.write(cfg,cfg.llm.api_key_env,None)
    assert load_config(path).llm.api_key is None  # old .env key stays masked
    monkeypatch.setenv(cfg.llm.api_key_env,'deployment-key')
    assert load_config(path).llm.api_key == 'deployment-key'
    with pytest.raises(SettingsError,match='部署环境'):
        credentials.write(cfg,cfg.llm.api_key_env,'cannot-win')
