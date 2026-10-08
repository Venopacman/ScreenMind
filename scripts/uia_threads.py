#!/usr/bin/env python3
"""
UI Automation threading and hang checks on Windows (G37, docs/plans/uptime.md).

Run from the repo root, one scenario at a time:

    uv run python scripts/uia_threads.py <scenario>

Shared-client scenarios (2026-10-08: all pass, no error, no delay). They
call UIA on the taskbar, which is always there:

    exit     a thread creates uiautomation's client, then exits; others use it
    exit2    the same, with comtypes imported on the main thread first, so the
             creator's COM apartment really ends
    nopump   the main thread (STA, never pumps, like the event loop) creates
             it; a worker uses it while main sleeps
    both     main and a worker use it at the same time for 30 s
    agile    whether the client and an element are IAgileObject

Hang scenario:

    hung     starts a Tk window that stops pumping messages (a hung app) and
             times ScreenMind's UIA reads on it. Each must end within its UIA
             job timeout (3 s for a11y text, 1.5 s for lookups).

Touches no ScreenMind data; only reads UIA.
"""

import ctypes
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screenmind.platform_support import windows as w  # noqa: E402

user32 = ctypes.WinDLL("user32")
user32.FindWindowW.restype = ctypes.c_void_p
TRAY = user32.FindWindowW("Shell_TrayWnd", None)


def out(*args):
    print(*args, flush=True)  # noqa: T201


def work(tag):
    """Raw UIA calls on the taskbar, bypassing the UIA worker on purpose."""
    auto = w.uia()
    ia = auto.uiautomation._AutomationClient.instance().IUIAutomation
    root = ia.ElementFromHandle(TRAY)
    n = root.FindAll(w._TreeScope_Descendants, ia.CreateTrueCondition()).Length
    kids = len(auto.ControlFromHandle(TRAY).GetChildren())
    docs = w._window_documents(TRAY)
    return f"{tag}: {n} nodes, {kids} children, {len(docs)} docs"


def timed(fn, tag, limit=10.0):
    """fn(tag) on a fresh thread; reports HUNG past `limit`."""
    res = {}

    def run():
        try:
            res["r"] = fn(tag)
        except Exception as e:
            res["r"] = f"{tag}: ERROR {e!r}"

    t = threading.Thread(target=run, daemon=True)
    t0 = time.perf_counter()
    t.start()
    t.join(limit)
    if t.is_alive():
        return f"{tag}: HUNG > {limit:.0f}s"
    return f"{res['r']} ({time.perf_counter() - t0:.2f}s)"


def scenario_exit():
    t = threading.Thread(target=lambda: out("creator:", work("creator")))
    t.start()
    t.join()
    out("creator thread exited")
    for i in range(3):
        out(timed(work, f"other#{i}"))


def scenario_exit2():
    import uiautomation  # noqa: F401  (comtypes sets up COM on main here)
    scenario_exit()


def scenario_nopump():
    out("main:", work("main-create"))
    res = []
    t = threading.Thread(target=lambda: res.append(timed(work, "worker")))
    t.start()
    time.sleep(12)  # main blocked, no message pump (like the asyncio loop)
    t.join()
    out(res[0])


def scenario_both():
    out("main:", work("main-create"))
    stop = threading.Event()
    errors = []
    rounds = {"worker": 0, "main": 0}

    def loop(tag):
        while not stop.is_set():
            r = timed(work, tag, 10)
            if "ERROR" in r or "HUNG" in r:
                errors.append(r)
            rounds[tag] += 1

    t = threading.Thread(target=loop, args=("worker",))
    t.start()
    end = time.time() + 30
    while time.time() < end:
        r = timed(work, "main", 10)
        if "ERROR" in r or "HUNG" in r:
            errors.append(r)
        rounds["main"] += 1
    stop.set()
    t.join()
    out(f"rounds: {rounds}, errors: {len(errors)} {errors[:3]}")


def scenario_agile():
    import comtypes
    from comtypes import GUID, IUnknown

    class IAgileObject(IUnknown):
        _iid_ = GUID("{94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90}")
        _methods_ = []

    client = w.uia().uiautomation._AutomationClient.instance()
    for name, obj in (("IUIAutomation", client.IUIAutomation),
                      ("element", client.IUIAutomation.ElementFromHandle(TRAY))):
        try:
            obj.QueryInterface(IAgileObject)
            out(name, "is agile")
        except comtypes.COMError:
            out(name, "is not agile")


HUNG_APP = r'''
import tkinter as tk, time, sys
r = tk.Tk(); r.title("g37-hung"); r.geometry("400x300+100+100")
tk.Entry(r).pack(); tk.Button(r, text="ok").pack(); tk.Text(r).pack()
for _ in range(20): r.update(); time.sleep(0.05)
print("hung", flush=True); time.sleep(float(sys.argv[1]))  # noqa: T201
'''


def scenario_hung():
    from screenmind.capture.ui_events.windows import WindowsUiEventBackend
    child = subprocess.Popen([sys.executable, "-c", HUNG_APP, "90"], stdout=subprocess.PIPE, text=True)
    try:
        child.stdout.readline()
        hwnd = user32.FindWindowW(None, "g37-hung")
        time.sleep(1)  # pumps no more, but not yet marked hung (that takes 5 s)
        adapter, backend = w.WindowsAdapter(), WindowsUiEventBackend()
        checks = [
            ("a11y text", lambda: adapter.extract_a11y_text(hwnd), w.UIA_A11Y_TIMEOUT_S),
            ("document title", lambda: adapter._best_title(hwnd, "python", "python"), w.UIA_LOOKUP_TIMEOUT_S),
            ("element at point", lambda: backend.element_at(250, 200), w.UIA_LOOKUP_TIMEOUT_S),
        ]
        worst = 0.0
        for name, fn, limit in checks:
            t0 = time.perf_counter()
            r = fn()
            took = time.perf_counter() - t0
            worst = max(worst, took - limit)
            out(f"{name:18} {took:5.2f}s (limit {limit:.1f}s)  {r!r:.60}")
        out("PASS" if worst < 0.5 else "FAIL: a read outlasted its limit")
    finally:
        child.kill()


if __name__ == "__main__":
    scenarios = {"exit": scenario_exit, "exit2": scenario_exit2, "nopump": scenario_nopump,
                 "both": scenario_both, "agile": scenario_agile, "hung": scenario_hung}
    if len(sys.argv) != 2 or sys.argv[1] not in scenarios:
        sys.exit(f"usage: {sys.argv[0]} {{{','.join(scenarios)}}}")
    scenarios[sys.argv[1]]()
