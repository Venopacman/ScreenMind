# Plan: UI event capture with accessibility APIs (macOS + Windows)

Status: phases 1 to 3 built for macOS, phase 4 built for Windows (see the two "Implementation notes" sections at the end). Phase 5 is not started. Branch: `claude/accessibility-api-user-events-f6a57d`, based on `origin/custom`.

## Goal

Record what the user does, not only what the screen shows. We listen for clicks, typing, app switches and clipboard changes. For each event we ask the accessibility API which UI element it hit. This follows the same model as screenpipe's `screenpipe-a11y` crate.

We get two things from this:

1. **Better captures.** Today `CaptureWorker.run()` polls every 5s and forces a capture every `capture_interval` (40s by default). With events, we can capture right after an app switch, a click, or a pause in typing. Those are the moments when something happened.
2. **Better context for Gemma.** Each frame gets a short "what the user did" block, for example: `clicked button "Send" in Slack`, `typed "fix login bug" in Linear title field`. Chat and search can then answer "what did I send to Alex?" from typed text, not only from OCR.

Linux is out of scope. Wayland blocks global input hooks, and X11 would need a third backend.

## Non-goals

- No raw keylogger. We never store single key codes. Typed text is grouped into chunks per focused field, and password fields are never read.
- No mouse-move stream. Moves are noisy and add little. We record clicks and scrolls only.
- No new UI page in phase 1. The events show up as extra detail in the existing activity view.

## Current state

- `A11yExtractor` ([a11y_extractor.py](../../screenmind/engine/a11y_extractor.py)) reads the text of the focused window once per capture. It does not listen for events.
- macOS AX code lives in [platform_support/macos.py](../../screenmind/platform_support/macos.py). It imports `ApplicationServices` and `Quartz`, but `pyobjc` is **not** in `pyproject.toml`. On a fresh install, macOS a11y is quietly off.
- Windows uses `uiautomation` (in `requirements.txt` only, not in `pyproject.toml`).
- Hotkeys use the `keyboard` package ([hotkey.py](../../screenmind/capture/hotkey.py)). On macOS it needs root, so it mostly does not work there.
- The DB schema uses a versioned migration list in `Database._init_db()`. The last one is v7.
- Only screenshots are encrypted ([encryption.py](../../screenmind/privacy/encryption.py)). Text in SQLite (`ocr_text` and others) is plain. Typed text would be plain too.

## Design

### Threads

Hook callbacks must be fast. Windows silently removes a low-level hook if the callback takes too long (`LowLevelHooksTimeout`). macOS turns the tap off with `kCGEventTapDisabledByTimeout`. Python's GIL makes this a real risk. So we split the work:

```
OS hook thread (run loop / message pump)
   callback: build RawEvent, put on queue.SimpleQueue, return
        |
        v
Enricher thread
   - AX / UIA lookups (with a timeout)
   - group keys into text chunks
   - privacy checks and filter_sensitive_text
   - batch insert into ui_events
   - send capture triggers to CaptureWorker
        |
        v
CaptureWorker (asyncio)
   reads a thread-safe trigger flag, same pattern as _pending_bookmark
```

The hook callback never calls AX, UIA, the DB or the logger.

### New package `screenmind/capture/ui_events/`

| File | What it holds |
|---|---|
| `models.py` | `RawEvent`, `UiEvent` dataclasses and an `EventType` enum: `click`, `scroll`, `text`, `app_switch`, `window_focus`, `clipboard` |
| `base.py` | `UiEventBackend` ABC: `start(queue)`, `stop()`, `check_permissions()`, `request_permissions()`, `element_at(x, y)`, `focused_element()` |
| `macos.py` | CGEventTap + AX backend |
| `windows.py` | Low-level hooks + UIA backend |
| `text_buffer.py` | Groups key presses into text chunks. Pure Python, no OS calls, easy to test |
| `recorder.py` | `UiEventRecorder`: owns both threads, picks the backend, runs the enricher loop |

### macOS backend

