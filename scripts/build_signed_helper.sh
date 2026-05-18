#!/usr/bin/env bash
# Build the Operator audio helper as a notarized .app bundle.
#
# Run on a machine that has:
#   - swiftc (Xcode Command Line Tools)
#   - the team's "Developer ID Application" cert in login Keychain
#   - notarytool credentials stored as keychain profile "notarytool-password"
#     (one-time setup: xcrun notarytool store-credentials notarytool-password ...)
#
# Output: ~/.operator/bin/Operator.app (signed + notarized + stapled)
#
# This is the release-time artifact builder. End users never run this; they
# get the prebuilt .app via the wheel (eventually) or this script bundled
# locally for now. Distribution shape is resolved in 14.21.

set -euo pipefail

# -- Configuration ----------------------------------------------------------

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SWIFT_SRC="${REPO_ROOT}/src/_1_800_operator/swift/operator-audio-capture.swift"
INFO_PLIST="${REPO_ROOT}/src/_1_800_operator/swift/Info.plist"
ENTITLEMENTS="${REPO_ROOT}/src/_1_800_operator/swift/helper.entitlements"
ICNS="${REPO_ROOT}/src/_1_800_operator/swift/Operator.icns"

BUNDLE_ID="com.1-800-operator.audio-capture"
SIGN_IDENTITY="Developer ID Application: Jojo Shapiro (DSW7V72HT7)"
NOTARY_PROFILE="notarytool-password"

OUT_DIR="${HOME}/.operator/bin"
APP_NAME="Operator.app"
APP_PATH="${OUT_DIR}/${APP_NAME}"
ZIP_PATH="${OUT_DIR}/Operator.zip"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
info() { printf '  %s\n' "$1"; }
warn() { printf '\033[33m  %s\033[0m\n' "$1"; }
err()  { printf '\033[31m  %s\033[0m\n' "$1" >&2; }

# -- Preflight --------------------------------------------------------------

bold "Building signed + notarized Operator.app"
echo

if [ "$(uname -s)" != "Darwin" ]; then
  err "This script only runs on macOS."
  exit 1
fi

for tool in swiftc codesign xcrun ditto plutil; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    err "Missing required tool: $tool"
    err "Install Xcode Command Line Tools: xcode-select --install"
    exit 1
  fi
done

if ! security find-identity -v -p codesigning | grep -q "${SIGN_IDENTITY}"; then
  err "Signing identity not found in Keychain:"
  err "  ${SIGN_IDENTITY}"
  err "Verify with: security find-identity -v -p codesigning"
  exit 1
fi

if ! security find-generic-password -s "com.apple.gke.notary.tool" -a "${NOTARY_PROFILE}" >/dev/null 2>&1; then
  warn "notarytool keychain profile '${NOTARY_PROFILE}' not found via direct lookup."
  warn "If notarize step fails, recreate with:"
  warn "  xcrun notarytool store-credentials ${NOTARY_PROFILE} \\"
  warn "    --apple-id <your-apple-id> --team-id DSW7V72HT7"
  echo
fi

for f in "${SWIFT_SRC}" "${INFO_PLIST}" "${ENTITLEMENTS}" "${ICNS}"; do
  if [ ! -f "$f" ]; then
    err "Missing required source file: $f"
    exit 1
  fi
done

# -- 1. Compile -------------------------------------------------------------

bold "1/5  Compiling Swift helper..."
mkdir -p "${OUT_DIR}"
TMP_BIN="$(mktemp -t Operator.XXXXXX)"
swiftc "${SWIFT_SRC}" -O -o "${TMP_BIN}"
chmod +x "${TMP_BIN}"
info "Built: ${TMP_BIN}"
echo

# -- 2. Bundle as .app -----------------------------------------------------

bold "2/5  Assembling .app bundle..."
# Idempotent: blow away any prior .app so stale signatures or old Info.plists
# can't poison the new build.
rm -rf "${APP_PATH}"
mkdir -p "${APP_PATH}/Contents/MacOS"
mkdir -p "${APP_PATH}/Contents/Resources"
cp "${INFO_PLIST}" "${APP_PATH}/Contents/Info.plist"
cp "${ICNS}" "${APP_PATH}/Contents/Resources/Operator.icns"
mv "${TMP_BIN}" "${APP_PATH}/Contents/MacOS/Operator"
chmod +x "${APP_PATH}/Contents/MacOS/Operator"
# Validate the plist before the codesign step uses it.
plutil -lint "${APP_PATH}/Contents/Info.plist" >/dev/null
info "Bundle: ${APP_PATH}"
echo

# -- 3. Codesign ------------------------------------------------------------

bold "3/5  Code-signing with Developer ID + hardened runtime..."
codesign --force --deep --options runtime --timestamp \
  --sign "${SIGN_IDENTITY}" \
  --identifier "${BUNDLE_ID}" \
  --entitlements "${ENTITLEMENTS}" \
  "${APP_PATH}"
codesign --verify --strict --verbose=2 "${APP_PATH}"
info "Signed."
echo

# -- 4. Notarize ------------------------------------------------------------

bold "4/5  Notarizing via Apple (this can take 1-5 minutes)..."
rm -f "${ZIP_PATH}"
ditto -c -k --keepParent "${APP_PATH}" "${ZIP_PATH}"
xcrun notarytool submit "${ZIP_PATH}" \
  --keychain-profile "${NOTARY_PROFILE}" \
  --wait
rm -f "${ZIP_PATH}"
info "Notarization accepted."
echo

# -- 5. Staple --------------------------------------------------------------

bold "5/5  Stapling notarization ticket..."
xcrun stapler staple "${APP_PATH}"
xcrun stapler validate "${APP_PATH}"
info "Stapled."
echo

# -- Done -------------------------------------------------------------------

printf '\033[1;32m✓\033[0m \033[1mHelper ready at:\033[0m %s\n' "${APP_PATH}"
echo
info "Next:"
info "  1. The Python helper-path resolution now expects this .app layout."
info "  2. Re-run tests/_helper_smoke_12s.py to confirm mic still works."
info "  3. Live-test with a real Meet for system audio."
