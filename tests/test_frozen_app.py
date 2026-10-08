"""The PyInstaller app (sys.frozen) must not assume a Python checkout.

Fixes F1-F4 in docs/plans/packaging.md. Each test fakes the app with
sys.frozen = True and sys.executable = the app binary, and checks that
running from source (not frozen) keeps its old behavior.
"""

import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from screenmind import config, setup_llama, startup
from screenmind.engine import model_manager

APP_EXE = r"C:\Program Files\ScreenMind\ScreenMind.exe" if sys.platform == "win32" \
    else "/Applications/ScreenMind.app/Contents/MacOS/ScreenMind"


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
        assert config._env_file() == home / ".screenmind" / ".env"

    def test_app_env_follows_data_dir(self, frozen, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
        assert config._env_file() == tmp_path / "data" / ".env"

    def test_app_env_is_optional_and_read(self, frozen, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.delenv("CAPTURE_INTERVAL", raising=False)
        assert config.Settings(_env_file=config._env_file()).capture_interval == 10
        (tmp_path / ".env").write_text("CAPTURE_INTERVAL=30\n")
        assert config.Settings(_env_file=config._env_file()).capture_interval == 30


# ── F2: model download ──────────────────────────────────────────────────


class TestDownload:
    def test_source_downloads_in_a_child_python(self, tmp_path):
        with patch("subprocess.Popen") as popen:
            job = model_manager._start_file_download("org/repo", "f.gguf", tmp_path)
        assert isinstance(job, model_manager._ProcessDownload)
        cmd = popen.call_args[0][0]
        assert cmd[:2] == [sys.executable, "-c"]
        assert cmd[3:] == ["org/repo", "f.gguf", str(tmp_path)]
        job.close()

    def test_app_downloads_in_a_thread_not_a_second_app(self, frozen, monkeypatch, tmp_path):
        def fake_download(repo_id, filename, local_dir, tqdm_class=None):
            (Path(local_dir) / filename).write_bytes(b"x" * 10)
            return str(Path(local_dir) / filename)
        monkeypatch.setattr("huggingface_hub.hf_hub_download", fake_download)
        with patch("subprocess.Popen") as popen:
            job = model_manager._start_file_download("org/repo", "f.gguf", tmp_path)
            assert job.wait(5) == 0
        popen.assert_not_called()
        assert isinstance(job, model_manager._ThreadDownload)
        assert (tmp_path / "f.gguf").read_bytes() == b"x" * 10

    def test_app_download_error_is_reported(self, frozen, monkeypatch, tmp_path):
        def fake_download(**kw):
            raise OSError("no network")
        monkeypatch.setattr("huggingface_hub.hf_hub_download", fake_download)
        job = model_manager._start_file_download("org/repo", "f.gguf", tmp_path)
        assert job.wait(5) == 1
        assert "no network" in job.error()

    def test_app_download_stops_on_cancel(self, frozen, monkeypatch, tmp_path):
        started = threading.Event()

        def fake_download(repo_id, filename, local_dir, tqdm_class=None):
            bar = tqdm_class(total=None, disable=True)
            started.set()
            for _ in range(500):  # 10 s without a cancel
                bar.update(1)
                time.sleep(0.02)
        monkeypatch.setattr("huggingface_hub.hf_hub_download", fake_download)
        job = model_manager._start_file_download("org/repo", "f.gguf", tmp_path)
        assert started.wait(5)
        assert job.poll() is None
        t0 = time.monotonic()
        job.kill()
        assert time.monotonic() - t0 < 5
        assert job.poll() == 1
        assert "_DownloadCancelled" in job.error()

    def test_do_download_in_the_app(self, frozen, monkeypatch, tmp_path):
        """The whole download (GGUF, then mmproj) without a child process."""
        info = model_manager.get_model_info("gemma-4-e2b")
        variant = info["variants"][0]
        calls = []

        def fake_download(repo_id, filename, local_dir, tqdm_class=None):
            calls.append(filename)
            Path(local_dir).mkdir(parents=True, exist_ok=True)
            (Path(local_dir) / filename).write_bytes(b"x")
        monkeypatch.setattr("huggingface_hub.hf_hub_download", fake_download)
        monkeypatch.setattr(model_manager, "_models_dir", lambda: tmp_path)
        monkeypatch.setattr(model_manager.time, "sleep", lambda s: None)
        with patch("subprocess.Popen") as popen:
            ok = model_manager._do_download("gemma-4-e2b", variant["hf_file"], variant["quant"])
        popen.assert_not_called()
        assert ok is True
        assert calls == [variant["hf_file"], info["mmproj_file"]]
        assert (tmp_path / "gemma-4-e2b" / variant["quant"] / variant["hf_file"]).exists()
        assert (tmp_path / "gemma-4-e2b" / info["mmproj_file"]).exists()
        model_manager._set_download_state(active=False, status="idle", model=None, message="")


# ── F3: llama-server location ───────────────────────────────────────────


class TestLlamaLocation:
    def test_source_uses_the_checkout(self):
        root = setup_llama._project_root()
        assert root in (Path(setup_llama.__file__).parent.parent, Path.home() / ".screenmind")
        assert setup_llama.llama_search_dirs() == [setup_llama.LLAMA_DIR]

    def test_app_installs_to_the_home_folder(self, frozen, home):
        assert setup_llama._project_root() == home / ".screenmind"

    def test_app_search_order(self, frozen, home, monkeypatch, tmp_path):
        exe_dir = tmp_path / "app"
        exe_dir.mkdir()
        monkeypatch.setattr(sys, "executable", str(exe_dir / "ScreenMind.exe"))
        user_dir = home / ".screenmind" / "llama"
        monkeypatch.setattr(setup_llama, "LLAMA_DIR", user_dir)
        assert setup_llama.llama_search_dirs() == [exe_dir.resolve() / "llama", user_dir]

        bin_name = setup_llama.LLAMA_SERVER_BIN
        with patch("shutil.which", return_value="/usr/bin/llama-server"):
            assert setup_llama.find_llama_server() == "/usr/bin/llama-server"
            user_dir.mkdir(parents=True)
            (user_dir / bin_name).touch()
            assert setup_llama.find_llama_server() == str(user_dir / bin_name)
            (exe_dir / "llama").mkdir()
            (exe_dir / "llama" / bin_name).touch()
            assert setup_llama.find_llama_server() == str(exe_dir.resolve() / "llama" / bin_name)

    def test_start_server_uses_the_found_binary(self, monkeypatch, tmp_path):
        info = model_manager.get_model_info("gemma-4-e2b")
        variant = info["variants"][0]
        (tmp_path / "gemma-4-e2b" / variant["quant"]).mkdir(parents=True)
        (tmp_path / "gemma-4-e2b" / variant["quant"] / variant["hf_file"]).touch()
        (tmp_path / "gemma-4-e2b" / info["mmproj_file"]).touch()
        monkeypatch.setattr(model_manager, "_models_dir", lambda: tmp_path)
        monkeypatch.setattr(model_manager.settings, "llama_server_shared", False)
        monkeypatch.setattr(model_manager, "_server_process", None)
        monkeypatch.setattr(model_manager.time, "sleep", lambda s: None)
        monkeypatch.setattr(model_manager, "_llama_supports", lambda binary, flag: False)

        def started_binary():
            with patch("subprocess.Popen") as popen:
                popen.return_value.poll.return_value = 1
                model_manager.start_server("gemma-4-e2b", timeout=1, hf_file=variant["hf_file"])
            return popen.call_args[0][0][0]

        monkeypatch.setattr(setup_llama, "find_llama_server", lambda: "/app/llama/llama-server")
        assert started_binary() == "/app/llama/llama-server"
        monkeypatch.setattr(setup_llama, "find_llama_server", lambda: None)
        assert started_binary() == setup_llama.LLAMA_SERVER_BIN


# ── F4: start at login, background start, launcher, shortcut ────────────


class TestStartCommand:
    def test_app_start_command_is_the_exe_alone(self, frozen):
        assert startup._get_startup_command() == f'"{APP_EXE}"'

    def test_app_run_key_matches_the_installer(self, frozen, monkeypatch):
        """The installer writes Run\\ScreenMind = "{app}\\ScreenMind.exe"; so must the app."""
        monkeypatch.setattr(sys, "executable", r"C:\Users\me\AppData\Local\Programs\ScreenMind\ScreenMind.exe")
        mock_winreg = MagicMock()
        mock_winreg.REG_SZ = 1
        with patch.dict("sys.modules", {"winreg": mock_winreg}):
            assert startup._install_windows() is True
        key, name, _, kind, value = mock_winreg.SetValueEx.call_args[0]
        assert name == "ScreenMind"
        assert mock_winreg.OpenKey.call_args[0][1] == r"Software\Microsoft\Windows\CurrentVersion\Run"
        assert value == r'"C:\Users\me\AppData\Local\Programs\ScreenMind\ScreenMind.exe"'

    def test_app_launch_agent_runs_the_app_binary(self, frozen, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "executable", "/Applications/Screen Mind.app/Contents/MacOS/ScreenMind")
        monkeypatch.setattr(startup, "_macos_plist_dir", lambda: tmp_path)
        monkeypatch.setattr(startup, "_macos_plist_file", lambda: tmp_path / "com.screenmind.plist")
        assert startup._install_macos() is True
        plist = (tmp_path / "com.screenmind.plist").read_text()
        assert ("<array>\n        <string>/Applications/Screen Mind.app/Contents/MacOS/ScreenMind</string>\n"
                "\n    </array>") in plist
        assert "launchd.log" in plist


class TestNoCheckoutFiles:
    def test_app_writes_no_shortcut_or_vbs(self, frozen, home, monkeypatch, tmp_path):
        from screenmind import main
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
        from screenmind import main
        monkeypatch.setattr(sys, "argv", [APP_EXE, "--background"])
        with patch("subprocess.Popen") as popen:
            main.run()
        assert popen.call_args[0][0] == [APP_EXE]

    def test_source_background_still_uses_python(self, monkeypatch):
        from screenmind import main
        monkeypatch.setattr(sys, "argv", ["screenmind", "--background"])
        with patch("subprocess.Popen") as popen:
            main.run()
        cmd = popen.call_args[0][0]
        assert cmd[1:] == ["-m", "screenmind"]
        assert "python" in Path(cmd[0]).name.lower()

    def test_app_launcher_starts_the_exe(self, frozen):
        from screenmind import launcher
        with patch("subprocess.Popen") as popen:
            launcher.start_screenmind()
        assert popen.call_args[0][0] == [APP_EXE]

    def test_source_launcher_still_uses_python(self):
        from screenmind import launcher
        with patch("subprocess.Popen") as popen:
            launcher.start_screenmind()
        assert popen.call_args[0][0][1:] == ["-m", "screenmind"]
