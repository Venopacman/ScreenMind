"""
startup.py — Cross-platform OS startup registration for ScreenMind.

Registers/removes ScreenMind to start automatically on user login.
  - Windows:  HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run (registry)
  - macOS:    ~/Library/LaunchAgents/com.screenmind.plist (launchd)
  - Linux:    ~/.config/autostart/screenmind.desktop (XDG autostart)

All operations are current-user only — no admin/root needed.
"""

import shutil
import sys
from pathlib import Path

from screenmind.config import is_frozen, settings


# ── Command Construction ─────────────────────────────────────────────────────

def _get_startup_command() -> str:
    """
    Build the command that the OS should run at login.

    App (frozen): the app's own executable, quoted, no arguments. On Windows
             that is "<install dir>\\ScreenMind.exe", the same value the
             installer writes under HKCU Run\\ScreenMind, so the two agree.
             On macOS it is ScreenMind.app/Contents/MacOS/ScreenMind.
    Windows: Always use pythonw to avoid console flash.
    Unix:    Prefer pip-installed 'screenmind' console script (survives Python upgrades).
    """
    if is_frozen():
        return f'"{sys.executable}"'
    if sys.platform == "win32":
        # Always use pythonw on Windows to avoid brief console flash at boot
        pythonw = sys.executable.replace("python.exe", "pythonw.exe")
        if Path(pythonw).exists():
            return f'"{pythonw}" -m screenmind'
        # Fallback: regular python (CREATE_NO_WINDOW handled by the child)
        return f'"{sys.executable}" -m screenmind --background'
    else:
        # macOS/Linux: prefer pip-installed console script (stable across upgrades)
        screenmind_exe = shutil.which("screenmind")
        if screenmind_exe:
            return f'"{screenmind_exe}" --background'
        return f'"{sys.executable}" -m screenmind --background'


# ── Windows ──────────────────────────────────────────────────────────────────

_WIN_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_WIN_REG_VALUE = "ScreenMind"


def _install_windows() -> bool:
    """Register in Windows startup via HKCU registry."""
    try:
        import winreg
        cmd = _get_startup_command()
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_REG_KEY, 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, _WIN_REG_VALUE, 0, winreg.REG_SZ, cmd)
        winreg.CloseKey(key)
        print("  ✓ ScreenMind registered to start at login.")  # noqa: T201
        print(f"    Command: {cmd}")  # noqa: T201
        return True
    except Exception as e:
        print(f"  ✗ Failed to register startup: {e}")  # noqa: T201
        return False


def _uninstall_windows() -> bool:
    """Remove from Windows startup registry."""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_REG_KEY, 0, winreg.KEY_SET_VALUE)
        try:
            winreg.DeleteValue(key, _WIN_REG_VALUE)
            print("  ✓ ScreenMind removed from startup.")  # noqa: T201
        except FileNotFoundError:
            print("  ✓ ScreenMind was not registered in startup.")  # noqa: T201
        winreg.CloseKey(key)
        return True
    except Exception as e:
        print(f"  ✗ Failed to remove startup entry: {e}")  # noqa: T201
        return False


def _is_installed_windows() -> bool:
    """Check if ScreenMind is registered in Windows startup."""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_REG_KEY, 0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, _WIN_REG_VALUE)
            winreg.CloseKey(key)
            return True
        except FileNotFoundError:
            winreg.CloseKey(key)
            return False
    except Exception:
        return False


# ── macOS ────────────────────────────────────────────────────────────────────

def _macos_plist_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"

def _macos_plist_file() -> Path:
    return _macos_plist_dir() / "com.screenmind.plist"