- **Input:** `Quartz.CGEventTapCreate(kCGSessionEventTap, kCGTailAppendEventTap, kCGEventTapOptionListenOnly, mask, cb, None)`. The mask covers `LeftMouseDown`, `RightMouseDown`, `ScrollWheel`, `KeyDown`. The tap runs on its own thread with `CFRunLoopRun()`. If the callback gets `kCGEventTapDisabledByTimeout`, it calls `CGEventTapEnable(tap, True)`.
- **Characters:** `CGEventKeyboardGetUnicodeString` in the callback. Skip events with the Cmd or Ctrl flags set, because those are shortcuts and not text. Record Cmd+C, Cmd+X and Cmd+V as hints for the clipboard check.
- **Element under a click:** `AXUIElementCopyElementAtPosition(systemWide, x, y)` in the enricher. Read `AXRole`, `AXSubrole`, `AXTitle`, `AXDescription`, and `AXValue` (cut to 200 chars).
- **Focused field while typing:** the `AXFocusedUIElement` of the system-wide element.
- **Hangs:** call `AXUIElementSetMessagingTimeout(systemWide, 0.25)` once at start.
- **App and window switch:** poll every 500 ms in the enricher, reusing the Quartz frontmost lookup from commit `854c4ba`. This is simpler than `NSWorkspace` notifications, which need a main-thread run loop that we do not have.
- **Clipboard:** poll `NSPasteboard.generalPasteboard().changeCount()` every 1s. Read the string type only.
- **Permissions:** Input Monitoring (`CGPreflightListenEventAccess` / `CGRequestListenEventAccess`) and Accessibility (`AXIsProcessTrustedWithOptions`). These are two separate grants.
- **Dependencies:** add `pyobjc-framework-Quartz`, `pyobjc-framework-ApplicationServices` and `pyobjc-framework-Cocoa` to `pyproject.toml`, with `sys_platform == "darwin"`. This also fixes the existing a11y text extraction on fresh installs.

### Windows backend

- **Input:** `ctypes` `SetWindowsHookExW(WH_KEYBOARD_LL)` and `SetWindowsHookExW(WH_MOUSE_LL)` on one thread with a `GetMessageW` loop. The callback enqueues and returns `CallNextHookEx`. Skip events flagged `LLKHF_INJECTED` / `LLMHF_INJECTED`, so we do not record our own or other tools' synthetic input.
- **Characters:** `ToUnicodeEx` with flag `0x4` (do not change keyboard state). Without it, dead keys break in the user's apps. The flag needs Windows 10 1607 or later. On older builds we only record "typing happened", without the text.
- **App switch:** `SetWinEventHook(EVENT_SYSTEM_FOREGROUND, ..., WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)` on the same message-loop thread.
- **Element under a click:** `uiautomation.ControlFromPoint(x, y)` in the enricher. The enricher thread needs its own COM init (`uiautomation.UIAutomationInitializerInThread`). Read `ControlTypeName`, `Name`, `AutomationId`, and the value pattern (cut to 200 chars).
- **Password check:** UIA `IsPassword`.
- **Clipboard:** poll `GetClipboardSequenceNumber()` every 1s.
- **Dependencies:** move `uiautomation` into `pyproject.toml` with `sys_platform == "win32"`.

### Text grouping (`text_buffer.py`)

- One buffer per focused element (key: app + element role + element name).
- Flush on: 2s with no typing, Enter, Tab, focus change, app switch, or 500 chars.
- Backspace removes the last char from the buffer. Arrow keys and mouse clicks inside the field flush the buffer, because we can no longer tell where the cursor is.
- Each flush becomes one `text` event.

### Privacy rules

These apply in the enricher, before anything reaches the DB:

