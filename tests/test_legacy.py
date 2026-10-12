"""Moving ScreenMind's data, key and start-at-login entry (the old name, before 0.3.0)."""

import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

from evolve_capture import legacy
from evolve_capture.config import Settings
from evolve_capture.storage.database import Database
from evolve_capture.storage.models import ScreenshotEntry


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake home dir, and no DATA_DIR from the environment."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.delenv("data_dir", raising=False)
    return tmp_path


def _settings() -> Settings:
    return Settings(_env_file=None)


def _make_old(home: Path) -> Path:
    """~/.screenmind as a v13 install leaves it: DB, WAL, log, settings, screenshots, models."""
    old = home / ".screenmind"
    shots = old / "screenshots" / "2026-10-08"
    shots.mkdir(parents=True)
    (old / "models" / "ocr").mkdir(parents=True)
    (old / "models" / "ocr" / "det.onnx").write_bytes(b"model")
    (old / "settings.json").write_text('{"capture_interval": 30}')
    (old / "screenmind.log").write_text("old log\n")
    (old / ".encryption_key").write_bytes(b"file-key")
    db = Database(db_path=old / "screenmind.db")
    for name in ("a.jpg", "b.jpg"):
        (shots / name).write_bytes(b"jpg")
        db.insert_activity(ScreenshotEntry(timestamp=datetime(2026, 10, 8, 12, 0),
                                           screenshot_path=str((shots / name).resolve())))
    # A path outside the data dir must stay as it is
    db.insert_activity(ScreenshotEntry(timestamp=datetime(2026, 10, 8, 12, 1),
                                       screenshot_path="/elsewhere/c.jpg"))
    db.close()
    return old


def _paths(db: Path):
    conn = sqlite3.connect(str(db))
    try:
        return [r[0] for r in conn.execute("SELECT screenshot_path FROM activities ORDER BY id")]
    finally:
        conn.close()


def _move(settings, running=False):
    lines = legacy.migrate_data_dir(settings, lambda: running)
    return lines + legacy.finish_pending(settings.data_path)


class TestDataDir:
    def test_moves_everything(self, home):
        old = _make_old(home)
        s = _settings()
        lines = _move(s)

        new = home / ".evolve-capture"
        assert s.data_path == new.resolve()
        assert (new / "evolve-capture.db").exists()
        assert not (new / "screenmind.db").exists()
        assert (new / "evolve-capture.log").read_text() == "old log\n"
        assert (new / "models" / "ocr" / "det.onnx").exists()
        assert (new / ".encryption_key").read_bytes() == b"file-key"
        assert not (new / legacy.MARKER).exists()
        assert s.capture_interval == 30  # settings.json read again from the new dir
        assert any("Moved" in line for line in lines)

        paths = _paths(new / "evolve-capture.db")
        prefix = str(new.resolve()) + os.sep
        assert paths[0].startswith(prefix) and paths[1].startswith(prefix)
        assert all(Path(p).exists() for p in paths[:2])
        assert paths[2] == "/elsewhere/c.jpg"

        if sys.platform != "win32":
            assert old.is_symlink() and old.resolve() == new.resolve()
        assert legacy.shared_home() == new

    def test_db_still_works_after_the_rewrite(self, home):
        _make_old(home)
        s = _settings()
        _move(s)
        db = Database(db_path=s.data_path / "evolve-capture.db")
        conn = db._get_conn()
        trigger = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='activities_fts_au'").fetchone()
        assert trigger, "the FTS update trigger must be back"
        conn.execute("UPDATE activities SET ocr_text = 'hello world' WHERE id = 1")
        conn.commit()
        hits = conn.execute("SELECT rowid FROM activities_fts WHERE activities_fts MATCH 'hello'").fetchall()
        assert [h[0] for h in hits] == [1]
        db.close()

    def test_second_start_does_nothing(self, home):
        _make_old(home)
        _move(_settings())
        assert _move(_settings()) == []

    def test_both_exist_uses_new_and_keeps_old(self, home):
        old = _make_old(home)
        new = home / ".evolve-capture"
        new.mkdir()
        (new / "evolve-capture.db").write_bytes(b"")
        s = _settings()
        lines = _move(s)
        assert any("Both" in line for line in lines)
        assert old.is_dir() and not old.is_symlink()
        assert (old / "screenmind.db").exists()
        assert s.data_path == new.resolve()
        assert legacy.shared_home() == new

    def test_empty_new_dir_does_not_block_the_move(self, home):
        _make_old(home)
        (home / ".evolve-capture").mkdir()
        s = _settings()
        _move(s)
        assert (s.data_path / "evolve-capture.db").exists()

    def test_data_dir_set_means_no_move(self, home, monkeypatch):
        old = _make_old(home)
        monkeypatch.setenv("DATA_DIR", str(home / "custom"))
        s = _settings()
        assert _move(s) == []
        assert old.is_dir() and not old.is_symlink()
        assert not (home / ".evolve-capture").exists()

    def test_running_instance_means_no_move(self, home):
        old = _make_old(home)
        lines = _move(_settings(), running=True)
        assert any("another instance" in line for line in lines)
        assert (old / "screenmind.db").exists()
        assert not (old / legacy.MARKER).exists()
        assert not (home / ".evolve-capture").exists()

    def test_failed_rename_keeps_using_the_old_dir(self, home, monkeypatch):
        old = _make_old(home)

        def fail(src, dst):
            raise OSError("cross-device link")

        monkeypatch.setattr(legacy.os, "rename", fail)
        s = _settings()
        lines = legacy.migrate_data_dir(s, lambda: False)
        assert any("Could not move" in line for line in lines)
        assert s.data_path == old.resolve()
        assert s.capture_interval == 30
        assert not (old / legacy.MARKER).exists()
        assert (old / "screenmind.db").exists()

    def test_crash_after_the_rename_is_finished_on_the_next_start(self, home, monkeypatch):
        """The dir moved, then the app died before the DB step: the next start finishes it."""
        _make_old(home)
        s = _settings()
        legacy.migrate_data_dir(s, lambda: False)
        new = s.data_path
        assert (new / legacy.MARKER).exists()
        assert (new / "screenmind.db").exists()  # not renamed yet

        lines = legacy.finish_pending(_settings().data_path)
        assert any("Rewrote 2" in line for line in lines)
        assert (new / "evolve-capture.db").exists()
        assert not (new / legacy.MARKER).exists()

    def test_no_old_dir(self, home):
        s = _settings()
        assert _move(s) == []
        assert not (home / ".evolve-capture").exists()


