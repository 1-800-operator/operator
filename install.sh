#!/usr/bin/env bash
# Operator installer — https://github.com/1-800-operator/operator
#
# Usage:
#   curl -LsSf https://1-800-operator.com/install | bash
#
# What this script does (read before piping to sh):
#   1. Verifies macOS or Linux.
#   2. Installs `uv` (Astral's package manager) if not already present.
#   3. Provisions Python 3.12 via `uv` if the system Python is < 3.10.
#   4. Installs the `operator` CLI via `uv tool install` from this repo.
#   5. Downloads Playwright's Chromium runtime (~170 MB).
#   6. Seeds ~/.operator/.env with commented API-key placeholders.
#   7. On macOS, checks for Google Chrome and prints an install nudge if missing.
#   8. On macOS, compiles operator-audio-capture (slip-mode audio helper).
#   9. Prints sendoff with the next-step command (auto-prefixed with
#      `source ~/.local/bin/env` if uv's tool dir wasn't already on PATH).
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

# Snapshot PATH before we mutate it in step 2 — used at the end to decide
# whether the user's future shells will already find `operator` (in which
# case we skip the `source ~/.local/bin/env` prefix in the sendoff).
INITIAL_PATH="${PATH:-}"

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

# -- 5. Seed ~/.operator/.env ------------------------------------------------

mkdir -p "${HOME}/.operator"
if [ ! -f "${ENV_PATH}" ]; then
  bold "Seeding ${ENV_PATH} with API-key placeholders..."
  cat > "${ENV_PATH}" <<'ENV_EOF'
# Operator API keys — uncomment + fill in the ones you need.
# Loaded by every `operator slip` invocation.
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

# -- 6. macOS Chrome cask nudge ----------------------------------------------

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

# -- 7. Register transcript MCP user-scope -----------------------------------

# Operator's bundled transcript MCP exposes the meeting JSONL as
# search_captions / list_captions / list_speakers tools. Registering it
# user-scope here means every `claude` session afterward (terminal-direct
# or meeting-spawned) has the transcript tools available without operator
# passing --mcp-config at spawn time — the naked-spawn invariant (Phase
# 14.22.3). Soft-skip if `claude` isn't on PATH yet; re-running install.sh
# after `claude login` lands the registration.

if command -v claude >/dev/null 2>&1; then
  bold "Registering transcript MCP user-scope..."
  MCP_TOOL_DIR="$(uv tool dir)/1-800-operator"
  MCP_PY_IN_TOOL="${MCP_TOOL_DIR}/bin/python"
  if [ ! -x "${MCP_PY_IN_TOOL}" ]; then
    err "Could not find tool venv python at ${MCP_PY_IN_TOOL} — skipping MCP registration."
  else
    # Idempotent: remove first (no-op if not registered), then re-add.
    claude mcp remove transcript --scope user >/dev/null 2>&1 || true
    if claude mcp add transcript --scope user -- "${MCP_PY_IN_TOOL}" -m _1_800_operator.mcp_servers.transcript_server; then
      info "Registered transcript MCP (user-scope)."
    else
      err "Failed to register transcript MCP — re-run install.sh after the issue is resolved."
    fi
  fi
  echo
else
  warn "Claude Code CLI not found on PATH — skipping transcript MCP + plugin registration."
  warn "Install it from https://claude.ai/code, run \`claude login\`, then re-run install.sh."
  echo
fi

# -- 7.5. Install operator plugin (user-scope slash commands) ---------------

# The plugin ships /operator:slip, /operator:status, /operator:hangup,
# /operator:doctor — the user-facing surface that lets you type slash
# commands into a Claude Code session. Without it, the operator CLI works
# but there's no way to bridge a live Claude Code session ID into a
# meeting (the slip skill body does the ${CLAUDE_SESSION_ID} substitution
# at dispatch time, which is the load-bearing handoff between Claude Code
# and the operator subprocess).
#
# The plugin is sourced from a self-hosted marketplace.json at the root of
# this CLI's GitHub repo, which references github.com/1-800-operator/operator-plugin.
# Both subcommands run non-interactively (stdin redirected from /dev/null)
# and write to user-scope settings, so the plugin is enabled in every
# subsequent Claude Code session until the user uninstalls it. Soft-skip
# if `claude` is missing (step 7's warning already covered that case).

if command -v claude >/dev/null 2>&1; then
  bold "Installing operator plugin (slash commands)..."
  # Idempotent: clear any prior state before re-adding. Errors swallowed
  # because remove/uninstall raise when the target doesn't exist.
  claude plugin uninstall operator </dev/null >/dev/null 2>&1 || true
  claude plugin marketplace remove 1-800-operator </dev/null >/dev/null 2>&1 || true
  if claude plugin marketplace add 1-800-operator/operator </dev/null >/dev/null; then
    if claude plugin install operator@1-800-operator </dev/null >/dev/null; then
      info "Installed operator plugin (user-scope). Slash commands /operator:* are now available in Claude Code."
    else
      err "Failed to install operator plugin — re-run install.sh after the issue is resolved."
    fi
  else
    err "Failed to add operator marketplace — re-run install.sh after the issue is resolved."
  fi
  echo
fi

# -- 8. macOS audio helper (operator-audio-capture) -------------------------