1. **Off by default.** New setting `ui_events_enabled: bool = False`. It is turned on in Settings with a clear note that it records typed text.
2. **Password fields.** If the focused element has subrole `AXSecureTextField` (macOS) or `IsPassword` (Windows), drop the typed text. Store only `text` = `[password field]`, with no element value. Screenpipe does the same.
3. **Blocked apps.** Drop every event when the frontmost app is in `settings.blocked_apps_list`. This is the same check `_capture_tick` uses.
4. **Paused capture.** When `CaptureWorker` is paused, the recorder drops events. The hook stays installed, so resuming is instant.
5. **Sensitive data.** Run `filter_sensitive_text()` on typed text, clipboard text and element values.
6. **Clipboard size.** Store at most 1,000 chars of clipboard text.
7. **Per-type switches.** `ui_events_types` lists which types to record, default `click,app_switch,text,clipboard`. A user can turn off `text` and still get clicks and app switches.

### Storage

Migration v8 in `Database._init_db()`:

```sql
CREATE TABLE IF NOT EXISTS ui_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    type TEXT NOT NULL,
    app_name TEXT,
    window_title TEXT,
    element_role TEXT,
    element_name TEXT,
    element_value TEXT,
    text TEXT,
    x INTEGER,
    y INTEGER,
    activity_id INTEGER REFERENCES activities(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_ui_events_ts ON ui_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_ui_events_activity ON ui_events(activity_id);
```

New `Database` methods:

- `insert_ui_events(events: list[UiEvent])`: one batch insert. The enricher flushes every 2s or every 50 events.
- `attach_ui_events(activity_id, until_ts)`: sets `activity_id` on all events with `activity_id IS NULL AND timestamp <= until_ts`. `CaptureWorker` calls this right after `insert_activity`.
- `get_ui_events(activity_id)` and `get_ui_events_range(start, end)`.
- `cleanup_old_data()` also deletes old `ui_events` rows.

### Event-driven captures

`CaptureWorker` gets `request_capture(trigger: str)`. It sets a thread-safe flag that the main loop checks, the same way `trigger_bookmark()` works.

| Trigger | When the recorder fires it |
|---|---|
| `app_switch` | 1s after the frontmost app changes, so the new window can finish drawing |
| `click` | 1.5s after a click that is not inside a text field |
| `typing_pause` | When a text chunk is flushed |

Limits:

- At most one event-driven capture every 3s. This keeps the GPU queue under control.
- The existing dedup (`ScreenDeduplicator`) still runs. A click that changes nothing on screen still produces no frame.
- The periodic `capture_interval` capture stays as a fallback.
- New setting `event_triggered_capture: bool = True`. It only has an effect when `ui_events_enabled` is on.

### Gemma context

In `AnalysisWorker`, before calling `analyze_fn`, load the events attached to the activity. Turn them into at most 10 short lines:

```
User actions before this screenshot:
- switched to Slack
- clicked button "Send" (Slack: #team-backend)
- typed "will push the fix today" in message field
```

Pass this to the analyzer as a new `user_actions` hint, next to `window_title` and `ocr_text`. It goes into the prompt the same way the other hints do. Keep it under about 300 tokens, because context size has caused problems before (see commit `6a28f46`).

### API and UI

- `GET /api/activity/{id}` adds a `ui_events` list.
- New `GET /api/ui-events?start=&end=&type=` for the MCP server and agents.
- In the activity detail panel, show a small "Actions" list under the screenshot.
- In Settings, add the toggles plus a permission status line. On macOS it shows whether Input Monitoring and Accessibility are granted, with a button that calls `request_permissions()`.
- Typed text and clipboard text go into the existing FTS5 index, so keyword search finds them.

### Startup

In `main()`, after `CaptureWorker` is created:

```python
if settings.ui_events_enabled:
    ui_recorder = UiEventRecorder(database=db, capture_worker=capture_worker)
    ui_recorder.start()  # logs a warning and does nothing if permissions are missing
```

Stop it in the shutdown handler before closing the DB.

## macOS permission problem

macOS grants Input Monitoring and Accessibility to an app bundle or a binary, not to a Python package. With `pip install screenmind`, the prompt names `Python` or the terminal app. Users will not know what they are approving. Options:

1. **Accept it for now.** Show clear text in Settings: "macOS will ask for permission for Python / Terminal. This is ScreenMind." This is the cheapest option and enough for personal use on the `custom` branch.
2. **Small signed helper app.** A tiny `.app` that runs the hooks and sends events to the main process over a local socket. The grant then says "ScreenMind". This is a lot more work (signing, notarization, IPC).

