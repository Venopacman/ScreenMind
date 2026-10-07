"""Tests for config.py — settings parsing and properties."""

from screenmind.config import Settings


def test_default_settings():
    s = Settings(data_dir="/tmp/screenmind_test")
    assert s.capture_interval == 40
    assert s.screenshot_quality == 70
    assert s.api_port == 7777


def test_blocked_apps_list_empty():
    s = Settings(data_dir="/tmp/test", blocked_apps="")
    assert s.blocked_apps_list == []


def test_blocked_apps_list_parsing():
    s = Settings(data_dir="/tmp/test", blocked_apps="1password, banking, keychain")
    assert s.blocked_apps_list == ["1password", "banking", "keychain"]


def test_env_file_keys_of_removed_settings_are_ignored(tmp_path, monkeypatch):
    """An old .env can still name removed settings. Startup must not fail."""
    env = tmp_path / ".env"
    env.write_text("BOOKMARK_HOTKEY=ctrl+shift+b\nWORKSPACE_DIRS=~/Projects\n"
                   "GEMMA_MODE=local\nCAPTURE_INTERVAL=25\n")
    s = Settings(_env_file=str(env), data_dir="/tmp/test")
    assert s.capture_interval == 25
    assert not hasattr(s, "bookmark_hotkey")


def test_heavy_apps_list():
    s = Settings(data_dir="/tmp/test", heavy_apps="game,valorant,blender")
    assert "game" in s.heavy_apps_list
    assert "valorant" in s.heavy_apps_list
    assert len(s.heavy_apps_list) == 3


def test_meeting_apps_list():
    s = Settings(data_dir="/tmp/test", meeting_apps="zoom,teams,meet")
    assert "zoom" in s.meeting_apps_list
    assert len(s.meeting_apps_list) == 3


def test_num_gpu_layers():
    s = Settings(data_dir="/tmp/test", performance_mode="minimal")
    assert s.num_gpu_layers == 0

    s = Settings(data_dir="/tmp/test", performance_mode="balanced")
    assert s.num_gpu_layers == 15

    s = Settings(data_dir="/tmp/test", performance_mode="maximum")
    assert s.num_gpu_layers == 99


def test_data_path_resolution():
    s = Settings(data_dir="~/.screenmind")
    assert s.data_path.is_absolute()
    assert "~" not in str(s.data_path)


def test_settings_json_keys_of_removed_settings_are_ignored(tmp_path, caplog):
    """An old settings.json still holds removed keys. Load the rest, quietly."""
    import json
    import logging
    s = Settings(data_dir=str(tmp_path))
    s.settings_json_path.write_text(json.dumps({
        "capture_interval": 25,
        "bookmark_hotkey": "ctrl+shift+b", "voice_hotkey": "ctrl+shift+v",
        "webhook_url": "https://example.com", "notion_token": "x",
        "obsidian_enabled": True, "agents_enabled": True, "auto_bookmark": True,
        "dashboard_lock_timeout": 30,
    }))
    with caplog.at_level(logging.INFO, logger="screenmind.config"):
        s.load_runtime_overrides()
    assert s.capture_interval == 25
    assert not hasattr(s, "bookmark_hotkey")
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert "bookmark_hotkey" not in caplog.text

