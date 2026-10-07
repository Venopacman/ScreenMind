# Packaging spikes: shared log

The shared place for packaging experiments on each machine. The plan is in [packaging.md](packaging.md). Each machine adds its results to its own section. Keep the same table rows, so results compare side by side.

Build files: [`packaging/screenmind.spec`](../../packaging/screenmind.spec) and [`packaging/entry.py`](../../packaging/entry.py). Output goes to `packaging/build/` and `packaging/dist/` (both in `.gitignore`). PyInstaller is not a project dependency. `uv run --with pyinstaller` pulls it in for the one command.

## Results

| | macOS (arm64) | Windows (x64) |
|---|---|---|
| Date, commit | 2026-10-07, `custom` 70e5611 (before the strip) | not run yet |
| OS | macOS 26 (Darwin 25.6) | |
| PyInstaller | 6.22.3, Python 3.14.6 | |
| Build time | 2 min 21 s | |
| Build result | OK. Only harmless warnings (pyobjc lazy names, Windows DLLs on macOS, `scipy.special._cdflib`) | |
| App size on disk | 363 MB (`ScreenMind.app`) | |
| Compressed | DMG 169 MB (zlib), 130 MB (LZMA) | |
| Largest parts | cv2 118 MB, onnxruntime 70 MB, scipy 32 MB, libpython 17 MB | |
| Signature | ad-hoc (PyInstaller default), bundle id `com.screenmind.app` | |
| Dashboard up after | 2 s | |
| `/`, `/css/styles.css`, `/js/core.js` | 200 | |
| `/api/status`, `/api/timeline`, `/api/settings`, `/api/models`, `/api/search?q=test` | 200 | |
| onnxruntime loads | yes (embedder model loaded, 384 dims) | |
| OCR, capture, UI events | not tested (no screen grabs from Claude sessions on macOS) | |
| Memory after start | 317 MB RSS | |
| Clean stop | `kill -INT`, stopped in 2 s | |

## macOS notes (2026-10-07)

- Ran the binary inside the bundle (`Contents/MacOS/ScreenMind`) from a scratch folder, so no `.env` loaded. Env: temp `DATA_DIR`, `API_PORT=7790`, `CAPTURE_ON_START=false`, `UI_EVENTS_ENABLED=false`, `MEETING_TRANSCRIPTION=false`, a dead llama port, and a `PATH` without Homebrew. It started in degraded mode ("llama-server not found"), as expected.
- Not tested: launching from Finder, permission prompts, screen grabs, OCR on a real frame, model download (fails by design until fix F2 in the plan).
- The bundle holds rapidocr's own default models (`PP-OCRv6_*_small`), which ScreenMind never uses (fix F7).

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

7. Stop it: the dashboard's Stop Server button, or

   ```powershell
   curl.exe -s -X POST http://127.0.0.1:7790/api/shutdown
   ```

8. Fill in the Windows column above and add a "Windows notes" section: errors, missing modules from `packaging\build\screenmind\warn-screenmind.txt` that matter, and anything that differs from macOS. Commit only this file (and spec fixes, if any).
