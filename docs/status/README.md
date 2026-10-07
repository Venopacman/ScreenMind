# Collection status per machine

What ScreenMind really collects on each machine, proven by an end-to-end run.

[`docs/architecture/capture.md`](../architecture/capture.md) says what the code is meant to collect. The files here say what a real run on a real machine collected. When they disagree, this folder shows the bug.

| File | What |
|---|---|
| [`summary.md`](summary.md) | One table, all machines side by side. Generated. |
| [`macos.md`](macos.md) | The Mac (main development machine). |
| [`windows.md`](windows.md) | The Windows laptop. |

Each machine writes only its own file. So two machines never edit the same file, and `git pull` stays clean.

## How to run

Run from the repo root, with the repo's venv (`uv sync` creates it; run it after a pull that changed dependencies). Use a normal terminal. `uv run python scripts/e2e_collect.py --write-status` works too, on both OSes.

macOS, from Terminal.app (not from a Claude session, see below):

```bash
.venv/bin/python scripts/e2e_collect.py --write-status
```

Windows, from PowerShell or Windows Terminal:

```
.venv\Scripts\python.exe scripts\e2e_collect.py --write-status
```

Then commit and push:

```bash
git add docs/status && git commit -m "docs(status): e2e run on macos" && git push origin custom
```

A run takes about 3 to 5 minutes. Most of it is waiting for Gemma.

**Hands off during the first minute.** The script switches apps, clicks and types by itself. If you click somewhere else at the same time, the test windows are in front too briefly to be captured, and the guards stop the typing. The rows then FAIL or SKIP for no real reason. Wait until it prints "You can use the computer again".

**macOS: why Terminal.app.** macOS gives permissions to the app that started the process. A process started from a Claude session can't grab the screen reliably (gap G1), and the results would be wrong. Terminal.app needs Screen Recording, Accessibility and Input Monitoring, the same as for the main instance.

**Windows: you type by hand.** ScreenMind ignores injected (programmatic) input on Windows. So the script stops twice and tells you what to click and type: once in Notepad, once in a small password box. Read the instructions, press Enter, then do it.

## What a run does

1. Starts ScreenMind with a temp data dir and a free port in 7900-7999. It never uses `~/.screenmind` or port 7777, so the main instance is not touched. UI events are on for this run only.
2. Before startup, puts 30-day-old rows into the temp DB, to test retention.
3. Opens `https://example.com/` in the default browser (and in `--browsers`, if given).
4. Opens a scratch text file in TextEdit or Notepad.
5. Clicks inside it and types two lines: a marker and `password: <test secret>`.
6. Opens a password box (its own dialog) and types a second test secret.
7. Puts a marker on the clipboard. On macOS the old clipboard text is put back at the end. Images on the clipboard are lost.
8. Waits for analysis (up to 4 minutes), stops ScreenMind and reads the DB.
9. Deletes the temp data dir (`--keep-data` keeps it).

Safety rules in the script:

- It types only into the scratch file and the password box it opened itself. Before every key it checks that the right window is in front. If not, it stops typing.
- It clicks only inside the scratch file's window.
- The browser tab stays open. Close it yourself.
- The evidence in the report shows only test data: the scratch file, example.com, the markers. The test secrets are masked.

## How to read a status file

Header: date, machine, OS version, git branch and sha, what the terminal is allowed to do (permissions), displays, input mode, and the command.

Results, one row per data point:

- **PASS**: the data point was collected. The evidence shows what was found (row counts, sample values).
- **FAIL**: it was checked and is missing or wrong. The evidence says what was found instead.
- **SKIP**: the run could not check it. For example, no call was active, or the by-hand step was skipped.
- **Known gaps**: ids from [capture.md section 8](../architecture/capture.md#8-gaps) that are related to this data point on this OS. A FAIL there is often one of them.

| Data point | What PASS means |
|---|---|
| Screenshots, every display | Each display has at least one saved JPEG. |
| Screen grab backend | The best backend was used the whole run: `sck` on macOS, `mss` on Windows. A fallback is a FAIL. |
| App name | A frame has `detected_app` = the scratch editor. |
| Window title | A frame of the editor has the scratch file name in `window_title`. |
| Screen text from accessibility | The marker from the scratch file is in `ocr_text` of an editor frame (read through a11y). |
| OCR text | At least one frame was read with OCR (`analysis_method` contains `ocr`). |
| Browser URL (name) | A frame of that browser has `active_url` on example.com. One row per browser seen. |
| UI event: click | A `click` event in the editor, with an element role. |
| UI event: app switch | An `app_switch` event to the editor. |
| UI event: typed text | A `text` event with the typed marker. |
| UI event: clipboard | A `clipboard` event with the clipboard marker. |
| UI events linked to frames | `activities.user_actions` is set on some frames. |
| Typed secret is redacted | The value after `password:` is in no text column of any table. |
| Password field is not stored | The password box secret is in no text column. `[password field]` was recorded instead. |
| Call detection | A `meetings` row exists. SKIP when no call was active. To test it, run the check during a call. |
| Analysis (Gemma) | At least one frame has `status='ok'`, with a category and summary. |
| Retention cleanup at startup | The 30-day-old rows and JPEG are gone after startup. |

## Rules

1. **One file per machine.** Only that machine writes it. The default name is the OS (`macos.md`, `windows.md`). A second machine with the same OS uses `--status-name`, for example `--status-name windows-desktop`.
2. **Don't edit status files by hand.** Run the check again. The file has a JSON block at the end that `summary.md` is built from.
3. **Commit your status file and `summary.md` together.** On a merge conflict in `summary.md`, run `python scripts/e2e_collect.py --summary` and commit the result.
4. **Run it again** after a change to capture code (the list in [capture.md section 10](../architecture/capture.md#10-how-to-update)), after a permission change, and before you say "X works on Windows".
5. **A new FAIL is a gap.** If it is not in [capture.md section 8](../architecture/capture.md#8-gaps) yet, add it there (and to the backlog), then link it from the check in `CHECKS` in the script.
6. **Only test data in the evidence.** If you add a check, show counts or test markers, not real screen content.

## Options

| Option | What it does |
|---|---|
| `--write-status` | Write `docs/status/<name>.md` and `summary.md`. Without it, the run only prints. |
| `--status-name NAME` | File name for this machine. Default: the OS. |
| `--browsers LIST` | Also open the test URL in these browsers. macOS: app names, `"Safari,Google Chrome,Firefox"`. Windows: `chrome,msedge,firefox`. |
| `--manual` | macOS: click and type by hand instead of injecting input. Tests the real input path. |
| `--no-input` | Skip the click, typing and password steps. Those rows become SKIP. |
| `--manual-seconds N` | Time for the by-hand typing step (default 30). |
| `--analysis-timeout N` | Max seconds to wait for Gemma (default 240). |
| `--keep-data` | Keep the temp data dir and print its path. |
| `--summary` | Only rebuild `summary.md` from the status files. |
