#!/usr/bin/env bash
# Build the aec3 universal-binary (arm64 + x86_64) locally — no CI, no
# Apple Developer account, no x86_64 Homebrew install needed.
#
# Background
# ----------
# `.github/workflows/build-aec3.yml` builds the universal binary on
# native runners (macos-14 arm64 + macos-13-large x86_64). The free
# `macos-13` runner pool is structurally unavailable as of May 2026 as
# GitHub winds down Intel runners, and we haven't enabled Actions
# billing for `macos-13-large` yet during the dev cycle. This script
# is the local fallback so we can keep cutting dev releases without
# manually-uploaded arm64-only stop-gaps.
#
# How it works
# ------------
# 1. arm64 leg: native `cargo build` on this Apple Silicon Mac.
# 2. x86_64 leg: `rustup run stable-x86_64-apple-darwin cargo build`
#    with `PATH` pointing at standalone x86_64 build tools
#    (ninja, pkg-config, meson-wrapper). The cargo toolchain runs
#    under Rosetta; meson sees x86_64 host (key for AVX2 source
#    inclusion in webrtc-audio-processing-sys); cc emits x86_64.
# 3. `lipo -create` the two arch binaries.
# 4. Smoke-test both slices (1s silence WAV through aec3 batch mode,
#    same shape as the CI smoke step).
# 5. Optional `--upload <tag>` uploads to a GitHub release (creating
#    the release if it doesn't exist yet).
#
# Prerequisites (auto-installed on first run if missing)
# ------------------------------------------------------
# - rustup target `stable-x86_64-apple-darwin` (force-non-host)
# - `/tmp/x86_64-tools/bin/`:
#     - ninja (downloaded from github.com/ninja-build/ninja releases —
#       the macOS asset is a fat binary with both arches)
#     - pkg-config (compiled from source for x86_64, ~30s)
#     - meson (bash wrapper around `arch -x86_64 /usr/bin/python3 -m
#       mesonbuild.mesonmain` — /usr/bin/python3 is universal and
#       reports machine=x86_64 under arch -x86_64, which is what
#       gives meson the correct host detection)
# - mesonbuild Python package installed for /usr/bin/python3
#   (`arch -x86_64 /usr/bin/python3 -m pip install --user meson`)
#
# Usage
# -----
#   scripts/build_aec3_universal.sh                    # build + smoke-test
#   scripts/build_aec3_universal.sh --upload v0.1.38   # build + upload to a release tag
#   scripts/build_aec3_universal.sh --clean            # nuke /tmp/aec3-build-x86 + /tmp/x86_64-tools cache
#
# Output: ./build/aec3-darwin-universal + .sha256 (in repo root)

set -euo pipefail

# -- paths -------------------------------------------------------------------

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AEC3_SRC="${REPO_ROOT}/src/_1_800_operator/rust/aec3"
TOOLS_DIR="/tmp/x86_64-tools"
BUILD_DIR_X86="/tmp/aec3-build-x86"
BUILD_DIR_ARM64="/tmp/aec3-build-arm64"
OUT_DIR="${REPO_ROOT}/build"
PKGCONFIG_VERSION="0.29.2"
NINJA_RELEASE_URL="https://github.com/ninja-build/ninja/releases/latest/download/ninja-mac.zip"

UPLOAD_TAG=""
CLEAN=0
for arg in "$@"; do
  case "$arg" in
    --upload) shift; UPLOAD_TAG="${1:-}"; [ -z "$UPLOAD_TAG" ] && { echo "ERR: --upload requires a tag"; exit 2; }; shift || true ;;
    --upload=*) UPLOAD_TAG="${arg#*=}" ;;
    --clean) CLEAN=1 ;;
    --help|-h) sed -n '2,40p' "$0"; exit 0 ;;
    *) ;;  # forward-compat
  esac
done

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
info() { printf '  %s\n' "$1"; }
warn() { printf '\033[33m  %s\033[0m\n' "$1"; }
err()  { printf '\033[31m  %s\033[0m\n' "$1" >&2; }

# -- 0. preflight -----------------------------------------------------------

bold "aec3 universal-binary builder"
echo

if [ "$(uname -s)" != "Darwin" ]; then
  err "This script only runs on macOS."
  exit 1
fi

if [ "$(uname -m)" != "arm64" ]; then
  err "This script is designed to run on Apple Silicon (arm64) hosts."
  err "If you're on Intel, just \`cargo build --release\` natively."
  exit 1
fi

for tool in cargo rustup curl tar unzip lipo make file shasum; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    err "Missing required tool: $tool"
    exit 1
  fi
done

if [ "$CLEAN" = "1" ]; then
  bold "--clean: removing caches"
  rm -rf "$BUILD_DIR_X86" "$BUILD_DIR_ARM64" "$TOOLS_DIR"
  info "Removed $BUILD_DIR_X86 $BUILD_DIR_ARM64 $TOOLS_DIR"
  echo
fi