def _install_macos() -> bool:
    """Register as a macOS LaunchAgent."""
    try:
        cmd = _get_startup_command()
        if is_frozen():
            # The app binary, unsplit: its path may hold spaces. SMAppService
            # replaces this plist in a later milestone (docs/plans/packaging.md).
            program, args = sys.executable, []
        else:
            # Split command into program + args for plist
            # Handle both quoted and unquoted paths
            parts = cmd.replace('"', '').split()
            program = parts[0]
            args = parts[1:] if len(parts) > 1 else []

        args_xml = "\n".join(f"        <string>{a}</string>" for a in args)

        log_path = settings.data_path / "screenmind.log"
        # The app writes screenmind.log itself (rotating); launchd appending
        # to the same file would double each line. Keep its output apart.
        if is_frozen():
            log_path = settings.data_path / "launchd.log"

        plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.screenmind</string>
    <key>ProgramArguments</key>
    <array>
        <string>{program}</string>
{args_xml}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
</dict>
</plist>
"""
        _macos_plist_dir().mkdir(parents=True, exist_ok=True)
        _macos_plist_file().write_text(plist_content)
        print("  ✓ ScreenMind registered as LaunchAgent.")  # noqa: T201
        print(f"    Plist: {_macos_plist_file()}")  # noqa: T201
        return True
    except Exception as e:
        print(f"  ✗ Failed to create LaunchAgent: {e}")  # noqa: T201
        return False


def _uninstall_macos() -> bool:
    """Remove macOS LaunchAgent."""
    try:
        plist = _macos_plist_file()
        if plist.exists():
            plist.unlink()
            print("  ✓ ScreenMind LaunchAgent removed.")  # noqa: T201
        else:
            print("  ✓ ScreenMind was not registered as LaunchAgent.")  # noqa: T201
        return True
    except Exception as e:
        print(f"  ✗ Failed to remove LaunchAgent: {e}")  # noqa: T201
        return False


def _is_installed_macos() -> bool:
    """Check if ScreenMind LaunchAgent exists."""
    return _macos_plist_file().exists()


# ── Linux ────────────────────────────────────────────────────────────────────

def _linux_autostart_dir() -> Path:
    return Path.home() / ".config" / "autostart"

def _linux_desktop_file() -> Path:
    return _linux_autostart_dir() / "screenmind.desktop"


def _install_linux() -> bool:
    """Register as XDG autostart entry."""
    try:
        cmd = _get_startup_command()
        desktop_content = f"""[Desktop Entry]
Type=Application
Name=ScreenMind
Comment=Privacy-first AI screen activity journal
Exec={cmd}
Hidden=false
X-GNOME-Autostart-enabled=true
StartupNotify=false
Terminal=false
"""
        _linux_autostart_dir().mkdir(parents=True, exist_ok=True)
        _linux_desktop_file().write_text(desktop_content)
        print("  ✓ ScreenMind registered in XDG autostart.")  # noqa: T201
        print(f"    File: {_linux_desktop_file()}")  # noqa: T201
        return True
    except Exception as e:
        print(f"  ✗ Failed to create autostart entry: {e}")  # noqa: T201
        return False


def _uninstall_linux() -> bool:
    """Remove XDG autostart entry."""
    try:
        desktop = _linux_desktop_file()
        if desktop.exists():
            desktop.unlink()
            print("  ✓ ScreenMind removed from XDG autostart.")  # noqa: T201
        else:
            print("  ✓ ScreenMind was not registered in autostart.")  # noqa: T201
        return True
    except Exception as e:
        print(f"  ✗ Failed to remove autostart entry: {e}")  # noqa: T201
        return False


def _is_installed_linux() -> bool:
    """Check if XDG autostart entry exists."""
    return _linux_desktop_file().exists()


# ── Public API ───────────────────────────────────────────────────────────────

def install_startup() -> bool:
    """Register ScreenMind to start at system login. Returns True on success."""
    print()  # noqa: T201
    print("  ScreenMind — Startup Registration")  # noqa: T201
    print("  ==================================")  # noqa: T201
    print()  # noqa: T201

    if sys.platform == "win32":
        return _install_windows()
    elif sys.platform == "darwin":
        return _install_macos()
    else:
        return _install_linux()


def uninstall_startup() -> bool:
    """Remove ScreenMind from system startup. Returns True on success."""
    print()  # noqa: T201
    print("  ScreenMind — Remove Startup")  # noqa: T201
    print("  ===========================")  # noqa: T201
    print()  # noqa: T201

    if sys.platform == "win32":
        return _uninstall_windows()
    elif sys.platform == "darwin":
        return _uninstall_macos()
    else:
        return _uninstall_linux()


def is_startup_installed() -> bool:
    """Check if ScreenMind is registered in system startup."""
    if sys.platform == "win32":
        return _is_installed_windows()
    elif sys.platform == "darwin":
        return _is_installed_macos()
    else:
        return _is_installed_linux()