class TestSharedHome:
    def test_old_dir_until_it_moved(self, home):
        (home / ".screenmind").mkdir()
        assert legacy.shared_home() == home / ".screenmind"

    def test_new_dir_without_old(self, home):
        assert legacy.shared_home() == home / ".evolve-capture"


class FakeKeyring:
    def __init__(self, entries=None, fail_set=False, bad_readback=False):
        self.entries = dict(entries or {})
        self.fail_set = fail_set
        self.bad_readback = bad_readback

    def get_password(self, service, user):
        value = self.entries.get((service, user))
        if self.bad_readback and service == "evolve-capture" and value:
            return value + "x"
        return value

    def set_password(self, service, user, value):
        if self.fail_set:
            raise RuntimeError("keychain locked")
        self.entries[(service, user)] = value

    def delete_password(self, service, user):
        del self.entries[(service, user)]


OLD = ("screenmind", "encryption_key")
NEW = ("evolve-capture", "encryption_key")


@pytest.fixture
def key_env(tmp_path, monkeypatch):
    """encryption.py with a fake keyring and an empty data dir."""
    from evolve_capture.privacy import encryption

    monkeypatch.setattr(encryption.settings, "data_dir", str(tmp_path))

    def use(fake):
        monkeypatch.setattr(encryption, "_get_keyring", lambda: fake)
        return encryption._get_or_create_key

    return use


class TestKeyring:
    def test_old_key_is_copied_and_old_entry_kept(self, key_env):
        fake = FakeKeyring({OLD: "old-key"})
        assert key_env(fake)() == b"old-key"
        assert fake.entries[NEW] == "old-key"
        assert fake.entries[OLD] == "old-key"

    def test_old_entry_deleted_on_a_later_start(self, key_env):
        fake = FakeKeyring({OLD: "old-key"})
        get_key = key_env(fake)
        get_key()
        assert get_key() == b"old-key"
        assert OLD not in fake.entries
        assert fake.entries[NEW] == "old-key"

    def test_different_keys_are_both_kept(self, key_env):
        fake = FakeKeyring({OLD: "old-key", NEW: "new-key"})
        assert key_env(fake)() == b"new-key"
        assert fake.entries[OLD] == "old-key"

    def test_failed_copy_uses_old_key_and_never_generates(self, key_env, tmp_path):
        fake = FakeKeyring({OLD: "old-key"}, fail_set=True)
        assert key_env(fake)() == b"old-key"
        assert fake.entries == {OLD: "old-key"}
        assert not (tmp_path / ".encryption_key").exists()

    def test_bad_readback_uses_old_key(self, key_env):
        fake = FakeKeyring({OLD: "old-key"}, bad_readback=True)
        assert key_env(fake)() == b"old-key"
        assert fake.entries[OLD] == "old-key"

    def test_no_key_anywhere_generates_one_under_the_new_service(self, key_env, tmp_path):
        fake = FakeKeyring()
        key = key_env(fake)()
        assert fake.entries[NEW] == key.decode()
        assert OLD not in fake.entries
        assert (tmp_path / ".encryption_key").read_bytes() == key


class TestStartupEntry:
    @pytest.mark.skipif(sys.platform == "win32", reason="LaunchAgent and XDG files")
    def test_old_entry_is_replaced(self, home, monkeypatch):
        from evolve_capture import startup

        monkeypatch.setattr(startup, "_get_startup_command",
                            lambda: '"/usr/local/bin/evolve-capture" --background')
        if sys.platform == "darwin":
            old, new = startup._old_macos_plist_file(), startup._macos_plist_file()
        else:
            old, new = startup._old_linux_desktop_file(), startup._linux_desktop_file()
        old.parent.mkdir(parents=True)
        old.write_text("old entry")

        line = startup.migrate_old_entry()
        assert "Replaced" in line
        assert not old.exists()
        assert "evolve-capture" in new.read_text()
        assert startup.migrate_old_entry() is None

    @pytest.mark.skipif(sys.platform == "win32", reason="LaunchAgent and XDG files")
    def test_no_old_entry_writes_nothing(self, home):
        from evolve_capture import startup

        assert startup.migrate_old_entry() is None
        assert not startup.is_startup_installed()