# -- 1. install x86_64 rust toolchain if missing -----------------------------

if ! rustup toolchain list 2>/dev/null | grep -q "^stable-x86_64-apple-darwin"; then
  bold "1/6  Installing x86_64 rust toolchain (will run under Rosetta)..."
  rustup toolchain install stable-x86_64-apple-darwin --force-non-host
  echo
else
  bold "1/6  x86_64 rust toolchain present"
  echo
fi

# -- 2. set up /tmp/x86_64-tools/ -------------------------------------------

bold "2/6  Setting up x86_64 build tools at $TOOLS_DIR"
mkdir -p "$TOOLS_DIR/bin"

# 2a. ninja (universal binary from github releases).
if [ ! -x "$TOOLS_DIR/bin/ninja" ]; then
  info "Downloading ninja..."
  tmpdir="$(mktemp -d)"
  curl -fsSL "$NINJA_RELEASE_URL" -o "$tmpdir/ninja-mac.zip"
  (cd "$tmpdir" && unzip -q -o ninja-mac.zip)
  mv "$tmpdir/ninja" "$TOOLS_DIR/bin/ninja"
  chmod +x "$TOOLS_DIR/bin/ninja"
  rm -rf "$tmpdir"
  info "ninja: $(file "$TOOLS_DIR/bin/ninja" | head -1)"
else
  info "ninja: already present"
fi

# 2b. pkg-config (build from source for x86_64).
if [ ! -x "$TOOLS_DIR/bin/pkg-config" ]; then
  info "Building pkg-config from source for x86_64..."
  cd "$TOOLS_DIR"
  if [ ! -d "pkg-config-$PKGCONFIG_VERSION" ]; then
    curl -fsSL "https://pkgconfig.freedesktop.org/releases/pkg-config-$PKGCONFIG_VERSION.tar.gz" \
      -o "pkg-config-$PKGCONFIG_VERSION.tar.gz"
    tar xzf "pkg-config-$PKGCONFIG_VERSION.tar.gz"
  fi
  cd "pkg-config-$PKGCONFIG_VERSION"
  # `--build` lies about host: configure's config.sub (1996-era) doesn't
  # recognize arm64-apple-darwin, but we're cross-compiling so build arch
  # doesn't really matter; saying x86_64 makes it happy.
  # CFLAGS -Wno-... downgrades modern clang errors back to warnings for
  # the bundled glib in 0.29.2 (gatomic.c has pointer-conversion issues
  # that pre-date clang's hard-fail).
  if [ ! -f pkg-config ]; then
    CFLAGS="-arch x86_64 -Wno-int-conversion -Wno-implicit-function-declaration -Wno-incompatible-pointer-types" \
    LDFLAGS="-arch x86_64" \
      ./configure --with-internal-glib --host=x86_64-apple-darwin --build=x86_64-apple-darwin \
                  --prefix=/tmp/x86_64-tools >/dev/null
    make -j8 >/dev/null
  fi
  cp pkg-config "$TOOLS_DIR/bin/pkg-config"
  cd "$REPO_ROOT"
  info "pkg-config: $(file "$TOOLS_DIR/bin/pkg-config" | head -1)"
else
  info "pkg-config: already present"
fi

# 2c. meson wrapper.
# /usr/bin/python3 is Apple's universal Python (~3.9). `arch -x86_64
# /usr/bin/python3` runs the x86_64 slice; from that python's POV
# uname -m == x86_64, which is what meson uses for host detection.
# Without this wrapper, cargo/build.rs invokes `meson` from PATH and
# meson is typically installed via arm64 Homebrew's Python — meson
# detects host=aarch64 and skips the AVX2 source files, causing
# linker errors in the x86_64 build.
if ! arch -x86_64 /usr/bin/python3 -c "import mesonbuild" >/dev/null 2>&1; then
  info "Installing mesonbuild Python package for /usr/bin/python3..."
  arch -x86_64 /usr/bin/python3 -m pip install --user meson >/dev/null
fi
cat > "$TOOLS_DIR/bin/meson" <<'EOF'
#!/bin/bash
# Wrapper that forces meson to run under x86_64-emulated Python so its
# host-arch detection reports x86_64 (required for webrtc-audio-processing-sys
# to include AVX2 source files when cross-targeting x86_64 from arm64).
exec arch -x86_64 /usr/bin/python3 -m mesonbuild.mesonmain "$@"
EOF
chmod +x "$TOOLS_DIR/bin/meson"
info "meson wrapper: $TOOLS_DIR/bin/meson"
echo

# -- 3. build arm64 (native) ------------------------------------------------

bold "3/6  Building arm64 aec3 (native)..."
cd "$AEC3_SRC"
cargo build --release --locked --target aarch64-apple-darwin \
  --target-dir "$BUILD_DIR_ARM64" 2>&1 | tail -3
ARM64_BIN="$BUILD_DIR_ARM64/aarch64-apple-darwin/release/aec3"
if [ ! -x "$ARM64_BIN" ]; then
  err "arm64 build did not produce $ARM64_BIN"
  exit 4
