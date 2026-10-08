"""
setup_llama.py — Auto-detect and install llama-server for ScreenMind.

Checks if llama-server is already available (project llama/ folder or system PATH).
If not found, prompts the user to install automatically.

Downloads pre-built binaries from the official llama.cpp GitHub releases:
  https://github.com/ggml-org/llama.cpp/releases

Can be run standalone:  python -m screenmind.setup_llama
Or imported:            from screenmind.setup_llama import ensure_llama_server
"""

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
import sysconfig
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

# ── Constants ────────────────────────────────────────────────────────────────

logger = logging.getLogger("screenmind.setup_llama")

GITHUB_API_LATEST = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"

def _is_frozen() -> bool:
    """True inside the PyInstaller app. Same as config.is_frozen()."""
    return bool(getattr(sys, "frozen", False))


def _project_root() -> Path:
    """Where llama/ is installed: ~/.screenmind or the checkout.

    The app (frozen) and a pip install use ~/.screenmind/. A checkout or an
    editable install uses the project root. The app must never write inside
    its own bundle: that breaks the signature and may not be writable.
    """
    if _is_frozen():
        return Path.home() / ".screenmind"
    if Path(__file__).parent.is_relative_to(Path(sysconfig.get_path("purelib"))):
        return Path.home() / ".screenmind"
    return Path(__file__).parent.parent


PROJECT_ROOT = _project_root()

# Install target, and the first place to look outside the app
LLAMA_DIR = PROJECT_ROOT / "llama"

# Binary name per platform
LLAMA_SERVER_BIN = "llama-server.exe" if sys.platform == "win32" else "llama-server"


def llama_search_dirs() -> list[Path]:
    """Folders to look in for llama-server, before PATH.

    From source: LLAMA_DIR. In the app: a binary shipped next to the
    executable (<exe dir>/llama/) first, then LLAMA_DIR (~/.screenmind/llama).
    """
    if _is_frozen():
        return [Path(sys.executable).resolve().parent / "llama", LLAMA_DIR]
    return [LLAMA_DIR]

# Minimum free disk space required (1 GB) before attempting download
MIN_DISK_SPACE_BYTES = 1024 ** 3


# ── Detection ────────────────────────────────────────────────────────────────

def find_llama_server() -> str | None:
    """
    Check if llama-server is available. Returns the path if found, None otherwise.

    Search order:
      1. llama_search_dirs(): the project's llama/ folder; in the app, the
         bundled binary, then ~/.screenmind/llama/
      2. System PATH (e.g. installed via brew, winget, or manually)
    """
    # 1. Project-local (or bundled) binary
    for folder in llama_search_dirs():
        local_bin = folder / LLAMA_SERVER_BIN
        if local_bin.exists():
            return str(local_bin)

    # 2. System PATH
    system_bin = shutil.which("llama-server")
    if system_bin:
        return system_bin

    return None


def _is_interactive() -> bool:
    """Check if stdin is attached to a TTY (interactive terminal)."""
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        return False