Recommendation: option 1 now. Revisit option 2 only if this goes upstream.

## Phases

Each phase can ship on its own.

**Phase 1: macOS, events stored, no triggers**
- `ui_events` package with the macOS backend, `text_buffer.py`, `recorder.py`
- Migration v8 and the DB methods
- Privacy rules 1 to 7
- Settings `ui_events_enabled` and `ui_events_types`
- pyobjc dependencies
- Done when: with the setting on, clicks, app switches, typed text and clipboard show up in `ui_events`, and a password field stores `[password field]`.

**Phase 2: link to frames and use in Gemma**
- `attach_ui_events` in `CaptureWorker`
- `user_actions` hint in the analyzer prompt
- `GET /api/activity/{id}` returns events, Actions list in the UI
- FTS indexing of typed and clipboard text
- Done when: an activity shows its actions, and the Gemma summary mentions them.

**Phase 3: event-driven captures**
- `request_capture()` and the three triggers, with the 3s limit
- `event_triggered_capture` setting
- Done when: switching apps produces a frame within about 2s, and the frame count per hour does not grow by more than about 30% on a normal workday.

**Phase 4: Windows backend**
- `windows.py` with hooks, WinEvent and UIA
- Move `uiautomation` into `pyproject.toml`
- Done when: phase 1 to 3 checks pass on Windows 10 and 11.

**Phase 5 (optional): replace `keyboard` for hotkeys**
- Run hotkey detection on top of the same hook thread. This removes the root requirement on macOS.

## Tests

- `text_buffer.py`: unit tests for flush rules, backspace and the 500-char limit. No OS needed.
- Enricher: a fake backend that feeds `RawEvent`s. Check the privacy rules (password, blocked app, paused, filter), batching, and the trigger rate limit.
- DB: migration v8 on an existing v7 DB (same style as `test_migration_v7.py`), `attach_ui_events`, and retention cleanup.
- Analyzer: the `user_actions` hint is cut to 10 lines and to the token budget.
- Platform backends: a live probe, `python -m screenmind.capture.ui_events`, prints events as they happen. CI cannot grant macOS permissions or run Windows hooks in a useful way, so the backends are tested by hand.

## Open questions

1. Should typed text be encrypted at rest? Today no DB text is encrypted. If yes, it should be a separate change that covers `ocr_text` too.
2. Should we store scroll events? They help to say "the user read this page" but add many rows. Proposal: off in phase 1, and record one `scroll` event per 2s burst if we add it later.
3. Browser URL per event. Screenpipe reads it from AX for some browsers. We already have `active_url` on activities. Is that enough for now?

## Implementation notes (macOS, phases 1 to 3)

What changed compared to the plan above:

- **Typing is recorded only in text inputs.** The focused element must be `AXTextField`, `AXTextArea`, `AXComboBox` or `AXSearchField`. Keys sent to windows, lists or games are dropped. If the focused element is unknown, text is dropped too, because we cannot rule out a password field.
- **Unnamed clicks stay out of the Gemma hint.** "clicked group in Slack" says nothing the screenshot doesn't. These clicks are still stored, and they still trigger a capture.
- **Container names are dropped.** Chrome puts the whole page text into an `AXGroup` description. Names on container roles longer than 60 chars are dropped, and all names are cut to 100 chars.
- **Buttons without a name** take the label from a child `AXStaticText` or from `AXHelp`.
- **Electron and Chromium apps.** The first time we see a process, we set `AXManualAccessibility` on it, so it exposes its full accessibility tree.
- **Clipboard.** Pasteboards marked `org.nspasteboard.ConcealedType`, `TransientType` or `AutoGeneratedType` are skipped. Password managers use these.
- **Linking events to frames.** Right before a frame is saved, the capture worker flushes the recorder and runs `attach_ui_events`. The bullet list is stored in a new `activities.user_actions` column, which is part of FTS5. Typed text is therefore searchable.
- **Probe:** `python -m screenmind.capture.ui_events [--types click,app_switch] [--seconds N] [--ask]`.
- **API:** `GET /api/ui-events/status`, `POST /api/ui-events/permissions`, `GET /api/ui-events?start=&end=&type=`. `GET /api/activity/{id}` returns `ui_events`.
- **Cache tiers.** If the per-app pHash cache finds an identical frame, Gemma does not run, so the actions hint is not used for that frame. The actions are still stored, shown in the UI and searchable.

