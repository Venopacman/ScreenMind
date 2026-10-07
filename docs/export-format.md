# Export format

ScreenMind can export captured data as a zip archive. The archive has one folder per day and one text file per work session. It is shaped for the workflows app mapper, which reads text files only (64 KB per read, 1 MB per upload, no images). Screenshots are in the archive too, for people.

Code: [`screenmind/export/`](../screenmind/export/) and [`screenmind/api/routes/export.py`](../screenmind/api/routes/export.py). Tests: [`tests/test_export.py`](../tests/test_export.py).

Format version: **1** (`manifest.json` → `schema_version`). Change `SCHEMA_VERSION` in `archive.py` and this file when a field changes meaning or goes away. Adding a field does not need a new version.

## API

Both routes work only from this computer (`127.0.0.1` / `::1`). When a dashboard PIN is set, they need a signed-in dashboard session like every other route.

### `GET /api/export`

Returns `application/zip` as a download.

| Parameter | Default | Meaning |
|---|---|---|
| `user` | required | User id or email. Typed at each export, not saved. Goes into the manifest, every session file and the file name. |
| `date` | | One day, `YYYY-MM-DD` |
| `from`, `to` | | A date range, both included. `to` defaults to `from`. At most 31 days. |
| `screenshots` | `true` | `false` leaves the images out |
| `gap_minutes` | `10` | Idle gap that starts a new session |

Pass `date`, or `from` (and `to`). Errors: `400` for bad or missing dates and a blank user, `422` for a missing `user`, `403` for a client that is not local.

The archive is built in a temp file first, then sent. A day with screenshots is a few hundred MB.

### `GET /api/export/preview`

Same `date` / `from` / `to` / `gap_minutes`. Returns counts per day without building the archive, so the UI can show the size first:

```json
{"date_from": "2026-10-06", "date_to": "2026-10-06",
 "days": [{"date": "2026-10-06", "sessions": 7, "activities": 605, "meetings": 0,
           "ui_events": 0, "screenshots": 605, "screenshot_bytes": 268000000}]}
```

## Archive layout

```
screenmind-export_<user>_<from>[_<to>]/
  manifest.json
  README.md
  2026-10-06/
    sessions/
      01_0941-1006.md
      02_1114-1241.md
      05_1443-1610_part1.md     # a session over the size limit is split
      05_1443-1610_part2.md
    sessions.json
    activities.jsonl
    ui_events.jsonl
    meetings.jsonl
    screenshots/
      812_09-41-02_118_m1.jpg   # <activity id>_<original file name>
```

`<user>` in the folder name keeps only `A-Z a-z 0-9 . _ @ -`; other characters become `_`. The real value is in `manifest.json`.

A day with no activities and no meetings gets no folder. It is still listed in `manifest.json` with zero counts.

Paths inside a day (`files` in `sessions.json`, `screenshot` in `activities.jsonl`, `Screenshot:` lines in `.md`) are relative to the day folder.

## Sessions

A session is a pluggable rule ([`sessions.py`](../screenmind/export/sessions.py)). The rule and its parameters are in `manifest.json` → `session_rule`, so a reader knows how the sessions were made.

Today there is one rule, `idle_gap`: activities are sorted by time, and a new session starts when the gap to the previous capture is `gap_minutes` or more. Capture runs every few seconds while the user works, so a long gap means the user was away, locked the screen or paused capture. On real data (2026-10-06, 605 activities), 10 minutes gives 7 sessions, 5 gives 13, 30 gives 2.

To add a rule: write a class with `name`, `params()` and `split(activities)`, and add it to `SPLITTERS`.

- `session_id` is `<date>#<index>`, for example `2026-10-06#03`. The index starts at 1 each day. It is stable within one export, not across exports with a different rule.
- A meeting is attached to every session it overlaps in time.
- A UI event is attached to the session of its linked activity. If it has no link, it goes to the session whose time span holds it. Otherwise it has no session.

## Files

### `manifest.json`