# Slip mode's dual-stream audio capture (mic + system) is delivered by a
# Swift helper that needs Apple-Dev signing + notarization to be allowed by
# macOS TCC for SCStream callbacks. There are two paths:
#
#   (a) Production: the wheel ships a pre-built, signed, notarized .app at
#       _1_800_operator/swift/operator-audio-capture.app. We copy it into
#       ~/.operator/bin/ (Granola-style — user never compiles or signs).
#
#   (b) From-source dev: the wheel doesn't ship the .app (e.g. you're
#       installing from a git checkout that hasn't been release-built yet).
#       We fall back to swiftc + ad-hoc-signed raw binary. THIS PATH CANNOT
#       DO SYSTEM AUDIO — SCStream callbacks are silently denied for ad-hoc
#       binaries on macOS 14+. Mic still works. To get system audio in dev,
#       run `scripts/build_signed_helper.sh` from a checkout (requires the
#       team's Developer-ID cert).
#
# Linux skips silently — slip is mac-only.

if [ "${OS}" = "macos" ]; then
  bold "Installing operator-audio-capture (slip-mode audio helper)..."
  TOOL_DIR="$(uv tool dir)/1-800-operator"
  PY_IN_TOOL="${TOOL_DIR}/bin/python"
  BIN_DIR="${HOME}/.operator/bin"
  mkdir -p "${BIN_DIR}"

  if [ ! -x "${PY_IN_TOOL}" ]; then
    err "Could not find tool venv python at ${PY_IN_TOOL} — skipping audio helper."
  else
    PKG_SWIFT_DIR="$(${PY_IN_TOOL} -c 'import _1_800_operator, pathlib; print(pathlib.Path(_1_800_operator.__file__).parent / "swift")')"
    PREBUILT_APP="${PKG_SWIFT_DIR}/operator-audio-capture.app"
    INSTALLED_APP="${BIN_DIR}/operator-audio-capture.app"

    if [ -d "${PREBUILT_APP}" ]; then
      # Path (a): production-shipped .app. Copy and we're done.
      rm -rf "${INSTALLED_APP}"
      cp -R "${PREBUILT_APP}" "${INSTALLED_APP}"
      info "Installed signed helper: ${INSTALLED_APP}"
    elif command -v swiftc >/dev/null 2>&1; then
      # Path (b): dev fallback. Mic-only; system audio will be denied by TCC.
      SWIFT_SRC="${PKG_SWIFT_DIR}/operator-audio-capture.swift"
      BIN_OUT="${PKG_SWIFT_DIR}/operator-audio-capture"
      if swiftc "${SWIFT_SRC}" -O -o "${BIN_OUT}"; then
        chmod +x "${BIN_OUT}"
        # Ad-hoc sign with stable identifier so TCC grants survive rebuilds.
        codesign --force --sign - --identifier com.1-800-operator.audio-capture "${BIN_OUT}" 2>/dev/null || true
        info "Built dev helper: ${BIN_OUT}"
        warn "Dev fallback: this build CANNOT capture system audio (ad-hoc signing)."
        warn "Mic capture still works. For system audio, run scripts/build_signed_helper.sh"
        warn "from a checkout (requires the team's Developer-ID cert) or wait for the next"
        warn "release wheel which will ship a notarized helper."
      else
        err "swiftc failed — slip will run chat-only."
      fi
    else
      warn "No prebuilt helper in wheel and swiftc not found — slip will run chat-only."
      warn "Install Xcode Command Line Tools (xcode-select --install) and re-run,"
      warn "or wait for the next release wheel which ships a prebuilt helper."
    fi
  fi
  echo
fi

# -- 9. Sendoff --------------------------------------------------------------

# Detect whether the user's shells already have ~/.local/bin on PATH (so
# `operator` will be found in this terminal *and* future ones). If not, the
# sendoff prefixes the next-step commands with `source ~/.local/bin/env &&`
# so the user can run operator without opening a new terminal.
case ":${INITIAL_PATH}:" in
  *":${HOME}/.local/bin:"*) PATH_PREFIX="" ;;
  *) PATH_PREFIX="source ~/.local/bin/env && " ;;
esac

printf '\n\033[1;32m✓\033[0m \033[1moperator successfully installed!\033[0m\n'
echo
info "Docs: https://github.com/1-800-operator/operator"
echo
bold "Next:"
printf '  Verify your install:\n'
printf '    %s\033[1;95moperator doctor\033[0m\n' "${PATH_PREFIX}"
echo
printf '  Open Claude Code and send it into a meeting:\n'
printf '    \033[1;95m/operator:slip\033[0m \033[2m<meet-url>\033[0m\n'
printf '    \033[2m(the operator plugin is already enabled — your meeting brain inherits this Claude Code session)\033[0m\n'
echo
printf '  Or attach directly from a terminal (no session bridge):\n'
printf '    %s\033[1;95moperator slip claude\033[0m \033[2m<meet-url>\033[0m\n' "${PATH_PREFIX}"
echo
info "Operator drives the Claude Code CLI for its LLM brain. If you haven't already:"
info "  Install:  https://claude.ai/code"
info "  Sign in:  claude login"
echo
info "Flags + power-user setup:"
info "  --yolo                 Skip per-tool permission prompts on any mode."
info "  ~/.claude/settings.json   Operator inherits your Claude Code allow-list —"
info "                            tools you've already trusted there auto-approve"
info "                            in operator too (no extra wiring)."
