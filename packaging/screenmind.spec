# PyInstaller spec for ScreenMind. Unsigned. The app ships the OCR models
# for the default languages (F6; others download on first use) and the
# person-name model (138 MB; a frozen app never downloads it).
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

# OCR models the app uses (F6 in docs/plans/packaging.md): fetched once into
# packaging/build/ocr_models (RapidOCR checks each SHA-256), then bundled as
# ocr_models/. engine/ocr.py looks there first inside the app.
sys.path.insert(0, ROOT)
from screenmind import __version__  # noqa: E402
from screenmind.engine.ocr import BUNDLED_MODELS_DIRNAME, fetch_bundled_models  # noqa: E402

_ocr_models = fetch_bundled_models(os.path.join(SPECPATH, "build", "ocr_models"))

# Person-name model (privacy/person_ner.py): downloaded once into
# packaging/build/ner-model (pinned revision, SHA-256 checked) and converted
# in a child Python, then bundled at models/ner/<name>. The 4 files must stay
# together: onnxruntime finds model.onnx.data next to model.onnx.
from pathlib import Path  # noqa: E402

from screenmind.privacy import person_ner  # noqa: E402

_ner_dir = Path(SPECPATH) / "build" / "ner-model"
if not person_ner.is_installed(_ner_dir):
    person_ner.install(_ner_dir)

datas = (
    collect_data_files("screenmind")
    # RapidOCR's own default models (PP-OCRv6 small, 31 MB) are never used.
    + collect_data_files("rapidocr", excludes=["models/*.onnx"])
    + [(str(f), BUNDLED_MODELS_DIRNAME) for f in _ocr_models]
    + [(str(_ner_dir / f), str(person_ner.BUNDLE_SUBDIR)) for f in person_ner.FILES]
)
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
        bundle_identifier="com.nebius.evolve.work.capture",
        version=__version__,
        info_plist={
            "CFBundleDisplayName": "ScreenMind",
            # Background app: no Dock icon. The UI is the browser dashboard.
            "LSUIElement": True,
            "LSMinimumSystemVersion": "14.0",
        },
    )
