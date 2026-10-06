# Setup, OCR and upstream PRs

From the first ScreenMind session (2026-10-05 to 2026-10-06): local setup on the M5 Mac, macOS fixes, OCR memory and languages, and PRs to `ayushh0110/ScreenMind`.

Done in this session, all merged into `custom`:

| Branch | Commit | What | On the fork |
|---|---|---|---|
| `fix/macos-frontmost-window` | `74ff138` | Front app from Quartz, not stale `NSWorkspace`. `PlatformAdapter.get_active_window_bounds()`. | pushed, upstream #20 |
| `fix/macos-app-name-precedence` | `b14b7ef` | macOS uses the OS app name before title parsing. `PlatformAdapter.trusts_os_app_name`. | pushed, upstream #21 |
| `fix/backfill-app-hint` | `5ed6650` | Backfill passes `detected_app` to the analyzer | pushed, upstream #22 |
| `perf/ocr-memory` | `3409bda` | EasyOCR on CPU unless CUDA, `canvas_size=800`. About 8 GB down to 3 GB. | pushed, upstream #23 closed |
| `feat/ocr-multilingual` | `c9dd66c` | `OCR_LANGUAGES`, Latin + Cyrillic Readers on shared boxes | pushed, upstream #24 closed |
| `test/isolate-data-dir` | `4bc23dc` | Tests never touch `~/.screenmind` | pushed, no PR yet |
| `feat/capture-on-start` | `ebfdaaa` | `CAPTURE_ON_START` always starts capture on launch | pushed, fork only |
| `test/logging-skip-venv` | `c3dd2ac` | Logging convention test skips `.venv`, `.claude` and virtualenvs | **local only**, not pushed |

Local only and not in git (excluded via `.git/info/exclude`): `local-notes/ocr-findings.md` (OCR memory and language benchmarks), `local-notes/ocr-bench/` (scripts and raw `results.jsonl`), `local-notes/prs/` (PR description drafts). The local `.env` sets `OCR_LANGUAGES=en,es,de,fr,ru` and `CAPTURE_ON_START=true`.

## Items

### Upstream PRs #20–#22 wait for review and CI
Status: blocked on the maintainer

ayushh0110/ScreenMind#20, #21 and #22 are open and ready for review. CI has not run, because GitHub waits for the maintainer to approve workflows from a first-time contributor. The maintainer has no Mac (see their review on upstream PR #3), so our manual macOS test notes in the PR bodies matter.

Refs: branches above, PR bodies on GitHub, drafts in `local-notes/prs/`.

### Offer the test isolation fix upstream
Status: open

Upstream tests write into the real `~/.screenmind`. `TestCaptureWorker.test_pause_resume` saves `capture_paused=true` to `settings.json`, so the next app start is paused, and agent and webhook tests leave files behind. `test/isolate-data-dir` fixes this with an autouse fixture in `tests/conftest.py`. It's pushed to the fork, but there's no upstream PR yet. `test/logging-skip-venv` could go in the same PR or a separate one.

Refs: `tests/conftest.py` (`_isolate_data_dir`), `tests/test_logging_conventions.py` (`_source_files`).

### #23 and #24 stay fork-only for now
Status: blocked on the upstream author

The user closed both upstream PRs. The OCR memory change is tuned for this Mac, and the upstream author can decide later whether they want multi-language OCR. If they ask, rebase `feat/ocr-multilingual` onto upstream `main` without `perf/ocr-memory`. It only needs `OCR_CANVAS_SIZE`, or upstream's default 2560.

Refs: `screenmind/engine/ocr.py`, `local-notes/ocr-findings.md`.

### Russian OCR not checked on live captures
Status: open

Multi-language OCR was checked on a synthetic 5-language screen and on saved screenshots (Telegram, Slack). It hasn't been checked yet on a live frame captured after the change. Look for a frame with Russian text and check `ocr_text` for Cyrillic.

