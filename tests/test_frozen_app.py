"""The PyInstaller app (sys.frozen) must not assume a Python checkout.

Fixes F1 and F4 in docs/plans/packaging.md (F2 and F3 went away with
the model download and llama-server). Each test fakes the app with
sys.frozen = True and sys.executable = the app binary, and checks that
running from source (not frozen) keeps its old behavior.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from evolve_capture import config, startup

APP_EXE = r"C:\Program Files\Evolve Capture\EvolveCapture.exe" if sys.platform == "win32" \
    else "/Applications/EvolveCapture.app/Contents/MacOS/EvolveCapture"


@pytest.fixture
def frozen(monkeypatch):
    """Pretend to run inside the app."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", APP_EXE)


@pytest.fixture
def home(monkeypatch, tmp_path):
    """A fake home folder (Path.home())."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("HOME", str(h))
    return h


def test_is_frozen(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert config.is_frozen() is False
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert config.is_frozen() is True


# ── F1: .env ────────────────────────────────────────────────────────────


class TestEnvFile:
    def test_source_reads_the_checkout_env(self):
        assert config._env_file() == Path(config.__file__).resolve().parents[1] / ".env"

    def test_app_reads_the_data_dir_env_not_the_cwd(self, frozen, home, monkeypatch, tmp_path):
        monkeypatch.delenv("DATA_DIR", raising=False)
        monkeypatch.delenv("data_dir", raising=False)
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        (cwd / ".env").write_text("CAPTURE_INTERVAL=99\n")
        monkeypatch.chdir(cwd)
        assert config._env_file() == home / ".evolve-capture" / ".env"

    def test_app_env_follows_data_dir(self, frozen, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
        assert config._env_file() == tmp_path / "data" / ".env"

    def test_app_env_is_optional_and_read(self, frozen, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.delenv("CAPTURE_INTERVAL", raising=False)
        assert config.Settings(_env_file=config._env_file()).capture_interval == 10
        (tmp_path / ".env").write_text("CAPTURE_INTERVAL=30\n")
        assert config.Settings(_env_file=config._env_file()).capture_interval == 30


# ── F4: start at login, background start, launcher, shortcut ────────────


class TestStartCommand:
    def test_app_start_command_is_the_exe_alone(self, frozen):
        assert startup._get_startup_command() == f'"{APP_EXE}"'

    def test_app_run_key_matches_the_installer(self, frozen, monkeypatch):
        """The installer writes Run\\Evolve Capture = "{app}\\EvolveCapture.exe"; so must the app."""
        monkeypatch.setattr(sys, "executable", r"C:\Users\me\AppData\Local\Programs\Evolve Capture\EvolveCapture.exe")
        mock_winreg = MagicMock()
        mock_winreg.REG_SZ = 1
        with patch.dict("sys.modules", {"winreg": mock_winreg}):
            assert startup._install_windows() is True
        key, name, _, kind, value = mock_winreg.SetValueEx.call_args[0]
        assert name == "Evolve Capture"
        assert mock_winreg.OpenKey.call_args[0][1] == r"Software\Microsoft\Windows\CurrentVersion\Run"
        assert value == r'"C:\Users\me\AppData\Local\Programs\Evolve Capture\EvolveCapture.exe"'

    def test_app_launch_agent_runs_the_app_binary(self, frozen, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "executable", "/Applications/Screen Mind.app/Contents/MacOS/EvolveCapture")
        monkeypatch.setattr(startup, "_macos_plist_dir", lambda: tmp_path)
        monkeypatch.setattr(startup, "_macos_plist_file", lambda: tmp_path / "com.nebius.evolve.work.capture.plist")
        assert startup._install_macos() is True
        plist = (tmp_path / "com.nebius.evolve.work.capture.plist").read_text()
        assert ("<array>\n        <string>/Applications/Screen Mind.app/Contents/MacOS/EvolveCapture</string>\n"
                "\n    </array>") in plist
        assert "launchd.log" in plist


class TestNoCheckoutFiles:
    def test_app_writes_no_shortcut_or_vbs(self, frozen, home, monkeypatch, tmp_path):
        from evolve_capture import main
        data = tmp_path / "data"
        monkeypatch.setattr(main.settings, "data_dir", str(data))
        (home / "Desktop").mkdir()
        with patch("subprocess.run") as run, patch("subprocess.Popen") as popen:
            main._install_desktop_shortcut()
        run.assert_not_called()
        popen.assert_not_called()
        assert not (data / "launcher.vbs").exists()
        assert list((home / "Desktop").iterdir()) == []

    def test_app_background_starts_the_exe(self, frozen, monkeypatch):
        from evolve_capture import main
        monkeypatch.setattr(sys, "argv", [APP_EXE, "--background"])
        with patch("subprocess.Popen") as popen:
            main.run()
        assert popen.call_args[0][0] == [APP_EXE]

    def test_source_background_still_uses_python(self, monkeypatch):
        from evolve_capture import main
        monkeypatch.setattr(sys, "argv", ["evolve_capture", "--background"])
        with patch("subprocess.Popen") as popen:
            main.run()
        cmd = popen.call_args[0][0]
        assert cmd[1:] == ["-m", "evolve_capture"]
        assert "python" in Path(cmd[0]).name.lower()

    def test_app_launcher_starts_the_exe(self, frozen):
        from evolve_capture import launcher
        with patch("subprocess.Popen") as popen:
            launcher.start_evolve_capture()
        assert popen.call_args[0][0] == [APP_EXE]

    def test_source_launcher_still_uses_python(self):
        from evolve_capture import launcher
        with patch("subprocess.Popen") as popen:
            launcher.start_evolve_capture()
        assert popen.call_args[0][0][1:] == ["-m", "evolve_capture"]
