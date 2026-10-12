# Evolve Capture: System Architecture

Evolve Capture is a local screen activity recorder. It collects actions and does the minimal analysis needed to dedup, unify and label them. It is a data source for a workflows app.

Frames are labelled by rules from the OS app name, the window title and the URL. No model runs. A local Gemma model did this until 2026-10-08. It was removed because its notes added nothing to workflow extraction and it was the largest resource cost.

This file gives the overview. The detailed, per-OS map of what is captured, and how, is [docs/architecture/capture.md](docs/architecture/capture.md). When the two disagree, capture.md follows the code and wins.

---

## 1. System Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                               Evolve Capture                                │
│                                                                         │
│  ┌──────────────┐   ┌────────────┐   ┌───────────────────────────────┐  │
│  │ Capture      │──▶│ asyncio    │──▶│ Analysis Worker               │  │
│  │ Worker       │   │ Queue      │   │                               │  │
│  │              │   └────────────┘   │ per-app cache → a11y / OCR    │  │
│  │ • grab per   │                    │ → data filter → layout text   │  │
│  │   display    │                    │ → rule labels → store         │  │
│  │ • pHash dedup│                    └───────────────┬───────────────┘  │
│  │ • window,    │                                    │                  │
│  │   a11y, URL  │   ┌────────────┐   ┌────────────┐  │                  │
│  └──────┬───────┘   │ UI event   │   │ Audio      │  │                  │
│         │           │ recorder   │   │ Worker     │  │                  │
│         │           │ (optional) │   │ • calls    │  │                  │
│         │           │ • clicks   │   │ • start,   │  │                  │
│         │           │ • app sw.  │   │   end, URL │  │                  │
│         │           └─────┬──────┘   └─────┬──────┘  │                  │
│         ▼                 ▼                ▼         ▼                  │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │ SQLite (WAL) + FTS5: activities, ui_events, meetings              │  │
│  │ screenshots/YYYY-MM-DD/*.jpg                                      │  │
│  └─────────────────────────────────┬─────────────────────────────────┘  │
│                                    │                                    │
│  ┌─────────────────────────────────┴─────────────────────────────────┐  │
│  │                 FastAPI REST Server (127.0.0.1:7777)              │  │
│  │  /timeline · /search · /stats · /meetings · /ui-events · /export  │  │
│  │                                                                   │  │
│  │  ┌─────────────────────────────────────────────────────────────┐  │  │
│  │  │              Web Dashboard (Vanilla JS SPA)                 │  │  │
│  │  │  Timeline · Search · Analytics · Meetings · Settings        │  │  │
│  │  └─────────────────────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
```

**Core principle:** everything runs locally. The network is used only to download the OCR models. There is no telemetry.

**Removed features.** Rewind, chat, voice memos, agents, bookmarks, daily summaries, the MCP server, integrations, global hotkeys, semantic search, git context and the dashboard PIN were removed (`57cf0dd`). Migration v11 dropped their data. See [docs/plans/strip-to-actions.md](docs/plans/strip-to-actions.md).

---

## 2. Analysis Pipeline

```
Screenshot (+ window info, a11y text and URL read at grab time)
    │
    ├──▶ Per-app pHash cache: identical → copy last frame's text, skip OCR
    │
    ├──▶ RapidOCR (CPU), only when the a11y text is not real content
    │
    ├──▶ Sensitive-data filter on text and OCR boxes
    │
    ├──▶ Empty screen (no app, no title, little text) → category idle
    │
    ├──▶ Layout analyzer: OCR boxes clustered into screen regions
    │
    └──▶ Rule labels (engine/labels.py): app_name, activity_category

    All results → SQLite + FTS5. The row gets status='ok'.
```

One model runs: **RapidOCR**. It reads text from pixels (PP-OCR models on ONNX Runtime, CPU). Labels come from rules.

`analysis_method` is `rules`, `cache:identical` or `rule:empty_screen`. Rows analyzed by the idle backfill get a `backfill:` prefix. `summary`, `details`, `mood`, `confidence`, `scene_description` and `visible_text` stay empty on new rows. Rows from before 2026-10-08 keep Gemma's values.

---

## 3. Component Deep-Dive

### 3.1 Capture Worker

`workers/capture_worker.py`. Details per OS are in [capture.md, section 4.1](docs/architecture/capture.md#41-screenshots).

| Property | Detail |
|---|---|
| **Method** | macOS: ScreenCaptureKit, then `screencapture`, then mss. Windows: mss. Linux: mss on X11, grim or XDG Portal on Wayland |
| **Displays** | One grab per display per tick (`CAPTURE_ALL_MONITORS`, on by default) |
| **Timing** | Acts about every 5 s. A change grab if 10 s passed since the last saved frame. A periodic grab every `CAPTURE_INTERVAL` (default 10 s) |
| **Event-driven** | With UI events on: a grab right after an app switch, click, Enter in a text field or page change |
| **Deduplication** | pHash per display (`capture/dedup.py`). Hamming distance 8 or less is a duplicate: the JPEG is deleted, no row is written |
| **Window info** | App name and window title per display |
| **A11y text and URL** | Focused display only, read right after the grab. Dropped if the focused window changed in between |
| **Privacy** | Blocked apps are not grabbed. Heavy apps pause the tick. Window titles go through the sensitive-data filter |
| **Output** | JPEG (`SCREENSHOT_QUALITY`, default 70) → `~/.evolve-capture/screenshots/{date}/{time}.jpg`, plus an `activities` row with `status='pending'` |
| **Encryption** | Optional Fernet encryption in place after save |

```
Capture tick (per display):
┌──────────┐   ┌──────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────┐
│ app +    │──▶│ grab +   │──▶│ pHash dedup  │──▶│ a11y text +  │──▶│ DB row + │
│ title    │   │ JPEG     │   │              │   │ URL (focused)│   │ queue    │
└──────────┘   └──────────┘   └──────┬───────┘   └──────────────┘   └──────────┘
                                     │ duplicate?
                                     └──▶ delete file, stop
```

### 3.2 Analysis Worker: Per-App Cache

`workers/analysis_worker.py`. Most frames look like the last frame of the same app. The cache avoids repeat OCR. It is keyed by `(app_name, window_title[:100])` and compares pHash with the last analyzed frame for that key.

| Tier | When | What runs |
|---|---|---|
| Identical | Distance 3 or less | No OCR. Copy the cached text. Labels still come from rules. |
| Full | Anything else | Everything |

Cache: LRU `OrderedDict`, at most 30 entries.

Other rules (see [capture.md, section 4.9](docs/architecture/capture.md#49-analysis)):

- A frame with no app, no title and under 20 chars of real text is stored as `idle` (`analysis_method='rule:empty_screen'`).
- A frame older than 180 s when its turn comes is marked `skipped`. Its a11y text and URL are kept. The idle backfill analyzes `pending`, `skipped` and `failed` rows from today later.

### 3.3 Rule Labels

`engine/labels.py`. `label_frame(app_name_hint, window_title, url)` returns `app_name` and `activity_category`. No model runs.

App name (first non-empty wins):

| Level | Source |
|---|---|
| L0 | OS app name, where the platform adapter trusts it (macOS) |
| L1 | Title suffix after `" - "` (`main.py - Visual Studio Code`) |
| L2 | OS process name, unless it is a generic wrapper (`electron`, `java`) |
| L3 | A short clean title (`Calculator`) |
| L4 | The raw OS name, else `unknown` |

Category:

- Browser windows: the site from the URL host (`SITE_CATEGORIES`), else words in the title, else the app's known category, else `browsing`.
- Other apps: `KNOWN_APP_CATEGORIES`, else `other`.

### 3.4 Layout Analyzer

`engine/layout_analyzer.py` turns raw OCR boxes into text grouped by screen region:

```
Input: OCR boxes with (x, y, width, height, text, confidence)

Process:
  0. cluster_ocr_layout(): cluster the boxes into regions by position
     [{"name": "nav_sidebar", "x_start": 0.0, "x_end": 0.15, ...}]
  1. Sort regions narrow-first (sidebar before main content)
  2. Put each OCR box into a region by its center point
  3. In chat-like regions: detect timestamps and attribute messages to senders
  4. In other regions: group by Y position (25px) into lines

Output (organized_text):
  [NAV SIDEBAR]
  Home | Messages | Settings

  [CHAT MESSAGES]
  Alice: Hey, did you push the fix? | What's the status?
  Bob: Just merged it | Tests passing now
```

### 3.5 UI Events

`capture/ui_events/`. macOS and Windows only. Clicks, app switches, typed text and clipboard are on by default. Window focus is opt-in. Password fields are never read.

```
OS hook thread → queue → enricher thread → ui_events table
                                  └──▶ CaptureWorker.request_capture()
```

Events before a frame are linked to it in `activities.user_actions`. Details: [capture.md, section 4.7](docs/architecture/capture.md#47-ui-events).

### 3.6 Audio Worker: Calls

`workers/audio_worker.py` and `workers/call_detection.py`. Calls are always tracked. No audio is recorded.

```
Detection (own thread, every 5 s, also while capture is paused):
  visible windows + apps using the mic (CoreAudio, macOS)
      │
      ▼
  match_call() rules for MEETING_APPS (zoom, teams, meet, webex, slack, discord)
      │
      ├── 2 detections in a row → meetings row (app, title, room URL)
      └── not seen for 120 s    → call ends
```

Reading which apps use the mic needs no mic permission. Call transcription and summaries (Gemma audio) were removed on 2026-10-08. Older meetings keep their transcript and summary. Details: [capture.md, section 4.8](docs/architecture/capture.md#48-calls-and-meetings).

### 3.7 Export

`evolve_capture/export/` and `api/routes/export.py`. `GET /api/export` builds a zip for a user and a date range: one folder per day, one text file per work session (split by idle gaps), JSONL copies and the screenshots. Only `status='ok'` rows go out. URLs and text are filtered again on the way. The route accepts only local clients. The dashboard has a Data Export card in Settings → Storage.

Format: [docs/export-format.md](docs/export-format.md).

---

## 4. Storage Layer

### SQLite schema (WAL mode, FTS5), version v11

Migrations live in `Database._init_db()` (`storage/database.py`). Version = list index + 1. The latest is **v11**. It dropped `activities.embedding`, `activities.bookmarked`, `dev_contexts` and `daily_summaries`.

```sql
activities (
    id, timestamp, screenshot_path, window_title, detected_app,
    app_name, category, summary, details, visible_text,
    mood, confidence, ocr_text, ocr_boxes (JSON),
    scene_description, organized_text, analyzed, analysis_error,
    analysis_method, active_url, status, user_actions, created_at
)

ui_events (
    id, timestamp, type, app_name, window_title, element_role,
    element_name, element_value, text, x, y,
    activity_id → activities(id) ON DELETE SET NULL, url
)

meetings (
    id, start_time, end_time, app_name, duration_minutes,
    transcript, summary, window_title, url, created_at
)

-- FTS5 virtual table for keyword search
activities_fts (summary, details, ocr_text, app_name, scene_description,
                organized_text, user_actions)

schema_version (version)
```

`activities.status` is one of `pending`, `ok`, `skipped`, `failed`, `dead`. Column-by-migration details are in [capture.md, section 5](docs/architecture/capture.md#5-storage).

### Search: keyword only

```
User query: "auth error"
      │
      ├──▶ FTS5 MATCH on activities_fts (status='ok' rows) → ranked matches
      │
      └──▶ LIKE search on meetings.transcript and meetings.summary (old rows)

      Merge → return top N
```

The dashboard highlights matching OCR boxes on the screenshot (`/api/screenshot/{id}/highlight`).

---

## 5. Privacy & Security Layer

```
┌─────────────────────────────────────────────────────────┐
│                    Privacy Pipeline                     │
│                                                         │
│  Capture time:                                          │
│    • Blocked apps (not grabbed, no UI events)           │
│    • Heavy app auto-pause (games, video editors)        │
│    • Incognito mode (manual pause)                      │
│    • Password fields never read                         │
│    • URLs cleaned by sanitize_url()                     │
│                                                         │
│  Before storage:                                        │
│    • Sensitive-data filter (regex redaction)            │
│      - Credit cards → [REDACTED:card]                   │
│      - SSNs → [REDACTED:ssn]                            │
│      - API keys, JWTs                                   │
│      - Passwords (key=value and similar patterns)       │
│    • The image itself is not redacted                   │
│                                                         │
│  At rest:                                               │
│    • Optional Fernet encryption (screenshots only)      │
│    • Key in the OS keyring, plus a 0600 file copy       │
│    • The SQLite DB is not encrypted                     │
│                                                         │
│  Access:                                                │
│    • API binds to 127.0.0.1 only (0.0.0.0 falls back)   │
│    • No auth on the API                                 │
│    • Export and shutdown accept only local clients      │
│                                                         │
│  Retention:                                             │
│    • Delete data older than RETENTION_DAYS at startup   │
│    • 0-365 days (default 7, 0 = keep forever)           │
└─────────────────────────────────────────────────────────┘
```

Filter details: [capture.md, section 7](docs/architecture/capture.md#7-privacy-filters).

---

## 6. Platform Abstraction

```
platform_support/
├── base.py          ← PlatformAdapter ABC
├── windows.py       ← Win32 ctypes + UI Automation
├── macos.py         ← Quartz + AXUIElement (pyobjc)
├── macos_audio.py   ← apps using the mic (CoreAudio)
└── linux.py         ← xdotool/xprop + AT-SPI

┌──────────────┬──────────────────────┬──────────────────────┬─────────────────┐
│ Feature      │ Windows              │ macOS                │ Linux           │
├──────────────┼──────────────────────┼──────────────────────┼─────────────────┤
│ Window title │ GetWindowTextW       │ Quartz kCGWindowName │ xdotool+xprop   │
│ App name     │ process image name   │ kCGWindowOwnerName   │ xdotool+/proc   │
│ A11y text    │ UI Automation        │ AXUIElement          │ AT-SPI          │
│ Browser URL  │ UIA Document value   │ AXURL                │ none            │
│ Screenshot   │ mss                  │ SCK / screencapture  │ mss / grim /    │
│              │                      │ / mss                │ XDG Portal      │
│ UI events    │ low-level hooks+UIA  │ CGEventTap + AX      │ none            │
│ Mic users    │ none                 │ CoreAudio            │ none            │
└──────────────┴──────────────────────┴──────────────────────┴─────────────────┘
```

Wayland: window info comes from compositor IPC (Sway, Hyprland, Niri). Permissions per OS are in [capture.md, section 6](docs/architecture/capture.md#6-permissions).

---

## 7. Performance Characteristics

| Metric | Value | Notes |
|---|---|---|
| Cache hit (identical) | ~0ms | Copy from memory, no OCR |
| GPU | not used | OCR runs on the CPU |

### Resource Management

| Setting | Effect |
|---|---|
| Auto-Pause Heavy Apps | Skip capture when games or editors are in front |

---

## 8. End-to-End Data Flow

```
┌─────────┐   ┌──────────────────┐   ┌──────────────────────────────────────┐
│  User   │──▶│ Capture tick     │──▶│         Analysis Pipeline            │
│  works  │   │ (timer, change,  │   │                                      │
└────┬────┘   │  or UI event)    │   │  1. a11y text + URL (at grab time)   │
     │        └──────────────────┘   │  2. Per-app cache check              │
     │                               │     ├─ identical → reuse text        │
     │                               │     └─ full → continue               │
     │                               │  3. RapidOCR (if a11y is not enough) │
     │                               │  4. Sensitive-data redaction         │
     │                               │  5. Organize text by regions         │
     │                               │  6. Rule labels (app, category)      │
     │                               │  7. Store to SQLite, status='ok'     │
     │                               └──────────────────────────────────────┘
     │
     ├──▶ UI event recorder → ui_events (+ user_actions on the next frame)
     └──▶ Audio Worker → meetings (app, title, room URL, start, end)
                                                  │
                                                  ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                               Readers                                    │
│                                                                          │
│  Timeline  → browse by date                                              │
│  Search    → FTS5 keyword search + old meeting transcripts               │
│  Analytics → category / app / hour aggregations                          │
│  Meetings  → calls (old ones may have a transcript and summary)          │
│  Export    → per-day zip for the workflows app                           │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 9. Models

RapidOCR models (~25 MB) download to `~/.evolve-capture/models/ocr` on first use. No other model is used.

Gemma GGUF files from before 2026-10-08 stay in `~/.evolve-capture/models/gemma-4-e2b` (about 3.2 GB). Evolve Capture does not use or delete them. You can delete that folder by hand.

---

## 10. Tech Stack

| Layer | Technology |
|---|---|
| **Labels** | Rules over app name, window title and URL (`engine/labels.py`) |
| **OCR** | RapidOCR (PP-OCR on ONNX Runtime, CPU) |
| **Dedup** | imagehash (pHash) |
| **Backend** | FastAPI + Uvicorn |
| **Database** | SQLite (WAL) + FTS5 |
| **Capture** | ScreenCaptureKit / `screencapture` (macOS), mss, grim / XDG Portal (Wayland) |
| **Accessibility and UI events** | AXUIElement + CGEventTap (macOS), UI Automation + low-level hooks (Windows), AT-SPI (Linux) |
| **Encryption** | cryptography (Fernet) + keyring |
| **Frontend** | Vanilla JS + CSS, no build step |
| **Platform** | Windows / macOS / Linux (X11 + Wayland) |