Refs: `activities.ocr_text`, `OCR_LANGUAGES` in `.env`.

### OCR known limits
Status: idea

- The Cyrillic model sometimes puts a Latin `V` inside Russian words (38 of 62 mixed-script words left after cleanup). A post-fix could map `V` to the Cyrillic letter that's most likely in that position.
- At `canvas_size=800`, short words (1–2 letters) are often missed, about 10% of text. 960 keeps 96% at about 4 GB.
- Other scripts (Chinese, Japanese, Arabic) would need another `detector=False` Reader, the same way Cyrillic works.

Refs: `screenmind/engine/ocr.py` (`_merge_readings`, `_fix_lookalikes`, `OCR_CANVAS_SIZE`).

### Workflows app integration
Status: blocked on the user (monorepo path, and whether the workflows app runs on a server or on the laptop)

Goal: use ScreenMind data as a source for the workflows app in the backend monorepo. What we learned:

- The REST API binds to `127.0.0.1:7777` only. `/api/agents/sdk/activities` and `/api/timeline` also reject non-local callers. There's no auth if no PIN is set.
- The MCP server is stdio only (`python -m screenmind.mcp_server`). It fits a local LLM client, not a backend feed.
- Webhooks fire only for `daily_summary`, `bookmark` and `meeting_end`.
- Rows start as `pending` and get their analysis 10–30s later. `since` uses capture time and filters within one `date`. So sync only `status='ok'` rows and track the last synced id.
- Suggested shape if the app is server-side: a small push agent on the laptop polls `/api/agents/sdk/activities` and posts `ok` rows to a backend ingest endpoint, text only by default.

Refs: `screenmind/api/server.py` (localhost-only paths), `screenmind/api/routes/agents.py` (`/sdk/activities`), `screenmind/mcp_server.py`, the `activities` and `dev_contexts` tables.

### Swap Gemma for a vendor LLM API
Status: idea

All model calls go through `screenmind/engine/llm_client.py`, which already speaks the OpenAI `/v1/chat/completions` format. For a vendor API:
- Add an auth header and a `model` field.
- `is_available()` must stop calling `/health`.
- `fast` mode puts a Gemma-only `<think></think>` assistant message at the end. Use `balanced`, or drop that message.
- Screenshots would leave the laptop unredacted. The sensitive-data filter only cleans OCR text.
- Audio uses `input_audio`, which most vendors don't accept.
- `GEMMA_MODE=api` exists but isn't wired to anything.

Refs: `screenmind/engine/llm_client.py`, `screenmind/engine/analyzer.py` (`analyze_screenshot_fast`), `screenmind/main.py:167`.

### Unexplained write of `capture_paused=true`
Status: open, low priority

On 2026-10-06 at 12:02:06, `~/.screenmind/settings.json` got `capture_paused: true` while the app kept capturing. Only `CaptureWorker.pause()` writes that. A test run was ruled out. `CAPTURE_ON_START=true` makes it harmless locally, but the source is still unknown. It could be another instance, for example a dev instance on another port that shares `~/.screenmind`.

Refs: `screenmind/workers/capture_worker.py` (`pause`, `resume`), `screenmind/config.py` (`save_runtime_overrides`).

### Docstring thresholds out of date
Status: idea

The module docstring in `analysis_worker.py` says the pHash cache tiers are `<= 2` and `3-7`. The code uses `<= 3` and `4-10`, with a 240s/420s stale limit. The thresholds are hardcoded, and they set API cost if the model moves to a paid vendor.

Refs: `screenmind/workers/analysis_worker.py` (top docstring and `_process`), `screenmind/capture/dedup.py` (threshold 8).

### Clarify the push policy for `custom`
Status: blocked on the user

Two other sessions relayed different rules: "`custom` stays local, never push it unless the user asks" and "push `custom` to origin after adding backlog docs". This session merged its backlog file into `custom` locally and asked the user before pushing. `origin/custom` on the fork exists from earlier pushes.
