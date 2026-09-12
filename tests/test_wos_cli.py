"""无人值守入口应显式报告失败，首次授权不能泄露令牌。"""
import json

import pytest
import yaml

from litradar import cli
from litradar.config import ROOT, load_config


def config_file(tmp_path, **extra):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"app": {"db_path": str(tmp_path / "test.db")}, **extra}))
    return str(path)


def test_wos_sync_passes_force_and_limit_and_returns_failure(tmp_path, monkeypatch, capsys):
    def fake(cfg, **kwargs):
        assert kwargs == {"force": True, "limit": 3}
        return {"errors": 1, "needs_login": 1}
    monkeypatch.setattr(cli.wos_sync, "run", fake)
    assert cli.main(["-c", config_file(tmp_path), "wos-sync", "--retry-now", "--limit", "3"]) == 1
    assert json.loads(capsys.readouterr().out)["needs_login"] == 1


def test_mail_login_only_shows_device_instructions_not_token(tmp_path, monkeypatch, capsys):
    def fake(cfg, *, on_device_code):
        on_device_code({"message": "Enter the example code at Microsoft's device page."})
        return "SECRET-ACCESS-TOKEN"
    monkeypatch.setattr(cli.mail_oauth, "device_authorize", fake)
    path = config_file(tmp_path, mail={"imap_auth": "oauth2"})
    assert cli.main(["-c", path, "mail-login"]) == 0
    output = capsys.readouterr().out
    assert "example code" in output
    assert "SECRET-ACCESS-TOKEN" not in output


def test_wos_can_be_disabled_without_browser_dependency(tmp_path, capsys):
    path = config_file(tmp_path, wos={"enabled": False})
    assert cli.main(["-c", path, "wos-sync"]) == 0
    assert json.loads(capsys.readouterr().out)["disabled"] is True


@pytest.mark.parametrize("wos", [
    {"batch_size": 1001}, {"batch_size": 0}, {"max_alerts_per_run": -1},
    {"max_records_per_alert": True}, {"retry_minutes": 0},
    {"min_interval_seconds": float("nan")}, {"min_interval_seconds": -1},
    {"enabled": "false"}, {"headless": "false"}, {"browser_profile_dir": ""},
    {"batch_szie": 100},
])
def test_unsafe_wos_config_rejected_before_running_browser(tmp_path, wos):
    with pytest.raises(ValueError, match="wos"):
        load_config(config_file(tmp_path, wos=wos))


def test_relative_runtime_paths_are_stable_outside_repository(tmp_path, monkeypatch):
    path = config_file(tmp_path, mail={"oauth_token_cache": "data/test-token.json"},
                       wos={"browser_profile_dir": "data/test-browser"})
    monkeypatch.chdir(tmp_path)
    cfg = load_config(path)
    assert cfg.wos.browser_profile_dir == str(ROOT / "data/test-browser")
    assert cfg.mail.oauth_token_cache == str(ROOT / "data/test-token.json")
    assert not cfg.mail.imap_mark_seen