fi
info "arm64: $(file "$ARM64_BIN" | head -1)"
echo

# -- 4. build x86_64 (Rosetta + standalone x86_64 tools) --------------------

bold "4/6  Building x86_64 aec3 (Rosetta + standalone x86_64 tools, ~5 min)..."
cd "$AEC3_SRC"
PATH="$TOOLS_DIR/bin:$PATH" \
  rustup run stable-x86_64-apple-darwin cargo build --release --locked \
    --target x86_64-apple-darwin --target-dir "$BUILD_DIR_X86" 2>&1 | tail -3
X86_BIN="$BUILD_DIR_X86/x86_64-apple-darwin/release/aec3"
if [ ! -x "$X86_BIN" ]; then
  err "x86_64 build did not produce $X86_BIN"
  err "Inspect meson logs at: $BUILD_DIR_X86/.../webrtc-audio-processing-build/meson-logs/"
  exit 5
fi
info "x86_64: $(file "$X86_BIN" | head -1)"
echo

# -- 5. lipo + sha256 + smoke-test ------------------------------------------

bold "5/6  lipo + smoke-test..."
mkdir -p "$OUT_DIR"
UNIVERSAL="$OUT_DIR/aec3-darwin-universal"
lipo -create "$ARM64_BIN" "$X86_BIN" -output "$UNIVERSAL"
chmod +x "$UNIVERSAL"
info "universal: $(file "$UNIVERSAL" | head -1)"

# sha256 alongside binary, matching CI's naming
shasum -a 256 "$UNIVERSAL" | sed "s|$OUT_DIR/||" > "$UNIVERSAL.sha256"
info "sha256: $(cat "$UNIVERSAL.sha256")"

# Smoke test (same shape as build-aec3.yml's CI step): 1s silence
# WAV through aec3 batch mode, assert non-empty output WAV.
SMOKE_DIR="$(mktemp -d)"
/usr/bin/python3 - "$SMOKE_DIR" <<'PY'
import sys, wave
d = sys.argv[1]
for n in ("silence_mic.wav", "silence_ref.wav"):
    with wave.open(f"{d}/{n}", "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 16000)
PY

# arm64 slice (native).
"$UNIVERSAL" --mic "$SMOKE_DIR/silence_mic.wav" --ref "$SMOKE_DIR/silence_ref.wav" \
             --out "$SMOKE_DIR/cleaned.wav" >/dev/null 2>&1
if [ ! -s "$SMOKE_DIR/cleaned.wav" ]; then
  err "arm64 slice smoke test FAILED"
  exit 6
fi
info "arm64 slice smoke: ok"

# x86_64 slice (via Rosetta).
arch -x86_64 "$UNIVERSAL" --mic "$SMOKE_DIR/silence_mic.wav" --ref "$SMOKE_DIR/silence_ref.wav" \
                          --out "$SMOKE_DIR/cleaned_x86.wav" >/dev/null 2>&1
if [ ! -s "$SMOKE_DIR/cleaned_x86.wav" ]; then
  err "x86_64 slice smoke test FAILED"
  exit 7
fi
info "x86_64 slice smoke: ok"
rm -rf "$SMOKE_DIR"
echo

# -- 6. optional upload -----------------------------------------------------

if [ -n "$UPLOAD_TAG" ]; then
  bold "6/6  Uploading to GitHub release $UPLOAD_TAG..."
  if ! command -v gh >/dev/null 2>&1; then
    err "gh CLI not installed — can't upload. Binary is at $UNIVERSAL"
    exit 8
  fi
  REPO_SLUG="1-800-operator/operator"
  if gh release view "$UPLOAD_TAG" --repo "$REPO_SLUG" >/dev/null 2>&1; then
    info "Release $UPLOAD_TAG exists; uploading assets (clobber)..."
    gh release upload "$UPLOAD_TAG" --repo "$REPO_SLUG" --clobber \
      "$UNIVERSAL" "$UNIVERSAL.sha256"
  else
    info "Release $UPLOAD_TAG doesn't exist yet; creating it..."
    gh release create "$UPLOAD_TAG" --repo "$REPO_SLUG" \
      --title "$UPLOAD_TAG" \
      --notes "AEC3 universal binary built locally via scripts/build_aec3_universal.sh during dev cycle. See docs/handoff.md for context." \
      "$UNIVERSAL" "$UNIVERSAL.sha256"
  fi
  echo
  info "Release URL: https://github.com/$REPO_SLUG/releases/tag/$UPLOAD_TAG"
else
  bold "6/6  Skipping upload (no --upload tag given)"
  info "To upload later: gh release upload <tag> --repo 1-800-operator/operator $UNIVERSAL $UNIVERSAL.sha256"
fi
echo

printf '\033[1;32m✓\033[0m \033[1mDone.\033[0m  %s\n' "$UNIVERSAL"
