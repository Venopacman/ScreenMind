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



# ── Settings that differ from the defaults (G39) ─────────────────────────────

def test_saving_the_default_value_removes_the_key(tmp_path):
    """The dashboard posts its whole form. Values left at the default must not
    be frozen into settings.json, or they miss later default changes."""
    import json
    s = Settings(data_dir=str(tmp_path))
    s.save_runtime_overrides({"capture_interval": 10, "performance_mode": "balanced",
                              "sensitive_filter_types": "credit_card,ssn,api_key,jwt,password"})
    assert json.loads(s.settings_json_path.read_text()) == {"capture_interval": 10}
    assert s.capture_interval == 10

    s.save_runtime_overrides({"capture_interval": 40})
    assert json.loads(s.settings_json_path.read_text()) == {}
    assert s.capture_interval == 40


def test_saving_keeps_unknown_keys_and_compares_with_env_file(tmp_path, monkeypatch):
    """.env sets the base: a dashboard value equal to the code default but not
    to .env is a real choice and stays. Keys the code doesn't know stay too."""
    import json
    env = tmp_path / ".env"
    env.write_text("CAPTURE_INTERVAL=25\n")
    s = Settings(_env_file=str(env), data_dir=str(tmp_path))
    s.settings_json_path.write_text(json.dumps({"bookmark_hotkey": "ctrl+shift+b"}))
    s.save_runtime_overrides({"capture_interval": 40, "retention_days": "7"})
    assert json.loads(s.settings_json_path.read_text()) == {
        "bookmark_hotkey": "ctrl+shift+b", "capture_interval": 40}


def test_loading_does_not_rewrite_settings_json(tmp_path):
    import json
    s = Settings(data_dir=str(tmp_path))
    text = json.dumps({"capture_interval": 40, "performance_mode": "balanced"})
    s.settings_json_path.write_text(text)
    s.load_runtime_overrides()
    assert s.settings_json_path.read_text() == text


def _clear_settings_env(monkeypatch):
    import os
    for k in list(os.environ):
        if k.lower() in Settings.model_fields:
            monkeypatch.delenv(k)


def test_non_defaults_name_their_source(tmp_path, monkeypatch):
    import json
    _clear_settings_env(monkeypatch)
    monkeypatch.setenv("RETENTION_DAYS", "3")
    env = tmp_path / ".env"
    env.write_text("SCREENSHOT_QUALITY=50\n")
    s = Settings(_env_file=str(env), data_dir=str(tmp_path))
    s.settings_json_path.write_text(json.dumps({
        "capture_interval": 10, "performance_mode": "balanced",  # the default: not listed
        "capture_paused": False, "setup_complete": True,  # runtime state: not listed
    }))
    s.load_runtime_overrides()
    found = {name: (value, source) for name, value, source in s.non_defaults()}
    found.pop("data_dir")  # a constructor argument here
    assert found == {
        "retention_days": (3, "env"),
        "screenshot_quality": (50, ".env"),
        "capture_interval": (10, "settings.json"),
    }
    assert "capture_interval=10 (settings.json)" in s.describe_non_defaults()


def test_describe_non_defaults_none_and_long_values(monkeypatch):
    _clear_settings_env(monkeypatch)
    assert Settings(_env_file=None).describe_non_defaults() == "none"
    s = Settings(_env_file=None, heavy_apps="x" * 100)
    assert s.describe_non_defaults() == "heavy_apps=" + "x" * 57 + "... (.env)"


# ── Log file (G38) ───────────────────────────────────────────────────────────

def _drop_file_handlers():
    import logging
    from logging.handlers import RotatingFileHandler
    root = logging.getLogger("screenmind")
    for h in [h for h in root.handlers if isinstance(h, RotatingFileHandler)]:
        root.removeHandler(h)
        h.close()


def test_file_log_in_the_data_dir_once(tmp_path, monkeypatch):
    import logging
    from logging.handlers import RotatingFileHandler
    from screenmind.config import LOG_MAX_BYTES, setup_file_log
    monkeypatch.delenv("SCREENMIND_LOG_FILE", raising=False)
    try:
        path = setup_file_log(tmp_path / "data")
        assert path == tmp_path / "data" / "screenmind.log"
        assert setup_file_log(tmp_path / "data") == path
        handlers = [h for h in logging.getLogger("screenmind").handlers
                    if isinstance(h, RotatingFileHandler)]
        assert len(handlers) == 1
        assert handlers[0].maxBytes == LOG_MAX_BYTES <= 2 * 1024 * 1024
        logging.getLogger("screenmind.test").info("hello log file")
        handlers[0].flush()
        assert "hello log file" in path.read_text(encoding="utf-8")
    finally:
        _drop_file_handlers()


def test_file_log_env_var_moves_the_file(tmp_path, monkeypatch):
    from screenmind.config import setup_file_log
    monkeypatch.setenv("SCREENMIND_LOG_FILE", str(tmp_path / "elsewhere.log"))
    try:
        assert setup_file_log(tmp_path / "data") == tmp_path / "elsewhere.log"
        assert (tmp_path / "elsewhere.log").exists()
    finally:
        _drop_file_handlers()


def test_importing_config_writes_no_log_file():
    """Tests and scripts import screenmind; only an app start opens the file."""
    import logging
    from logging.handlers import RotatingFileHandler
    assert not [h for h in logging.getLogger("screenmind").handlers
                if isinstance(h, RotatingFileHandler)]
