# ScreenMind as a data source for the workflows questionnaire

From the session "Questionnaire extraction from screenshots" (2026-10-06).

Question (1.1): can the workflows app (backend-monorepo, `services/workflows`) fill the personal workflow record (`RegisterExistingPersonalWorkflowCommand`) from screenshots plus accessibility data? We assume a per-user incremental feed: a first batch after a week, then hourly.

## Where things stand

The answer at end of day: partly. Screen data can now draft `title`, `description`, `tools_used` and `ai_usage`, plus part of `inputs`/`outputs` and `stakeholders`. `duration`, `trigger` and `frequency` still need UI events and more days of data. The "why" fields (`closing_decisions`, `quality_standard`, `feedback_loops`, `pain_points`) need the person.

Changes that moved the data today, all on `custom`: `8198161` (a11y text, browser URL, Electron titles), `6e01292` (Cyrillic with RapidOCR, `active_url` only from the browser), `117e37e` + `1ca9352` (empty displays), `7fa39c5` + `d1c2b23` (per-display Claude titles), `0fcf92a` (URL sanitizer).

Measured on live frames, morning vs evening:
- Frames whose title is just the app name: 45% -> 0%.
- OCR dictionary-word ratio: 0.57 -> 0.74.
- Generic summaries: 69% -> 40-64% live, and 8% in the replay benchmark.

## Tools

- **Replay benchmark, `bench/replay.py`.** On branch `claude/questionnaire-screenshots-extraction-1904e8` (`d563d8b`, `a477a15`). **Not merged into custom, not pushed.**
  - It freezes real frames as fixtures and replays them through `AnalysisWorker._process` against a throwaway data dir, then checks the stored row.
  - Fixtures and `scenarios.yaml` are in `~/.screenmind-bench/`. They hold private screen content, so they stay outside the repo.
  - Last score on `custom` (`752b04c`): 8 pass, 3 known gaps, 1 fail.
- **Data-quality scan, `~/.screenmind-bench/scan/scan.py`.** It reads `~/.screenmind` read-only, with snapshots and a running log (`notes.md`) in the same folder. It is not in the repo and not scheduled.

## Items

### Merge the replay benchmark into custom
Status: open (waiting for the user's decision)

`bench/` exists only on the session branch. Without it on `custom`, other sessions can't check capture or analysis changes against real frames. It may also need a small README.

Refs: branch `claude/questionnaire-screenshots-extraction-1904e8`, `bench/replay.py`, `bench/scenarios.example.yaml`.

### UI events are not enabled
Status: blocked (the user has to turn on UI Events in Settings and grant Input Monitoring + Accessibility)

`ui_events` has 0 rows and `activities.user_actions` is empty, so `duration`, `trigger` and time per site (`ui_events.url`, v9) have no data. This is the biggest gap left for the record. Once it is on, check `app_switch` timing and the `activity_id` link with the scan.

Refs: `screenmind/capture/ui_events/`, settings `ui_events_enabled`, `ui_events_types`.

### Drop file:// URLs in the sanitizer
Status: open

Activity 654 stored `file:///Users/pavel/Downloads/...` as `active_url`, which leaks a local path and is never a work page. `sanitize_url()` should return None for `file:` like it does for `chrome:`.

Refs: `screenmind/privacy/url_filter.py` (`sanitize_url`).

### Do not backfill rows from before 55a9198
Status: open (rule for the feed design)

Activities 437, 441 and 443 still hold unsanitized OAuth/Keycloak URLs with tokens. The user chose to let retention remove them (about 7 days). A feed or export must skip or re-sanitize `active_url` on rows older than the sanitizer.

Refs: `0fcf92a`, `activities.active_url`.

### Slack summaries are generic
Status: open

38 of 71 Slack frames (15:20-16:10) got summaries like "viewing a Slack channel conversation", even though the window title names the channel or DM. The benchmark scenario `slack_channel_goals` fails because the cycle goals are visible but not mentioned. Make the analysis prompt use the title and the OCR text, and check whether Gemma E2B is enough.

Refs: `screenmind/engine/analyzer.py` (`analyze_screenshot_fast` prompt), bench scenarios `slack_channel_goals`, `slack_dm`.

### Meetings are seen but not recorded
Status: open (covered in `multi-display-capture.md`, "Calls that are not the focused window are not detected")

From 16:31 to 17:24, "Meet - distillery daily #2" showed in 24 frames with one stable room URL (`meet.google.com/<room>`), and `meetings` stayed empty. The same room URL across days also gives the recurring-meeting `frequency`.

### Normalize app names for tools_used
Status: open

The same tool shows up under several names: "Claude Code" next to "Claude", and older rows have "120×30", "iCloud Notes" vs "Notes", and page titles as app names. `tools_used` needs one canonical name per tool, either at capture time or in the feed.

Refs: activity ids 43, 45, 55, 63, 265, 270; `analysis_worker`, `analyzer.KNOWN_APP_CATEGORIES`.

### Work/personal flag per frame
Status: idea

Telegram chats and personal browsing (for example a Russian real-estate article) land in the same data as work. Before the hourly feed ships, it needs an app/site allowlist or a per-frame work/personal flag.

Refs: bench scenario `telegram_personal`; activity ids 340-381 (Telegram, personal pages).

### Granola frames described as a locked screen
Status: open

Granola (meeting notes) frames 456-460 got category `idle` or `terminal` and the summary "locked or restricted system access screen". Notes taken in a meeting are good input for the record. Freeze one frame with real notes as a bench scenario and check the analysis.

### Refresh the benchmark scenarios
Status: open

Add scenarios from today's live frames: a titled Claude Code frame (504-507), a Chrome frame with a real URL (452/465, GitLab MR), the Meet call (639-684) and Granola. Retire the three known-gap fixtures that carry pre-fix labels (`empty_display_chrome_title`, `idle_wallpaper`, and the OCR-URL case), because current capture no longer produces that input.

Refs: `bench/replay.py freeze <id> --name <scenario>`, `~/.screenmind-bench/scenarios.yaml`.

### Design the per-user feed for the workflows app
Status: idea

The mapper agent reads text attachments only: 64 KB per read, 1 MB per upload, and no images (`AttachmentNotTextError`). An hourly feed item should be text: app, title, sanitized URL, a short OCR excerpt, and UI events once they exist. The closest existing model is the Evolve Coach intake (`POST /intake/chat-events`). The mapper prompt should follow `ai_session_agent_wip/workflow-mapper-from-sessions-prompt.md`: every screen-derived value is an assumption, and the person signs off.

Refs (backend-monorepo): `services/workflows/src/bc/workflows/agent/application/ai_session_agent_wip/`, `.../conversation/application/attachment/get_attachment_content_use_case.py`, `evolve-coach/contracts/src/chat-event.ts`.

### Frequency needs 1-2 weeks of capture
Status: blocked (on time)

There are about 8 hours of data over 2 days. The mapper looks back 12 weeks. Keep capture running before judging `frequency`.
