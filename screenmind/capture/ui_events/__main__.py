"""
Live probe: print UI events as they happen, without starting ScreenMind.

    python -m screenmind.capture.ui_events            # run until Ctrl+C
    python -m screenmind.capture.ui_events --seconds 30
    python -m screenmind.capture.ui_events --ask      # show the macOS permission prompts first

Nothing is written to the database. Runs the same recorder, privacy rules
and text grouping as the app, with all event types on.
"""

import argparse
import sys
import time

from screenmind.capture.ui_events.recorder import UiEventRecorder, create_backend
from screenmind.config import settings


class _PrintSink:
    def insert_ui_events(self, events):
        for e in events:
            extra = f"  [{e.element_role}]" if e.element_role else ""
            print(f"{e.timestamp:%H:%M:%S}  {e.type.value:<12} {e.describe()}{extra}", flush=True)  # noqa: T201
        return len(events)


class _PrintTriggers:
    is_paused = False

    def request_capture(self, reason, delay):
        print(f"           -> capture requested ({reason}, in {delay}s)", flush=True)  # noqa: T201


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seconds", type=float, default=0, help="Stop after N seconds (default: run until Ctrl+C)")
    parser.add_argument("--ask", action="store_true", help="Show the macOS permission prompts")
    parser.add_argument("--types", default="click,app_switch,window_focus,text,clipboard",
                        help="Comma-separated event types to show")
    args = parser.parse_args()

    settings.ui_events_types = args.types

    backend = create_backend()
    if backend is None:
        print("UI events are not supported on this platform.")  # noqa: T201
        return 1

    perms = backend.request_permissions() if args.ask else backend.check_permissions()
    print(f"Input Monitoring: {'yes' if perms.input_monitoring else 'NO'}   "  # noqa: T201
          f"Accessibility: {'yes' if perms.accessibility else 'NO'}")
    if not perms.all_granted:
        print("Grant both to the app that runs this command (e.g. Terminal) in "  # noqa: T201
              "System Settings > Privacy & Security, then run it again.")

    recorder = UiEventRecorder(database=_PrintSink(), capture_worker=_PrintTriggers(), backend=backend)
    if not recorder.start():
        print("Could not start the event tap.")  # noqa: T201
        return 1
    print("Listening. Click, type, switch apps, copy text...", flush=True)  # noqa: T201
    try:
        end = time.time() + args.seconds if args.seconds else None
        while end is None or time.time() < end:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        recorder.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
