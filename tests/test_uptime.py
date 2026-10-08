"""Uptime (G37, docs/plans/uptime.md): a hung OS call must not stop capture,
UI events or shutdown, and the watchdog must see a stuck part.

The UIA worker tests use fake jobs that hang on an Event, so they run on
any OS. The watchdog and the shutdown deadline are OS-neutral.
"""

import subprocess
import sys
import textwrap
import threading
import time
import types

import pytest

from screenmind import watchdog as wd

pw = pytest.importorskip("screenmind.platform_support.windows")


@pytest.fixture
def release():
    """Set at teardown, so hung fake jobs end and their threads exit."""
    ev = threading.Event()
    yield ev
    ev.set()


def make_worker(**kw):
    return pw.UiaWorker("test", init=lambda: None, **kw)


def elapsed(fn):
    t0 = time.monotonic()
    result = fn()
    return result, time.monotonic() - t0


# ── UIA worker ───────────────────────────────────────────────────────


class TestUiaWorker:
    def test_result(self):
        assert make_worker().run("job", lambda: 42, timeout=1.0) == 42

    def test_runs_on_its_own_thread(self):
        name = make_worker().run("job", lambda: threading.current_thread().name, timeout=1.0)
        assert name == "uia-test"

    def test_error_gives_default(self):
        def boom():
            raise OSError("COM error")
        assert make_worker().run("job", boom, timeout=1.0, default="d") == "d"

    def test_hung_job_returns_default_in_time(self, release):
        result, took = elapsed(lambda: make_worker().run("hang", release.wait, timeout=0.3, default="d"))
        assert result == "d"
        assert took < 1.0

    def test_next_job_fails_fast_while_one_is_overdue(self, release):
        w = make_worker(stuck_s=60)
        w.run("hang", release.wait, timeout=0.2)
        result, took = elapsed(lambda: w.run("next", lambda: 1, timeout=2.0, default="d"))
        assert result == "d"
        assert took < 0.1  # did not queue behind the stuck job

    def test_job_queues_behind_one_that_is_on_time(self):
        w = make_worker()
        slow = threading.Thread(target=w.run, args=("slow", lambda: time.sleep(0.3), 2.0))
        slow.start()
        time.sleep(0.05)
        assert w.run("next", lambda: 7, timeout=2.0) == 7
        slow.join()

    def test_stuck_thread_is_replaced(self, release):
        w = make_worker(stuck_s=0.3)
        w.run("hang", release.wait, timeout=0.1)
        time.sleep(0.4)
        assert w.run("next", lambda: threading.current_thread().ident, timeout=1.0) is not None
        assert w.replacements == 1
        assert w.run("again", lambda: 3, timeout=1.0) == 3

    def test_replacements_are_capped(self, release):
        w = make_worker(stuck_s=0.1, max_replacements=2)
        for _ in range(3):
            w.run("hang", release.wait, timeout=0.05)
            time.sleep(0.15)
        # third stuck thread: no more replacements, UIA off for this consumer
        assert w.run("next", lambda: 1, timeout=1.0, default="off") == "off"
        assert w.disabled and w.replacements == 2

    def test_cancelled_job_never_runs(self, release):
        w = make_worker(stuck_s=60)
        ran = []
        threading.Thread(target=w.run, args=("slow", lambda: release.wait(0.5), 1.0)).start()
        time.sleep(0.05)
        w.run("queued", lambda: ran.append(1), timeout=0.1)  # gives up while queued
        time.sleep(0.6)
        assert ran == []

    def test_busy_for(self, release):
        w = make_worker()
        assert w.busy_for() == 0.0
        w.run("hang", release.wait, timeout=0.1)
        assert w.busy_for() > 0.0


class SlowControl:
    """A UIA element whose app answers slowly: each GetChildren takes 50 ms."""

    def __init__(self, depth=0):
        self.ControlTypeName = "TextControl"
        self.Name = f"line {depth}"
        self.IsPassword = False
        self._depth = depth

    def GetPattern(self, _):
        return None

    def GetChildren(self):
        time.sleep(0.05)
        return [SlowControl(self._depth + 1)] if self._depth < 200 else []