| Field | Meaning |
|---|---|
| `schema_version` | Format version, `1` |
| `generator`, `app_version`, `git_sha` | `ScreenMind`, the package version, the commit (null for an installed package) |
| `exported_at` | Local time with offset |
| `user` | As typed |
| `date_from`, `date_to` | The range, both included |
| `time_zone` | `name` (for example `CEST`) and `utc_offset` at export time. Every timestamp also carries its own offset, so DST changes inside the range are handled. |
| `session_rule` | `name` and `params` |
| `includes_screenshots` | Whether images are in the archive |
| `privacy` | `activity_status` (`ok`), `url_filter`, `text_filter_types`, `screenshots_redacted` (`false`) |
| `limits` | `text_file_max_bytes`, `screen_text_max_chars_in_md`, `meeting_transcript_max_chars_in_md` |
| `days[]` | Per day: `date`, `sessions`, `activities`, `meetings`, `ui_events`, `screenshots`, `screenshots_missing` |
| `totals` | Sums of the day counts |

`screenshots_missing` counts activities whose image file is gone (deleted, outside the screenshots folder, or encrypted with a key that is not available).

### `sessions/*.md`

One Markdown file per session, for the mapper and for people. Each file is under 900,000 bytes (`MAX_TEXT_FILE_BYTES`). A larger session is split into `_part1.md`, `_part2.md` and so on, at activity boundaries. Each part has the full header with "part N of M".

Header: session id, user, start and end time, counts, the top apps. Then a note that the content was read from the screen and is an assumption.

Then `## Meetings` (if any) and `## Timeline`. Each activity is one block:

```
### 16:46:02 | Google Chrome | meeting
Window: Meet - distillery daily #2
URL: https://meet.google.com/dsv-einb-rbg
Summary: ...
Details: ...
User actions: ...
Repo: ScreenMind (custom)
Screenshot: screenshots/1290_16-46-02_118_m2.jpg
Screen text:
```

Screen text is `organized_text` if there is one, else `ocr_text` (a11y or OCR text). It is cut at 4,000 characters, with a note that the full text is in `activities.jsonl`. When it is the same as the previous activity's text, the block says "Screen text: same as above." Meeting transcripts are cut at 20,000 characters.

### `sessions.json`

A list, one object per session: `session_id`, `start`, `end`, `activities`, `meetings`, `ui_events`, `apps` (sorted names), `files` (the `.md` paths).

### `activities.jsonl`

One JSON object per line, one line per captured screen, oldest first. Full text, no cuts.

| Field | Meaning |
|---|---|
| `id` | `activities.id` |
| `timestamp` | Local time with offset |
| `session_id` | |
| `app` | `app_name`, or `detected_app` if empty |
| `detected_app` | App name from the OS |
| `window_title` | |
| `url` | Sanitized browser URL or null |
| `category`, `summary`, `details`, `scene_description`, `mood`, `confidence` | From the Gemma analysis |
| `screen_text` | `ocr_text`: a11y text or OCR text |
| `organized_text` | OCR text in layout order, or null |
| `visible_text` | Key snippets, a list |
| `user_actions` | Readable summary of the UI events for this frame |
| `analysis_method` | For example `full:fast`, `cache:minor` |
| `bookmarked` | |
| `dev_context` | null, or `repo`, `branch`, `last_commit`, `changed_files`, `insertions`, `deletions` |
| `screenshot` | Path in the day folder, or null |

Left out on purpose: `ocr_boxes`, `embedding`, `screenshot_path` (a local path), `status`, `analyzed`, `analysis_error`.

### `ui_events.jsonl`

One line per event: `id`, `timestamp`, `session_id`, `activity_id`, `type` (`click`, `text`, `app_switch`, `window_focus`, `clipboard`), `app`, `window_title`, `url`, `element_role`, `element_name`, `element_value`, `text`, `x`, `y`.

### `meetings.jsonl`

One line per recorded call: `id`, `start`, `end`, `session_ids`, `app`, `window_title`, `url`, `duration_minutes`, `summary`, `transcript` (full).

## Privacy

- Only activities with `status = 'ok'` are exported. UI events linked to other rows are left out. Unlinked UI events are kept.
- URLs go through `sanitize_url()` again, because rows from before `55a9198` / `0fcf92a` may hold raw tokens.
- All text fields go through the sensitive-data filter again: window titles, summaries, screen text, UI event text, meeting transcripts, commit messages. The filter types are the defaults (`credit_card`, `ssn`, `api_key`, `jwt`, `password`) plus any extra types turned on in settings. This runs even when the filter is off in settings.
- Screenshots are not redacted. They show the screen as it was. Encrypted screenshots are decrypted into the archive.
- The archive is built in the system temp folder and deleted after it is sent.
