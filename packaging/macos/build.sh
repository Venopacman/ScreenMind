#!/bin/bash
# Builds the unsigned macOS app and DMG:
#   packaging/dist/ScreenMind.app
#   packaging/dist/ScreenMind-<version>-macos-arm64.dmg
#
# 1. uv sync --frozen
# 2. PyInstaller with packaging/screenmind.spec (it also fetches the OCR models, F6)
# 3. With MACOS_TESTER_P12 and MACOS_TESTER_P12_PASSWORD set: signs the app
#    with the tester certificate (sign_app.sh), so testers keep their
#    permission grants across builds. Without them the app stays ad-hoc signed.
# 4. A DMG (LZMA) with the app and an Applications link
#
# Run from anywhere; paths are relative to the repo. Needs uv and Xcode's
# command line tools (hdiutil, codesign come with macOS).
# In CI: .github/workflows/package-macos.yml. Smoke test: packaging/macos/smoke_test.sh.
#
# Options:
#   --skip-pyinstaller   reuse packaging/dist/ScreenMind.app from an earlier run
#   --skip-dmg           stop after the app (for example to sign it first)
set -euo pipefail

PYINSTALLER_VERSION="6.22.3"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
DIST="$REPO/packaging/dist"
APP="$DIST/ScreenMind.app"

skip_pyinstaller=0
skip_dmg=0
for arg in "$@"; do
    case "$arg" in
        --skip-pyinstaller) skip_pyinstaller=1 ;;
        --skip-dmg) skip_dmg=1 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

# Version: screenmind/__init__.py is the one source (pyproject reads it too).
version="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$REPO/screenmind/__init__.py")"
if [ -z "$version" ]; then
    echo "No __version__ in screenmind/__init__.py" >&2
    exit 1
fi
echo "ScreenMind $version"

cd "$REPO"
if [ "$skip_pyinstaller" -eq 0 ]; then
    uv sync --frozen
    uv run --frozen --with "pyinstaller==$PYINSTALLER_VERSION" \
        pyinstaller packaging/screenmind.spec --noconfirm \
        --distpath packaging/dist --workpath packaging/build
fi
[ -d "$APP" ] || { echo "Missing $APP" >&2; exit 1; }
if [ -n "${MACOS_TESTER_P12:-}" ]; then
    "$REPO/packaging/macos/sign_app.sh" "$APP"
else
    echo "Signature: ad-hoc (set MACOS_TESTER_P12 and MACOS_TESTER_P12_PASSWORD to sign" \
         "with the tester certificate)"
fi
codesign --verify --deep "$APP"
echo "App: $(du -sm "$APP" | cut -f1) MB"

if [ "$skip_dmg" -eq 0 ]; then
    dmg="$DIST/ScreenMind-$version-macos-arm64.dmg"
    stage="$(mktemp -d)"
    trap 'rm -rf "$stage"' EXIT
    ditto "$APP" "$stage/ScreenMind.app"
    ln -s /Applications "$stage/Applications"
    hdiutil create -quiet -volname ScreenMind -srcfolder "$stage" -format ULMO -ov "$dmg"
    echo "DMG: $dmg ($(du -sm "$dmg" | cut -f1) MB)"
fi