class TestWalkDeadline:
    def test_walk_stops_at_its_deadline_with_what_it_read(self):
        """200 slow levels would take 10 s; the job gets 1 s and keeps the start."""
        w = make_worker()
        adapter = pw.WindowsAdapter()
        adapter._uia = types.SimpleNamespace(PatternId=types.SimpleNamespace(ValuePattern=1, TextPattern=2))

        def job():
            texts = []
            adapter._walk_tree(SlowControl(), texts, depth=0, max_depth=500)
            return texts

        texts, took = elapsed(lambda: w.run("walk", job, timeout=1.0))
        assert took < 1.0
        assert texts and texts[0] == "line 0" and len(texts) < 200
        assert w.partial == 1

    def test_no_deadline_outside_a_job(self):
        assert pw.uia_out_of_time() is False


class TestCallers:
    """The adapter and the UI-event backend give up on a hung app."""

    @pytest.fixture(autouse=True)
    def fresh_workers(self, monkeypatch):
        monkeypatch.setattr(pw, "_uia_workers", {
            name: pw.UiaWorker(name, init=lambda: None) for name in ("capture", "recorder")})
        monkeypatch.setattr(pw, "UIA_A11Y_TIMEOUT_S", 0.3)
        monkeypatch.setattr(pw, "UIA_LOOKUP_TIMEOUT_S", 0.2)

    def test_a11y_text_of_a_hung_app(self, monkeypatch, release):
        adapter = pw.WindowsAdapter()
        adapter._a11y_initialized, adapter._uia_installed = True, True
        monkeypatch.setattr(adapter, "_extract_uiautomation", lambda hwnd: release.wait())
        result, took = elapsed(lambda: adapter.extract_a11y_text(1))
        assert result == (None, "none")
        assert took < 1.0

    def test_document_title_of_a_hung_app(self, monkeypatch, release):
        monkeypatch.setattr(pw, "_window_documents", lambda hwnd: release.wait())
        result, took = elapsed(lambda: pw.WindowsAdapter()._best_title(1, "Slack", "slack"))
        assert result == "Slack"  # the window title, without the page title
        assert took < 1.0

    def test_capture_and_recorder_do_not_wait_for_each_other(self, monkeypatch, release):
        monkeypatch.setattr(pw, "_window_documents", lambda hwnd: release.wait())
        pw.WindowsAdapter()._document_title(1, "x")  # capture worker now stuck
        assert pw.run_uia("recorder", "lookup", lambda: "ok", 0.5) == "ok"

    def test_backend_lookups_of_a_hung_app(self, release):
        win = pytest.importorskip("screenmind.capture.ui_events.windows")
        backend = win.WindowsUiEventBackend()
        backend._auto = types.SimpleNamespace(
            ControlFromPoint=lambda x, y: release.wait(),
            GetFocusedControl=lambda: release.wait())
        (clicked, took) = elapsed(lambda: backend.element_at(1, 1))
        assert clicked is None and took < 1.0

    def test_backend_focus_of_a_hung_app(self, release):
        win = pytest.importorskip("screenmind.capture.ui_events.windows")
        backend = win.WindowsUiEventBackend()
        backend._auto = types.SimpleNamespace(GetFocusedControl=lambda: release.wait())
        focused, took = elapsed(backend.focused_element)
        assert focused is None and took < 1.0

    def test_clipboard_read_of_a_hung_owner(self, monkeypatch, release):
        win = pytest.importorskip("screenmind.capture.ui_events.windows")
        backend = win.WindowsUiEventBackend()
        monkeypatch.setattr(backend, "_read_clipboard", release.wait)
        text, took = elapsed(backend.read_clipboard)
        assert text is None and took < 2.0


# ── Watchdog ─────────────────────────────────────────────────────────


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeRecorder:
    def __init__(self):
        self.running = True
        self.ticks = 0
        self.restarts = 0

    def restart(self):
        self.restarts += 1
        return True


class FakeQueue:
    def __init__(self, n=0):
        self.n = n

    def qsize(self):
        return self.n


