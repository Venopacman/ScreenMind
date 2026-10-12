#!/bin/bash
# Starts the built app once and stops it, without touching the user's data.
#
#   packaging/macos/smoke_test.sh [path/to/ScreenMind.app]
#
# The app runs from its own binary with a temp HOME and data dir, capture,
# UI events and the tray off, and a port from SMOKE_PORT (default 7899).
# Checks: the bundle has its OCR models (F6) and no rapidocr defaults, OCR on
# a fixed image inside the app uses them, the dashboard and API answer,
# memory after start, and a clean stop through POST /api/shutdown (exit
# code 0). Prints the log on failure.
# It does not grab the screen: that needs a start from Finder (see
# docs/plans/packaging-spikes.md, "macOS hand test").
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
APP="${1:-$REPO/packaging/dist/ScreenMind.app}"
BIN="$APP/Contents/MacOS/ScreenMind"
PORT="${SMOKE_PORT:-7899}"
BASE="http://127.0.0.1:$PORT"

fail() { echo "FAIL: $*" >&2; [ -f "${LOG:-}" ] && tail -60 "$LOG" >&2; exit 1; }

[ -x "$BIN" ] || fail "no app binary at $BIN"
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    fail "port $PORT is in use; set SMOKE_PORT"
fi

# F6: our OCR models are in the bundle, rapidocr's own are not.
models="$APP/Contents/Resources/ocr_models"
for f in PP-OCRv6_det_tiny.onnx ch_ppocr_mobile_v2.0_cls_mobile.onnx \
         en_PP-OCRv5_rec_mobile.onnx eslav_PP-OCRv5_rec_mobile.onnx latin_PP-OCRv5_rec_mobile.onnx; do
    [ -f "$models/$f" ] || fail "OCR model missing from the bundle: $f"
done
if ls "$APP/Contents/Resources/rapidocr/models/"*.onnx >/dev/null 2>&1; then
    fail "rapidocr's default models are still in the bundle"
fi
echo "OK OCR models in the bundle"

TMP="$(mktemp -d)"
DATA="$TMP/data"
mkdir -p "$DATA" "$TMP/home"
LOG="$DATA/screenmind.log"
cleanup() {
    if [ -n "${PID:-}" ] && kill -0 "$PID" 2>/dev/null; then kill -9 "$PID" 2>/dev/null || true; fi
    rm -rf "$TMP"
}
trap cleanup EXIT

# OCR inside the frozen app: onnxruntime and the bundled models load and read
# a fixed image (no screen access needed).
ocr_out="$(cd "$TMP" && env -i HOME="$TMP/home" PATH=/usr/bin:/bin:/usr/sbin:/sbin \
    DATA_DIR="$DATA" "$BIN" --ocr-self-test "$REPO/packaging/macos/ocr_self_test.png" 2>&1 | tail -1)" \
    || fail "OCR self-test failed: $ocr_out"
echo "$ocr_out" | grep -q '"models_dir": "[^"]*/ocr_models"' \
    || fail "OCR did not use the bundled models: $ocr_out"
echo "$ocr_out" | grep -q 'Quarterly report' || fail "OCR read the wrong text: $ocr_out"
[ -z "$(ls -A "$TMP/home")" ] || fail "OCR wrote into HOME: $(find "$TMP/home" | head)"
echo "OK OCR in the app: $ocr_out"

start=$(date +%s)
(cd "$TMP" && exec env -i HOME="$TMP/home" PATH=/usr/bin:/bin:/usr/sbin:/sbin \
    DATA_DIR="$DATA" SCREENMIND_DATA_DIR="$DATA" \
    API_PORT="$PORT" SETUP_COMPLETE=true CAPTURE_ON_START=false \
    UI_EVENTS_ENABLED=false TRAY_ICON=false \
    "$BIN" >"$TMP/stdout.txt" 2>&1) &
PID=$!

up=0
for _ in $(seq 1 120); do
    kill -0 "$PID" 2>/dev/null || fail "the app exited during start"
    if curl -sf -o /dev/null "$BASE/api/status"; then up=1; break; fi
    sleep 0.5
done
[ "$up" -eq 1 ] || fail "no answer on $BASE within 60 s"
echo "OK up after about $(( $(date +%s) - start )) s"

for path in / /css/styles.css /js/core.js /api/status /api/timeline /api/settings \
            "/api/search?q=test" /api/stats; do
    code="$(curl -s -o /dev/null -w '%{http_code}' "$BASE$path")"
    [ "$code" = "200" ] || fail "$path answered $code"
done
echo "OK dashboard and API answer 200"

sleep 5
rss_kb="$(ps -o rss= -p "$PID" | tr -d ' ')"
fp="$(footprint "$PID" 2>/dev/null | sed -n 's/.*phys_footprint: *//p' | head -1)"
echo "OK memory after start: $(( rss_kb / 1024 )) MB RSS, footprint ${fp:-unknown}"

curl -sf -X POST "$BASE/api/shutdown" >/dev/null || fail "POST /api/shutdown failed"
for _ in $(seq 1 120); do
    kill -0 "$PID" 2>/dev/null || break
    sleep 0.5
done
kill -0 "$PID" 2>/dev/null && fail "still running 60 s after /api/shutdown"
set +e
wait "$PID"
code=$?
set -e
[ "$code" -eq 0 ] || fail "exit code $code after /api/shutdown (3 means a forced stop)"
grep -q "Goodbye!" "$LOG" || fail "no 'Goodbye!' in the log"
if grep -q "Traceback" "$LOG"; then fail "a traceback in the log"; fi
echo "OK clean stop"