Checked end to end on macOS with a separate instance (port 7778, separate data dir):

- The event tap installs, and clicks, app switches and typing arrive.
- Events link to the next saved frame, and `user_actions` is filled in.
- `[event:click]` captures fire.
- The timeline shows the Actions list.
- The Settings toggle starts and stops the recorder.
- Shutdown removes the tap and does not stop a llama-server it did not start.

## Windows implementation notes (phase 4)

Built in `screenmind/capture/ui_events/windows.py` (`WindowsUiEventBackend`). Tested on Windows 11 (build 26200), Python 3.12, uiautomation 2.0.29. What differs from the plan, or is worth knowing:

**Input hooks**

- `WH_KEYBOARD_LL` and `WH_MOUSE_LL` run on one thread with a `GetMessageW` loop. `stop()` posts `WM_QUIT` to that thread. The thread creates its message queue (`PeekMessageW`) before it reports ready, so `WM_QUIT` is never lost.
- The callbacks only build a `RawEvent`, put it on the queue and return `CallNextHookEx`. Measured cost: about 13 µs per key press (including `ToUnicodeEx`) and under 1 µs per mouse move, far below `LowLevelHooksTimeout`.
- Watchdog: Windows removes a hook that times out, without telling us. A 10 s timer on the hook thread compares each hook's last callback with `GetLastInputInfo`. If a hook saw nothing for 30 s while input arrived, it is installed again. The new hook goes in before the old one comes out. The keyboard hook also gets re-installed while you only use the mouse; that false alarm costs nothing. Tested by removing the mouse hook behind the backend's back.
- Injected input (`LLKHF_INJECTED`, `LLMHF_INJECTED`) is skipped, so input from remote tools, macro tools and our own tests is not recorded.
- ctypes uses a private `WinDLL("user32")` with its own argtypes. The `keyboard` package sets argtypes on the shared `ctypes.windll.user32`, and sharing would break one of the two.

**Keys**

- Characters come from `ToUnicodeEx` with flag `0x4` (keyboard state unchanged), in the keyboard layout of the foreground window's thread. Shift comes from `GetAsyncKeyState`, Caps Lock from `GetKeyState`. Caps Lock's toggle bit is correct even on a thread that reads no input (tested).
- Dead keys: flag `0x4` keeps them working in the user's app, but Windows then does not combine them for us. `DeadKeyState` keeps the pending accent and composes it with the next letter (`´` + `e` = `é`; accent + space = the accent; no precomposed form = both characters).
- Shortcuts: Ctrl or Win held means `shortcut=True`. Two choices beyond the plan: Ctrl+Alt is AltGr on European layouts and types text (`@`, `€`), so it is **not** a shortcut. Alt alone drives menus (Alt+F), so it **is** a shortcut. `shortcut_char` comes from the virtual key, so Ctrl+C is `c` on any layout, Russian included.
- Modifier, Caps Lock and Num Lock presses on their own are not queued.
- Not covered: IME input (Chinese, Japanese, Korean) arrives as `VK_PROCESSKEY` and is not recorded as text.

**Elements (UI Automation)**

