# Ship ScreenMind as an app

Status: 2026-10-08. Fixes F1-F5 are done. Windows has an unsigned NSIS installer, a tray icon and a CI build. macOS has only the spike: rebuilt on `custom` 3092a5f, it builds with no spec change, starts in 1.7 s at 81 MB footprint, and F1 and F4 work there. macOS still has no CI build, no signing, no menu bar, no first-run flow and no tested login item. F6 and the OCR memory fix are open on both OS. F7 (macOS) is partly done. Next macOS steps, each with its reason: [Next steps for macOS](#next-steps-for-macos-2026-10-08). Spike results: [packaging-spikes.md](packaging-spikes.md).

## Short answer

Two requirements from the user (2026-10-07) shape the plan:

1. **No UI ships.** The product is a headless background agent that starts at login. The dashboard stays a developer tool. The shipped build has only a menu bar / tray item and a first-run permissions flow. See [What ships: a headless agent](#what-ships-a-headless-agent).
2. **It must not be greedy with RAM and CPU.** See [Resource budget](#resource-budget). Today the agent is far over any fair budget: about 1-1.6 GB for ScreenMind (mostly OCR) plus about 4 GB for `llama-server`.

Recommendation:

- **Both OS: freeze with PyInstaller (onedir).** It supports Python 3.14 and has hooks for onnxruntime, sounddevice and keyring. The macOS spike built a working `ScreenMind.app` (343 MB, 122 MB as a DMG) that started in 2 s. The bundler adds no RAM: the frozen app runs the same single Python process.
- **macOS: a signed, notarized `.app` in a DMG.** Native minimum UI through pyobjc: a menu bar item (status, pause, quit) and a short first-run window that explains each permission before macOS asks. Start at login uses `SMAppService`, not a LaunchAgent plist.
- **Windows: an NSIS wizard, per-user install.** The user chose NSIS over Inno Setup for the license (2026-10-08). The wizard explains what is recorded and has a "Start at login" checkbox. A tray item gives status, pause and quit. Uninstall asks whether to delete `%USERPROFILE%\.screenmind`.
- **Do not ship local Gemma in the default agent.** Collect on the device, label on the server (or through a cheap vendor API in batch). That removes about 4 GB of RAM and a 3 GB download. Keep local labeling as an opt-in mode that loads the model only when needed. See [Should local LLM analysis ship?](#should-local-llm-analysis-ship).
- **Fix OCR memory before shipping.** OCR alone holds 1.1-1.8 GB and uses 4-7 CPU-seconds per frame (measured below). The onnxruntime tuning is done (peak -400 MB, CPU -40%). It is not enough for the budget, so the OCR worker process is still needed.
- **Target budget:** median 250 MB, peak 500 MB of memory for the agent, and on average under 3% of one CPU core over a workday. Measured by the e2e check on each machine.
- **Architectures:** Apple Silicon only, macOS 14+. Windows x64 only. Intel Macs are out: onnxruntime has no Intel macOS wheels for Python 3.14. Windows ARM64 is out for now: our lock has no `opencv-python` wheel for it. ARM64 laptops can run the x64 build under emulation (not tested).
- **First milestone (about one to two weeks):** the code fixes below, the OCR memory fix, and an unsigned `.app` + DMG and NSIS wizard for internal testers, built in GitHub Actions.
- **Public release:** Apple Developer Program (99 USD/year) for signing and notarization. Azure Artifact Signing (about 10 USD/month) or an OV certificate for Windows. Auto-update comes last.

## What we ship

Measured on the user's Mac (2026-10-07, branch `custom` after the strip, `cd95561`):

| Part | Size | Where it comes from today |
|---|---|---|
| Python 3.14 + all packages, frozen (`.app`) | 343 MB on disk (363 MB before the strip) | PyInstaller spike |
| Same, as a compressed DMG | 157 MB (zlib), 122 MB (LZMA, `ULMO`) | `hdiutil create` |
| `llama-server` (llama.cpp) | 10-31 MB per OS (CPU, Metal, Vulkan builds). CUDA builds are 150-400 MB plus runtime | Homebrew or PATH, or `setup_llama.py` downloads it into the checkout |
| Gemma 4 E2B GGUF + mmproj | Q4_0 2.8 GB + mmproj 0.56 GB = 3.2 GB on this Mac; Q8_0 5 GB. (The "~1.5 GB" label in `model_manager.py` is too low.) | `huggingface_hub` into `~/.screenmind/models/<model>` |
| RapidOCR models | 25 MB | downloaded on first use into `~/.screenmind/models/ocr` |
| Embedder | 87 MB in `~/.screenmind/models/embedder` | removed by the strip ([strip-to-actions.md](strip-to-actions.md)); the folder can be deleted |

The biggest parts of the frozen app are onnxruntime (70 MB), `cv2` (40 MB plus the ffmpeg/X11 libraries it bundles, from `opencv-python`, pulled in by `rapidocr`) and scipy (32 MB, pulled in by `imagehash`). Ways to make it smaller are in [Size](#size).

## What ships: a headless agent

The user's decision: no UI ships. The dashboard stays for development.

| Part | Developer build (today) | Shipped agent |
|---|---|---|
| Capture, a11y text, UI events, OCR, dedup, call tracking, export | yes | yes |
| Dashboard (static pages, Model Hub, settings pages) | yes | no |
| HTTP server (FastAPI + uvicorn) | yes | only if the export needs it. Ask the export session. Dropping it saves some RAM (not measured) and closes a local port. |
| Settings | dashboard and `settings.json` | `settings.json`, plus a few menu items (pause, start at login) |
| Labeling model | local Gemma | none by default; see [Should local LLM analysis ship?](#should-local-llm-analysis-ship) |
| UI on macOS | none | menu bar item + first-run permissions window ([details](#first-run-flow-native-no-dashboard)) |
| UI on Windows | none | installer wizard + tray item ([details](#install-details-nsis)) |

The minimum per OS:

- **macOS needs a little UI.** The system prompts don't explain why ScreenMind needs Screen Recording, Accessibility or Input Monitoring. They send the user to System Settings. So the agent shows its own short explanation first, one step per permission. It also needs a menu bar item: macOS can revoke Screen Recording (the monthly prompt), and the user must see that recording runs and be able to pause it.
- **Windows needs almost none.** No permission prompts, except the microphone for call transcription. The installer wizard explains what is recorded. A tray item shows status and offers pause and quit.

In the build, a flag selects agent mode. The spec then leaves out `screenmind/api/static` (and FastAPI/uvicorn, if the export doesn't need them).

## Resource budget

The agent must not be greedy with RAM and CPU. Today it is.

### Measured today

On the user's Mac (24 GB, 4 performance + 6 efficiency cores), 2026-10-07:

| Part | Memory | CPU | How measured |
|---|---|---|---|
| ScreenMind main process (main instance, code from before the strip, still with the embedder) | 1.6 GB footprint (peak 1.9 GB) | | `footprint` on the running process |
| ...of that, OCR (RapidOCR on onnxruntime) | 1.1-1.8 GB footprint after 20-60 full-size Retina frames | 7.1 CPU-seconds per frame (1.5 s wall) | offline run on 20 and 60 saved screenshots, separate process |
| ...OCR with tuning (`enable_mem_pattern=False`, 2 threads). **In the app since 2026-10-07.** | median 1.29 GB, peak 1.48 GB over 40 frames (before: 1.45 / 1.89 GB) | 4.0 CPU-s per frame, 2.0 s wall (before: 6.8 CPU-s, 1.4 s) | `packaging/ocr_mem_bench.py`, same 40 frames, `OCR_LANGUAGES=en,es,de,fr,ru`. Same text, boxes and confidences. |
| ...Python + OCR imports / OCR models loaded | 35-50 MB / about 100 MB | | same |
| Frozen app right after start (no OCR yet, nothing captured) | 123 MB RSS. On 2026-10-08 (`3092a5f`): 124 MB RSS, 81 MB footprint | about 0 | spike |
| ScreenMind main process after the strip (main instance, dashboard and analysis worker included), 20 min after its start | 415 MB footprint, peak 1177 MB | 60 CPU-s in 20 min, about 5% of one core | `footprint` and `ps` on the running process, 2026-10-08. A single sample, not a workday |
| `llama-server` footprint, own test instance, after load / after 25 analysis-like calls | 0.79 GB / 1.31 GB with the default prompt cache (+21 MB per call, never freed). 0.79 GB / 0.88 GB with `--cache-ram 0` (flat). RSS: 3.5 GB after load, most of it the memory-mapped weights. | no change per call | port 5898, the app's flags, `-ngl 99`; 2026-10-07 |
| `llama-server`, Gemma 4 E2B Q4_0 + mmproj, context 6144 | about 4.0 GB RSS | about 12 s per call. About 880 calls per workday (861 full analyses + 300 cache hits on 2026-10-06/07), so about 3 hours of inference a day | coordinator's measurement; call counts from the DB |
| **Total** | **about 5-5.5 GB** | | |

What this shows:

- **The bundler costs nothing.** PyInstaller onedir runs the same single Python process. Nuitka is about the same. Onefile adds a second process and unpacks to a temp folder on every start, so avoid it. Electron or Tauri would add a webview process, which a headless agent doesn't need.
- **OCR is the main cost in the ScreenMind process.** RapidOCR already turns onnxruntime's memory arena off. The memory is per-frame working buffers that the allocator keeps dirty. Calling `malloc_zone_pressure_relief` and even dropping the OCR engine gave nothing back. Halving the image size changed almost nothing. Over 60 frames it levels off at 1.1-1.5 GB, so it is a working set, not a leak.
- **Measure footprint, not RSS.** On macOS, RSS also counts freed pages the allocator keeps. Activity Monitor shows "phys_footprint". On Windows, Task Manager shows the private working set.
- **`llama-server` has two hidden costs.** The host prompt cache (`--cache-ram`) defaults to 8 GiB. It grew 21 MB per call in our test, so at about 880 calls a day it would reach the 8 GiB cap. The app now starts llama-server with `--cache-ram 0` (done). The mmproj (0.56 GB) is only needed when images are sent. The model weights are memory-mapped, so most of the 4 GB RSS is file pages the OS can reclaim. The footprint after load is 0.79 GB.

### Target budget

| Metric | Target | Hard cap |
|---|---|---|
| Memory of the agent and its child processes (macOS footprint, Windows private working set) | median 250 MB | peak 500 MB. Above 750 MB the agent restarts its OCR worker and logs a warning. |
| CPU, average over a workday | under 3% of one core (about 15 CPU-minutes in 8 hours) | |
| CPU per captured frame | under 1 CPU-second | |
| Screen not changing | about 0% CPU | |
| Opt-in local labeling | up to +1.5 GB, only while a batch runs, and 0 when idle | only on AC power, after the user is idle for a few minutes |

Why these numbers: 250 MB is about 3% of an 8 GB laptop. That is small enough to stay unnoticed. The start footprint (123 MB) plus the OCR models (about 100 MB) fits. The OCR working memory does not fit today, so the levers below are needed. The numbers are proposals; M0 proves or adjusts them.

### Levers, biggest first

1. **Label off the device** (saves about 4 GB and 3 hours of inference a day). See the next section.
2. **OCR in a short-lived worker process.** The agent starts a worker (a hidden mode of the same executable) for a batch of frames, then the worker exits and its memory goes back to the OS. Cost: about 100 MB and a second or two to load the models per batch (to measure).
3. **Tune onnxruntime for OCR. Done (2026-10-07).** `enable_mem_pattern=False`, `intra_op_num_threads=2` (setting `OCR_THREADS`), memory arena off. Measured: peak -410 MB (-22%), median -160 MB, CPU -40%, the same text. Each frame takes 45% longer, which a background agent can afford. The memory pattern gives the memory saving, the threads the CPU saving. RapidOCR 3.9 takes threads and the arena from `EngineConfig.onnxruntime`, but has no setting for the memory pattern. So `engine/ocr.py` loads its models again with our options (`_tune_sessions()`).
4. **Turn off `llama-server`'s host prompt cache. Done (2026-10-07).** `--cache-ram 0` saves up to 8 GiB over a day: +21 MB per call without it, flat with it. `model_manager.py` adds the flag only if `llama-server --help` lists it. Older builds (before llama.cpp PR 16391, October 2025) have no such cache.
5. **OCR less.** Skip OCR when the accessibility text is real content (exists: `_a11y_is_content`) and when the frame is a near-duplicate (exists: pHash). New: OCR only the front window region, not every display. Try a smaller detector input (`Det.limit_side_len`, `Global.max_side_len`). Halving the whole image did not help, so measure each change.
6. **Lower priority.** This reduces CPU contention, not RAM.
   - macOS: `setpriority(PRIO_DARWIN_PROCESS, 0, PRIO_DARWIN_BG)` at start. That means lowest CPU priority, efficiency cores only, and throttled disk I/O. If grabs then come late, keep the capture thread at utility QoS. For a `llama-server` child, `taskpolicy -b -p <pid>` works from outside. ([Eclectic Light](https://eclecticlight.co/2025/05/09/what-is-quality-of-service-and-how-does-it-matter/), [man page](https://cs.iossec.tech/xnu/latest/source/bsd/man/man2/getpriority.2))
   - Windows: EcoQoS through `SetProcessInformation(ProcessPowerThrottling, PROCESS_POWER_THROTTLING_EXECUTION_SPEED)`, which also works on a child's handle. `BELOW_NORMAL_PRIORITY_CLASS` (inherited by children) and `MEMORY_PRIORITY_LOW` (its pages are trimmed first). psutil covers the priority class; EcoQoS needs ctypes. ([Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/procthread/quality-of-service))
   - `llama-server`: `--prio -1 --poll 0` and a small `-t`.
7. **Do heavy work at good times.** The analysis queue (`defer_analysis` exists) runs on AC power when the user is idle or the screen is locked. It pauses on battery saver, Low Power Mode or high thermal state. Detection: macOS `IOPSGetProvidingPowerSourceType`, `NSProcessInfo.thermalState`, `isLowPowerModeEnabled`, `CGEventSourceSecondsSinceLastEventType`. Windows `GetSystemPowerStatus`, `GetLastInputInfo`. psutil `sensors_battery().power_plugged` works on both.
8. **Hard caps are a last resort.** Windows Job Objects can cap committed memory, but allocations then fail instead of slowing down. macOS has no supported per-process memory cap for apps. So the agent watches itself and recycles the OCR worker.

### Should local LLM analysis ship?

Inputs to the labeling call are mostly text (accessibility or OCR text, after redaction) plus one 768 px screenshot.

| Option | Extra RAM on the device | CPU/GPU on the device | Money | Privacy | Effort |
|---|---|---|---|---|---|
| A. As today: Gemma 4 E2B with vision, always loaded | about 4 GB | about 3 h of inference per workday | none | stays on the device | none |
| B. Same model, loaded on demand: `llama-server --sleep-idle-seconds 60`, batches only when idle on AC | about 4 GB during a batch; the model unloads when idle (the PR's test went from 43 GB to 442 MB) | same total, moved to idle time | none | stays on the device | small: flags and a scheduler |
| C. Smaller local model, text only: Gemma 3 1B (Q4_K_M 0.8 GB), Qwen3 0.6B (Q8_0 0.64 GB), or Qwen3.5 0.8B with vision (0.74 GB with mmproj) | about 1 GB while loaded, 0 when idle (with B) | much less per call (not measured) | none | stays on the device | medium: prompt work and a quality check on the questionnaire bench |
| D. Label on the workflows server | none | none | server cost | the export already sends this data to the workflows app | medium: a server job |
| E. Vendor API from the agent, text only, batched | almost none | almost none | per user per workday: Gemini 2.5 Flash-Lite about 0.2 USD, gpt-5-nano about 0.1-0.3 USD, Claude Haiku 4.5 about 2.2 USD (1.1 with the Batch API) | redacted text leaves the device. Images would leave unredacted, so send none. | medium: see "Swap Gemma for a vendor LLM API" in [setup-ocr-and-upstream-prs.md](../backlog/setup-ocr-and-upstream-prs.md). Also an API key in the client. |

Cost assumption for E: 880 calls a workday, about 2,000 input and 100 output tokens each. For example, Gemini 2.5 Flash-Lite at 0.10/0.40 USD per million tokens: 1.76 M input = 0.18 USD, plus 0.09 M output = 0.04 USD. Claude Haiku 4.5 at 1/5 USD per million: 1.76 + 0.44 = 2.20 USD. Over 22 workdays: about 5 USD per user a month with Flash-Lite, about 48 USD with Haiku (24 USD with the Batch API). gpt-5-nano bills its reasoning tokens as output, so its cost depends on how much it reasons. ([Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing), [OpenAI pricing](https://developers.openai.com/api/docs/pricing))

Evidence so far (2026-10-08):

- The workflow-extraction prompt run compared three input tiers on 5 sessions, judged by an LLM on a 0-2 scale. T1 (accessibility text and UI events): 1.63. T2 (T1 + OCR): 1.85. T3 (T2 + a Gemma note per frame): 1.79. The extractor quoted a Gemma note as evidence 0 times out of 211 quotes. Extraction itself runs on Claude, on the server. Early and not yet reviewed by the user, but it supports shipping no local model. It also shows that OCR is worth its cost, so the OCR memory fix stays.
- The PII filter (`screenmind/privacy/data_filter.py`, 38793c2) is regex only: emails, phone numbers and IBANs, on by default, at capture and at export. It adds no package and no RAM. It does not find person names, addresses or message content. If labeling or extraction runs off the device, that text leaves the device.

**Recommendation: D by default.** The agent only collects and exports. Labeling runs where the data goes anyway, with no model download, no 4 GB of RAM and no API key in the client. The server can call a vendor API in batch (E's prices, without the key problem). Offer **B + C as an opt-in "label on this computer" mode** for people who must keep everything local, after C passes the questionnaire bench. This is a product decision; see the open questions.

### Measure and enforce

- **Metric.** macOS: `phys_footprint` from `proc_pid_rusage(RUSAGE_INFO_V4)` (what Activity Monitor shows). Windows: private working set, psutil `memory_full_info().uss` (what Task Manager shows). Add up the agent and its children. CPU: psutil `cpu_times()` user + system, per frame and per hour.
- **In the agent.** The 10-minute summary log line gets memory (median, peak), CPU-seconds per frame and frames OCR'd. Above the hard cap: recycle the OCR worker, log a warning.
- **In the e2e check** (`scripts/e2e_collect.py`). Sample memory and CPU of ScreenMind and `llama-server` every second during the run. Write "Memory median / peak" and "CPU-s per frame" rows into `docs/status/<os>.md`. WARN over the target, FAIL over the hard cap.
- **In CI.** A benchmark job on GitHub's macOS and Windows runners. It runs OCR and the analysis pipeline (with a fake LLM) on a fixed set of synthetic screenshots committed to `tests/fixtures`, with no private data. It records memory and CPU per frame and fails on more than 20% regression against the stored baseline for that runner. Runners are not laptops, so compare each runner with its own baseline, not with the laptop target.

## Things in the code that break inside an app

These need fixing before any installer is useful. They are small. Most of them are "do not assume a Python checkout".

| # | Where | Problem in a frozen app | Fix |
|---|---|---|---|
| F1 | `config.py` `model_config["env_file"] = ".env"` | `.env` is read from the current directory. A LaunchAgent starts with cwd `/`, so the user's `.env` did not load (seen on 2026-10-07). An app has no cwd worth trusting. | Done (34ae529): in the app, `.env` comes from the data dir (`DATA_DIR`, else `~/.screenmind`) and is optional. Never from cwd or the bundle. `settings.json` as before. Checked on macOS on 2026-10-08: a data-dir `.env` value showed as `(.env)` in the start log. |
| F2 | `engine/model_manager.py` (`cmd = [sys.executable, "-c", "...hf_hub_download..."]`) | Model downloads run `sys.executable -c`. In a frozen app `sys.executable` is `ScreenMind` itself. It ignores `-c`, starts a second ScreenMind, which exits on "port in use". Downloads fail. | Done (34ae529): in the app, `hf_hub_download` runs in a thread of the same process, with the same progress polling. Cancel stops it: the progress bar raises, and Xet transfers are aborted. From source the child process stays. |
| F3 | `setup_llama.py` (`PROJECT_ROOT`) and `model_manager.start_server()` | They decide "pip install vs checkout" with `is_relative_to(site-packages)`. A frozen app looks like a checkout, so `llama/` resolves to a folder inside the app bundle. Installing there breaks the signature, and `/Applications` may not be writable. | Done (34ae529): in the app, look in `<exe dir>/llama/`, then `~/.screenmind/llama/`, then PATH. Installs go to `~/.screenmind/llama/`. `start_server()` uses the same lookup. |
| F4 | `startup.py` `_get_startup_command()`, `main.run()` `--background`, `launcher.py`, `_install_desktop_shortcut()` | They build commands from `sys.executable -m screenmind`, `pythonw.exe` or `launcher.py`. None of these exist in an app. | Done (34ae529) on Windows: the start command is `"<path>\ScreenMind.exe"` alone. That is the value the installer writes under HKCU `Run\ScreenMind`. `--background` and the launcher start the exe. No desktop shortcut, no `launcher.vbs`. macOS: the LaunchAgent plist runs `ScreenMind.app/Contents/MacOS/ScreenMind` (untested). `SMAppService` is still to do. Checked on macOS on 2026-10-08: no desktop shortcut (before 34ae529 the first start wrote `~/Desktop/ScreenMind.command` pointing at `launcher.py` inside the bundle). |
| F5 | `config._setup_logging()` | A windowed build has no console. Logs go nowhere unless `SCREENMIND_LOG_FILE` is set. | Done earlier (G38): every start logs to `<data dir>/screenmind.log` (rotating), console or not. Checked in the windowed Windows build on 2026-10-08. |
| F6 | `engine/ocr.py` (`Global.model_root_dir`) | The app ships rapidocr's own default models (31 MB) but never uses them. It downloads other models into `~/.screenmind/models/ocr` on first use. | Ship the 5 models we use inside the app (works offline) and exclude rapidocr's defaults. |
| F7 | `capture_worker`, `ui_events` permission requests | Screen Recording is never requested explicitly. macOS asks on the first grab, at a random moment. | The first-run window requests each permission on a button press (see [First-run flow](#first-run-flow-native-no-dashboard)). Partly done: Input Monitoring and Accessibility are checked and requested when UI events start (`capture/ui_events/macos.py`). Nothing calls `CGRequestScreenCaptureAccess` yet. |
| F8 | `packaging/screenmind.spec` on Windows | PyInstaller finds the DLLs that extensions link to through `PATH`. Another app's folder on `PATH` (a JDK on the Windows laptop) put an old `msvcp140.dll` 14.16 into the bundle, and onnxruntime crashed on the first OCR frame. | Fixed in the spec (2026-10-08): on Windows the build keeps only `%SystemRoot%` folders on `PATH`. CI should also check the bundled `msvcp140.dll` is 14.40 or newer. |

The spikes ran with F1-F7 still in place. The dashboard worked because the test needed none of these paths. F1-F5 are fixed since 2026-10-08. F1 and F4 were checked on macOS the same day; F2 and F3 only on Windows.

## 1. Freezing options

| Tool | Latest (date) | Python 3.14 | macOS `.app` | Windows output | Fit for ScreenMind |
|---|---|---|---|---|---|
| **PyInstaller** | 6.22.3 (2026-09-12) | Yes, since 6.15 (Aug 2025) | Yes (`BUNDLE`, bundle id, Info.plist, codesign flags). Use onedir; onefile inside `.app` will be blocked in v7. | onedir folder or onefile exe. Bring your own installer. | **Best.** Hooks exist for onnxruntime, sounddevice, keyring, numpy, cryptography, uvicorn. No hook for rapidocr (one `collect_data_files` line). Spike passed. |
| **Nuitka** | 4.2.2 (2026-09-22) | Yes, official since 4.2 (2026-08-27) | Yes, with sign and notarize flags and TCC usage strings | exe; new installer support in 4.2 (not checked) | Good runner-up. Compiles to C, so builds are slow and need Xcode/MSVC. No config for rapidocr or comtypes, none for sounddevice on Windows ARM64. Paid tier (EUR 250/year) for extras. |
| **py2app** | 0.28.10 (2026-02-13) | Yes, since 0.28.9 | Yes | none | macOS only, one maintainer (also maintains pyobjc). We would still need a second tool for Windows. |
| **BeeWare Briefcase** | 0.4.5 (2026-09-08) | Yes, since 0.3.25 | Yes, and it signs and notarizes by default. DMG or pkg. Uses uv on macOS since 0.4.5. | MSI via WiX | Possible, but beta and aimed at Toga apps. It requires binary wheels. Its default universal build fails because onnxruntime has no x86_64 wheel (set `universal_build = false`). WiX now asks companies for a fee (below). |
| **PyOxidizer** | 0.24.0 (2022-12-30) | No | | | Abandoned. The author calls it "in a zombie state". Do not use. |
| **python-build-standalone + uv** | 20261003 (CPython 3.14.8) | Yes | Do it yourself | Do it yourself | A real interpreter, so `sys.executable -c` keeps working. But we would hand-build the bundle layout, sign every `.so`, and write a native launcher. `uv venv --relocatable` only fixes scripts, not binaries. More work than PyInstaller for no clear gain. |
| **PyApp** | 0.29.0 (2025-10-15) | Not stated | No `.app` | single exe that installs on first run | Stale for a year. Skip. |

Why onedir matters on macOS: PyInstaller's onedir bootloader loads Python into its own process. So the process macOS sees is `ScreenMind.app` itself, and permission prompts name ScreenMind. Onefile unpacks and starts a child process, which makes permission attribution less certain.

### Native shell around the Python backend

| Option | What you get | Cost |
|---|---|---|
| Tauri v2 + Python sidecar (`externalBin`) | A real app window for the dashboard (system WebView), small shell (a few MB) | The Python part is still a frozen app (~360 MB). Two things to sign. A Rust codebase. Signing nested sidecars is not covered in Tauri's docs. |
| Electron + Python child process | Same, with bundled Chromium | Adds Chromium (often 100+ MB, not measured). Two runtimes. |
| Swift menu-bar app launching an embedded Python | The most native macOS app: SwiftUI onboarding, `SMAppService`, Sparkle all "just work" | A second codebase in Swift, and nothing for Windows (that would need its own C# shell). |

Child processes and permissions: Apple DTS says macOS follows the chain from a child process to its parent to find the "responsible" app, as long as the parent stays alive ([forum](https://developer.apple.com/forums/thread/805245)). So a sidecar would usually get the shell app's grants. But the chain can break, and macOS 26.1 had a bug in how such children show in Settings ([forum](https://developer.apple.com/forums/thread/807898)).

**Recommendation: no native shell.** No UI ships, so a webview shell would only add a process and RAM (Electron alone is often 100+ MB). A menu bar / tray item in Python (pyobjc `NSStatusItem` on macOS, `Shell_NotifyIconW` through ctypes on Windows) is enough.

## 2. macOS

### Options

| Option | Effort | Risk | Notes |
|---|---|---|---|
| **PyInstaller `.app` + DMG** (recommended) | Low | Notarization of pyobjc/onnxruntime binaries needs a test round | DMG is one signed and notarized file. Drag to Applications. Works with Sparkle later. |
| PyInstaller `.app` + `.pkg` | Low | Needs admin, a second cert type (Developer ID Installer) | Only useful to install for all users or run scripts. We need neither. |
| Briefcase `.app` + DMG | Medium | Beta, wheel rules, uv support new | Does signing and notarization for us. |
| Swift shell + Python | High | Two codebases | Best native feel. |

### Install

DMG with the app and an "Applications" link. Build it with `create-dmg` or `dmgbuild`; plain `hdiutil` is enough for testers. A `.pkg` adds nothing we need.

### First-run flow (native, no dashboard)

macOS needs some UI. The system prompts for Screen Recording, Accessibility and Input Monitoring say nothing about why, and they send the user to System Settings to flip a switch. Without our own explanation first, most people will deny or ignore them. The minimum is a small native window or a few `NSAlert` dialogs, built with pyobjc (already a dependency):

1. **What ScreenMind records and where it goes.** One screen, plain words, a "Continue" button.
2. **One step per permission.** A sentence on why, then a button that triggers the request and opens the right Settings pane (`x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture`, `...Privacy_Accessibility`, `...Privacy_ListenEvent`). The step shows a check mark when the grant is seen.
   - Screen Recording: `CGPreflightScreenCaptureAccess` / `CGRequestScreenCaptureAccess`.
   - Accessibility: `AXIsProcessTrustedWithOptions`.
   - Input Monitoring: `CGPreflightListenEventAccess` / `CGRequestListenEventAccess`.
   - Microphone: only if call transcription is on. `AVCaptureDevice.requestAccessForMediaType_`, with `NSMicrophoneUsageDescription` in Info.plist (the spike has it).
   - Some grants need a restart. The last step offers "Restart ScreenMind".
3. **Start at login** (on by default), then start capturing.

The menu bar item has a "Permissions..." entry that reopens this flow. It also shows "Needs permission" when a grant is missing or was revoked, for example after the monthly Screen Recording prompt was dismissed.

If local labeling is on (opt-in, see [Resource budget](#resource-budget)), the model download shows as a menu bar line ("Downloading model: 42%"). No window is needed for that.

### Start at login

Use `SMAppService.mainApp.register()` (macOS 13+) through pyobjc (`pyobjc-framework-ServiceManagement`, a new dependency). It shows up in System Settings > General > Login Items, and macOS shows a "Background item added" notice once. It replaces the hand-written `~/Library/LaunchAgents/com.screenmind.plist`. That also fixes the cwd problem (F1), because the app starts like any app. Calling SMAppService from a PyInstaller app is not tested yet. ([Apple docs](https://developer.apple.com/documentation/servicemanagement/smappservice))

**Open: `mainApp` or `agent`.** The uptime plan ([uptime.md](uptime.md), step 5) wants launchd to restart the agent after a crash (`KeepAlive` with `SuccessfulExit=false`). `SMAppService.mainApp` is a plain login item and has no `KeepAlive`. A restart on crash needs `SMAppService.agent(plistName:)` with a LaunchAgent plist inside the bundle (`Contents/Library/LaunchAgents/`). So the likely choice is `agent`. The clean-architecture plan has the same note on its LoginItem port. Not tested from a PyInstaller app.

### Menu bar

An `NSStatusItem` with a status line (Capturing / Paused / Needs permission), Pause or Resume, Permissions..., and Quit. Developer builds can add "Open dashboard". It needs the AppKit run loop on the main thread. Today the main thread runs asyncio, so asyncio moves to a worker thread. Set `LSUIElement` so there is no Dock icon (the spike does this). AppKit in the same process costs little RAM (not measured; expect tens of MB).

### Does a signed app fix the permission prompts?

Mostly yes. Facts:

- TCC stores the app's *designated requirement*. For a Developer ID app, that is the bundle id plus the Team ID. A new version signed the same way keeps all grants ([TN3127](https://developer.apple.com/documentation/technotes/tn3127-inside-code-signing-requirements)).
- An ad-hoc signed or unsigned build has a requirement tied to that exact build. **Every rebuild loses the grants.** Testers of unsigned builds must re-grant after each update. A workaround for testers: sign every build with the same self-signed certificate. The requirement then names that certificate, and grants should survive rebuilds. This follows from TN3127 but is not tested.
- With a real `.app` launched from Finder or as a login item, the responsible process is ScreenMind. Prompts say "ScreenMind" (fixes G22 in [capture.md](../architecture/capture.md)). Child processes we start (`screencapture`, `llama-server`) count as ScreenMind too.
- G1 (grabs hang in Claude-started processes) is about processes whose responsible app has no grant. A real app with a grant should not hang. Check this by hand in milestone 1. It cannot be checked from a Claude session.
- What a signed app does not fix: macOS 15+ asks again **every month** whether ScreenMind may keep recording the screen ("Allow for one month"). The entitlement that avoids this (`persistent-content-capture`) is only for VNC-type apps, by application. We live with the monthly prompt. ([9to5Mac](https://9to5mac.com/2024/08/14/macos-sequoia-screen-recording-prompt-monthly/), [Apple](https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.developer.persistent-content-capture))

## 3. Windows

### Options

| Option | Effort | Risk | Notes |
|---|---|---|---|
| **NSIS 3** (chosen 2026-10-08) | Low-medium | Older scripting language | Free (zlib license), no fee for companies. Modern UI 2 wizard, nsDialogs for custom pages. Per-user install with `RequestExecutionLevel user`. Built: `packaging/windows/screenmind.nsi` (see [packaging-spikes.md](packaging-spikes.md#windows-installer)). |
| Inno Setup 7.1 | Low | License: paid for commercial use (see below) | Classic wizard. Per-user install with `PrivilegesRequired=lowest`. `[Registry]` for HKCU Run, `[UninstallDelete]` and a Pascal `[Code]` step for "delete my data?". x64 and Arm64. ([jrsoftware.org](https://jrsoftware.org/isdl.php)) Not chosen: the user picked NSIS for the license (2026-10-08). |
| WiX v7 (MSI) | Medium-high | MSI authoring is verbose. Since v6, organizations with more than 10,000 USD revenue must pay the Open Source Maintenance Fee. ([FireGiant](https://docs.firegiant.com/wix/osmf/)) | Only worth it for IT-managed (GPO/Intune) rollouts. |
| MSIX | Medium | AppData and HKCU writes are virtualized and deleted on uninstall. Start at login needs a manifest `startupTask`. Low-level hooks and UI Automation from a full-trust MSIX are not confirmed. ([Microsoft Learn](https://learn.microsoft.com/en-us/windows/msix/desktop/desktop-to-uwp-behind-the-scenes)) | Not for v1. |
| Velopack | Medium | Not a wizard: a one-click installer | Installer **and** auto-updater, supports Python + PyInstaller onedir on Windows and macOS. Good candidate for the auto-update milestone. ([docs](https://docs.velopack.io/getting-started/python)) |

### Install details (NSIS)

The first version is built (2026-10-08): [packaging-spikes.md, Windows installer](packaging-spikes.md#windows-installer).

- Per-user, no admin: install to `%LOCALAPPDATA%\Programs\ScreenMind`. Data stays in `%USERPROFILE%\.screenmind` as today.
- Wizard pages: welcome, "What ScreenMind records" (plain words, an "I understand" checkbox), folder, "Start ScreenMind when I sign in" checkbox, "Start ScreenMind now" on finish. This wizard is the whole first-run flow on Windows. No window after install.
- Start at login: the installer writes `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\ScreenMind = "<path>\ScreenMind.exe"`. The app's own toggle (`startup.py`) must write the same value (F4).
- Uninstall: stop the running app first. Remove the Run value. Ask: "Also delete your ScreenMind data (screenshots, database, models: N GB)?" Default No. Models are large, so offer them separately.
- Tray icon: status line, Pause or Resume, Quit. A tooltip shows the status. This is the minimum, so the user can always see that recording runs and can stop it. Done (2026-10-08): `screenmind/tray.py` and `platform_support/win_tray.py`, `Shell_NotifyIconW` through ctypes instead of `pystray` (under 1 MB against about 3 MB, no new package). It is on in the app and off from source (`TRAY_ICON=true` turns it on). Windows 11 puts a new icon in the hidden icons (the ^ button) until the user pins it.
- Permissions: only the microphone ("Let desktop apps access your microphone"), and only if call transcription is on. Screen grabs, UI Automation and low-level hooks need no prompt. So Windows needs less UI than macOS.

## 4. Signing and trust

### macOS

- **Apple Developer Program: 99 USD/year.** Enroll as an organization (needs a D-U-N-S number) so the publisher name is the company. ([Apple](https://developer.apple.com/programs/enroll/))
- Sign the app and every nested binary with a **Developer ID Application** certificate, with hardened runtime (`--options runtime`) and a secure timestamp. PyInstaller signs each binary when given `codesign_identity` and an entitlements file.
- Entitlements to start with: `com.apple.security.device.audio-input` (microphone under hardened runtime). Add `com.apple.security.cs.allow-unsigned-executable-memory` only if ctypes/cffi callbacks crash. We sign every `.so` with our Team ID, so `disable-library-validation` should not be needed. Test on a notarized build. ([Apple](https://developer.apple.com/documentation/security/hardened-runtime))
- No App Sandbox. It is only required for the Mac App Store, and it would block capture and event taps.
- **Notarize** with `notarytool` using an App Store Connect API key (good for CI), then `stapler staple` the DMG.
- `llama-server` from llama.cpp releases is **not** signed. We re-sign it (and its dylibs) with our Team ID before notarizing.
- Unsigned builds on macOS 15+: right-click > Open no longer works. The user opens the app once, then System Settings > Privacy & Security > "Open Anyway" ([Apple](https://support.apple.com/guide/mac-help/open-a-mac-app-from-an-unknown-developer-mh40616/mac)). Or `xattr -dr com.apple.quarantine /Applications/ScreenMind.app`. Fine for internal testers.

### Windows

- Unsigned: SmartScreen shows "Windows protected your PC" > More info > Run anyway. On Windows 11 with Smart App Control on, unsigned apps can be blocked outright.
- **EV certificates no longer give instant SmartScreen reputation** (Microsoft changed this in 2024). Reputation builds over "several weeks and hundreds of clean installs" for any certificate. So EV is not worth the extra money. Keep one signing identity so reputation carries over. ([Microsoft Learn](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation))
- Options:

| Option | Cost | Notes |
|---|---|---|
| **Azure Artifact Signing** (formerly Trusted Signing) | ~10 USD/month (5,000 signatures) | No hardware token. Identity check takes 1-20 business days. Organizations in the US, Canada, EU, UK and some other countries (Microsoft's pages disagree on the list). Individuals: US/Canada only. GitHub Action `Azure/artifact-signing-action`. ([Learn](https://learn.microsoft.com/en-us/azure/artifact-signing/quickstart)) |
| OV certificate (SSL.com, Sectigo, DigiCert...) | ~130-300 USD/year, plus cloud signing (SSL.com eSigner 20-250 USD/month) | Since 2023 the key must live on a token or HSM. Max certificate life is 460 days since 2026-03. |
| Certum Open Source | from EUR 49 | Only for open-source projects. Needs a phone app for codes, so CI is awkward. |

### Rough yearly cost for a signed public release

Apple 99 USD + Azure Artifact Signing ~120 USD = **about 220 USD/year**. With an OV certificate instead of Azure: about 250-500 USD/year.

## 5. The heavy parts

### llama-server

Only needed for the opt-in local labeling mode. If that mode ships:

| Choice | Pros | Cons |
|---|---|---|
| **Bundle a pinned build** (recommended) | Works offline after install. One known-good version. Signed with the app. | Adds 10-30 MB. We track llama.cpp updates ourselves. |
| Download on first run | Smaller installer | One more network step that can fail. On macOS a downloaded binary must still be signed, or it is quarantined. |

- macOS: the arm64 release build (about 11 MB, Metal). Put it in `Contents/Resources/llama/`.
- Windows: the Vulkan build (about 31 MB) runs on most GPUs (Intel, AMD, NVIDIA) and should fall back to CPU (to check). CUDA builds are 150-400 MB plus runtime, so offer them only as an optional download when an NVIDIA GPU is found. `setup_llama.py` already detects that.
- Pin a llama.cpp build tag (today `b11476`) and a SHA-256 in the build script. Note that llama.cpp now has two kinds of release: version tags like `v0.6.0` with notes only, and build tags `bNNNN` with the binaries.
- Keep the current behavior of reusing a llama-server already running on port 5809.
- Start it with `--sleep-idle-seconds 60 --cache-ram 0 --prio -1 --poll 0` (see [Resource budget](#resource-budget)). `--sleep-idle-seconds` needs build b7492 or newer.

### Gemma models

Only for the opt-in local labeling mode (see [Resource budget](#resource-budget)). Then download on demand, with progress in the menu bar item and resume (`hf_hub_download` resumes). Gemma 4 E2B Q4_0 is 2.8 GB + 0.56 GB mmproj; a smaller text-only model would be under 1 GB. Needs fix F2 first. Show free disk space before starting. Never ship models inside the installer: most updates would re-download them.

### OCR models

Ship the 5 RapidOCR models we use (25 MB) inside the app. Exclude rapidocr's own defaults (F6).

### Architectures

| Target | Status | Why |
|---|---|---|
| macOS arm64 (Apple Silicon), macOS 14+ | **Yes** | All wheels exist. onnxruntime needs macOS 14+. |
| macOS x86_64 (Intel) | **No** | onnxruntime dropped Intel macOS in 1.24, the first version with Python 3.14 wheels. An Intel build would need onnxruntime built from source, or Python 3.13. Also Homebrew has no Intel llama.cpp bottles. ([release notes](https://github.com/microsoft/onnxruntime/releases/tag/v1.24.1)) |
| Windows x64 | **Yes** | |
| Windows ARM64 | **Later** | onnxruntime, numpy and llama.cpp have ARM64 builds, but `opencv-python` (needed by rapidocr) has no ARM64 wheel in our lock. Run the x64 build under emulation until then (not tested). |

### Size

The app is 343 MB unpacked, 122-157 MB as a DMG. Ways to shrink it, none tested:

- `cv2` with its libraries is about 118 MB because `opencv-python` bundles ffmpeg, X11 and more. Try forcing `opencv-python-headless` with a uv override.
- `scipy` (32 MB) and `pywavelets` come only from `imagehash`. A numpy pHash of ~15 lines could replace it. `test_phash_distances` must still pass with identical hashes.

## 6. Auto-update

| Option | OS | Notes |
|---|---|---|
| **"New version" check** (first) | both | On start and daily, read the latest GitHub Release. Show it in the menu bar / tray item ("Update available") with a download link. Half a day of work, no risk. |
| Sparkle 2 (2.10.0) | macOS | The standard. EdDSA-signed appcast, delta updates. Needs `Sparkle.framework` in the app, loaded via pyobjc (untested), or the `sparkle-cli` helper. ([docs](https://sparkle-project.org/documentation/)) |
| Velopack | both | Installer + updater, has a Python package, works with PyInstaller onedir. Needs the .NET SDK on the build machine. On Windows it would replace the NSIS wizard. ([docs](https://docs.velopack.io/getting-started/python)) |
| WinSparkle 0.9.4 | Windows | C DLL, called from Python via ctypes (untested). Pairs with the NSIS installer. |
| MSIX App Installer | Windows | Only with MSIX. Not for v1. |
| Squirrel.Windows | Windows | Unmaintained since 2024. Skip. |

Plan: the version check in milestone 1. Then pick Sparkle + WinSparkle (keeps the NSIS wizard) or Velopack (one tool for both, no wizard) after the first public release.

## 7. CI (GitHub Actions)

One workflow on a version tag, a matrix of two jobs:

- `macos-15` (arm64): `uv sync --frozen` > PyInstaller spec > download pinned `llama-server` and check SHA-256 > sign (import the `.p12` into a temporary keychain) > DMG > `notarytool submit --wait` > `stapler staple` > upload to the GitHub Release.
- `windows-2025` (x64): `uv sync --frozen` > PyInstaller > pinned `llama-server` > sign the exe and DLLs > `makensis` (NSIS from Chocolatey: the image has none) > sign the installer > upload. The unsigned part is built: `.github/workflows/package-windows.yml`.

No Intel macOS job (no Intel build). `macos-13` was retired in 2025-12 anyway. A `windows-11-arm` runner exists for later.

Secrets:

| Secret | Used for |
|---|---|
| `MACOS_CERT_P12_BASE64`, `MACOS_CERT_PASSWORD`, `MACOS_KEYCHAIN_PASSWORD` | Developer ID Application cert |
| `ASC_KEY_ID`, `ASC_ISSUER_ID`, `ASC_KEY_P8` | notarytool (App Store Connect API key) |
| `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` (or OIDC), account and profile names | Azure Artifact Signing |
| `SPARKLE_ED_PRIVATE_KEY` (later) | Sparkle appcast signing |

The unsigned milestone needs no secrets.

## 8. Effort, risk and recommendation

### Milestones

| Milestone | What | Effort (rough) | Main risk |
|---|---|---|---|
| **M0: internal testers** | Fix F1-F6. Agent-mode build without the dashboard. OCR memory fixes (worker process, onnxruntime settings) and background priority. Memory and CPU rows in the e2e check. Labeling decision (server, vendor or local) implemented in its simplest form. Unsigned `.app` + DMG. Unsigned NSIS wizard (per-user, Run key, uninstall data question; built 2026-10-08). CI builds both on a tag. Short tester guide ("Open Anyway", SmartScreen "Run anyway"). | ~2 weeks | OCR worker may not reach 250 MB. Unsigned macOS builds lose permission grants on every update (try the self-signed certificate trick). |
| **M1: app feel** | Native first-run permissions window (macOS). Menu bar / tray item (Windows tray done, 2026-10-08). `SMAppService` login item. "Update available" menu line. Idle/AC scheduling of analysis. CI resource benchmark. Check by hand that a Finder-launched app gets "ScreenMind" prompts and SCK grabs work (G1, G22). | ~1-1.5 weeks | Main thread change for the menu bar. SMAppService via pyobjc untested. |
| **M2: signed public release** | Apple Developer enrollment, Developer ID signing, entitlements, notarization in CI. Azure Artifact Signing (or OV cert) for Windows. | 3-5 days of work, plus waiting: Apple org enrollment and Azure identity checks take days to weeks | Notarization fails on some nested binary. SmartScreen warnings for the first weeks anyway. |
| **M3: auto-update** | Sparkle + WinSparkle, or Velopack | ~1 week | Integration with a frozen Python app is untested for all three. |

### Recommendation

- **Product:** a headless agent that collects and exports. No dashboard, no local LLM by default. Labeling on the server. Target: median 250 MB, peak 500 MB, under 3% of a core.
- **macOS:** PyInstaller onedir `.app`, DMG, Developer ID + notarization, native first-run permissions window, `SMAppService`, menu bar item. Apple Silicon only.
- **Windows:** PyInstaller onedir, NSIS per-user wizard (it explains what is recorded), Azure Artifact Signing, tray item. x64 only.
- **Start with M0** (the strip has landed). Fix OCR memory first: it is needed whatever we decide about labeling. The PyInstaller spec from the spike (`packaging/screenmind.spec`) is the starting point.

## Next steps for macOS (2026-10-08)

Where macOS stands: the spike builds and runs, F1-F5 work, and nothing else for milestone M0 exists on macOS. Each step says what it is, why, what it costs and whether it needs the user. They are in the order we propose to do them.

1. **A macOS CI build (`package-macos.yml`).** `macos-15` runner: `uv sync --frozen`, PyInstaller with `packaging/screenmind.spec`, an `hdiutil` DMG (LZMA), then the spike's smoke test (temp `DATA_DIR`, the 9 endpoints, `POST /api/shutdown`). Unsigned, on dispatch and on `v*` tags, like `package-windows.yml`.
   - Why: today a macOS build only exists when someone builds it on the user's Mac. CI gives testers a DMG and catches a broken spec on every tag. On Windows the first CI build found the DLL problem (F8), which a local build hid.
   - Cost: about 30-60 lines, no secrets, a few minutes of runner time per build. Nothing changes on users' machines.
   - User decision: none.
2. **Ship our OCR models in the app (F6).** Put the 5 RapidOCR models we use (25 MB) in the bundle and leave out rapidocr's own `PP-OCRv6_*_small` (31 MB).
   - Why: the app ships 31 MB it never uses, then downloads 25 MB on the first OCR frame. With the models inside, the first run works offline and has no network step that can fail. The app gets about 6 MB smaller.
   - Cost: a spec change and a lookup in `engine/ocr.py` (bundle first, then `~/.screenmind/models/ocr`).
   - User decision: none.
3. **Ask for Screen Recording at a known moment (the rest of F7).** In the app, call `CGPreflightScreenCaptureAccess()` before the first grab and `CGRequestScreenCaptureAccess()` when it is false, then log the result.
   - Why: today macOS asks at the first grab, at a random moment, with no context. A tester who misses it gets a silent agent. Input Monitoring and Accessibility already work this way. The full first-run window comes in M1; this is the minimum for testers.
   - Cost: a few lines in the capture start path, macOS only.
   - User decision: none.
4. **OCR in a short-lived worker process.** See lever 2 in [Resource budget](#levers-biggest-first).
   - Why: once OCR runs, the agent holds 1.07-1.5 GB footprint on the Mac. The hard cap is 500 MB. The onnxruntime tuning is done and was not enough. A worker that exits after a batch gives its memory back to the OS. The tier run says OCR improves extraction (1.85 against 1.63 without it), so dropping OCR is not the answer.
   - Cost: the biggest item, a few days. About 100 MB and 1-2 s per batch to load the models again (to measure). The clean-architecture plan moves it behind a `TextRecognizer` port later (its step 16); building it first is fine.
   - User decision: none. It is needed whatever the labeling decision is.
5. **Hand test of a CI DMG (needs the user, about 10 minutes).** Open the DMG from Finder, then check: the permission prompts name ScreenMind, SCK grabs work, OCR runs on a real frame, and a model download (F2) and `llama-server` lookup (F3) work.
   - Why: these are the reasons to ship an `.app` at all, and none of them can be tested from a Claude session. Grabs hang in Claude-started processes ([sck-capture.md](../backlog/sck-capture.md)).
   - User decision: pick the bundle id before this test (below).

Decisions that block or shape these steps:

- **Bundle id.** `com.screenmind.app` is a placeholder. TCC grants are tied to the bundle id (and, once signed, the Team ID). If the id changes after testers grant permissions, every tester grants again. It should be a reverse-DNS name of a domain the publisher owns.
- **Where labeling runs** (open question 5). If it is the server or a vendor API, the macOS app ships no `llama-server` and no Gemma: about 4 GB of RAM and a 3 GB download less, and F2/F3 matter only for the opt-in mode. The evidence so far points that way (see [Should local LLM analysis ship?](#should-local-llm-analysis-ship)).
- **Does the agent keep the HTTP server** (open question 7). The export is an HTTP route today. This decides whether the agent-mode build can drop FastAPI, uvicorn and the dashboard.
- **Who publishes** (open question 1). Not needed for M0. It blocks signing (M2) and the self-signed certificate trick is the stopgap for testers.
- **Login item: `SMAppService.agent` or `mainApp`.** Technical, see [Start at login](#start-at-login). We propose `agent`, because only it can restart a crashed agent.

## Open questions for the user

1. Who publishes? A company account (TripleTen) or a personal one? That decides Apple org enrollment and Azure Artifact Signing eligibility.
2. ~~Is Inno Setup's commercial license OK, or do we prefer NSIS (free)?~~ Answered 2026-10-08: NSIS, for the license.
3. Is a monthly "keep allowing screen recording?" prompt acceptable? There is no way around it for this kind of app.
4. Is Apple Silicon + Windows x64 enough for the first testers?
5. Where does labeling run: on the workflows server (recommended), through a vendor API, or on the device? Is sending redacted text off the device OK?
6. Is the budget right: median 250 MB, peak 500 MB, under 3% of one core?
7. Does the export need the HTTP server, or can the agent drop FastAPI/uvicorn?

## Sources

Freezing:
- PyInstaller changelog (3.14 since 6.15): https://pyinstaller.org/en/stable/CHANGES.html
- PyInstaller macOS usage and code signing: https://pyinstaller.org/en/stable/usage.html, https://github.com/pyinstaller/pyinstaller/wiki/Recipe-OSX-Code-Signing
- PyInstaller contrib hooks: https://github.com/pyinstaller/pyinstaller-hooks-contrib/tree/master/_pyinstaller_hooks_contrib/stdhooks
- Nuitka changelog and manual: https://nuitka.net/changelog/Changelog.html, https://nuitka.net/user-documentation/user-manual.html, https://nuitka.net/doc/commercial.html
- py2app changelog: https://py2app.readthedocs.io/en/latest/changelog.html
- Briefcase releases and platform docs: https://briefcase.beeware.org/en/stable/about/releases.html, https://briefcase.beeware.org/en/stable/reference/platforms/macOS/index.html
- PyOxidizer status: https://gregoryszorc.com/blog/2024/03/17/my-shifting-open-source-priorities/
- python-build-standalone: https://astral.sh/blog/python-build-standalone, https://github.com/astral-sh/python-build-standalone/releases/tag/20261003
- PyApp changelog: https://ofek.dev/pyapp/latest/changelog/
- onnxruntime 1.24.1 (3.14 wheels, no Intel macOS): https://github.com/microsoft/onnxruntime/releases/tag/v1.24.1
- pyobjc on PyPI: https://pypi.org/project/pyobjc-core/
- RapidOCR model paths: https://rapidai.github.io/RapidOCRDocs/main/install_usage/rapidocr/usage/
- Tauri sidecar: https://v2.tauri.app/develop/sidecar/

macOS:
- Apple Developer Program: https://developer.apple.com/programs/enroll/
- Notarization issues: https://developer.apple.com/documentation/security/resolving-common-notarization-issues
- Hardened runtime: https://developer.apple.com/documentation/security/hardened-runtime
- Audio input entitlement: https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.security.device.audio-input
- TN3127 code signing requirements: https://developer.apple.com/documentation/technotes/tn3127-inside-code-signing-requirements
- Responsible process (DTS): https://developer.apple.com/forums/thread/805245, https://developer.apple.com/forums/thread/807898
- Monthly screen recording prompt: https://9to5mac.com/2024/08/14/macos-sequoia-screen-recording-prompt-monthly/, https://tidbits.com/2024/08/19/apple-reduces-excessive-sequoia-permission-requests-shifts-to-monthly
- Persistent content capture entitlement: https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.developer.persistent-content-capture
- SMAppService: https://developer.apple.com/documentation/servicemanagement/smappservice
- Open an unsigned app: https://support.apple.com/guide/mac-help/open-a-mac-app-from-an-unknown-developer-mh40616/mac
- Sparkle: https://sparkle-project.org/documentation/, https://sparkle-project.org/documentation/sparkle-cli/

Windows:
- Inno Setup: https://jrsoftware.org/isdl.php, https://jrsoftware.org/isorder.php
- NSIS: https://nsis.sourceforge.io/Docs/, license https://nsis.sourceforge.io/License
- WiX maintenance fee: https://docs.firegiant.com/wix/osmf/
- MSIX behind the scenes: https://learn.microsoft.com/en-us/windows/msix/desktop/desktop-to-uwp-behind-the-scenes
- SmartScreen reputation and signing options: https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation, https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/code-signing-options
- Azure Artifact Signing: https://learn.microsoft.com/en-us/azure/artifact-signing/quickstart
- Certificate life 460 days: https://sslinsights.com/code-signing-certificate-validity-reduced-460-days/
- SSL.com eSigner pricing: https://ssl.com/guide/esigner-pricing-for-code-signing/
- Certum open source: https://shop.certum.eu/open-source-code-signing-on-simplysign.html
- Velopack for Python: https://docs.velopack.io/getting-started/python, https://docs.velopack.io/packaging/signing
- MSIX auto-update: https://learn.microsoft.com/en-us/windows/msix/app-installer/auto-update-and-repair--overview

Resources:
- llama-server idle sleep (`--sleep-idle-seconds`, build b7492): https://github.com/ggml-org/llama.cpp/pull/18228
- llama-server host prompt cache (`--cache-ram`, default 8 GiB): https://github.com/ggml-org/llama.cpp/pull/16391
- llama-server options (`--prio`, `--poll`): https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md
- Router mode and model management: https://huggingface.co/blog/ggml-org/model-management-in-llamacpp
- Gemma model sizes: https://ai.google.dev/gemma/docs/core, https://huggingface.co/ggml-org/gemma-4-E2B-it-GGUF
- Small model GGUFs: https://huggingface.co/unsloth/Qwen3.5-0.8B-GGUF, https://huggingface.co/unsloth/Qwen3-VL-2B-Instruct-GGUF
- RapidOCR default config (arena off): https://raw.githubusercontent.com/RapidAI/RapidOCR/main/python/rapidocr/config.yaml
- macOS QoS: https://eclecticlight.co/2025/05/09/what-is-quality-of-service-and-how-does-it-matter/, https://developer.apple.com/library/archive/documentation/Performance/Conceptual/EnergyGuide-iOS/PrioritizeWorkWithQoS.html
- macOS `PRIO_DARWIN_BG`: https://cs.iossec.tech/xnu/latest/source/bsd/man/man2/getpriority.2
- macOS footprint: https://leancrew.com/all-this/man/man1/footprint.html
- macOS freed pages still in RSS: https://codereview.chromium.org/2743563004
- Windows EcoQoS and priority: https://learn.microsoft.com/en-us/windows/win32/procthread/quality-of-service, https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-setprocessinformation, https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-setpriorityclass
- Windows job memory limits: https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_extended_limit_information
- Task Manager memory column: https://scorpiosoftware.net/2023/04/12/memory-information-in-task-manager/
- psutil changelog: https://psutil.io/changelog/
- Gemini pricing: https://ai.google.dev/gemini-api/docs/pricing
- OpenAI pricing: https://developers.openai.com/api/docs/pricing

CI and llama.cpp:
- macos-13 runner retired: https://github.blog/changelog/2025-09-19-github-actions-macos-13-runner-image-is-closing-down/
- GitHub-hosted runners: https://docs.github.com/en/actions/reference/runners/github-hosted-runners
- llama.cpp release workflow (no macOS signing): https://github.com/ggml-org/llama.cpp/blob/master/.github/workflows/release.yml
- Homebrew llama.cpp: https://formulae.brew.sh/formula/llama.cpp
