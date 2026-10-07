# Strip ScreenMind down to action collection

Status: done on 2026-10-07. All slices are merged into `custom`. See [Progress](#progress) at the end. The tables below show the final decisions.

## Goal

The user's words: "strip all the extra features like rewind, ask, memos, Agents, bookmarks. We care only about actions collection plus minimalistic analysis for deduplication, unification and preliminary labeling purposes."

ScreenMind becomes a data source for the workflows app. It records what the person did: apps, windows, URLs, a11y/OCR text, UI events and calls. A per-user feed maps that onto the personal workflow record. See [questionnaire-data-source.md](../backlog/questionnaire-data-source.md) and [capture.md](../architecture/capture.md).

So a feature stays only if it does one of these:

1. **Collects** an action: screenshot, window/app, URL, a11y text, OCR text, UI events, calls.
2. **Dedups** it: pHash at capture, and the per-app analysis cache.
3. **Unifies** it: one app name per tool, sanitized URLs, redacted secrets.
4. **Labels** it: app, category and a one-line summary from Gemma.
5. **Runs, shows or exports** the above: the capture loop, the model server, the timeline, settings, retention, and the export that another session is adding.

Everything else goes: features that produce new content (chat, summaries, agents), push data somewhere else (Obsidian, Notion, webhooks, MCP), or are user conveniences that do not add data (rewind, bookmarks, memos, hotkeys).

## What other sessions need from this

- **Export session** adds `screenmind/export/`, `api/routes/export.py` and one include line in `api/server.py`. It needs screenshots on disk, the `activities` text fields (app, title, URL, a11y/OCR text, summary, category, `user_actions`), `ui_events` and `meetings`. All of these stay. Keep its include line when editing `server.py`. It adds an export button after the nav changes land.
- **macOS click collection** owns `screenmind/capture/ui_events/`. Not touched here.
- **pip to uv** owns `pyproject.toml` and lock files. Dependency removal waits until its step 1 is merged.
- **Windows laptop** pushes to `custom`. Windows code paths must keep working. Most removals here are cross-platform. The only Windows-only piece that goes is the `keyboard` hotkey listener.

## Facts that shaped the plan

From the live DB (`~/.screenmind`, read-only, 2026-10-07):

- 1313 activities. 299 of them (23%) are bookmarked. Almost all are auto-bookmarks: the keywords `merge`, `docker`, `deploy` match ordinary screen text. Bookmarked frames skip the analysis cache, so this also costs extra Gemma calls.
- 577 `dev_contexts` rows, 2 `daily_summaries`, 6 `meetings` with no transcripts.
- Analysis runs in `fast` mode only. `balanced` and `merged` are never used.
- Every row has an embedding. Nothing reads them except search, chat and MCP. **Embeddings are not used for dedup.** Dedup is pHash only.

From the code:

- Hotkeys, and so voice memos, never run on macOS (`capture/hotkey.py` skips the `keyboard` library there).
- Default agents are copied into `~/.screenmind/agents` on every start, even when agents are off.
- Two embedding model instances load at startup (API and analysis worker).
- Webhook event `meeting_end` is configured by default but never fired.
- Unknown keys in `settings.json` are ignored on load (`config.py` `load_runtime_overrides`). Removed settings keys are safe for existing installs.

## Inventory

Legend: **Keep**, **Remove**, **Slim** (keep the core, cut parts).

### Dashboard pages

All pages render into `#content` from `screenmind/api/static/js/*.js`. Nav buttons are in `index.html` lines 62-97.

| Page | JS | Routes it calls | Proposal | Why |
|---|---|---|---|---|
| Timeline | `timeline.js` | `/api/timeline`, `/api/meetings`, `/api/activity/{id}`, `/api/screenshot/{id}`, delete, clear, reanalyze, bookmark toggle | **Slim** | The only way to see and check what was collected. Drop the bookmark star. Reanalyze stays but stops writing embeddings. |
| Search | `search.js` | `/api/search`, `/api/screenshot/{id}/highlight` | **Slim** | The user keeps it. Embeddings go, so it becomes keyword-only (FTS5 on `activities_fts`, plus the `meetings` LIKE search). No more 503 without an embedder. |
| Bookmarks | `bookmarks.js` | `/api/bookmarks` | **Remove** | Named by the user. |
| Analytics | `analytics.js` | `/api/stats` | **Keep** | The user keeps it. Its range bug (`range` is ignored, so it shows one day) goes to a separate session. Remove only the `top_repos` part (git context goes). |
| Rewind | `rewind.js` | `/api/rewind` | **Remove** | Named by the user. |
| Summary | `summary.js` | `/api/summary`, `/summary/generate`, `/standup/generate` | **Remove** | Generates new text; it does not collect or label actions. |
| Ask (chat) | `chat.js` | `/api/chat` (SSE) | **Remove** | Named by the user. |
| Meetings | `meetings.js` | `/api/meetings*` | **Keep** | The user keeps calls in full: tracking, transcription and the per-call summary. |
| Memos | `memos.js` | `/api/memos*` | **Remove** | Named by the user. |
| Agents | `agents.js` | `/api/agents*` | **Remove** | Named by the user. |
| Settings | `settings.js` | see below | **Slim** | Needed to run capture. |

Global parts of the dashboard (`core.js`, `index.html`):

| Part | Proposal | Why |
|---|---|---|
| Model Hub overlay and pill | **Keep** | Installs and switches the Gemma model that does the labeling. |
| Capture start/stop, Incognito, status badge, Stop Server | **Keep** | Control of collection. |
| Screenshot modal | **Keep** | Used by the timeline. |
| Welcome screen | **Slim** | Drop the hotkey hints (lines 47-49). |
| PIN lock screen | **Remove** | The user drops the PIN. The first-run welcome screen stays. |
| Chart.js CDN tag | **Keep** | Analytics stays. |

### Settings page sections (`settings.js` lines 20-217)

| Section | Keys | Proposal |
|---|---|---|
| Keyboard Shortcuts | `bookmark_hotkey`, `pause_hotkey`, `voice_hotkey` | **Remove** (hotkeys go) |
| Capture | `capture_interval`, `auto_pause_heavy_apps`, `heavy_apps`, `defer_analysis`, `capture_active_monitor`, start at login | **Keep** |
| AI & Models | `performance_mode`, `context_window`, `kv_cache_quant`, `flash_attention`, `analysis_mode` | **Keep** (all three analysis modes stay) |
| Audio & Meetings | `meeting_transcription`, `meeting_apps` | **Keep** |
| Storage | `retention_days`, storage estimate | **Keep** |
| MCP Integration card | none | **Remove** |
| Integrations | `obsidian_*`, `notion_*`, `webhook_*` | **Remove** |
| Automation | `agents_enabled`, `agents_auto_run_python`, `auto_bookmark`, `auto_bookmark_keywords` | **Remove** |
| Privacy & Security | `sensitive_filter_*`, PIN, `dashboard_lock_timeout`, `encryption_enabled`, UI events (`ui_events_enabled`, `ui_events_types`, `event_triggered_capture`) | **Slim**: remove the PIN controls and `dashboard_lock_timeout`. Keep the rest. |

### API routes (`screenmind/api/routes/`, all registered in `api/server.py`)

| Route file | Proposal | Notes |
|---|---|---|
| `auth.py` | **Slim** | Keep `GET /status` (first-run flag only) and `POST /setup-complete` (without the PIN part). Remove `/verify`, `/set-pin`, `/logout`, the session store in `dependencies.py` and `AuthMiddleware` in `server.py`. |
| `capture.py` | **Slim** | Drop `POST /capture/bookmark` (only the SDK and MCP use it). Keep pause, resume, incognito, status. |
| `timeline.py` | **Slim** | Reanalyze: drop the embedding step. Drop the manual-entry embedding code. |
| `screenshots.py` | **Keep** | Search keeps `/highlight`. |
| `data.py` | **Keep** | `/timeline/clear`. `/activities/before/{date}` is unused but harmless. |
| `settings.py` | **Slim** | Drop removed keys from `GET`, drop `/integrations/test` and `/webhooks/log`. Keep startup and shutdown. |
| `models.py` | **Keep** | Model Hub. |
| `ui_events.py` | **Keep** | Owned in spirit by the click-collection session; not edited. |
| `stats.py` | **Keep** | Analytics and Storage use it. `get_stats` loses its `dev_contexts` top-repos query. |
| `meetings.py` | **Keep** | |
| `search.py` | **Slim** | Keyword-only: drop the semantic branch and the embedder dependency. |
| `bookmarks.py` | **Remove** | |
| `rewind.py` | **Remove** | |
| `summary.py` | **Remove** | Also removes the only caller of Obsidian, Notion and the summary webhook. |
| `chat.py` | **Remove** | |
| `agents.py` | **Remove** | Includes `/api/agents/sdk/activities`. |
| `memos.py` | **Remove** | |

`api/server.py`: drop the removed includes, `AuthMiddleware` and the `Embedder` fallback. Keep the export include. `main.py` keeps forcing `127.0.0.1` when `api_host` is `0.0.0.0` (today it does that only without a PIN; now always).

### Backend modules

| Module | Proposal | Why | DB | Settings keys | Deps | Tests |
|---|---|---|---|---|---|---|
| `workers/capture_worker.py`, `capture/screen.py`, `sck.py`, `wayland.py`, `window.py`, `dedup.py`, `platform_support/*` | **Keep**; slim the bookmark path | Collection and dedup. Drop `trigger_bookmark` / `_do_bookmark_capture` and the bookmark bypass of dedup. | `activities` (capture fields) | capture keys | mss, Pillow, imagehash, pyobjc, uiautomation | test_capture, test_phash_distances, test_workers, test_windows_adapter, test_macos_frontmost |
| `workers/analysis_worker.py` | **Slim** | Labeling. Drop embeddings, auto-bookmark, the webhook call, the chat pre-emption, and `dev_context`. | `activities` (analysis fields) | `analysis_mode`, `auto_bookmark*` | | test_workers, test_analyzer, test_a11y_quality |
| `engine/analyzer.py` | **Keep** | The user keeps all three modes. | | `analysis_mode` | | test_analyzer |
| `engine/layout_analyzer.py` | **Keep** | Builds `organized_text`; merged mode stays. | `activities.organized_text` | | | test_layout_analyzer |
| `engine/ocr.py`, `a11y_extractor.py` | **Keep** | Collection. | | `ocr_languages` | rapidocr, onnxruntime | test_ocr*, test_a11y_quality |
| `engine/llm_client.py` | **Slim** | Drop `cancel_current_inference` / `InferenceCancelled` (chat only). Keep `transcribe_audio` and whatever the per-call summary uses. Drop `generate` only if nothing else calls it. | | | httpx | test_llm_client |
| `engine/model_manager.py`, `setup_llama.py` | **Keep** | Runs Gemma. | | model keys | huggingface_hub | test_model_manager, test_setup_llama |
| `engine/embedder.py` | **Remove** | Not used for dedup. Only search, chat and MCP read embeddings. Saves two model loads at startup and a step per frame. | `activities.embedding` left NULL | | tokenizers (onnxruntime and huggingface_hub stay for OCR and Gemma) | test_embedder |
| `engine/dev_context.py` (git) | **Remove** | Guesses a repo from the window title or the newest repo in `workspace_dirs`. It is a guess, not a collected action. | `dev_contexts` | `workspace_dirs` | gitpython | test_dev_context |
| `engine/agent_runner.py`, `default_agents/`, `screenmind_sdk.py`, `docs/BUILD_YOUR_OWN_AGENT.md` | **Remove** | Named by the user. Stop copying default agents on start. Leave `~/.screenmind/agents` on disk; tell the user it can be deleted. | none (reads only) | `agents_enabled`, `agents_auto_run_python` | | test_agent_runner, test_sdk |
| `mcp_server.py`, `MCP_SETUP.md` | **Remove** | A second way to read the data. The export replaces it for the workflows app. | reads only | | mcp | test_mcp |
| `integrations/obsidian.py`, `notion.py`, `webhooks.py` | **Remove** | Push data elsewhere. `webhook_log.db` stays on disk; tell the user. | separate `webhook_log.db` | `obsidian_*`, `notion_*`, `webhook_*` | (notion_client, undeclared) | test_obsidian, test_notion, test_webhooks, test_webhooks_v3 |
| Daily summary / standup (in `summary.py`) | **Remove** | Generates text. | `daily_summaries` | | | test_api_routes `test_summary_not_generated`, test_ui_events `test_summary_actions_line` |
| Auto-bookmark (in `analysis_worker.py`) | **Remove** | Marks 23% of frames and costs extra Gemma calls. | `activities.bookmarked` | `auto_bookmark`, `auto_bookmark_keywords` | | none |
| Manual bookmarks | **Remove** | Named by the user. | `activities.bookmarked` | | | test_api_routes bookmark tests |
| `capture/hotkey.py` | **Remove** | Bookmark, pause and voice hotkeys. Dead on macOS. Pause stays in the dashboard. | | `*_hotkey` | keyboard | none |
| `capture/voice_recorder.py` + memo thread in `main.py` | **Remove** | Voice memos, named by the user. Leave `~/.screenmind/memos` on disk. | `activities` rows with app "Voice Memo" | | (sounddevice stays for call transcription) | none |
| `ui/overlay.py` | **Remove** | Toasts for hotkeys, memos and call start/end. No data. | | | tkinter (stdlib) | none |
| `workers/audio_worker.py`, `call_detection.py`, `platform_support/macos_audio.py` | **Keep** | Tracking, transcription and the per-call summary all stay. Only the overlay toasts go. | `meetings` | `meeting_transcription`, `meeting_apps` | numpy, sounddevice | test_call_detection |
| `privacy/data_filter.py`, `url_filter.py` | **Keep** | Unification and redaction. | | `sensitive_filter_*` | | test_privacy, test_url_filter, test_ui_events_redaction |
| `privacy/encryption.py` | **Keep** | Screenshots only, off by default. It is the image loader used by analysis, timeline and screenshots routes, and the export will read screenshots. | | `encryption_enabled` | cryptography, keyring | test_encryption |
| `api/routes/auth.py` + `AuthMiddleware` | **Slim** | PIN lock goes; first-run flag stays. | | `dashboard_pin_hash`, `dashboard_lock_timeout` go; `setup_complete` stays | | test_api, test_api_routes |
| `launcher.py`, `startup.py` | **Keep** | Start at login and the splash. | | | tkinter, winreg (stdlib) | test_startup |

### Dead settings keys to drop in the same pass

`gemma_mode`, `google_api_key`, `ollama_model`, `ollama_host`: never implemented or legacy. `GET /api/settings` still returns `ollama_model`.

### DB tables and columns

The user chose a real cleanup: **migration v11 drops what nothing writes any more.** The number is confirmed with the coordinator before the slice starts (v10 is the latest today).

| Table / column | After the strip |
|---|---|
| `activities` capture and analysis fields | Written as today. `mood` and `scene_description` stay (all analysis modes stay). |
| `activities.embedding` | **Dropped** in v11 |
| `activities.bookmarked` + `idx_activities_bookmarked` | **Dropped** in v11 (index first, then the column) |
| `activities_fts` + triggers | Kept. Search uses it. |
| `dev_contexts` + its 3 indexes | **Dropped** in v11. The `LEFT JOIN`s in `get_activities_by_date`, `get_activity_by_id`, `get_bookmarks` and `get_stats` go first. |
| `daily_summaries` | **Dropped** in v11 |
| `meetings` | Written as today |
| `ui_events` | Written as today |
| Voice memo rows in `activities` | None new. Old ones stay until retention. |

Migration notes:

- `DROP COLUMN` needs SQLite 3.35+. The main venv has 3.53. Python 3.10+ on Windows ships a newer one too. If the runtime is older, v11 skips the column drops and logs it; the tables still go.
- `CREATE TABLE` for `dev_contexts` and `daily_summaries` is removed from `_init_db`, so a fresh DB never gets them. `activities.embedding` and `bookmarked` leave the base `CREATE TABLE` too, and v11 tolerates "no such column".
- `delete_by_date` and `cleanup_old_data` stop touching `daily_summaries`.
- This can't be undone on an existing DB. The user said yes on 2026-10-07. Before the main instance first runs it, copy `~/.screenmind/screenmind.db` aside.
- Old branches that still write `embedding` or `bookmarked` will fail against a v11 DB. That matters for the Windows laptop: it must pull `custom` before running on a migrated DB. Each machine has its own DB, so a laptop on old code only breaks if it runs on a DB that newer code migrated.

### Dependencies (after the uv step 1 is merged)

| Package | Why it goes |
|---|---|
| `keyboard` | hotkeys go |
| `tokenizers` | embeddings go |
| `mcp[cli]` (extra; pinned in `requirements*.txt`) | MCP goes |
| `gitpython` | git context goes |
| `aiofiles` (`requirements-test.txt`) | already unused |

Stay: `cryptography` and `keyring` (encryption stays), `sounddevice` and `numpy` (call transcription), `onnxruntime` (RapidOCR), `huggingface_hub` (Gemma downloads), `httpx`, `imagehash`, `Pillow`, `mss`, pyobjc, `uiautomation`.

### Docs

- Remove: `MCP_SETUP.md`, `docs/BUILD_YOUR_OWN_AGENT.md`, `docs/screenshots/agents.png`, `docs/screenshots/chat-demo.gif`.
- Rewrite: `README.md` (feature list, MCP and agents sections), `architecture.md`, `CONTRIBUTING.md` (MCP lines).
- Update `docs/architecture/capture.md` and `capture.html` (republish to the same Artifact URL): drop the embedding and voice memo rows in the matrix, the auto-bookmark, embedding and git text in 4.9, the bookmark notes in 4.1/4.2, `daily_summaries`, `dev_contexts` and `webhook_log.db` in section 5, gap G19. Add v11.
- Backlog: delete G19 (hotkeys) from `capture-architecture.md`. In `setup-ocr-and-upstream-prs.md` "Workflows app integration", replace the `/api/agents/sdk/activities` and webhook notes with a pointer to the export. Keep "Encrypt the whole DB" in `ui-events.md` (encryption stays).
- Tell the user these stay on disk and can be deleted by hand: `~/.screenmind/agents/`, `~/.screenmind/memos/`, `~/.screenmind/webhook_log.db`, `~/.screenmind/models/embedder/`.

## Slices (step 2)

Each slice is one commit (or a few), merged on its own into `custom` after `pytest` passes and a dev instance starts cleanly with capture paused. The coordinator gets the file list before each merge and the new sha after.

1. **Dashboard.** Remove the nav items and JS files for Rewind, Ask, Memos, Agents, Bookmarks and Summary. Remove the settings sections (shortcuts, MCP card, integrations, automation, PIN), the PIN lock screen, the hotkey hints and the bookmark star. Files: `api/static/index.html`, `api/static/js/*.js`, `api/static/css/styles.css`. Then tell the coordinator, so the export button can land.
2. **Routes.** Delete `rewind.py`, `chat.py`, `memos.py`, `agents.py`, `bookmarks.py`, `summary.py`. Slim `auth.py`, `capture.py`, `timeline.py`, `search.py`, `settings.py`. Remove `AuthMiddleware` and the session store. Update `api/server.py` (keep the export include) and `api/dependencies.py`. Remove the matching tests and `conftest.py` patches.
3. **Workers and engine.** Remove hotkey, overlay, voice memo, agents, SDK, MCP, integrations, embedder, git context, auto-bookmark, the bookmark capture path and chat pre-emption. Slim `main.py`, `analysis_worker.py`, `llm_client.py`, `audio_worker.py` (overlay calls only), `capture_worker.py`, `database.py` (dead methods, `dev_contexts` joins). Remove the matching tests.
4. **Settings keys.** Drop the fields from `config.py` and `_ALLOWED_OVERRIDES`, `.env.example`, `conftest.py`.
5. **DB migration v11.** As above, with a test on a v10 DB.
6. **Docs and backlog.** As listed above, plus `capture.md` / `capture.html`.
7. **Dependencies.** Only after the uv migration step 1 is merged and the coordinator says so.

Windows check for every slice: no new `sys.platform` branches, and grep for Windows-only callers (`keyboard`, `winreg`, `uiautomation`) before deleting.

Restarting the main instance (port 7777) to pick up the changes needs the user's OK. Until then it keeps running the old code.

## Decisions

The user's answers, 2026-10-07:

1. **Pages.** Timeline, Search, Analytics and Meetings stay. Rewind, Ask, Memos, Agents, Bookmarks and Summary go. Analytics shows only one day; that fix goes to a separate session.
2. **Calls.** Keep all of it: tracking, transcription and the per-call summary.
3. **Gemma analysis.** Keep all three modes.
4. **Embeddings.** Remove. Search becomes keyword-only.
5. **Daily summary and MCP server.** Remove.
6. **Git context.** Remove.
7. **Privacy.** Keep screenshot encryption. Drop the PIN lock.
8. **DB.** Drop unused tables and columns in migration v11.

## Progress

All on `custom`, 2026-10-07:

| Slice | Commit |
|---|---|
| Plan | `824470b` |
| 1. Dashboard pages and the PIN lock | `ea9510c` |
| 2. Routes | `1c408f6` |
| 3. Workers and engine | `57cf0dd` |
| 4. Settings keys (`.env` ignores unknown keys now) | `cd95561` |
| 5. Migration v11 and export format 2 | `edfc76e` |
| Leftovers: e2e env, old `settings.json` test | `d2308cd` |
| 6. Docs and backlog | `3c7a2f7` |
| 7. Dependencies | `b76042a` |

Left for the user:

- Back up the DB before the main instance first runs v11: `cp ~/.screenmind/screenmind.db ~/.screenmind/screenmind.db.pre-v11`.
- Run `uv sync` for the main `.venv` before the next start.
- These folders and files are no longer used and can be deleted by hand: `~/.screenmind/agents/`, `~/.screenmind/memos/`, `~/.screenmind/webhook_log.db`, `~/.screenmind/models/embedder/`.

