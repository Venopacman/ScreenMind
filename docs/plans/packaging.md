# Ship ScreenMind as an app

Status: research and recommendation, 2026-10-07. Nothing here is built or signed yet. One unsigned macOS spike was built (see [packaging-spikes.md](packaging-spikes.md)).

## Short answer

- **Both OS: freeze with PyInstaller (onedir).** It supports Python 3.14 and has hooks for onnxruntime, sounddevice and keyring. The macOS spike built a working `ScreenMind.app` in 2.5 minutes. The dashboard answered 2 s after start.
- **macOS: a signed, notarized `.app` in a DMG.** The first-run wizard is a page in the existing dashboard. It walks through the permissions and the model download. Start at login uses `SMAppService`, not a LaunchAgent plist. Add a menu bar icon.
- **Windows: an Inno Setup wizard, per-user install.** It needs no admin rights. It adds an optional "Start at login" checkbox (HKCU Run) and asks on uninstall whether to delete `%USERPROFILE%\.screenmind`. Add a tray icon.
- **Heavy parts:** bundle a pinned CPU/Metal (macOS) or CPU/Vulkan (Windows) `llama-server`, about 10-30 MB. Download Gemma (1.5-3 GB) on first run, with progress in the wizard.
- **Architectures:** Apple Silicon only, macOS 14+. Windows x64 only. Intel Macs are out: onnxruntime has no Intel macOS wheels for Python 3.14. Windows ARM64 is out for now: our lock has no `opencv-python` wheel for it. ARM64 laptops can run the x64 build under emulation (not tested).
- **First milestone (about one week):** an unsigned `.app` + DMG and an unsigned Inno Setup wizard for internal testers, built in GitHub Actions. That needs a few code fixes first (listed below).
- **Public release:** Apple Developer Program (99 USD/year) for signing and notarization. Azure Artifact Signing (about 10 USD/month) or an OV certificate for Windows. Auto-update comes last.

## What we ship

Measured on the user's Mac (2026-10-07, branch `custom` before the strip):

| Part | Size | Where it comes from today |
|---|---|---|
| Python 3.14 + all packages, frozen (`.app`) | 363 MB on disk | PyInstaller spike |
| Same, as a compressed DMG | 169 MB (zlib), 130 MB (LZMA, `ULMO`) | `hdiutil create` |
| `llama-server` (llama.cpp) | 10-31 MB per OS (CPU, Metal, Vulkan builds). CUDA builds are 150-400 MB plus runtime | Homebrew or PATH, or `setup_llama.py` downloads it into the checkout |
| Gemma 4 E2B GGUF + mmproj | 1.5 GB (Q4_0) to 5 GB; 3.2 GB on this Mac | `huggingface_hub` into `~/.screenmind/models/<model>` |
| RapidOCR models | 25 MB | downloaded on first use into `~/.screenmind/models/ocr` |
| Embedder | 87 MB | goes away with the strip ([strip-to-actions.md](strip-to-actions.md)) |

