<div align="center">

# ScreenMind

**Local screen activity recorder. It collects what you do on your computer and labels it with Gemma 4.**

[![Python 3.14+](https://img.shields.io/badge/Python-3.14+-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![Gemma 4](https://img.shields.io/badge/Gemma_4-E2B_%7C_E4B_%7C_12B-8B5CF6?style=flat-square&logo=google&logoColor=white)](https://ai.google.dev/gemma)
[![llama.cpp](https://img.shields.io/badge/llama.cpp-Local_Inference-333?style=flat-square)](https://github.com/ggerganov/llama.cpp)
[![License MIT](https://img.shields.io/badge/License-MIT-10B981?style=flat-square)](LICENSE)

[Features](#features) · [Quick Start](#quick-start) · [Architecture](#architecture) · [API](#api-reference) · [Configuration](#configuration)

![Timeline: analyzed screen activity feed](docs/screenshots/timeline.png)

</div>

ScreenMind records your actions: screenshots, apps and windows, browser URLs, screen text, clicks and app switches, and calls. Near-duplicate frames are dropped by image hash. A local Gemma 4 model gives each frame a first label: app, category and a short summary. Everything runs on your machine.

It is a data source for a workflows app. The per-day export is the hand-off point. See [docs/export-format.md](docs/export-format.md).

Based on [ayushh0110/ScreenMind](https://github.com/ayushh0110/ScreenMind). This version keeps only action collection and the analysis it needs. Rewind, chat, voice memos, agents, bookmarks, daily summaries, the MCP server, integrations, hotkeys, semantic search, git context and the dashboard PIN were removed.

---

## Features

### Collection

- **Screenshots.** One JPEG per display. The capture loop grabs on change, on a timer (`CAPTURE_INTERVAL`) and, with UI events on, right after an app switch, click or typing pause.
- **Dedup.** pHash per display (`screenmind/capture/dedup.py`). A frame that did not change is deleted and not stored.
- **Window and app info.** App name and window title for each display.
- **Accessibility text.** Text of the focused window, read through the OS accessibility API.
- **Browser URL.** The URL the browser reports, cleaned by `privacy/url_filter.py` before storage.
- **OCR.** RapidOCR on ONNX Runtime, CPU only. It runs when the accessibility text is not real content.
- **UI events (macOS and Windows).** Clicks, app switches, typed text and clipboard by default. Password fields are never recorded; secrets are redacted by the sensitive-data filter, but there is no PII detection yet. Code: `screenmind/capture/ui_events/`.
- **Calls.** Zoom, Teams, Meet, Webex, Slack and Discord calls are tracked with start, end, app and room URL. Optional: Gemma transcribes the audio and writes a summary per call.

The detailed map of what is collected per OS, and how, is [docs/architecture/capture.md](docs/architecture/capture.md).

### Analysis

- **Labels.** Gemma 4 (via llama-server) reads each frame with its text and returns app, category, summary, details, scene and mood.
- **Three modes.** `fast` (default, no thinking), `balanced` (thinking) and `merged` (thinking plus layout regions).
- **Per-app analysis cache.** A frame close to the last one for the same app and title reuses its analysis. This saves Gemma calls.
- **Layout-ordered text.** `engine/layout_analyzer.py` groups OCR boxes into screen regions.
- **App name reconciliation.** The OS app name, the window title and Gemma's guess are combined into one app name.

### Privacy

- **Local only.** Data stays on your machine. The network is used only to download llama-server and models.
- **Sensitive-data filter.** Redacts credit cards, SSNs, API keys, JWTs and passwords in text before Gemma and storage (`privacy/data_filter.py`). The image is not redacted.
- **Screenshot encryption.** Optional, off by default. Fernet with the key in the OS keyring (`privacy/encryption.py`).
- **Blocked apps and incognito.** Blocked apps are never captured. Incognito pauses capture with one click.
- **Local API.** The API binds to `127.0.0.1` only and has no auth. Export and shutdown accept only local clients.

### Dashboard

At `http://127.0.0.1:7777`:

- **Timeline**: activities by date with screenshots, labels and calls.
- **Search**: keyword search over SQLite FTS5 and meeting transcripts. It highlights matches on the screenshot.
- **Analytics**: categories, top apps, hourly heatmap, meeting stats.
- **Meetings**: tracked calls, transcripts and summaries.
- **Settings**: capture, AI and models, audio and meetings, storage with the **Data Export** card, privacy and security (including UI events).
- **Model Hub**: download, switch and delete Gemma models and quantization variants.
- Start/stop capture, incognito, and a welcome screen on first run.

### Running it

- **Retention.** Data older than `RETENTION_DAYS` (default 7) is deleted at startup.
- **Start at login.** Windows Registry, macOS LaunchAgent or Linux XDG autostart (`startup.py`).
- **Launcher.** `screenmind --launch` shows a splash screen and opens the dashboard.
- **Auto-pause for heavy apps.** Games and video editors pause capture.

---

## Quick Start

> **Requirements:** Python 3.14+ · GPU recommended (4GB+ VRAM) · ~2GB disk for model (E2B Q4_0)

### 1. Install

```bash
pip install screenmind
# or, with uv:
uv tool install screenmind
```

### 2. Run

```bash
screenmind              # Normal start (foreground)
screenmind --launch     # Splash screen + auto-open dashboard
screenmind --background # Run silently without console window
```

### 3. Open http://127.0.0.1:7777

On first run, ScreenMind will:

- Detect your GPU and download `llama-server` if it is not found (CUDA or CPU build).
- Open the **Model Hub**. Pick a model and a quantization variant, then download it in the UI.
- Start `llama-server` in the background, or use one that is already running.
- Show the welcome screen.
- Create `~/.screenmind/` for data and `~/.screenmind/models/` for GGUF files.
- Create a desktop shortcut.

<details>
<summary><b>More CLI options</b></summary>

<br>

```bash
screenmind --install-startup    # Register to start at system login
screenmind --uninstall-startup  # Remove from system startup
screenmind --install-shortcut   # Create desktop shortcut
screenmind --version            # Show version
screenmind --help               # Show all options
```

</details>

<details>
<summary><b>Developer install (from source)</b></summary>

<br>

```bash
git clone https://github.com/ayushh0110/ScreenMind.git
cd ScreenMind

# Needs uv: https://docs.astral.sh/uv/getting-started/installation/
uv sync                      # creates .venv from uv.lock, with dev tools
uv run screenmind            # or: uv run python -m screenmind
```

Dependencies live in `pyproject.toml` and are pinned in `uv.lock`. Add or change one with `uv add <package>` (or edit `pyproject.toml` and run `uv lock`), then commit both files.

**Windows checkouts set up with pip:** since `c68311a` the project needs Python 3.14 and uv. Install uv before the next pull (PowerShell: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`). Then pull and run `uv sync`. uv downloads Python 3.14 and replaces the old `.venv`.

**Second instance from a git worktree:** run `scripts/dev-instance.sh` from the worktree root. It uses its own data dir (`~/.screenmind-dev/<worktree>`) and a port in 7800-7899. It shares the models and the running llama-server. `scripts/dev-instance.sh info` prints the port and data dir.

</details>

<details>
<summary><b>Install from source with pip</b></summary>

<br>

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux

pip install -e . pytest pytest-asyncio pytest-cov
```

This ignores `uv.lock`, so you may get newer versions than CI tests.

</details>

<details>
<summary><b>Optional: configure via .env</b></summary>

<br>

```bash
cp .env.example .env
# Edit capture interval, blocked apps, OCR languages, UI events, etc.
```

Or configure everything from the **Settings** tab in the dashboard.

</details>

---

## Gemma 4 models

| Model | Variants | VRAM | Audio |
|---|---|---|---|
| **Gemma 4 E2B** (2B), *default* | Q4_0 · Q8_0 · BF16 | ~4 GB | yes |
| **Gemma 4 E4B** (4B) | Q4_0 · Q8_0 · BF16 | ~6 GB | yes |
| **Gemma 4 12B** | IQ3_M · Q4_K_M · Q5_K_M · Q6_K · Q8_0 | ~10 GB | yes |

E2B is the default. It is small enough to run all the time next to your work, and it reads images and audio. More VRAM lets you use E4B or 12B for richer labels.

Models come from [ggml-org](https://huggingface.co/ggml-org) and other llama.cpp-compatible GGUF repos. They are stored in `~/.screenmind/models/`.

Call transcription uses Gemma's own audio encoder. There is no Whisper dependency.

### Analysis modes

Times are from a GTX 1650 (4GB VRAM), where the model does not fit in VRAM.

| Mode | `ANALYSIS_MODE` | What it does | ~Time |
|---|---|---|---|
| Fast (default) | `fast` | No thinking. Layout from OCR box clustering. | ~12s |
| Balanced | `balanced` | Thinking, analysis only. Layout from OCR box clustering. | ~40s |
| Accurate | `merged` | One call with thinking. Gemma also finds layout regions. | ~76s |

<details>
<summary><b>GPU scaling</b></summary>

<br>

The numbers above are a worst case: the model spills to CPU RAM. When it fits in VRAM, inference gets much faster:

| GPU | VRAM | Bandwidth | Regime | ~Fast Mode |
|---|---|---|---|---|
| **GTX 1650** *(baseline)* | 4 GB | ~190 GB/s | spilling | ~12s |
| **RTX 3060** | 12 GB | ~360 GB/s | full fit | ~3-4s |
| **RTX 4060 Ti** | 16 GB | ~290 GB/s | full fit | ~2-3s |
| **RTX 3090** | 24 GB | ~935 GB/s | full fit | ~1-2s |
| **RTX 4090** | 24 GB | ~1000 GB/s | full fit | ~1s |

The big jump is from "spilling" to "full fit". Any GPU with 6GB+ VRAM should run E2B fully on the GPU.

</details>

---

## Architecture

The full description is in [architecture.md](architecture.md). The per-OS capture map is [docs/architecture/capture.md](docs/architecture/capture.md).

```
                 OS APIs (screen, windows, accessibility, input hooks, mic)
                         │                    │                     │
                         ▼                    ▼                     ▼
               ┌──────────────────┐  ┌─────────────────┐  ┌──────────────────┐
               │  Capture Worker  │  │ UI event        │  │  Audio Worker    │
               │ • grab / display │  │ recorder        │  │ • call detection │
               │ • pHash dedup    │  │ • clicks, apps  │  │ • transcription  │
               │ • window, a11y,  │  │ • text, clip    │  │   + summary      │
               │   URL            │  │                 │  │   (optional)     │
               └────────┬─────────┘  └────────┬────────┘  └────────┬─────────┘
                        │ queue               │                    │
                        ▼                     │                    │
               ┌──────────────────┐           │                    │
               │ Analysis Worker  │           │                    │
               │ • per-app cache  │           │                    │
               │ • RapidOCR       │           │                    │
               │ • data filter    │           │                    │
               │ • Gemma 4 label  │           │                    │
               │ • layout text    │           │                    │
               └────────┬─────────┘           │                    │
                        ▼                     ▼                    ▼
               ┌────────────────────────────────────────────────────────────┐
               │ SQLite (WAL) + FTS5: activities, ui_events, meetings       │
               │ screenshots/YYYY-MM-DD/*.jpg                               │
               └─────────────────────────────┬──────────────────────────────┘
                                             ▼
               ┌────────────────────────────────────────────────────────────┐
               │ FastAPI on 127.0.0.1:7777                                  │
               │ Dashboard · /api/* · /api/export (per-day zip)             │
               └────────────────────────────────────────────────────────────┘
```

Gemma runs in a separate `llama-server` process (default port 5809).

---

## API Reference

Full Swagger docs are at `http://127.0.0.1:7777/docs`.

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/status` | Capture state and worker stats |
| `GET` | `/api/timeline?date=2026-05-21` | Activities for a date |
| `GET` | `/api/activity/{id}` | One activity |
| `POST` | `/api/activities/{id}/reanalyze` | Run analysis again |
| `GET` | `/api/screenshot/{id}` | Screenshot image |
| `GET` | `/api/search?q=auth error` | Keyword search (FTS5) plus meeting transcripts |
| `GET` | `/api/stats?range=day` | Analytics (categories, apps, meetings) |
| `GET` | `/api/meetings` | Tracked calls |
| `GET` | `/api/ui-events?start=&end=&type=` | Recorded UI events in a time range |
| `GET` | `/api/ui-events/status` | UI event recorder state, backend, macOS permissions |
| `GET` | `/api/export?user=&date=` | Per-day zip archive (local clients only) |
| `GET` | `/api/export/preview?date=` | Counts and sizes before an export |
| `GET` | `/api/models` | Available models and variants |
| `POST` | `/api/models/variant` | Set the preferred quantization variant |
| `POST` | `/api/models/delete` | Delete a downloaded model variant |
| `GET` / `POST` | `/api/settings` | Read or change settings |
| `POST` | `/api/capture/pause` | Pause capture |
| `POST` | `/api/capture/resume` | Resume capture |
| `POST` | `/api/incognito/toggle` | Toggle incognito mode |
| `POST` | `/api/shutdown` | Graceful shutdown (local clients only) |

---

## Configuration

The defaults below are the settings for every machine. Change one only when a machine needs it: in `.env` (in the checkout root), as an environment variable, or on the **Settings** page (saved to `settings.json` in the data dir; `settings.json` beats `.env`). The Settings page stores only values that differ from the default, so the rest follow later default changes. At startup ScreenMind logs every setting that differs from its default, and where it comes from.

| Variable | Default | Description |
|----------|---------|-------------|
| `CAPTURE_INTERVAL` | `10` | Seconds between periodic captures (10-120). Every grab is deduped, so a screen that does not change gets no new frame |
| `ANALYSIS_MODE` | `fast` | `fast` (~12s), `balanced` (~40s), or `merged` (~76s, accurate) |
| `PERFORMANCE_MODE` | `balanced` | GPU layers: `minimal` / `balanced` / `maximum` |
| `BLOCKED_APPS` | *(empty)* | Comma-separated apps to never capture |
| `OCR_LANGUAGES` | `en,es,de,fr,ru` | OCR languages. One script per frame; the default reads Latin and Cyrillic |
| `MEETING_TRANSCRIPTION` | `false` | Transcribe calls with Gemma |
| `RETENTION_DAYS` | `7` | Delete data older than N days (0 = forever) |
| `ENCRYPTION_ENABLED` | `false` | Encrypt screenshots at rest |
| `SENSITIVE_FILTER_ENABLED` | `true` | Redact credit cards, SSNs, API keys, passwords |
| `CAPTURE_PAUSED` | `true` | Capture state, kept across restarts |
| `UI_EVENTS_ENABLED` | `true` | Record the UI event types listed in `UI_EVENTS_TYPES` (macOS, Windows) |
| `UI_EVENTS_TYPES` | `click,app_switch,text,clipboard` | Which UI event types to record (`window_focus` also available) |
| `EVENT_TRIGGERED_CAPTURE` | `true` | Capture right after app switches, clicks and typing pauses |
| `SCREENMIND_LOG_LEVEL` | `INFO` | Log verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` (environment variable only, not `.env`) |
| `SCREENMIND_LOG_FILE` | `<data dir>/screenmind.log` | Log file, written on every start (rotating, 1 MB × 3 backups; environment variable only) |

See `.env.example` for the full list.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **Labels and audio** | Gemma 4 (E2B / E4B / 12B) |
| **Inference server** | llama-server (llama.cpp), OpenAI-compatible API |
| **OCR** | RapidOCR (PP-OCR on ONNX Runtime), CPU only, ~15MB of models |
| **Dedup** | imagehash (pHash) |
| **Backend** | FastAPI + Uvicorn |
| **Database** | SQLite (WAL) + FTS5 |
| **Capture** | ScreenCaptureKit / `screencapture` (macOS), mss, grim / XDG Portal (Wayland) |
| **Accessibility and UI events** | AXUIElement + CGEventTap (macOS), UI Automation + low-level hooks (Windows), AT-SPI (Linux, text only) |
| **Frontend** | Vanilla JS + CSS, no build step |

---

## Wayland Support

ScreenMind detects Wayland sessions and uses compositor-native capture:

| Compositor | Capture | Window Detection | Notes |
|---|---|---|---|
| **Sway** | grim | swaymsg IPC | Full support |
| **Hyprland** | grim | hyprctl IPC | Full support |
| **Niri** | grim | niri msg IPC | Full support |
| **river / Wayfire / labwc** | grim | Title only (no IPC) | Capture works, app name may be missing |
| **GNOME (Mutter)** | XDG Portal | No IPC | Portal asks on every capture. Not usable for background recording |
| **KDE (KWin)** | XDG Portal | No IPC | Same as GNOME |

**Install grim** (recommended for wlroots compositors):

```bash
# Arch
sudo pacman -S grim

# Ubuntu / Debian (if available)
sudo apt install grim

# Fedora
sudo dnf install grim
```

**GNOME / KDE Wayland** is best-effort only. The XDG Desktop Portal asks for permission on each capture, so continuous recording does not work. Use an X11 session or a wlroots compositor with grim.

**Optional** (for the portal fallback): the `python3-gi` / `python-gobject` system package.

---

<details>
<summary><b>Project Structure</b></summary>

<br>

```
ScreenMind/
├── screenmind/
│   ├── main.py                  # Entry point, starts all services
│   ├── config.py                # Pydantic settings (env + runtime overrides)
│   ├── setup_llama.py           # Detect + install llama-server
│   ├── launcher.py              # Splash screen launcher (tkinter)
│   ├── startup.py               # Start at login (Windows, macOS, Linux)
│   ├── assets/                  # Logo, favicon
│   ├── capture/
│   │   ├── screen.py            # Capture facade (SCK / screencapture / mss / Wayland)
│   │   ├── sck.py               # ScreenCaptureKit backend (macOS 14+)
│   │   ├── wayland.py           # Wayland backend (grim / XDG Portal)
│   │   ├── window.py            # Active window detection
│   │   ├── dedup.py             # pHash dedup
│   │   └── ui_events/           # Clicks, typing, app switches, clipboard
│   ├── engine/
│   │   ├── analyzer.py          # Gemma 4 labels, three modes, app reconciliation
│   │   ├── llm_client.py        # llama-server client (text, vision, audio)
│   │   ├── model_manager.py     # Server lifecycle, model download/switch
│   │   ├── ocr.py               # RapidOCR
│   │   ├── layout_analyzer.py   # Layout-ordered OCR text
│   │   └── a11y_extractor.py    # Accessibility text
│   ├── workers/
│   │   ├── capture_worker.py    # Capture loop, dedup, privacy checks
│   │   ├── analysis_worker.py   # Cache, OCR, filter, Gemma, layout, store
│   │   ├── audio_worker.py      # Call tracking, transcription, summary
│   │   └── call_detection.py    # Call rules per app
│   ├── export/                  # Per-day zip archives for the workflows app
│   ├── storage/
│   │   ├── database.py          # SQLite + FTS5 + migrations (schema v11)
│   │   └── models.py            # Pydantic data models
│   ├── privacy/
│   │   ├── data_filter.py       # Sensitive-data redaction
│   │   ├── url_filter.py        # URL cleaning
│   │   └── encryption.py        # Screenshot encryption
│   ├── platform_support/        # Windows, macOS, Linux adapters
│   └── api/
│       ├── server.py            # FastAPI app
│       ├── routes/              # REST routes
│       └── static/              # Web dashboard (HTML + CSS + JS)
├── scripts/
│   ├── dev-instance.sh          # Isolated instance for a git worktree
│   └── e2e_collect.py           # End-to-end collection check
├── tests/                       # pytest suite
├── docs/
│   ├── architecture/capture.md  # What is captured per OS
│   └── export-format.md         # Export archive format
├── pyproject.toml               # Dependencies and extras
└── uv.lock                      # Exact versions for every platform (uv)
```

</details>

---

## Error Handling

| Scenario | Behavior |
|----------|----------|
| **llama-server not found** | Downloads the right binary from GitHub releases (CUDA or CPU). Checks disk space first. |
| **llama-server already running** | Detects and uses it. Known models show as active. Unknown models show an amber warning. |
| **Model not downloaded** | The Model Hub shows download cards with progress. |
| **GPU out of memory** | Detects OOM, retries after a delay, re-queues on repeated failure. |
| **Duplicate frames** | pHash dedup drops frames within hamming distance 8 of the last kept one. |
| **Stale queue items** | Frames older than 3 min when their turn comes are marked `skipped`. The idle backfill analyzes them later. |
| **App in blocklist** | Skipped. No screenshot is saved. |
| **Call ended** | A call ends 120 s after its window or mic use was last seen. |
| **Re-analyze** | Uses the full context (app name, title, URL), same as the first analysis. |
| **Crash recovery** | Stale meetings are cleaned on startup. Unanalyzed rows are backfilled. The capture pause state is kept. |

---

## Development

Run the test suite:

```bash
uv run pytest tests -q
uv run pytest --cov=screenmind --cov-report=term-missing -q   # with coverage
```

`uv run` installs the locked dependencies first if needed. CI runs the same locked install on push/PR via GitHub Actions (Windows, Python 3.14).

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

---

## License

MIT License. See [LICENSE](LICENSE) for details.

Original project by [ayushh0110](https://github.com/ayushh0110/ScreenMind).
