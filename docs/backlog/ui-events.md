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

### PII detection before storing typed text and clipboard
Status: open

Typed text and clipboard are off by default, because they can hold private content: messages, names, addresses, health or money details. Today only passwords and known secret patterns are removed (`privacy/data_filter.py`: cards, SSNs, API keys, JWTs, passwords). Before `text` and `clipboard` can be on by default, add PII detection that runs before a row is stored. For example names, emails, phone numbers, addresses, and free-form private messages. Then decide again whether to add `text,clipboard` to the default `ui_events_types`.

Refs: `UiEventRecorder._add()` (the one place every text field is filtered), `privacy/data_filter.py`, `config.ui_events_types`.

### Firefox: probably no URL on macOS
Status: idea

`get_browser_url()` now uses `_ax_page_url()`: exactly one top-level `AXWebArea` with an http(s) `AXURL`, so docked DevTools and side panels no longer hide or replace the URL. Firefox is still untested (not installed on this Mac). If Firefox exposes no `AXURL`, `active_url` stays NULL there; then fall back to the address bar field.

Refs: `MacOSAdapter._ax_page_url()`, `_ax_page_areas()`, `BROWSER_APPS`.

### Chrome shows its page tree only after another client turns it on
Status: idea

Chrome ignores `AXManualAccessibility`, which `enable_full_a11y_tree()` sets. Its web content is in the AX tree only when some accessibility client has turned it on (VoiceOver, or another app that sets `AXEnhancedUserInterface`). Otherwise there is no `AXWebArea`: no `active_url` and no page text from a11y (OCR still works). Chrome rows with a URL: 135 of 225 on 2026-10-06, 0 of 72 on 2026-10-05, none after a reboot on 2026-10-07. Setting `AXEnhancedUserInterface` on browsers would fix it, but it is known to slow some apps and break window managers, so test it first. Windows does not have this problem: UIA clients turn the tree on.

Refs: `MacOSAdapter.enable_full_a11y_tree()`.

### Capture side should mark "no app window on this display" explicitly
Status: open, after the ScreenCaptureKit merge

The empty-display rule in `analysis_worker` skips Gemma only when a frame has no app, no title and under 20 chars of text (menu bar and clock not counted). A desktop with widgets and file icons has more text than that, so it still goes to Gemma. Activity 690 (macOS desktop, weather and calendar widgets, file names) came back as "File Explorer/Desktop". The capture side knows when it found no app window on a display (`_label_monitor` returns `None, None, False` when `can_find_top_window()` is true). It should pass that on as a flag, and the analysis worker should store such frames as idle. Don't use app=None alone as the signal: on Linux, an unfocused display also has app=None, and it can show real content.

Refs: `CaptureWorker._label_monitor()`, `_capture_monitor()`, `analysis_worker._screen_text_len()`, activity 690.

### UI events linked to a frame hours later or of another app
Status: done 2026-10-08. v13 ran on the main DB after the restart: 0 links to another app, max lag 5.0 min.

`ui_events.activity_id` had no time limit, and builds before `cf72b8e` did not check the app. In the Mac DB on 2026-10-08, 424 of 974 linked events pointed to a frame of another app or one saved over 5 min later. The worst: a Terminal `app_switch` and `click` at 2026-10-07 18:58 linked to a Chrome frame at 09:49 the next day. Most of the rest were Chrome events (display 2) linked to a Slack frame (display 1). These links also went into `activities.user_actions`.

Fix: `attach_ui_events()` now skips events older than `UI_EVENT_MAX_LINK_LAG` (5 min) before the frame. App matching was already there (`cf72b8e`). Migration v13 (`_unlink_stale_ui_events`) unlinks old links that break either rule and rebuilds `user_actions` for those 62 frames. Summaries Gemma already wrote from the wrong actions stay as they are.

Refs: `Database.attach_ui_events()`, `_unlink_stale_ui_events()`, `UI_EVENT_MAX_LINK_LAG`.

### Same app on two displays: events without a point go to the first frame
Status: idea

Events link by app, and clicks by position (G36 fix). With the same app on several displays, events without a point (typing, app switches, clipboard) still go to the first frame of that app saved in a tick (G21). Better: prefer the focused display, or the window the event came from.

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
