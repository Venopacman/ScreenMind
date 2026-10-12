#!/bin/bash
# Makes the self-signed code-signing certificate for tester builds, once.
#
#   packaging/macos/make_tester_cert.sh <out-dir>
#
# Writes <out-dir>/screenmind-tester.p12 (key + certificate) and prints a
# random password for it. Nothing goes into a keychain. Keep the .p12 and the
# password: every tester build must be signed with this same certificate, so
# that macOS keeps the testers' permission grants across builds (the
# designated requirement names the certificate, not the build). See
# docs/plans/packaging.md, "Tester builds: one self-signed certificate".
#
# For CI, store it as repository secrets:
#   gh secret set MACOS_TESTER_P12_BASE64 < <(base64 -i <out-dir>/screenmind-tester.p12)
#   gh secret set MACOS_TESTER_P12_PASSWORD
set -euo pipefail

out="${1:?usage: make_tester_cert.sh <out-dir>}"
mkdir -p "$out"
umask 077
name="ScreenMind Tester Signing"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

cat > "$work/cert.cnf" <<CNF
[req]
distinguished_name = dn
prompt = no
x509_extensions = ext
[dn]
CN = $name
[ext]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
CNF

# 10 years: the certificate must outlive every tester build signed with it.
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -sha256 \
    -config "$work/cert.cnf" -keyout "$work/key.pem" -out "$work/cert.pem" 2>/dev/null
password="$(openssl rand -base64 24)"
# -legacy where supported: macOS's `security import` reads only the older p12 format
openssl pkcs12 -export -legacy -inkey "$work/key.pem" -in "$work/cert.pem" -name "$name" \
    -out "$out/screenmind-tester.p12" -passout "pass:$password" 2>/dev/null \
  || openssl pkcs12 -export -inkey "$work/key.pem" -in "$work/cert.pem" -name "$name" \
    -out "$out/screenmind-tester.p12" -passout "pass:$password"
echo "Wrote $out/screenmind-tester.p12 ($name)"
echo "Password (store it with the file): $password"
