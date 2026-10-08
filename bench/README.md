# Replay benchmark

Checks that analysis still does the right thing on real frames. It replays stored frames through `AnalysisWorker._process` and checks the row that analysis writes: category, summary keywords, text source and size, `active_url`, and time per frame.

- It doesn't grab the screen and doesn't write to `~/.screenmind`. Each run uses a throwaway data dir.
- It reads the main instance's `.env` and `settings.json`, so OCR languages and analysis mode match the main instance.
- It uses the shared `llama-server` on port 5809, the same way the main instance does.

## Data

Fixtures hold real screen content, so they stay outside the repo, in `~/.screenmind-bench/` (override with `SCREENMIND_BENCH_DIR`):

```
fixtures/<name>/screenshot.jpg   the frame
fixtures/<name>/capture.json     what the capture side saw: app, title, a11y text, browser URL, user actions
scenarios.yaml                   expectations per fixture (start from bench/scenarios.example.yaml)
runs/<timestamp>.json            results of each run, with the code version
```

## Use

From the repo or worktree root:

```bash
uv run python bench/replay.py run
```

It runs every scenario. The exit code is 1 if any scenario fails.

```bash
uv run python bench/replay.py run --only slack_dm meet_call --repeat 3
```

`--repeat` runs each scenario several times, to see how much Gemma's output varies.

```bash
uv run python bench/replay.py freeze 918 --name menubar_only_display
```

`freeze` copies activity 918 from `~/.screenmind/screenmind.db` into a fixture. Then add an entry for it in `scenarios.yaml`.

- Frame time is about 10 s with Gemma. Rule-based frames (empty displays) take under 1 s. The first frame also loads OCR.
- Each scenario prints `PASS`, `FAIL` or `GAP`. `GAP` means the scenario failed but has a `known_gap:` reason. Use it for problems that are known and parked, so they don't hide new failures.

## Checks

All of these are optional, per scenario under `expect:`:

| Check | Passes when |
|---|---|
| `status` | the row status equals this |
| `app_name_any` | the stored app contains one of these |
| `category_in`, `category_not_in` | the category is (not) in the list |
| `summary_any` | one of these words is in summary + details |
| `summary_none` | none of these words is in summary + details |
| `summary_not_generic` | no "is interacting with" / "likely" phrasing |
| `text_any`, `text_none` | the stored screen text has / doesn't have these |
| `text_min_chars`, `text_max_chars` | the stored screen text length |
| `text_source_in` | `a11y`, `ocr`, `a11y+ocr` or `none` |
| `active_url`, `active_url_prefix` | the stored URL (`null` = must be empty) |
| `max_seconds` | time to analyze the frame |

## Notes

- `freeze` stores what the capture side recorded (`detected_app`), not the app name analysis wrote. A frame with no app and no title stays unlabeled, the way capture stores an empty display.
- If the capture side changes what it records, re-freeze the affected fixtures, or add a new fixture next to the old one. Old fixtures keep the old labels.
