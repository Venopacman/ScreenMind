# Packaging spikes: shared log

The shared place for packaging experiments on each machine. The plan is in [packaging.md](packaging.md). Each machine adds its results to its own section. Keep the same table rows, so results compare side by side.

Build files: [`packaging/screenmind.spec`](../../packaging/screenmind.spec) and [`packaging/entry.py`](../../packaging/entry.py). OCR resource benchmark: [`packaging/ocr_mem_bench.py`](../../packaging/ocr_mem_bench.py). Output goes to `packaging/build/` and `packaging/dist/` (both in `.gitignore`). PyInstaller is not a project dependency. `uv run --with pyinstaller` pulls it in for the one command.

## Results

| | macOS (arm64) | Windows (x64) |
|---|---|---|
| Date, commit | 2026-10-07, `custom` cd95561 (after the strip) | not run yet |
| OS | macOS 26 (Darwin 25.6) | |
| PyInstaller | 6.22.3, Python 3.14.6 | |
| Build time | 2 min 21 s cold, 25 s warm | |
| Build result | OK. Only harmless warnings (pyobjc lazy names, Windows DLLs on macOS, `scipy.special._cdflib`) | |
| App size on disk | 343 MB (`ScreenMind.app`); 363 MB before the strip | |
| Compressed | DMG 157 MB (zlib), 122 MB (LZMA) | |
| Largest parts | onnxruntime 70 MB, cv2 40 MB + its ffmpeg/X11 libs, scipy 32 MB, libpython 17 MB | |
| Signature | ad-hoc (PyInstaller default), bundle id `com.screenmind.app` | |
| Dashboard up after | 2 s | |
| `/`, `/css/styles.css`, `/js/core.js` | 200 | |
| `/api/status`, `/api/timeline`, `/api/settings`, `/api/models`, `/api/search?q=test`, `/api/stats` | 200 | |
| onnxruntime loads | yes, before the strip (the embedder loaded). After the strip only OCR uses it, and OCR was not run | |
| OCR, capture, UI events | not tested (no screen grabs from Claude sessions on macOS) | |
| Memory after start | 123 MB RSS (317 MB before the strip, with the embedder) | |
| Clean stop | `POST /api/shutdown`, stopped in 2 s | |
| OCR benchmark, `default`, 20 frames (memory at end / max; CPU) | 1770 / 1770 MB footprint; 7.1 CPU-s and 1.46 s wall per frame (3024x1964 frames, 10 cores) | |
| OCR benchmark, `tuned`, 20 frames | 1314 / 1514 MB footprint; 4.3 CPU-s and 2.14 s wall per frame | |
| OCR benchmark, `tuned`, 60 frames | 1072 / 1554 MB footprint; 4.4 CPU-s per frame | |

## macOS notes (2026-10-07)

- Ran the binary inside the bundle (`Contents/MacOS/ScreenMind`) from a scratch folder, so no `.env` loaded. Env: temp `DATA_DIR`, `API_PORT=7790`, `CAPTURE_ON_START=false`, `UI_EVENTS_ENABLED=false`, `MEETING_TRANSCRIPTION=false`, a dead llama port, and a `PATH` without Homebrew. It started in degraded mode ("llama-server not found"), as expected.
- Not tested: launching from Finder, permission prompts, screen grabs, OCR on a real frame, model download (fails by design until fix F2 in the plan).
- OCR benchmark: run with the worktree's `.venv` on saved screenshots from `~/.screenmind/screenshots/2026-10-07`. Memory is `phys_footprint` (what Activity Monitor shows). RSS was 5-10% higher. Halving the image size first changed almost nothing. Analysis is in [packaging.md, Resource budget](packaging.md#resource-budget).
- The bundle holds rapidocr's own default models (`PP-OCRv6_*_small`), which ScreenMind never uses (fix F6).

## Windows: how to run the same spike

For the Windows laptop session. It only builds and starts the app. It installs nothing system-wide.

1. Update and sync, in the repo root:

   ```powershell
   git pull origin custom
   uv sync
   ```

2. Build (takes a few minutes):

   ```powershell
   Measure-Command { uv run --with pyinstaller pyinstaller packaging/screenmind.spec --noconfirm --distpath packaging/dist --workpath packaging/build | Out-Default }
   ```

3. Size, unpacked and zipped:

   ```powershell
   "{0:N0} MB" -f ((Get-ChildItem -Recurse packaging\dist\ScreenMind | Measure-Object Length -Sum).Sum / 1MB)
   Compress-Archive packaging\dist\ScreenMind "$env:TEMP\ScreenMind-spike.zip" -Force
   "{0:N0} MB" -f ((Get-Item "$env:TEMP\ScreenMind-spike.zip").Length / 1MB)
   ```

4. Start it with a temp data dir, a spare port and capture off. The build is windowed (no console), so set a log file:

   ```powershell
   $repo = (Get-Location).Path
   $d = "$env:TEMP\sm-spike"; New-Item -ItemType Directory -Force $d | Out-Null
   $env:DATA_DIR = $d; $env:SCREENMIND_DATA_DIR = $d; $env:SCREENMIND_LOG_FILE = "$d\screenmind.log"
   $env:API_PORT = "7790"; $env:SETUP_COMPLETE = "true"; $env:CAPTURE_ON_START = "false"
   $env:UI_EVENTS_ENABLED = "false"; $env:MEETING_TRANSCRIPTION = "false"
   $env:LLAMA_SERVER_HOST = "http://127.0.0.1:5898"; $env:LLAMA_SERVER_PORT = "5898"
   Push-Location $env:TEMP
   Start-Process "$repo\packaging\dist\ScreenMind\ScreenMind.exe"
   Pop-Location
   ```

5. Check it answers (wait a few seconds first):

   ```powershell
   foreach ($u in "/", "/css/styles.css", "/js/core.js", "/api/status", "/api/timeline", "/api/settings", "/api/models", "/api/search?q=test") { "$u " + (curl.exe -s -o NUL -w "%{http_code}" "http://127.0.0.1:7790$u") }
   Get-Content "$env:TEMP\sm-spike\screenmind.log" -Tail 40
   ```

6. Optional, Windows only (grabs work from Claude-started processes there): in the dashboard on port 7790, turn on UI events and start capturing for a minute. Check that screenshots, a11y text and clicks arrive. This tests `uiautomation`/`comtypes` and `mss` inside the frozen app. The spec collects `uiautomation` DLLs and `comtypes` submodules on Windows; that part is untested.

7. Memory of the running app (Task Manager's "Memory" column is the private working set):

   ```powershell
   uv run --with psutil python -c "import psutil; p=[x for x in psutil.process_iter(['name']) if x.info['name']=='ScreenMind.exe']; print([round(x.memory_full_info().uss/2**20) for x in p], 'MB')"
   ```

8. OCR resource benchmark (no screen grabs; uses saved screenshots from `%USERPROFILE%\.screenmind\screenshots`):

   ```powershell
   uv run --with psutil python packaging/ocr_mem_bench.py default
   uv run --with psutil python packaging/ocr_mem_bench.py tuned
   ```

9. Stop it: the dashboard's Stop Server button, or

   ```powershell
   curl.exe -s -X POST http://127.0.0.1:7790/api/shutdown
   ```

10. Fill in the Windows column above and add a "Windows notes" section: errors, missing modules from `packaging\build\screenmind\warn-screenmind.txt` that matter, and anything that differs from macOS. Commit only this file (and spec fixes, if any).
