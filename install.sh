#!/usr/bin/env bash
# Operator installer — https://github.com/1-800-operator/operator
#
# Usage:
#   curl -LsSf https://1-800-operator.com/install | sh
#
# What this script does (read before piping to sh):
#   1. Verifies macOS or Linux.
#   2. Installs `uv` (Astral's package manager) if not already present.
#   3. Provisions Python 3.12 via `uv` if the system Python is < 3.10.
#   4. Installs the `operator` CLI via `uv tool install` from this repo.
#   5. Downloads Playwright's Chromium runtime (~170 MB).
#   6. Seeds ~/.operator/.env with commented API-key placeholders.
#   7. On macOS, checks for Google Chrome and prints an install nudge if missing.
#   8. Verifies `operator` is on PATH.
#
# Idempotent — safe to re-run. Does not modify shell rc files.

set -euo pipefail

REPO_URL="https://github.com/1-800-operator/operator.git"
ENV_PATH="${HOME}/.operator/.env"
MIN_PY_MAJOR=3
MIN_PY_MINOR=10

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
info() { printf '  %s\n' "$1"; }
warn() { printf '\033[33m  %s\033[0m\n' "$1"; }
err()  { printf '\033[31m  %s\033[0m\n' "$1" >&2; }

bold "Operator installer"
echo

# -- 1. OS detection ---------------------------------------------------------

case "$(uname -s)" in
  Darwin) OS=macos ;;
  Linux)  OS=linux ;;
  *) err "Unsupported OS: $(uname -s). Operator runs on macOS and Linux."; exit 1 ;;
esac
info "Detected OS: ${OS}"
echo

# -- 2. uv bootstrap ---------------------------------------------------------

if ! command -v uv >/dev/null 2>&1; then
  bold "Installing uv (Astral package manager)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv installs to ~/.local/bin or ~/.cargo/bin depending on shell; surface it
  # to this script's PATH for the rest of the run. User's shell rc handles
  # future sessions (uv's installer prints the relevant export hint).
  if [ -d "${HOME}/.local/bin" ]; then PATH="${HOME}/.local/bin:${PATH}"; fi
  if [ -d "${HOME}/.cargo/bin" ]; then PATH="${HOME}/.cargo/bin:${PATH}"; fi
  if ! command -v uv >/dev/null 2>&1; then
    err "uv installed but not on PATH. Open a new shell and re-run this script."
    exit 1
  fi
fi
info "uv: $(uv --version)"
echo

# -- 3. Python preflight (uv-provisioned if needed) --------------------------

PY_OK=0
if command -v python3 >/dev/null 2>&1; then
  PY_VERSION="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  PY_OK="$(python3 -c "import sys; print(1 if sys.version_info >= (${MIN_PY_MAJOR}, ${MIN_PY_MINOR}) else 0)")"
  info "System Python: ${PY_VERSION}"
fi

if [ "${PY_OK}" != "1" ]; then
  bold "Installing Python 3.12 via uv (system Python is < ${MIN_PY_MAJOR}.${MIN_PY_MINOR})..."
  uv python install 3.12
fi
echo

# -- 4. Operator install via uv tool -----------------------------------------

# Pin uv to a >= 3.10 interpreter. If the system Python is too old, this picks
# the uv-managed one provisioned in step 3.
UV_PY_SPEC=">=${MIN_PY_MAJOR}.${MIN_PY_MINOR}"

bold "Installing operator..."
uv tool install --force --python "${UV_PY_SPEC}" "git+${REPO_URL}"
echo

# -- 5. Playwright Chromium runtime ------------------------------------------

bold "Downloading Playwright Chromium runtime (~170 MB)..."
# Run via uv tool so we use the same env operator uses.
uv tool run --python "${UV_PY_SPEC}" --from "git+${REPO_URL}" playwright install chromium
echo

# -- 6. Seed ~/.operator/.env ------------------------------------------------

mkdir -p "${HOME}/.operator"
if [ ! -f "${ENV_PATH}" ]; then
  bold "Seeding ${ENV_PATH} with API-key placeholders..."
  cat > "${ENV_PATH}" <<'ENV_EOF'
# Operator API keys — uncomment + fill in the ones you need.
# This file is loaded by every `operator dial <bot>` invocation.
#
# Anthropic (claude agent default model):
# ANTHROPIC_API_KEY=sk-ant-...
#
# OpenAI (used by the codex agent and any custom bot pointed at OpenAI):
# OPENAI_API_KEY=sk-...
#
# GitHub (for the bundled GitHub MCP — read-only ops on issues, PRs, repos):
# GITHUB_TOKEN=ghp_...
ENV_EOF
  chmod 600 "${ENV_PATH}"
  info "Wrote ${ENV_PATH} (mode 600)."
else
  info "${ENV_PATH} already exists — leaving untouched."
fi
echo

# -- 7. macOS Chrome cask nudge ----------------------------------------------

if [ "${OS}" = "macos" ]; then
  if [ ! -d "/Applications/Google Chrome.app" ]; then
    warn "Google Chrome not found in /Applications."
    warn "Operator drives a real Chrome (not bundled Chromium) for Google Meet sign-in."
    warn "Install it before your first meeting:"
    warn "  brew install --cask google-chrome"
    warn "  (or download from https://www.google.com/chrome/)"
    echo
  fi
fi

# -- 8. Verify on PATH -------------------------------------------------------

if ! command -v operator >/dev/null 2>&1; then
  warn "operator installed but not on PATH yet."
  warn "Open a new shell, or add uv's tool dir to PATH:"
  warn "  export PATH=\"\$HOME/.local/bin:\$PATH\""
  echo
fi

bold "Done."
echo
info "Next: run \`operator setup\` to configure your first agent."
info "Then \`operator dial claude\` (or your bot name) to join a meeting."
info "Docs: https://github.com/1-800-operator/operator"
