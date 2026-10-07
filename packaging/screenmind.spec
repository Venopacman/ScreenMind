# PyInstaller spec for the packaging spike. Not a release build: unsigned,
# no llama-server inside, no models inside.
#
# Build from the repo root (macOS or Windows):
#   uv run --with pyinstaller pyinstaller packaging/screenmind.spec --noconfirm \
#       --distpath packaging/dist --workpath packaging/build
#
# Output: packaging/dist/ScreenMind.app (macOS) or packaging/dist/ScreenMind/ (Windows).
# Results go to docs/plans/packaging-spikes.md.
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = collect_data_files("screenmind") + collect_data_files("rapidocr")
binaries = []
hiddenimports = (
    collect_submodules("screenmind")
    # uvicorn imports its loop/protocol/lifespan modules by string name.
    + collect_submodules("uvicorn")
)
if sys.platform == "win32":
    # Untested guess for the Windows spike: uiautomation may load DLLs from its
    # package folder, and comtypes generates modules at runtime.
    datas += collect_data_files("uiautomation")
    binaries += collect_dynamic_libs("uiautomation")
    hiddenimports += collect_submodules("comtypes")

a = Analysis(
    [os.path.join(SPECPATH, "entry.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    # Test-only and dev-only packages that the venv may hold.
    excludes=["pytest", "_pytest", "PyObjCTest", "tkinter.test"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ScreenMind",
    console=False,
    icon=os.path.join(ROOT, "screenmind", "assets", "favicon.ico") if sys.platform == "win32" else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="ScreenMind")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="ScreenMind.app",
        bundle_identifier="com.screenmind.app",
        info_plist={
            "CFBundleDisplayName": "ScreenMind",
            # Background app: no Dock icon. The UI is the browser dashboard.
            "LSUIElement": True,
            "NSMicrophoneUsageDescription": "ScreenMind records call audio to transcribe meetings.",
            "LSMinimumSystemVersion": "14.0",
        },
    )
