# UI events and accessibility text

From the session "Accessibility API for user event collection" (2026-10-06). Design and decisions: [docs/plans/ui-events.md](../plans/ui-events.md).

Done today, all merged into `custom`:

- `2d99814`: UI events on macOS. Clicks, typed text, app switches and clipboard via CGEventTap + AXUIElement. `ui_events` table (migration v8), `activities.user_actions`, event-driven captures.
- `8198161`: cleaner a11y text (no menu bar, visible Terminal text only), `active_url` from the browser itself, Electron window titles.
- `7fa39c5`, `d1c2b23`: Electron title also for per-display labels, from the window on that display.
- `752b04c`: `ui_events.url` (migration v9), user actions in the daily summary and chat context.
- `0fcf92a`: `privacy/url_filter.sanitize_url()` for stored URLs (no query strings, no sign-in/token pages).
- The Windows backend (`87a2358`) came from the Windows laptop and was merged in `af680b7`.

Branches: `claude/accessibility-api-user-events-f6a57d` is fully merged into `custom`. Its copy on the fork (`e57d387`) is stale. Don't build on it.

## Items

### Check the new fields on live data after a restart
Status: open

The main instance ran an older build when this was written. After a restart, check new frames:
- Claude desktop frames get the conversation name as `window_title`, not "Claude".
- `active_url` has no query strings.
- Browser tab switches create `window_focus` rows with `url`.

The questionnaire session's hourly scan covers most of this.

Refs: `MacOSAdapter._best_title()`, `capture_worker._get_browser_url()`, `UiEventRecorder._poll_front()`.

### UI events are off on the main instance
Status: blocked on the user

`ui_events_enabled` is false by default, and the main instance's `settings.json` doesn't set it. So `ui_events` and `activities.user_actions` stay empty, and the workflows feed gets no clicks or app switches. To turn it on, go to Settings → Privacy & Security → UI Events. The app that starts ScreenMind (Terminal) needs Input Monitoring and Accessibility.

### Windows: no browser URL
Status: open

`WindowsAdapter` has no `get_browser_url()`, so `active_url` and `ui_events.url` are always NULL on Windows. Read the address bar with UI Automation, for example the Edit control named "Address and search bar" in Chrome or Edge. Add `https://` when it is missing. Callers already run the result through `sanitize_url()`.

Refs: `screenmind/platform_support/windows.py`, `base.PlatformAdapter.get_browser_url()`, `privacy/url_filter.py`.

### Firefox: probably no URL on macOS
Status: idea

`get_browser_url()` reads `AXURL` from the first `AXWebArea`. Chrome and Safari expose it. Firefox was not tested and may not, which would leave `active_url` NULL there. Test it, and if needed fall back to the address bar field.

Refs: `MacOSAdapter.get_browser_url()`, `BROWSER_APPS`.

### Events go to the first frame of a tick, not the matching display
Status: idea

With several displays, `_link_ui_events()` attaches all pending events to the first frame saved in a tick. A click on display 2 can end up on display 1's frame. Better: link by app or by the window bounds of the event.

Refs: `CaptureWorker._capture_monitor()`, `Database.attach_ui_events()`.

### Electron full accessibility tree: watch for slowdowns
Status: idea

`MacOSAdapter.enable_full_a11y_tree()` sets `AXManualAccessibility` once per process on Electron and Chromium apps (Claude, Slack, VS Code). This made titles and text much better. It may cost some CPU in those apps. If they get slower, limit it to an allowlist of apps.

### macOS permission prompts name Terminal or Python, not ScreenMind
Status: idea

Input Monitoring and Accessibility are granted to whatever launched ScreenMind. A small signed helper `.app` would show "ScreenMind" in the prompts. It only matters for an upstream release.

Refs: plan section "macOS permission problem".

### Encrypt the whole DB
Status: idea

Typed text, `user_actions`, `ocr_text` and `active_url` are plain text in SQLite. Only screenshots can be encrypted. If needed, use SQLCipher for the whole DB, tied to `encryption_enabled`. Encrypting single columns would break FTS and would protect little. Decision 1 in the plan.

### Terminal: store only new lines
Status: idea, low priority

a11y text for Terminal is the visible part (about 3-4 KB per frame). Storing only the lines that are new since the previous frame of the same window would cut repeats. The questionnaire feed doesn't need it now.

Refs: `MacOSAdapter._ax_visible_text()`.