- `element_at` uses `uiautomation.ControlFromPoint`; `focused_element` uses `GetFocusedControl`. Both run on the enricher thread. COM is set up lazily, once per thread, with a `UIAutomationInitializerInThread` kept in a thread-local, so it is released on that thread when it ends.
- UIA's own timeouts are cut from 20 s to 1 s (`IUIAutomation2.ConnectionTimeout` / `TransactionTimeout`), so a hung app cannot freeze the enricher. The setting is global to the process; the a11y text extractor shares it.
- DPI: low-level hooks report per-monitor physical coordinates. `start()` makes the process per-monitor DPI aware (`SetProcessDpiAwareness(2)`, the same mode `mss` sets), so click points match UIA. `ui_events.x/y` are physical pixels on Windows and points on macOS.
- Roles: uiautomation reports `EditControl`, `DocumentControl` and so on. `normalize_uia_role()` in `models.py` drops the suffix, so roles are `Edit`, `Document`, `Button` and match `TEXT_INPUT_ROLES`. `role_label()` has Windows labels (`Edit` -> "text field", `Hyperlink` -> "link") and splits other CamelCase names ("list item").
- **Read-only documents are not text inputs.** A browser page is a UIA `Document`. Without a check, shortcut keys on a page (j/k, space) would be stored as typed text. `ElementInfo` has a new `editable` field, set from the value pattern's `IsReadOnly`, and `is_text_input` needs `editable is not False`. A `Document` without a value pattern counts as read-only. macOS leaves `editable` as `None`, so nothing changes there.
- Checked on a test page in Edge: text input, password input and `contenteditable` (all `Edit`; the password one with `is_password`), read-only textarea (not a text input), text button, icon-only button (name from `aria-label`), link, plain text, and the page itself (read-only `Document`). Windows 11 Notepad's editor is an editable `Document`.
- Names and values follow the macOS rules: text-input values are never read, container names over 60 chars are dropped, names are cut to 100 chars and values to 200. Unnamed buttons take a child's name or `HelpText`. A click on a label or icon walks up to 3 parents to find the control.

**App switches and app names**

- Polled every 0.5 s through the new `WindowsAdapter.get_front_window()` (pid, name and title from one `HWND`), the same way as macOS. `SetWinEventHook(EVENT_SYSTEM_FOREGROUND)` was not needed, because the trigger waits 1 s anyway.
- App names are executable names without `.exe` (`chrome`, `notepad`). That is what the capture worker's blocked-app check uses, so `blocked_apps` works the same for frames and for UI events.
- The adapter now opens processes with `PROCESS_QUERY_LIMITED_INFORMATION`. The old `PROCESS_QUERY_INFORMATION | PROCESS_VM_READ` failed for elevated (admin) apps, which then had no name and could not be blocked.
- UWP apps (Calculator, Settings) run inside `ApplicationFrameHost`. The front window then reports `ApplicationFrameHost`, while a click inside reports the app's own process.

**Clipboard**

- `GetClipboardSequenceNumber()` is the change count. `read_clipboard` opens the clipboard (3 tries, 20 ms apart) and returns `None` when the owner marked the content: `ExcludeClipboardContentFromMonitorProcessing` is present, or `CanIncludeInClipboardHistory` / `CanUploadToCloudClipboard` has the value 0. KeePass, 1Password and Bitwarden set these. Only `CF_UNICODETEXT` is read.

**Permissions and wiring**

- No permissions are needed: `check_permissions()` returns granted. `/api/ui-events/status` now includes `backend` ("macos" / "windows"), and Settings shows "No permissions needed" instead of the macOS lines when it is not macOS.
- `create_backend()` returns the Windows backend on `win32`. The constructor is cheap (no `uiautomation` import, no DPI change), because it runs at every startup, even with UI events off.
- `uiautomation` is now a core dependency on `win32`, like pyobjc on darwin. The `windows` extra stays, so existing install commands work.
- Hotkeys: the `keyboard` package and our hooks were tested together in both install orders. Hotkeys fire, and our hook still sees every key. Note for tests: `keyboard` matches keys by scan code and ignores injected keys while Alt is down, so synthetic tests must send real scan codes and avoid Alt.

**Tests**

- `tests/test_ui_events_windows.py` needs no OS: key mapping, shortcut rules, dead-key composition, role normalization and labels, clipboard secret formats with fakes, element parsing with fake UIA controls (password, read-only document, child labels, label-to-button walk), and the recorder with Windows-style elements (typing in `Edit` kept, typing on a read-only page dropped, password placeholder, blocked app by exe name). The module imports on any OS.
