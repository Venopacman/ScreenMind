# Packaging spikes: shared log

The shared place for packaging experiments on each machine. The plan is in [packaging.md](packaging.md). Each machine adds its results to its own section. Keep the same table rows, so results compare side by side.

Build files: [`packaging/screenmind.spec`](../../packaging/screenmind.spec) and [`packaging/entry.py`](../../packaging/entry.py). OCR resource benchmark: [`packaging/ocr_mem_bench.py`](../../packaging/ocr_mem_bench.py). Output goes to `packaging/build/` and `packaging/dist/` (both in `.gitignore`). PyInstaller is not a project dependency. `uv run --with pyinstaller` pulls it in for the one command.

## Results

| | macOS (arm64) | Windows (x64) |
|---|---|---|
| Date, commit | 2026-10-07, `custom` cd95561 (after the strip) | 2026-10-08, `custom` 4f98416 |
| OS | macOS 26 (Darwin 25.6) | Windows 11 Home 25H2 (build 26200), Ryzen 7 5800H (16 logical), 16 GB |
| PyInstaller | 6.22.3, Python 3.14.6 | 6.22.3, Python 3.14.8 (uv) |
| Build time | 2 min 21 s cold, 25 s warm | 2 min 45 s cold, 1 min 36 s warm |
| Build result | OK. Only harmless warnings (pyobjc lazy names, Windows DLLs on macOS, `scipy.special._cdflib`) | OK after a spec fix: the first build took `msvcp140.dll`, `vcruntime140.dll` and `ucrtbase.dll` from a JDK on `PATH` and crashed on the first OCR frame (see notes). Harmless warnings: `UIAutomationClient_VC140_*.dll` "not found" (bundled anyway), `scipy.special._cdflib`, `tzdata`, `pycparser` tables |
| App size on disk | 343 MB (`ScreenMind.app`); 363 MB before the strip | 394 MB (`dist\ScreenMind\`, 331 files) |
| Compressed | DMG 157 MB (zlib), 122 MB (LZMA) | zip 172 MB (`Compress-Archive`) |
| Largest parts | onnxruntime 70 MB, cv2 40 MB + its ffmpeg/X11 libs, scipy 32 MB, libpython 17 MB | cv2 112 MB (`cv2.pyd` 82 MB + ffmpeg DLL 29 MB), scipy 46 MB + `scipy.libs` 19 MB, onnxruntime 36 MB, rapidocr 30 MB, `numpy.libs` 20 MB. Two OpenBLAS copies (numpy + scipy), 39 MB. Tcl/Tk DLLs (5 MB) came along |
| Signature | ad-hoc (PyInstaller default), bundle id `com.screenmind.app` | unsigned |
| Dashboard up after | 2 s | 6-7 s (first start, includes the DB migration) |
| `/`, `/css/styles.css`, `/js/core.js` | 200 | 200 |
| `/api/status`, `/api/timeline`, `/api/settings`, `/api/models`, `/api/search?q=test`, `/api/stats` | 200 | 200 |
| onnxruntime loads | yes, before the strip (the embedder loaded). After the strip only OCR uses it, and OCR was not run | yes, OCR ran on a real frame (after the spec fix) |
| OCR, capture, UI events | not tested (no screen grabs from Claude sessions on macOS) | all work: `mss` grabs, a11y text, the UI events hook (clicks trigger grabs), OCR on a Photos window. Before the spec fix the app crashed on the first OCR frame |
| Memory after start | 123 MB RSS (317 MB before the strip, with the embedder) | 64 MB private working set (USS), 88 MB working set. After one OCR frame: 175 MB USS, 229 MB working set |
| Clean stop | `POST /api/shutdown`, stopped in 2 s | `POST /api/shutdown`, stopped in 1.3 s |
| OCR benchmark, `default`, 20 frames (memory at end / max; CPU) | 1770 / 1770 MB footprint; 7.1 CPU-s and 1.46 s wall per frame (3024x1964 frames, 10 cores) | 466 / 466 MB USS; 51.0 CPU-s and 6.83 s wall per frame (2560x1600 frames, 16 logical CPUs) |
| OCR benchmark, `tuned`, 20 frames | 1314 / 1514 MB footprint; 4.3 CPU-s and 2.14 s wall per frame | 453 / 454 MB USS; 24.2 CPU-s and 12.32 s wall per frame |
| OCR benchmark, `tuned`, 60 frames | 1072 / 1554 MB footprint; 4.4 CPU-s per frame | not run |

## macOS notes (2026-10-07)

- Ran the binary inside the bundle (`Contents/MacOS/ScreenMind`) from a scratch folder, so no `.env` loaded. Env: temp `DATA_DIR`, `API_PORT=7790`, `CAPTURE_ON_START=false`, `UI_EVENTS_ENABLED=false`, `MEETING_TRANSCRIPTION=false`, a dead llama port, and a `PATH` without Homebrew. It started in degraded mode ("llama-server not found"), as expected.
- Not tested: launching from Finder, permission prompts, screen grabs, OCR on a real frame, model download (fails by design until fix F2 in the plan).
- OCR benchmark: run with the worktree's `.venv` on saved screenshots from `~/.screenmind/screenshots/2026-10-07`. Memory is `phys_footprint` (what Activity Monitor shows). RSS was 5-10% higher. Halving the image size first changed almost nothing. Analysis is in [packaging.md, Resource budget](packaging.md#resource-budget).
- The bundle holds rapidocr's own default models (`PP-OCRv6_*_small`), which ScreenMind never uses (fix F6).

## Windows notes (2026-10-08)

- Ran steps 1-9 below from a Claude session in a worktree, with temp data dirs under `C:\Users\pdsmi\sm-spike*`, port 7790 and a dead llama port. It started in degraded mode ("Starting without Gemma 4"), as expected.
- **The first build crashed on the first OCR frame.** Windows Error Reporting: `ScreenMind.exe`, faulting module `_internal\MSVCP140.dll` 14.16.27033, `0xc0000005`. PyInstaller resolves the DLLs that extensions link to through `PATH`. On this laptop `C:\Program Files\Microsoft\jdk-11.0.16.8-hotspot\bin` comes before `System32`, so the bundle got the JDK's `msvcp140.dll`, `vcruntime140.dll`, `ucrtbase.dll` and about 40 `api-ms-win-*` stubs, all from 2019-2022. onnxruntime needs a newer `msvcp140.dll` (14.40 or later, the `std::mutex` change in VS 2022 17.10). Fix in the spec: on Windows, `PATH` keeps only folders under `%SystemRoot%` during the build. The bundle now has `msvcp140.dll` and `vcruntime140.dll` 14.51 from `System32` and no `ucrtbase`/`api-ms-win-*` copies. Any build machine with Java, an old app or a dev tool on `PATH` would have hit this; a CI runner too.
- After the fix: a Photos window (no a11y text) went through OCR in the frozen app. OCR models came from the shared `%USERPROFILE%\.screenmind\models\ocr` (they were already there, so no download). The models folder ignores `DATA_DIR`.
- UI events: the low-level hooks install and clicks trigger grabs. `UIAutomationClient_VC140_X64.dll` lands in `_internal\uiautomation\bin\`, where `uiautomation` loads it from, so the build warning about it does not matter.
- F4 seen for real: the first start wrote `launcher.vbs` into the data dir. It runs `ScreenMind.exe "...\_internal\screenmind\launcher.py"`, which cannot work. It also tried to put `ScreenMind.lnk` on the desktop. Nothing appeared there: this laptop's desktop is `OneDrive\<localized name>`, not `%USERPROFILE%\Desktop`, and the PowerShell step fails without an error.
- OCR costs much more CPU than on the Mac: 24 CPU-s and 12 s wall per frame tuned, against 4.3 CPU-s on the Mac, with smaller frames (2560x1600). Memory is much lower (450 MB against 1.3-1.8 GB). Not looked into. At a 10 s interval, OCR on every frame would not keep up on this laptop; it only runs when a11y text is chrome-only.
- Private bytes (commit) were 548 MB at idle and 1150 MB after one OCR frame, against 64 / 175 MB USS. Task Manager's Memory column shows the USS-like number; commit counts against the page file, not RAM.
- `Get-Content` in PowerShell 5.1 shows the log's `→` as `â†'`. The log file is UTF-8; pass `-Encoding UTF8`.
- Not tested: an installer, start at login, a start from Explorer or the Start menu, SmartScreen, the microphone and call transcription, llama-server inside the app, model download (F2).

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
   Get-Content "$env:TEMP\sm-spike\screenmind.log" -Tail 40 -Encoding UTF8
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
