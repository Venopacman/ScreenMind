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

if sys.platform == "win32":
    # PyInstaller finds the DLLs that extensions link to through PATH. Any other
    # app's folder on PATH can then put its own msvcp140.dll, vcruntime140.dll and
    # ucrtbase.dll into the bundle. On the Windows laptop a JDK 11 folder came
    # first: its msvcp140.dll 14.16 crashed onnxruntime (0xc0000005) on the first
    # OCR call. Keep only Windows' own folders, so the runtime comes from System32
    # (or from Python and the wheels themselves).
    _windir = os.path.normcase(os.environ.get("SystemRoot", r"C:\Windows"))
    os.environ["PATH"] = os.pathsep.join(
        p for p in os.environ.get("PATH", "").split(os.pathsep)
        if os.path.normcase(os.path.abspath(p)).startswith(_windir)
    )

datas = collect_data_files("screenmind") + collect_data_files("rapidocr")
binaries = []
hiddenimports = (
    collect_submodules("screenmind")
    # uvicorn imports its loop/protocol/lifespan modules by string name.
    + collect_submodules("uvicorn")
)
if sys.platform == "win32":
    # uiautomation loads its DLLs from its own bin/ folder through ctypes, so
    # PyInstaller warns "not found"; this keeps them at that path. comtypes
    # generates modules at runtime.
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