The biggest parts of the frozen app are `cv2` (118 MB, from `opencv-python`, pulled in by `rapidocr`), onnxruntime (70 MB) and scipy (32 MB, pulled in by `imagehash`). Ways to make it smaller are in [Size](#size).

## Things in the code that break inside an app

These need fixing before any installer is useful. They are small. Most of them are "do not assume a Python checkout".

| # | Where | Problem in a frozen app | Fix |
|---|---|---|---|
| F1 | `config.py` `model_config["env_file"] = ".env"` | `.env` is read from the current directory. A LaunchAgent starts with cwd `/`, so the user's `.env` did not load (seen on 2026-10-07). An app has no cwd worth trusting. | In app mode, read config only from `~/.screenmind/settings.json` (and optionally `~/.screenmind/.env`). Never from cwd. |
| F2 | `engine/model_manager.py` (`cmd = [sys.executable, "-c", "...hf_hub_download..."]`) | Model downloads run `sys.executable -c`. In a frozen app `sys.executable` is `ScreenMind` itself. It ignores `-c`, starts a second ScreenMind, which exits on "port in use". Downloads fail. | Download in a thread in the same process (`hf_hub_download` directly), or add a hidden `--download-model` flag to `main.run()`. |
| F3 | `setup_llama.py` (`PROJECT_ROOT`) and `model_manager.start_server()` | They decide "pip install vs checkout" with `is_relative_to(site-packages)`. A frozen app looks like a checkout, so `llama/` resolves to a folder inside the app bundle. Installing there breaks the signature, and `/Applications` may not be writable. | Add a `sys.frozen` branch: use the bundled binary first, then `~/.screenmind/llama/`, then PATH. Never write inside the bundle. |
| F4 | `startup.py` `_get_startup_command()`, `main.run()` `--background`, `launcher.py`, `_install_desktop_shortcut()` | They build commands from `sys.executable -m screenmind`, `pythonw.exe` or `launcher.py`. None of these exist in an app. | In app mode the start command is the app itself. macOS: `SMAppService.mainApp` instead of the plist. Windows: HKCU Run with the path to `ScreenMind.exe` (the installer can write it). Drop the shortcut and splash code from the app path. |
| F5 | `config._setup_logging()` | A windowed build has no console. Logs go nowhere unless `SCREENMIND_LOG_FILE` is set. | In app mode, always log to `~/.screenmind/screenmind.log` (rotating). |
| F6 | `ui/overlay.py` (`[sys.executable, "-c", script]`) | Same as F2: would start a second ScreenMind. | Removed by the strip. |
| F7 | `engine/ocr.py` (`Global.model_root_dir`) | The app ships rapidocr's own default models (31 MB) but never uses them. It downloads other models into `~/.screenmind/models/ocr` on first use. | Ship the 5 models we use inside the app (works offline) and exclude rapidocr's defaults. |
| F8 | `capture_worker`, `ui_events` permission requests | Screen Recording is never requested explicitly. macOS asks on the first grab, at a random moment. | The onboarding page requests each permission on a button press (see [macOS onboarding](#first-run-wizard-onboarding)). |

The spike ran with F1-F8 still in place. The dashboard worked because the test needed none of these paths.

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

**Recommendation: no native shell now.** The dashboard already is the UI. A menu bar / tray icon in Python (pyobjc `NSStatusItem` on macOS, `pystray` on Windows) gives "Open dashboard", "Pause", "Quit". Revisit a shell only if we need real app windows.

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

### First-run wizard (onboarding)

Make it a page of the existing dashboard. The first-run welcome screen already exists (`setup_complete`). Steps:

1. **Permissions.** One row per permission, with its state and a "Grant" button. The backend calls the request API, and the page polls the state.
   - Screen Recording: `CGPreflightScreenCaptureAccess` / `CGRequestScreenCaptureAccess`.
   - Accessibility: `AXIsProcessTrustedWithOptions` (already in `POST /api/ui-events/permissions`).
   - Input Monitoring: `CGPreflightListenEventAccess` / `CGRequestListenEventAccess` (same route).
   - Microphone: only if call transcription is on. `AVCaptureDevice.requestAccessForMediaType_`. Needs `NSMicrophoneUsageDescription` in Info.plist (the spike has it).
   - Some grants need an app restart. The page says so and offers a "Restart ScreenMind" button.
2. **Model.** Pick a Gemma size, show download progress. The Model Hub already reports `downloaded_bytes`. Allow "Skip: collect now, label later".
3. **Start at login** (on by default) and **Start capturing**.

### Start at login

Use `SMAppService.mainApp.register()` (macOS 13+) through pyobjc (`pyobjc-framework-ServiceManagement`, a new dependency). It shows up in System Settings > General > Login Items, and macOS shows a "Background item added" notice once. It replaces the hand-written `~/Library/LaunchAgents/com.screenmind.plist`. That also fixes the cwd problem (F1), because the app starts like any app. Calling SMAppService from a PyInstaller app is not tested yet. ([Apple docs](https://developer.apple.com/documentation/servicemanagement/smappservice))

### Menu bar

An `NSStatusItem` with Open dashboard / Pause / Quit. It needs the AppKit run loop on the main thread. Today the main thread runs asyncio, so asyncio moves to a worker thread. Set `LSUIElement` so there is no Dock icon (the spike does this).

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
| **Inno Setup 7.1** (recommended) | Low | License: paid for commercial use (see below) | Classic wizard. Per-user install with `PrivilegesRequired=lowest`. `[Registry]` for HKCU Run, `[UninstallDelete]` and a Pascal `[Code]` step for "delete my data?". x64 and Arm64. ([jrsoftware.org](https://jrsoftware.org/isdl.php)) |
| NSIS | Low-medium | Older scripting language | Free (zlib license), no fee for companies. A fine fallback if the Inno license is a problem. |
| WiX v7 (MSI) | Medium-high | MSI authoring is verbose. Since v6, organizations with more than 10,000 USD revenue must pay the Open Source Maintenance Fee. ([FireGiant](https://docs.firegiant.com/wix/osmf/)) | Only worth it for IT-managed (GPO/Intune) rollouts. |
| MSIX | Medium | AppData and HKCU writes are virtualized and deleted on uninstall. Start at login needs a manifest `startupTask`. Low-level hooks and UI Automation from a full-trust MSIX are not confirmed. ([Microsoft Learn](https://learn.microsoft.com/en-us/windows/msix/desktop/desktop-to-uwp-behind-the-scenes)) | Not for v1. |
| Velopack | Medium | Not a wizard: a one-click installer | Installer **and** auto-updater, supports Python + PyInstaller onedir on Windows and macOS. Good candidate for the auto-update milestone. ([docs](https://docs.velopack.io/getting-started/python)) |

### Install details (Inno Setup)

- Per-user, no admin: install to `%LOCALAPPDATA%\Programs\ScreenMind`. Data stays in `%USERPROFILE%\.screenmind` as today.
- Wizard pages: welcome, folder, "Start ScreenMind when I sign in" checkbox, "Open the dashboard now" on finish. The model download happens in the dashboard's first-run page, same as macOS.
- Start at login: the installer writes `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\ScreenMind = "<path>\ScreenMind.exe"`. The app's own toggle (`startup.py`) must write the same value (F4).
- Uninstall: stop the running app first. Remove the Run value. Ask: "Also delete your ScreenMind data (screenshots, database, models: N GB)?" Default No. Models are large, so offer them separately.
- Tray icon via `pystray`: Open dashboard / Pause / Quit.
- Permissions: only the microphone ("Let desktop apps access your microphone"). Screen grabs, UI Automation and low-level hooks need no prompt.

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

| Choice | Pros | Cons |
|---|---|---|
| **Bundle a pinned build** (recommended) | Works offline after install. One known-good version. Signed with the app. | Adds 10-30 MB. We track llama.cpp updates ourselves. |
| Download on first run | Smaller installer | One more network step that can fail. On macOS a downloaded binary must still be signed, or it is quarantined. |

- macOS: the arm64 release build (about 11 MB, Metal). Put it in `Contents/Resources/llama/`.
- Windows: the Vulkan build (about 31 MB) runs on most GPUs (Intel, AMD, NVIDIA) and should fall back to CPU (to check). CUDA builds are 150-400 MB plus runtime, so offer them only as an optional download when an NVIDIA GPU is found. `setup_llama.py` already detects that.
- Pin a llama.cpp build tag (today `b11476`) and a SHA-256 in the build script. Note that llama.cpp now has two kinds of release: version tags like `v0.6.0` with notes only, and build tags `bNNNN` with the binaries.
- Keep the current behavior of reusing a llama-server already running on port 5809.

### Gemma models

Download on first run, in the onboarding page, with progress and resume (`hf_hub_download` resumes). Default to E2B Q4_0 (1.5 GB). Needs fix F2 first. Show free disk space before starting. Never ship models inside the installer: it would be 2-5 GB, and most updates would re-download it.

### OCR models

Ship the 5 RapidOCR models we use (25 MB) inside the app. Exclude rapidocr's own defaults (F7).

### Architectures

| Target | Status | Why |
|---|---|---|
| macOS arm64 (Apple Silicon), macOS 14+ | **Yes** | All wheels exist. onnxruntime needs macOS 14+. |
| macOS x86_64 (Intel) | **No** | onnxruntime dropped Intel macOS in 1.24, the first version with Python 3.14 wheels. An Intel build would need onnxruntime built from source, or Python 3.13. Also Homebrew has no Intel llama.cpp bottles. ([release notes](https://github.com/microsoft/onnxruntime/releases/tag/v1.24.1)) |
| Windows x64 | **Yes** | |
| Windows ARM64 | **Later** | onnxruntime, numpy and llama.cpp have ARM64 builds, but `opencv-python` (needed by rapidocr) has no ARM64 wheel in our lock. Run the x64 build under emulation until then (not tested). |

### Size

The app is 363 MB unpacked, 130-170 MB as a DMG. Ways to shrink it, none tested:

- The strip removes `tokenizers`, `gitpython`, `keyboard` and the embedder: about 10 MB.
- `cv2` is 118 MB because `opencv-python` bundles ffmpeg, X11 and more. Try forcing `opencv-python-headless` with a uv override.
- `scipy` (32 MB) and `pywavelets` come only from `imagehash`. A numpy pHash of ~15 lines could replace it. `test_phash_distances` must still pass with identical hashes.

## 6. Auto-update

| Option | OS | Notes |
|---|---|---|
| **"New version" check** (first) | both | On start and daily, read the latest GitHub Release. Show a banner in the dashboard with a download link. Half a day of work, no risk. |
| Sparkle 2 (2.10.0) | macOS | The standard. EdDSA-signed appcast, delta updates. Needs `Sparkle.framework` in the app, loaded via pyobjc (untested), or the `sparkle-cli` helper. ([docs](https://sparkle-project.org/documentation/)) |
| Velopack | both | Installer + updater, has a Python package, works with PyInstaller onedir. Needs the .NET SDK on the build machine. On Windows it would replace the Inno wizard. ([docs](https://docs.velopack.io/getting-started/python)) |
| WinSparkle 0.9.4 | Windows | C DLL, called from Python via ctypes (untested). Pairs with an Inno installer. |
| MSIX App Installer | Windows | Only with MSIX. Not for v1. |
| Squirrel.Windows | Windows | Unmaintained since 2024. Skip. |

Plan: the version check in milestone 1. Then pick Sparkle + WinSparkle (keeps the Inno wizard) or Velopack (one tool for both, no wizard) after the first public release.

## 7. CI (GitHub Actions)

One workflow on a version tag, a matrix of two jobs:

- `macos-15` (arm64): `uv sync --frozen` > PyInstaller spec > download pinned `llama-server` and check SHA-256 > sign (import the `.p12` into a temporary keychain) > DMG > `notarytool submit --wait` > `stapler staple` > upload to the GitHub Release.
- `windows-2025` (x64): `uv sync --frozen` > PyInstaller > pinned `llama-server` > sign the exe and DLLs > `iscc` (install Inno Setup if the image lacks it) > sign the installer > upload.

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
| **M0: internal testers** | Fix F1-F5 and F7. Bundle `llama-server`. Unsigned `.app` + DMG. Unsigned Inno Setup wizard (per-user, Run key, uninstall data question). CI builds both on a tag. Short tester guide ("Open Anyway", SmartScreen "Run anyway"). | ~1 week | Unsigned macOS builds lose permission grants on every update (try the self-signed certificate trick). |
| **M1: app feel** | Onboarding page (permissions, model download, start at login). Menu bar / tray icon. `SMAppService` login item. "New version" banner. Check by hand that a Finder-launched app gets "ScreenMind" prompts and SCK grabs work (G1, G22). | ~1-1.5 weeks | Main thread change for the menu bar. SMAppService via pyobjc untested. |
| **M2: signed public release** | Apple Developer enrollment, Developer ID signing, entitlements, notarization in CI. Azure Artifact Signing (or OV cert) for Windows. | 3-5 days of work, plus waiting: Apple org enrollment and Azure identity checks take days to weeks | Notarization fails on some nested binary. SmartScreen warnings for the first weeks anyway. |
| **M3: auto-update** | Sparkle + WinSparkle, or Velopack | ~1 week | Integration with a frozen Python app is untested for all three. |

### Recommendation

- **macOS:** PyInstaller onedir `.app`, DMG, Developer ID + notarization, dashboard onboarding, `SMAppService`, menu bar icon. Apple Silicon only.
- **Windows:** PyInstaller onedir, Inno Setup per-user wizard, Azure Artifact Signing, tray icon. x64 only.
- **Start with M0** once the strip has landed, so we freeze the slimmed app. The PyInstaller spec from the spike (`packaging/screenmind.spec`) is the starting point.

## Open questions for the user

1. Who publishes? A company account (TripleTen) or a personal one? That decides Apple org enrollment and Azure Artifact Signing eligibility.
2. Is Inno Setup's commercial license OK, or do we prefer NSIS (free)?
3. Is a monthly "keep allowing screen recording?" prompt acceptable? There is no way around it for this kind of app.
4. Is Apple Silicon + Windows x64 enough for the first testers?

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
- WiX maintenance fee: https://docs.firegiant.com/wix/osmf/
- MSIX behind the scenes: https://learn.microsoft.com/en-us/windows/msix/desktop/desktop-to-uwp-behind-the-scenes
- SmartScreen reputation and signing options: https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation, https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/code-signing-options
- Azure Artifact Signing: https://learn.microsoft.com/en-us/azure/artifact-signing/quickstart
- Certificate life 460 days: https://sslinsights.com/code-signing-certificate-validity-reduced-460-days/
- SSL.com eSigner pricing: https://ssl.com/guide/esigner-pricing-for-code-signing/
- Certum open source: https://shop.certum.eu/open-source-code-signing-on-simplysign.html
- Velopack for Python: https://docs.velopack.io/getting-started/python, https://docs.velopack.io/packaging/signing
- MSIX auto-update: https://learn.microsoft.com/en-us/windows/msix/app-installer/auto-update-and-repair--overview

CI and llama.cpp:
- macos-13 runner retired: https://github.blog/changelog/2025-09-19-github-actions-macos-13-runner-image-is-closing-down/
- GitHub-hosted runners: https://docs.github.com/en/actions/reference/runners/github-hosted-runners
- llama.cpp release workflow (no macOS signing): https://github.com/ggml-org/llama.cpp/blob/master/.github/workflows/release.yml
- Homebrew llama.cpp: https://formulae.brew.sh/formula/llama.cpp
