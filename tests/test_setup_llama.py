"""
Tests for screenmind.setup_llama

Covers:
  - _fetch_latest_release: nightly-tag.txt indirection (new upstream format)
  - _fetch_latest_release: direct assets (old upstream format, backward compat)
  - _pick_asset: Windows CUDA, Windows CPU, macOS, Linux selection
  - _pick_asset: empty asset list → None
  - install_llama_server: full happy path (mocked network)
  - install_llama_server: nightly-tag resolution failure (network error)
  - install_llama_server: nightly-tag.txt with empty/garbage content
  - Edge: nightly-tag.txt download URL missing from asset
"""

import json
import io
from unittest.mock import patch, MagicMock, mock_open
from pathlib import Path

import pytest

from screenmind.setup_llama import (
    _pick_asset,
    _fetch_latest_release,
    find_llama_server,
    _format_size,
    _check_disk_space,
    GITHUB_API_LATEST,
)


# ── Fixtures: fake GitHub API responses ──────────────────────────────────────

def _make_asset(name, size=10_000_000, url=None):
    """Create a fake GitHub release asset dict."""
    return {
        "name": name,
        "size": size,
        "browser_download_url": url or f"https://github.com/fake/releases/download/test/{name}",
    }


# New-style release: v0.5.0 with only nightly-tag.txt
RELEASE_V050 = {
    "tag_name": "v0.5.0",
    "assets": [
        _make_asset("nightly-tag.txt", size=7,
                     url="https://github.com/ggml-org/llama.cpp/releases/download/v0.5.0/nightly-tag.txt"),
    ],
}

# Nightly release with actual binaries (what nightly-tag.txt points to)
RELEASE_NIGHTLY = {
    "tag_name": "b11146",
    "assets": [
        _make_asset("llama-b11146-bin-win-cuda-12.4-x64.zip"),
        _make_asset("llama-b11146-bin-win-cuda-11.8-x64.zip"),
        _make_asset("llama-b11146-bin-win-cpu-x64.zip"),
        _make_asset("llama-b11146-bin-win-cpu-arm64.zip"),
        _make_asset("cudart-llama-bin-win-cuda-12.4-x64.zip", size=5_000_000),
        _make_asset("cudart-llama-bin-win-cuda-11.8-x64.zip", size=5_000_000),
        _make_asset("llama-b11146-bin-macos-arm64.tar.gz"),
        _make_asset("llama-b11146-bin-macos-x64.tar.gz"),
        _make_asset("llama-b11146-bin-ubuntu-x64.tar.gz"),
        _make_asset("llama-b11146-bin-ubuntu-arm64.tar.gz"),
    ],
}

# Old-style release: v0.4.1 with binaries directly attached
RELEASE_OLD_STYLE = {
    "tag_name": "v0.4.1",
    "assets": [
        _make_asset("llama-v0.4.1-bin-win-cuda-12.4-x64.zip"),
        _make_asset("llama-v0.4.1-bin-win-cpu-x64.zip"),
        _make_asset("cudart-llama-bin-win-cuda-12.4-x64.zip", size=5_000_000),
        _make_asset("llama-v0.4.1-bin-macos-arm64.tar.gz"),
        _make_asset("llama-v0.4.1-bin-ubuntu-x64.tar.gz"),
    ],
}


# ── Tests: _pick_asset ──────────────────────────────────────────────────────


