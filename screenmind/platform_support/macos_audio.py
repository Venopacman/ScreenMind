"""
Which apps are using the microphone, from CoreAudio (macOS 14+).

CoreAudio keeps one "process object" per audio client, with a flag for
"is capturing input". This is the same signal behind the orange mic dot.
Reading it opens no audio device and needs no mic permission.

Audio often runs in a helper process ("Slack Helper", "Google Chrome
Helper"), so each process is mapped to its outermost .app bundle name.
That matches the Quartz window owner ("Slack", "Google Chrome").
"""

import ctypes
import ctypes.util
import logging
import os
import struct
from typing import Optional, Set

logger = logging.getLogger("screenmind.platform_support.macos_audio")


def _fourcc(s: str) -> int:
    return struct.unpack(">I", s.encode())[0]


_SYSTEM_OBJECT = 1
_SCOPE_GLOBAL = _fourcc("glob")
_PROCESS_LIST = _fourcc("prs#")      # kAudioHardwarePropertyProcessObjectList
_PROCESS_PID = _fourcc("ppid")       # kAudioProcessPropertyPID
_PROCESS_INPUT = _fourcc("piri")     # kAudioProcessPropertyIsRunningInput


class _Address(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32),
                ("element", ctypes.c_uint32)]


_lib = None


def _coreaudio():
    global _lib
    if _lib is None:
        lib = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))
        lib.AudioObjectGetPropertyDataSize.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32)]
        lib.AudioObjectGetPropertyData.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        _lib = lib
    return _lib


def _get_u32(obj: int, selector: int) -> Optional[int]:
    addr = _Address(selector, _SCOPE_GLOBAL, 0)
    value = ctypes.c_uint32(0)
    size = ctypes.c_uint32(4)
    err = _coreaudio().AudioObjectGetPropertyData(
        obj, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(value))
    return value.value if err == 0 else None


def _process_objects() -> list:
    lib = _coreaudio()
    addr = _Address(_PROCESS_LIST, _SCOPE_GLOBAL, 0)
    size = ctypes.c_uint32(0)
    if lib.AudioObjectGetPropertyDataSize(_SYSTEM_OBJECT, ctypes.byref(addr), 0, None, ctypes.byref(size)):
        return []
    buf = (ctypes.c_uint32 * (size.value // 4))()
    if lib.AudioObjectGetPropertyData(_SYSTEM_OBJECT, ctypes.byref(addr), 0, None, ctypes.byref(size), buf):
        return []
    return list(buf)


def _pid_path(pid: int) -> Optional[str]:
    try:
        libproc = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib")
        buf = ctypes.create_string_buffer(4096)
        n = libproc.proc_pidpath(ctypes.c_int(pid), buf, ctypes.c_uint32(4096))
        return buf.value.decode("utf-8", "replace") if n > 0 else None
    except Exception:
        return None


def app_name_from_path(path: Optional[str]) -> Optional[str]:
    """Outermost .app bundle name in an executable path, e.g.
    ".../Slack.app/Contents/Frameworks/Slack Helper.app/..." -> "Slack"."""
    if not path:
        return None
    for part in path.split("/"):
        if part.endswith(".app"):
            return part[:-4]
    return None


def mic_apps() -> Optional[Set[str]]:
    """Lowercased app names currently capturing the mic, or None if this
    macOS has no process list (before 14) or CoreAudio fails."""
    try:
        objects = _process_objects()
    except Exception as e:
        logger.debug("CoreAudio process list failed: %s", e)
        return None
    if not objects:
        return None
    own_pid = os.getpid()
    apps = set()
    for obj in objects:
        if not _get_u32(obj, _PROCESS_INPUT):
            continue
        pid = ctypes.c_int32(_get_u32(obj, _PROCESS_PID) or 0).value
        if pid <= 0 or pid == own_pid:
            continue
        name = app_name_from_path(_pid_path(pid))
        if name:
            apps.add(name.lower())
    return apps
