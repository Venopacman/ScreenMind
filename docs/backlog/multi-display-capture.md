# Multi-display capture

From the session "Call window visibility with multiple apps" (2026-10-06).

Done today, on `custom`:

- `cd7d618`: capture every display, one entry per display (`CAPTURE_ALL_MONITORS`, default on)
- `117e37e`: an empty display doesn't borrow the focused app's label
- `1ca9352`: empty displays are stored as `idle` without a Gemma call
- `d3940e7`: `scripts/dev-instance.sh` and `CLAUDE.md` for running worktree builds next to the main instance

## Items

### Calls that are not the focused window are not detected
Status: open

Today a Slack huddle (about 11:14-11:31, activity ids 240-266) and a Google Meet call (12:02-12:14, ids 287-300) left `meetings` empty. There are three causes:

- `CaptureWorker.run()` passes only the focused app to `AudioWorker.check_meeting()`. The call usually sits on the other display.
- On macOS the app name is the window owner ("Google Chrome"), so `meeting_apps` entries like "meet" never match a Meet tab. The window title ("Meet - ...") would.
- `_is_meeting_process_alive()` uses `tasklist`, which only exists on Windows.

Fix idea: detect a call from any visible window, matching on owner and title, and record start and end times. Decide whether call tracking should work while `MEETING_TRANSCRIPTION` is off.

Refs: `screenmind/workers/audio_worker.py`, `screenmind/workers/capture_worker.py` (`run`), `MacOSAdapter._front_window()`.

### Per-frame list of visible apps and an "active call" flag
Status: idea

The workflows/questionnaire work needs to know which apps were visible in each frame, and whether a call was running. That way call time and participants count even while the user types in another app. Store the visible layer-0 windows (owner, title, bounds) per activity. Mark frames taken during a call.

Depends on the item above. Needs a DB migration, so check the next free version number first: v9 is the last one used.

### Linux: no per-display app lookup
Status: open

`get_top_window_in()` and `can_find_top_window` exist on macOS and, since `16f933a`, on Windows. On Linux X11 the display without focus is still saved with no app name, so Gemma guesses it, or the idle rule fires when there is no text. Implement both in `LinuxAdapter`, for example with `xdotool search --onlyvisible` plus window geometry, or with python-xlib. Wayland can't do this.

Refs: `screenmind/platform_support/linux.py`, `base.py`, `CaptureWorker._label_monitor()`.

### Focus-only mode: label and image can disagree on a focus switch
Status: open, low priority

With `CAPTURE_ACTIVE_MONITOR=true` and all-displays off, the app name and the display are read a moment apart. Activity 312 (12:24:57) is labeled "Claude" but shows Slack on the laptop. Fix: read the front window once per tick and use it for both the app name and the monitor choice.

Refs: `ScreenCapture._get_active_monitor()`, `CaptureWorker._capture_tick()`.

### No dashboard toggle for CAPTURE_ALL_MONITORS
Status: idea

It can only be set in `.env`. `capture_active_monitor` has a switch in Settings. Add `capture_all_monitors` to `_ALLOWED_OVERRIDES` in `config.py`, `api/routes/settings.py` and `static/js/settings.js`. Explain that it wins over the active-monitor switch.

### Analysis cost with several displays
Status: idea

When both screens change, there are up to 2 Gemma runs per tick (about 8 s each on the laptop). If the queue backs up, frames older than 3 minutes are skipped. Watch `cache_skips` in `/api/status`. If it grows, consider analyzing the display without focus less often.

### Leftover test setting
Status: open, trivial

`~/.screenmind/settings.json` still has `capture_active_monitor: true` from the focus-only test. All-displays mode wins, so it has no effect. Set it back to false via Settings or `POST /api/settings`, after asking the user.

### Dev instance: small rough edges
Status: idea

- Shutdown takes about a minute.
- The port comes from a hash of the worktree name mod 100, so two worktrees can collide. `dev-instance.sh info` shows the port. Nothing detects a collision except the "port in use" exit.
- New worktrees may start from `main`, not `custom`. `CLAUDE.md` says to reset them, but a base-branch setting in the app would be better.

Refs: `scripts/dev-instance.sh`, `CLAUDE.md`.