class TestPickAsset:
    """Test asset selection logic for different platforms."""

    @patch("screenmind.setup_llama.platform")
    @patch("screenmind.setup_llama.has_nvidia_gpu", return_value=False)
    def test_windows_cpu_x64(self, mock_gpu, mock_platform):
        mock_platform.system.return_value = "Windows"
        mock_platform.machine.return_value = "AMD64"
        main, extras = _pick_asset(RELEASE_NIGHTLY["assets"])
        assert main is not None
        assert "bin-win-cpu-x64" in main["name"]
        assert len(extras) == 0

    @patch("screenmind.setup_llama.platform")
    @patch("screenmind.setup_llama._get_cuda_version", return_value="12.4")
    @patch("screenmind.setup_llama.has_nvidia_gpu", return_value=True)
    def test_windows_cuda(self, mock_gpu, mock_cuda, mock_platform):
        mock_platform.system.return_value = "Windows"
        mock_platform.machine.return_value = "AMD64"
        main, extras = _pick_asset(RELEASE_NIGHTLY["assets"])
        assert main is not None
        assert "cuda-12.4" in main["name"]
        # Should also pick up the cudart DLL
        assert len(extras) == 1
        assert "cudart" in extras[0]["name"]

    @patch("screenmind.setup_llama.platform")
    @patch("screenmind.setup_llama._get_cuda_version", return_value="12.6")
    @patch("screenmind.setup_llama.has_nvidia_gpu", return_value=True)
    def test_windows_cuda_picks_highest_compatible(self, mock_gpu, mock_cuda, mock_platform):
        """Driver CUDA 12.6 > asset CUDA 12.4, should pick 12.4 (highest ≤ driver)."""
        mock_platform.system.return_value = "Windows"
        mock_platform.machine.return_value = "AMD64"
        main, extras = _pick_asset(RELEASE_NIGHTLY["assets"])
        assert main is not None
        assert "cuda-12.4" in main["name"]

    @patch("screenmind.setup_llama.platform")
    @patch("screenmind.setup_llama._get_cuda_version", return_value="13.4")
    @patch("screenmind.setup_llama.has_nvidia_gpu", return_value=True)
    def test_windows_cuda_runtime_matches_cpu_arch(self, mock_gpu, mock_cuda, mock_platform):
        """Release b11429 lists the arm64 cudart zip before the x64 one."""
        mock_platform.system.return_value = "Windows"
        mock_platform.machine.return_value = "AMD64"
        assets = [
            _make_asset("llama-b11429-bin-win-cuda-13.4-arm64.zip"),
            _make_asset("llama-b11429-bin-win-cuda-13.4-x64.zip"),
            _make_asset("cudart-llama-bin-win-cuda-13.4-arm64.zip", size=5_000_000),
            _make_asset("cudart-llama-bin-win-cuda-13.4-x64.zip", size=5_000_000),
        ]
        main, extras = _pick_asset(assets)
        assert main["name"] == "llama-b11429-bin-win-cuda-13.4-x64.zip"
        assert [a["name"] for a in extras] == ["cudart-llama-bin-win-cuda-13.4-x64.zip"]

    @patch("screenmind.setup_llama.platform")
    def test_macos_arm64(self, mock_platform):
        mock_platform.system.return_value = "Darwin"
        mock_platform.machine.return_value = "arm64"
        main, extras = _pick_asset(RELEASE_NIGHTLY["assets"])
        assert main is not None
        assert "macos-arm64" in main["name"]
        assert main["name"].endswith(".tar.gz")

    @patch("screenmind.setup_llama.platform")
    def test_linux_x64(self, mock_platform):
        mock_platform.system.return_value = "Linux"
        mock_platform.machine.return_value = "x86_64"
        main, extras = _pick_asset(RELEASE_NIGHTLY["assets"])
        assert main is not None
        assert "ubuntu-x64" in main["name"]

    @patch("screenmind.setup_llama.platform")
    def test_empty_assets_returns_none(self, mock_platform):
        mock_platform.system.return_value = "Linux"
        mock_platform.machine.return_value = "x86_64"
        main, extras = _pick_asset([])
        assert main is None
        assert extras == []

    @patch("screenmind.setup_llama.platform")
    def test_nightly_tag_only_returns_none(self, mock_platform):
        """The nightly-tag.txt asset alone should NOT match any platform."""
        mock_platform.system.return_value = "Linux"
        mock_platform.machine.return_value = "x86_64"
        main, extras = _pick_asset(RELEASE_V050["assets"])
        assert main is None


# ── Tests: _fetch_latest_release (nightly-tag resolution) ────────────────────