@pytest.fixture
def clock():
    return Clock()


class TestWatchdog:
    def test_all_well(self, clock):
        cap = types.SimpleNamespace(last_beat=clock.now)
        dog = wd.Watchdog(capture_worker=cap, ui_recorder=FakeRecorder(), clock=clock)
        assert dog.check() == []

    def test_not_started_parts_are_not_stuck(self, clock):
        dog = wd.Watchdog(capture_worker=object(), analysis_worker=object(),
                          analysis_queue=FakeQueue(5), clock=clock)
        clock.now += 3600
        assert dog.check() == []

    def test_capture_loop_without_a_pass_is_stuck(self, clock, caplog):
        cap = types.SimpleNamespace(last_beat=clock.now)
        dog = wd.Watchdog(capture_worker=cap, clock=clock)
        clock.now += wd.CAPTURE_STUCK_S - 1
        assert dog.check() == []
        clock.now += 2
        with caplog.at_level("WARNING", logger="screenmind.watchdog"):
            assert dog.check() == ["capture"]
        assert "Thread stacks" in caplog.text and "MainThread" in caplog.text

    def test_paused_capture_keeps_beating(self, clock):
        """The loop passes every <= 5 s while paused or idle, so no stall."""
        cap = types.SimpleNamespace(last_beat=clock.now)
        dog = wd.Watchdog(capture_worker=cap, clock=clock)
        for _ in range(100):
            clock.now += 5
            cap.last_beat = clock.now
            assert dog.check() == []

    def test_stall_is_logged_once_then_every_10_min(self, clock, caplog):
        cap = types.SimpleNamespace(last_beat=clock.now)
        dog = wd.Watchdog(capture_worker=cap, clock=clock)
        clock.now += wd.CAPTURE_STUCK_S
        with caplog.at_level("WARNING", logger="screenmind.watchdog"):
            dog.check()
            clock.now += 60
            dog.check()
            assert caplog.text.count("Watchdog: The capture loop") == 1
            clock.now += wd.REPORT_AGAIN_S
            dog.check()
            assert caplog.text.count("Watchdog: The capture loop") == 2

    def test_ticking_recorder_is_fine(self, clock):
        rec = FakeRecorder()
        dog = wd.Watchdog(ui_recorder=rec, clock=clock)
        for _ in range(20):
            rec.ticks += 100
            clock.now += 30
            assert dog.check() == []
        assert rec.restarts == 0

    def test_stopped_recorder_is_not_stuck(self, clock):
        rec = FakeRecorder()
        rec.running = False
        dog = wd.Watchdog(ui_recorder=rec, clock=clock)
        dog.check()
        clock.now += 3600
        assert dog.check() == []

    def test_stuck_recorder_is_restarted(self, clock):
        rec = FakeRecorder()
        dog = wd.Watchdog(ui_recorder=rec, clock=clock)
        dog.check()
        clock.now += wd.RECORDER_STUCK_S
        assert dog.check() == ["recorder"]
        assert rec.restarts == 1
        rec.ticks += 1  # the new enricher ticks
        clock.now += 30
        assert dog.check() == []

    def test_recorder_restarts_are_capped(self, clock):
        rec = FakeRecorder()
        dog = wd.Watchdog(ui_recorder=rec, clock=clock)
        dog.check()
        for _ in range(wd.RECORDER_MAX_RESTARTS + 2):
            clock.now += wd.RECORDER_STUCK_S
            dog.check()
        assert rec.restarts == wd.RECORDER_MAX_RESTARTS
        assert dog.recorder_restarts_off

    def test_analysis_with_empty_queue_is_idle_not_stuck(self, clock):
        ana = types.SimpleNamespace(last_beat=clock.now)
        dog = wd.Watchdog(analysis_worker=ana, analysis_queue=FakeQueue(0), clock=clock)
        clock.now += 10 * wd.ANALYSIS_STUCK_S
        assert dog.check() == []

    def test_analysis_with_frames_waiting_is_stuck_after_20_min(self, clock):
        ana = types.SimpleNamespace(last_beat=clock.now)
        queue = FakeQueue(3)
        dog = wd.Watchdog(analysis_worker=ana, analysis_queue=queue, clock=clock)
        clock.now += 300  # one slow Gemma call: fine
        assert dog.check() == []
        clock.now += wd.ANALYSIS_STUCK_S
        assert dog.check() == ["analysis"]

    def test_thread_stacks_name_threads(self, release):
        t = threading.Thread(target=release.wait, name="stuck-in-com", daemon=True)
        t.start()
        assert "stuck-in-com" in wd.thread_stacks()