def has_nvidia_gpu() -> bool:
    """Detect NVIDIA GPU by running nvidia-smi."""
    try:
        result = subprocess.run(
            ["nvidia-smi"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _get_cuda_version() -> str | None:
    """Detect installed CUDA version from nvidia-smi output."""
    try:
        result = subprocess.run(
            ["nvidia-smi"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        # nvidia-smi output contains "CUDA Version: XX.Y"
        for line in result.stdout.split("\n"):
            if "CUDA Version" in line:
                parts = line.split("CUDA Version:")
                if len(parts) >= 2:
                    version = parts[1].strip().split()[0]
                    return version
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _check_disk_space(required_bytes: int) -> bool:
    """Check if there's enough free disk space for the download + extraction."""
    try:
        usage = shutil.disk_usage(PROJECT_ROOT)
        # Need roughly 2x the download size (download + extraction)
        needed = max(required_bytes * 2, MIN_DISK_SPACE_BYTES)
        if usage.free < needed:
            print(f"  [Setup] WARNING: Low disk space!")
            print(f"  [Setup]   Free:     {_format_size(usage.free)}")
            print(f"  [Setup]   Required: ~{_format_size(needed)} (download + extraction)")
            return False
        return True
    except Exception:
        return True  # If we can't check, don't block


# ── Asset Selection ──────────────────────────────────────────────────────────

def _pick_asset(assets: list) -> tuple[dict | None, list[dict]]:
    """
    Pick the right release asset from the actual asset list.
    Matches against the real asset names rather than constructing filenames.

    Returns:
        (main_asset, extra_assets) — the binary zip/tar + any CUDA runtime DLLs
    """
    system = platform.system().lower()
    machine = platform.machine().lower()
    is_arm = "arm" in machine or "aarch64" in machine

    main_asset = None
    extra_assets = []

    if system == "windows":
        arch = "arm64" if is_arm else "x64"
        nvidia = has_nvidia_gpu()
        if nvidia:
            cuda_ver = _get_cuda_version()
            cuda_major = cuda_ver.split(".")[0] if cuda_ver else None

            # Search for matching CUDA binary in actual asset list
            if cuda_ver:
                # Parse driver CUDA version as tuple for comparison
                try:
                    driver_ver = tuple(int(x) for x in cuda_ver.split("."))
                except (ValueError, TypeError):
                    driver_ver = None

                if driver_ver:
                    # Collect all CUDA binaries with their parsed versions
                    # Exclude 'cudart-' prefixed assets — those are runtime DLLs, not the binary
                    cuda_candidates = []
                    for a in assets:
                        name = a["name"]
                        if "bin-win-cuda" not in name or not name.endswith(f"-{arch}.zip"):
                            continue
                        if name.startswith("cudart"):
                            continue
                        # Extract CUDA version from asset name (e.g. "13.3" from "bin-win-cuda-13.3-x64")
                        import re
                        m = re.search(r"cuda-(\d+\.\d+)", name)
                        if m:
                            try:
                                asset_ver = tuple(int(x) for x in m.group(1).split("."))
                            except ValueError:
                                continue
                            # Only consider builds that the driver can run (asset ≤ driver)
                            if asset_ver <= driver_ver:
                                cuda_candidates.append((asset_ver, a))

                    # Pick highest compatible version
                    if cuda_candidates:
                        cuda_candidates.sort(key=lambda x: x[0], reverse=True)
                        main_asset = cuda_candidates[0][1]

            # Fallback: pick any CUDA Windows build (prefer highest version ≤ driver)
            if not main_asset:
                cuda_assets = [a for a in assets
                               if "bin-win-cuda" in a["name"] and a["name"].endswith(f"-{arch}.zip")
                               and not a["name"].startswith("cudart")]
                if cuda_assets:
                    cuda_assets.sort(key=lambda a: a["name"], reverse=True)
                    main_asset = cuda_assets[0]

            # Find matching CUDA runtime DLLs
            if main_asset:
                # Extract CUDA version string from asset name (e.g. "cuda-12.4" from "bin-win-cuda-12.4-x64")
                cuda_tag = None
                for part in main_asset["name"].split("-"):
                    if part.startswith("cuda"):
                        # Get "cuda-XX.Y" from asset name
                        idx = main_asset["name"].index("cuda-")
                        rest = main_asset["name"][idx + 5:]  # after "cuda-"
                        cuda_tag = rest.split("-")[0]  # e.g. "12.4"
                        break
                if cuda_tag:
                    for a in assets:
                        # Releases ship cudart for both x64 and arm64; match the CPU too.
                        if a["name"] == f"cudart-llama-bin-win-cuda-{cuda_tag}-{arch}.zip":
                            extra_assets.append(a)
                            break

                cuda_display = f" (CUDA {cuda_ver})" if cuda_ver else ""
                print(f"  [Setup] NVIDIA GPU detected{cuda_display} -> {main_asset['name']}")
            else:
                print(f"  [Setup] NVIDIA GPU detected but no matching CUDA build found -> falling back to CPU")
                nvidia = False  # Fall through to CPU

        if not nvidia:
            # CPU build
            for a in assets:
                if f"bin-win-cpu-{arch}" in a["name"] and a["name"].endswith(".zip"):
                    main_asset = a
                    break
            gpu_note = "No NVIDIA GPU detected -> " if not has_nvidia_gpu() else ""
            print(f"  [Setup] {gpu_note}CPU build{' (will be slower than GPU)' if not has_nvidia_gpu() else ''}")

    elif system == "darwin":
        suffix = "macos-arm64" if is_arm else "macos-x64"
        for a in assets:
            if f"bin-{suffix}" in a["name"] and a["name"].endswith(".tar.gz"):
                main_asset = a
                break
        print(f"  [Setup] macOS {'Apple Silicon' if is_arm else 'Intel'} detected")

    else:  # Linux
        suffix = "ubuntu-arm64" if is_arm else "ubuntu-x64"
        for a in assets:
            name = a["name"]
            # Match plain ubuntu-x64, not ubuntu-vulkan-x64 or ubuntu-rocm-x64
            if f"bin-{suffix}" in name and name.endswith(".tar.gz"):
                # Exclude specialized builds
                if not any(x in name for x in ["vulkan", "rocm", "sycl", "openvino"]):
                    main_asset = a
                    break
        print(f"  [Setup] Linux {'ARM64' if is_arm else 'x64'} detected")

    return main_asset, extra_assets


def _format_size(size_bytes: int) -> str:
    """Format byte count as human-readable string."""
    if size_bytes >= 1024 ** 3:
        return f"{size_bytes / (1024 ** 3):.1f} GB"
    elif size_bytes >= 1024 ** 2:
        return f"{size_bytes / (1024 ** 2):.0f} MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.0f} KB"
    return f"{size_bytes} bytes"


# ── Download & Install ───────────────────────────────────────────────────────

def _has_binary_assets(assets: list) -> bool:
    """Check if an asset list contains actual binary downloads (not just nightly-tag.txt)."""
    return any(
        a["name"].startswith("llama-") and (a["name"].endswith(".zip") or a["name"].endswith(".tar.gz"))
        for a in assets
    )


def _fetch_latest_release() -> dict:
    """Fetch latest release info from GitHub API.

    Handles the nightly-tag.txt indirection introduced in llama.cpp v0.5.0:
    stable releases now contain only a nightly-tag.txt file pointing to the
    nightly build tag (e.g. "b11146") where the actual binaries are published.

    If the latest release already has binary assets (old-style), returns it
    directly for backward compatibility.
    """
    headers = {"Accept": "application/vnd.github.v3+json"}
    req = Request(GITHUB_API_LATEST, headers=headers)
    with urlopen(req, timeout=15) as resp:
        release = json.loads(resp.read().decode())

    assets = release.get("assets", [])

    # Old-style release with binaries directly attached — return as-is
    if _has_binary_assets(assets):
        return release

    # New-style: look for nightly-tag.txt pointer
    nightly_tag_asset = next((a for a in assets if a["name"] == "nightly-tag.txt"), None)
    if not nightly_tag_asset:
        # No binaries and no pointer — return as-is (caller will handle no-match)
        return release

    # Download the nightly-tag.txt to get the build tag name
    tag_url = nightly_tag_asset["browser_download_url"]
    tag_req = Request(tag_url, headers={"Accept": "application/octet-stream"})
    with urlopen(tag_req, timeout=15) as resp:
        nightly_tag = resp.read().decode().strip()

    if not nightly_tag:
        raise RuntimeError(
            "nightly-tag.txt is empty, cannot determine nightly build tag. "
            "Manual download: https://github.com/ggml-org/llama.cpp/releases"
        )

    # Fetch the actual nightly release with binary assets
    nightly_url = f"https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/{nightly_tag}"
    nightly_req = Request(nightly_url, headers=headers)
    with urlopen(nightly_req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _download_with_progress(url: str, dest: Path, total_size: int = 0) -> None:
    """Download a file with a simple progress bar."""
    req = Request(url, headers={"Accept": "application/octet-stream"})
    is_tty = _is_interactive()
    with urlopen(req, timeout=600) as resp:
        downloaded = 0
        chunk_size = 1024 * 256  # 256 KB chunks
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if is_tty:
                    if total_size > 0:
                        pct = (downloaded / total_size) * 100
                        bar_len = 30
                        filled = int(bar_len * downloaded / total_size)
                        bar = "#" * filled + "." * (bar_len - filled)
                        print(
                            f"\r  [{bar}] {pct:5.1f}% ({_format_size(downloaded)}/{_format_size(total_size)})",
                            end="",
                            flush=True,
                        )
                    else:
                        print(f"\r  Downloaded: {_format_size(downloaded)}", end="", flush=True)
        if is_tty:
            print()  # newline after progress
        else:
            print(f"  Downloaded: {_format_size(downloaded)}")


def _extract_archive(archive_path: Path, dest_dir: Path, archive_type: str) -> None:
    """Extract a zip or tar.gz archive to dest_dir."""
    dest_dir.mkdir(parents=True, exist_ok=True)

    if archive_type == "zip":
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(dest_dir)
    else:
        with tarfile.open(archive_path, "r:gz") as tf:
            tf.extractall(dest_dir)


def _flatten_to_llama_dir(extract_dir: Path) -> None:
    """
    Move all files from the extracted directory into LLAMA_DIR.
    Handles the case where the archive extracts into a subdirectory.
    """
    LLAMA_DIR.mkdir(parents=True, exist_ok=True)

    # Check if extraction created a single subdirectory
    items = list(extract_dir.iterdir())
    source_dir = extract_dir
    if len(items) == 1 and items[0].is_dir():
        source_dir = items[0]  # e.g. llama-b9682-bin-win-cpu-x64/

    # Move all files/dirs to llama/
    for item in source_dir.iterdir():
        dest = LLAMA_DIR / item.name
        if dest.exists():
            if dest.is_dir():
                shutil.rmtree(dest)
            else:
                dest.unlink()
        shutil.move(str(item), str(dest))


def install_llama_server() -> bool:
    """
    Download and install llama-server from GitHub releases.

    Returns True if installation succeeded, False otherwise.
    """
    print()
    print("  [Setup] Fetching latest llama.cpp release info...")

    try:
        release = _fetch_latest_release()
    except (URLError, json.JSONDecodeError, OSError) as e:
        print(f"  [Setup] ERROR: Could not reach GitHub: {e}")
        print("  [Setup] Please check your internet connection and try again.")
        print(f"  [Setup] Manual download: https://github.com/ggml-org/llama.cpp/releases")
        return False

    tag = release.get("tag_name", "unknown")
    assets = release.get("assets", [])
    print(f"  [Setup] Latest release: {tag}")

    # Pick the right asset from the actual asset list
    main_asset, extra_assets = _pick_asset(assets)

    if not main_asset:
        print(f"  [Setup] ERROR: No matching binary found for this platform.")
        print(f"  [Setup] Available assets: {[a['name'] for a in assets[:10]]}")
        print(f"  [Setup] Manual download: https://github.com/ggml-org/llama.cpp/releases/tag/{tag}")
        return False

    # Calculate total download size
    total_size = main_asset["size"]
    for ea in extra_assets:
        total_size += ea["size"]

    # ── Disk space check ─────────────────────────────────────────────────
    if not _check_disk_space(total_size):
        try:
            proceed = input("  Continue anyway? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if proceed not in ("y", "yes"):
            return False

    # ── Download size warning ────────────────────────────────────────────
    print()
    print("  +----------------------------------------------------------+")
    print(f"  |  Download: {main_asset['name']}")
    if extra_assets:
        for ea in extra_assets:
            print(f"  |         + {ea['name']}")
    print(f"  |  Total size: {_format_size(total_size)}")
    print("  +----------------------------------------------------------+")
    print()

    if _is_interactive():
        try:
            confirm = input("  Proceed with download? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  [Setup] Installation cancelled.")
            return False
        if confirm and confirm not in ("y", "yes"):
            print("  [Setup] Installation cancelled.")
            return False
    else:
        print("  [Setup] Non-interactive mode, proceeding with download.")

    # ── Download & Extract ───────────────────────────────────────────────
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        all_downloads = [(main_asset, "main binaries")] + [(ea, "CUDA runtime") for ea in extra_assets]

        for asset, label in all_downloads:
            archive_name = asset["name"]
            archive_path = tmpdir / archive_name
            print(f"\n  [Setup] Downloading {label}: {archive_name}")
            try:
                _download_with_progress(
                    asset["browser_download_url"],
                    archive_path,
                    asset["size"],
                )
            except (URLError, OSError) as e:
                print(f"\n  [Setup] ERROR: Download failed: {e}")
                return False

            # Extract
            ext_dir = tmpdir / f"extract_{archive_name}"
            print(f"  [Setup] Extracting...")
            try:
                a_type = "zip" if archive_name.endswith(".zip") else "tar.gz"
                _extract_archive(archive_path, ext_dir, a_type)
                _flatten_to_llama_dir(ext_dir)
            except Exception as e:
                print(f"  [Setup] ERROR: Extraction failed: {e}")
                return False

    # ── Verify ───────────────────────────────────────────────────────────
    installed_bin = LLAMA_DIR / LLAMA_SERVER_BIN
    if not installed_bin.exists():
        print(f"  [Setup] ERROR: {LLAMA_SERVER_BIN} not found after extraction.")
        print(f"  [Setup] Contents of llama/: {list(LLAMA_DIR.iterdir())[:10]}")
        return False

    # Make executable on Unix
    if sys.platform != "win32":
        installed_bin.chmod(installed_bin.stat().st_mode | 0o755)

    print()
    print(f"  [Setup] OK: llama-server installed to: {LLAMA_DIR}")
    return True


# ── Main Entry Point ─────────────────────────────────────────────────────────

def ensure_llama_server() -> bool:
    """
    Ensure llama-server is available. Prompts for install if missing.

    Returns True if llama-server is available (found or just installed),
    False if not available (user declined or install failed).

    Called from main.py during startup.

    If no TTY is available (e.g. launched via shortcut, pythonw, or service),
    skips to degraded mode instead of hanging on input().

    With llama_server_shared (a dev instance) another process owns
    llama-server: no binary lookup, no prompt, no download. Returns True so
    the caller adopts the shared server if it runs (start_server never
    starts one in that mode). On Windows a stdin of NUL (`</dev/null`)
    looks like a TTY, so the prompt check alone is not enough.

    Output is plain ASCII: redirected stdout on Windows is cp1252.
    """
    from screenmind.config import settings
    if settings.llama_server_shared:
        logger.info("llama-server is shared (LLAMA_SERVER_SHARED): not looking for, "
                    "installing or starting one here")
        return True

    path = find_llama_server()
    if path:
        print(f"[Setup] llama-server found: {path}")
        return True

    # Not found — check if we can prompt interactively
    if not _is_interactive():
        # No TTY — can't prompt. Start in degraded mode.
        print()
        print("[Setup] llama-server not found (non-interactive mode).")
        print("[Setup] Starting without AI features.")
        print("[Setup] To install, run interactively:  python -m screenmind.setup_llama")
        print()
        return False

    # Interactive — show prompt
    print()
    print("  +----------------------------------------------------------+")
    print("  |  llama-server not found                                 |")
    print("  |                                                         |")
    print("  |  llama-server (from llama.cpp) is required for:         |")
    print("  |    - Screenshot analysis (Gemma 4 vision)               |")
    print("  |    - Chat with your screen memory                       |")
    print("  |    - Voice memo transcription                           |")
    print("  |    - Meeting transcription                              |")
    print("  |                                                         |")
    print("  |  Without it, ScreenMind will only capture screenshots   |")
    print("  |  but cannot analyze or understand them.                 |")
    print("  +----------------------------------------------------------+")
    print()

    try:
        answer = input("  Install llama-server automatically? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n")
        answer = "n"

    if answer and answer not in ("y", "yes"):
        _print_degraded_warning()
        return False

    # User said yes — install
    return install_llama_server()


def _print_degraded_warning():
    """Print the degraded-mode warning with platform-specific install instructions."""
    print()
    print("=" * 60)
    print("  WARNING: Starting without llama-server")
    print()
    print("  Screenshots will be captured but NOT analyzed.")
    print("  Chat and voice memos will not work.")
    print()
    if sys.platform == "win32":
        print("  To install later, run:  python -m screenmind.setup_llama")
        print("  Or download manually:   https://github.com/ggml-org/llama.cpp/releases")
    elif sys.platform == "darwin":
        print("  To install later:")
        print("    brew install llama.cpp")
        print("    or: python -m screenmind.setup_llama")
    else:
        print("  To install later, run:  python -m screenmind.setup_llama")
        print("  Or download manually:   https://github.com/ggml-org/llama.cpp/releases")
    print("=" * 60)
    print()


# ── Standalone Execution ─────────────────────────────────────────────────────

if __name__ == "__main__":
    print()
    print("  ScreenMind - llama-server Setup")
    print("  ================================")
    print()

    path = find_llama_server()
    if path:
        print(f"  OK: llama-server already installed: {path}")
        print()

        reinstall = input("  Reinstall/update? [y/N]: ").strip().lower()
        if reinstall not in ("y", "yes"):
            print("  Nothing to do.")
            sys.exit(0)

    success = install_llama_server()
    sys.exit(0 if success else 1)