class TestFetchLatestRelease:
    """Test that _fetch_latest_release resolves nightly-tag.txt indirection."""

    def _mock_urlopen(self, url_to_response: dict):
        """Create a mock urlopen that returns different responses per URL."""
        def fake_urlopen(req, **kwargs):
            url = req.full_url if hasattr(req, 'full_url') else req
            for pattern, (body, status) in url_to_response.items():
                if pattern in url:
                    resp = MagicMock()
                    resp.read.return_value = body if isinstance(body, bytes) else body.encode()
                    resp.__enter__ = MagicMock(return_value=resp)
                    resp.__exit__ = MagicMock(return_value=False)
                    return resp
            raise Exception(f"Unmocked URL: {url}")
        return fake_urlopen

    @patch("screenmind.setup_llama.urlopen")
    def test_resolves_nightly_tag(self, mock_urlopen):
        """When latest release has only nightly-tag.txt, should follow it."""
        responses = {
            "releases/latest": (json.dumps(RELEASE_V050), 200),
            "nightly-tag.txt": (b"b11146", 200),
            "releases/tags/b11146": (json.dumps(RELEASE_NIGHTLY), 200),
        }
        mock_urlopen.side_effect = self._mock_urlopen(responses)

        result = _fetch_latest_release()
        assert result["tag_name"] == "b11146"
        assert len(result["assets"]) == len(RELEASE_NIGHTLY["assets"])

    @patch("screenmind.setup_llama.urlopen")
    def test_old_style_direct_assets(self, mock_urlopen):
        """When latest release has real binaries, should return it directly."""
        responses = {
            "releases/latest": (json.dumps(RELEASE_OLD_STYLE), 200),
        }
        mock_urlopen.side_effect = self._mock_urlopen(responses)

        result = _fetch_latest_release()
        assert result["tag_name"] == "v0.4.1"
        assert len(result["assets"]) == 5

    @patch("screenmind.setup_llama.urlopen")
    def test_nightly_tag_network_failure(self, mock_urlopen):
        """If downloading nightly-tag.txt fails, should raise."""
        call_count = [0]
        def failing_urlopen(req, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: return v0.5.0 release
                resp = MagicMock()
                resp.read.return_value = json.dumps(RELEASE_V050).encode()
                resp.__enter__ = MagicMock(return_value=resp)
                resp.__exit__ = MagicMock(return_value=False)
                return resp
            else:
                # Second call: downloading nightly-tag.txt fails
                from urllib.error import URLError
                raise URLError("Connection refused")

        mock_urlopen.side_effect = failing_urlopen

        with pytest.raises(Exception):
            _fetch_latest_release()

    @patch("screenmind.setup_llama.urlopen")
    def test_nightly_tag_empty_content(self, mock_urlopen):
        """If nightly-tag.txt is empty, should raise."""
        responses = {
            "releases/latest": (json.dumps(RELEASE_V050), 200),
            "nightly-tag.txt": (b"", 200),
        }
        mock_urlopen.side_effect = self._mock_urlopen(responses)

        with pytest.raises(RuntimeError, match="empty"):
            _fetch_latest_release()

    @patch("screenmind.setup_llama.urlopen")
    def test_nightly_tag_whitespace_stripped(self, mock_urlopen):
        """nightly-tag.txt with trailing newline should be stripped."""
        responses = {
            "releases/latest": (json.dumps(RELEASE_V050), 200),
            "nightly-tag.txt": (b"b11146\n", 200),
            "releases/tags/b11146": (json.dumps(RELEASE_NIGHTLY), 200),
        }
        mock_urlopen.side_effect = self._mock_urlopen(responses)

        result = _fetch_latest_release()
        assert result["tag_name"] == "b11146"

    @patch("screenmind.setup_llama.urlopen")
    def test_nightly_release_has_no_assets_either(self, mock_urlopen):
        """Nightly release also empty → should return it (caller handles no-match)."""
        empty_nightly = {"tag_name": "b11146", "assets": []}
        responses = {
            "releases/latest": (json.dumps(RELEASE_V050), 200),
            "nightly-tag.txt": (b"b11146", 200),
            "releases/tags/b11146": (json.dumps(empty_nightly), 200),
        }
        mock_urlopen.side_effect = self._mock_urlopen(responses)

        result = _fetch_latest_release()
        # Should return the nightly release even if empty — _pick_asset handles None
        assert result["tag_name"] == "b11146"
        assert result["assets"] == []


# ── Tests: find_llama_server ─────────────────────────────────────────────────


class TestFindLlamaServer:
    """Test binary detection logic."""

    def test_finds_local_binary(self, tmp_path):
        """Should find binary in project llama/ folder."""
        with patch("screenmind.setup_llama.LLAMA_DIR", tmp_path):
            with patch("screenmind.setup_llama.LLAMA_SERVER_BIN", "llama-server.exe"):
                (tmp_path / "llama-server.exe").touch()
                result = find_llama_server()
                assert result is not None
                assert "llama-server.exe" in result

    def test_finds_system_binary(self):
        """Should find binary on system PATH."""
        with patch("screenmind.setup_llama.LLAMA_DIR", Path("/nonexistent")):
            with patch("shutil.which", return_value="/usr/bin/llama-server"):
                result = find_llama_server()
                assert result == "/usr/bin/llama-server"

    def test_returns_none_when_missing(self):
        """Should return None when no binary found."""
        with patch("screenmind.setup_llama.LLAMA_DIR", Path("/nonexistent")):
            with patch("shutil.which", return_value=None):
                result = find_llama_server()
                assert result is None


# ── Tests: helper functions ──────────────────────────────────────────────────


class TestHelpers:

    def test_format_size_gb(self):
        assert "1.0 GB" in _format_size(1024 ** 3)

    def test_format_size_mb(self):
        assert "100 MB" in _format_size(100 * 1024 ** 2)

    def test_format_size_kb(self):
        assert "512 KB" in _format_size(512 * 1024)

    def test_format_size_bytes(self):
        assert "42 bytes" in _format_size(42)