class TestRecorderRestart:
    def test_restart_with_a_stuck_enricher_gets_a_new_lock(self, monkeypatch, release):
        from screenmind.capture.ui_events import recorder as rec_mod
        monkeypatch.setattr(rec_mod, "_STOP_JOIN_S", 0.1)
        monkeypatch.setattr(rec_mod, "_LOCK_TIMEOUT_S", 0.1)

        class Backend:
            name = "fake"

            def check_permissions(self):
                return types.SimpleNamespace(accessibility=True)

            def start(self, out):
                return True

            def stop(self):
                pass

            def is_running(self):
                return True

            def clipboard_change_count(self):
                return 0

            def tap_stats(self):
                return {}

            def front_window(self):
                return None

        rec = rec_mod.UiEventRecorder(backend=Backend())
        stuck = threading.Event()

        def hang(raw, now):  # the old enricher: stuck while holding the lock
            with rec._lock:
                stuck.set()
                release.wait()

        monkeypatch.setattr(rec, "_tick", hang)
        assert rec.start()
        assert stuck.wait(2)
        old_lock = rec._lock
        monkeypatch.setattr(rec, "_tick", rec_mod.UiEventRecorder._tick.__get__(rec))
        assert rec.restart()
        assert rec._lock is not old_lock
        before = rec.ticks
        time.sleep(0.6)
        assert rec.ticks > before  # the new enricher runs
        rec.flush_for_capture()  # and capture does not wait on the old lock
        release.set()
        rec.stop()


# ── Shutdown deadline ────────────────────────────────────────────────


class TestShutdownDeadline:
    @pytest.fixture(autouse=True)
    def no_timer(self, monkeypatch):
        monkeypatch.setattr(wd, "_deadline_timer", None)

    def test_fires_once_runs_before_exit_and_exits_3(self):
        calls = []
        done = threading.Event()

        def exit_fn(code):
            calls.append(("exit", code))
            done.set()

        wd.start_shutdown_deadline(before_exit=lambda: calls.append("checkpoint"),
                                   seconds=0.05, exit_fn=exit_fn)
        wd.start_shutdown_deadline(seconds=0.05, exit_fn=exit_fn)  # second call: no-op
        assert done.wait(2)
        time.sleep(0.1)
        assert calls == ["checkpoint", ("exit", wd.FORCED_EXIT_CODE)]

    def test_failing_before_exit_still_exits(self):
        done = threading.Event()

        def boom():
            raise RuntimeError("db locked")

        wd.start_shutdown_deadline(before_exit=boom, seconds=0.05, exit_fn=lambda c: done.set())
        assert done.wait(2)

    def test_ends_a_process_kept_alive_by_a_stuck_thread(self):
        """Without the deadline, Python waits for the non-daemon thread forever."""
        code = textwrap.dedent("""
            import threading
            from screenmind import watchdog
            threading.Thread(target=threading.Event().wait).start()  # never ends
            watchdog.start_shutdown_deadline(seconds=0.5)
        """)
        proc = subprocess.run([sys.executable, "-c", code], timeout=30, capture_output=True)
        assert proc.returncode == wd.FORCED_EXIT_CODE

    def test_normal_exit_is_not_delayed(self):
        code = "from screenmind import watchdog; watchdog.start_shutdown_deadline(seconds=60)"
        t0 = time.monotonic()
        proc = subprocess.run([sys.executable, "-c", code], timeout=30, capture_output=True)
        assert proc.returncode == 0
        assert time.monotonic() - t0 < 20
