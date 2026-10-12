#!/bin/bash
# Signs a built EvolveCapture.app with the tester certificate (self-signed).
#
#   MACOS_TESTER_P12=<file.p12> MACOS_TESTER_P12_PASSWORD=<pw> \
#       packaging/macos/sign_app.sh [path/to/EvolveCapture.app]
#
# Every tester build signed with the same certificate gets the same
# designated requirement (bundle id + this certificate), so macOS keeps the
# testers' Screen Recording, Accessibility and Input Monitoring grants across
# builds. An ad-hoc signature ties the grants to one exact build instead.
# The certificate comes from packaging/macos/make_tester_cert.sh.
#
# The identity lives only in a temporary keychain that is deleted at the end;
# the login keychain and the keychain search list are not touched. No
# hardened runtime: library validation needs a Team ID, which a self-signed
# certificate does not have. This is not a Developer ID signature, so
# Gatekeeper still blocks the first start ("Open Anyway").
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
APP="${1:-$REPO/packaging/dist/EvolveCapture.app}"
P12="${MACOS_TESTER_P12:?set MACOS_TESTER_P12 to the .p12 file}"
PW="${MACOS_TESTER_P12_PASSWORD:?set MACOS_TESTER_P12_PASSWORD}"
[ -d "$APP" ] || { echo "No app at $APP" >&2; exit 1; }

work="$(mktemp -d)"
kc="$work/tester.keychain-db"
kc_pw="$(openssl rand -hex 16)"
cleanup() { security delete-keychain "$kc" 2>/dev/null || true; rm -rf "$work"; }
trap cleanup EXIT

security create-keychain -p "$kc_pw" "$kc"
security set-keychain-settings "$kc"  # no auto-lock during the run
security unlock-keychain -p "$kc_pw" "$kc"
security import "$P12" -k "$kc" -P "$PW" -T /usr/bin/codesign >/dev/null
# Let codesign use the key without a GUI prompt
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$kc_pw" "$kc" >/dev/null

# The identity's SHA-1. It is untrusted (self-signed), so ask for all
# identities, not only valid ones.
sha1="$(security find-identity -p codesigning "$kc" | awk '/ScreenMind Tester Signing/ {print $2; exit}')"
[ -n "$sha1" ] || { echo "No 'ScreenMind Tester Signing' identity in $P12" >&2; exit 1; }

codesign --force --deep --timestamp=none --keychain "$kc" --sign "$sha1" "$APP"
codesign --verify --deep --strict "$APP"
echo "Signed $APP with $sha1"
codesign -d -r- "$APP" 2>&1 | sed -n 's/^designated => /Designated requirement: /p'
