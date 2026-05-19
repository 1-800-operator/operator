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
#   8. On macOS, compiles the Operator audio helper (dial-mode dual-stream).
#   9. Prints sendoff with the next-step command (auto-prefixed with
#      `source ~/.local/bin/env` if uv's tool dir wasn't already on PATH).
#
# Idempotent — safe to re-run. Does not modify shell rc files.

set -euo pipefail

REPO_URL="https://github.com/1-800-operator/operator.git"
ENV_PATH="${HOME}/.operator/.env"
MIN_PY_MAJOR=3
MIN_PY_MINOR=10

# Single source of truth for the release pin. install.sh, the plugin
# marketplace.json `ref`, and the operator-plugin git tag must all agree
# at release time. Pinning to a tag instead of `main` HEAD closes a
# supply-chain hole — a phished contributor or a brief account
# compromise on `main` would otherwise ship arbitrary code (with TCC
# Mic + Screen Recording grants three steps later) to every install in
# the compromise window. Same reason rustup / uv / pyenv all pin a tag.
#
# Override during pre-release / dev installs:
#   OPERATOR_INSTALL_REF=main  curl … | bash
OPERATOR_INSTALL_REF="${OPERATOR_INSTALL_REF:-v0.1.33}"

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

bold "Installing operator (ref=${OPERATOR_INSTALL_REF})..."
uv tool install --force --python "${UV_PY_SPEC}" "git+${REPO_URL}@${OPERATOR_INSTALL_REF}"
echo

# -- 5. Seed ~/.operator/.env ------------------------------------------------

mkdir -p "${HOME}/.operator"
if [ ! -f "${ENV_PATH}" ]; then
  bold "Seeding ${ENV_PATH} with API-key placeholders..."
  cat > "${ENV_PATH}" <<'ENV_EOF'
# Operator API keys — uncomment + fill in the ones you need.
# Loaded by every `operator dial` invocation.
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
    # Also remove the pre-S238 `transcript` name in case the user is
    # upgrading across the rename (pre-launch, but a few dev installs
    # have the old registration).
    claude mcp remove transcript --scope user >/dev/null 2>&1 || true
    claude mcp remove operator-meeting-record --scope user >/dev/null 2>&1 || true
    if claude mcp add operator-meeting-record --scope user -- "${MCP_PY_IN_TOOL}" -m _1_800_operator.mcp_servers.record_server; then
      info "Registered operator-meeting-record MCP (user-scope)."
    else
      err "Failed to register operator-meeting-record MCP — re-run install.sh after the issue is resolved."
    fi
  fi
  echo
else
  warn "Claude Code CLI not found on PATH — skipping operator-meeting-record MCP + plugin registration."
  warn "Install it from https://claude.ai/code, run \`claude login\`, then re-run install.sh."
  echo
fi

# -- 7.5. Install operator plugin (user-scope slash commands) ---------------

# The plugin ships /operator:dial, /operator:status, /operator:hangup,
# /operator:doctor — the user-facing surface that lets you type slash
# commands into a Claude Code session. Without it, the operator CLI works
# but there's no way to bridge a live Claude Code session ID into a
# meeting (the dial skill body does the ${CLAUDE_SESSION_ID} substitution
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
  # Use explicit HTTPS URL (not the `owner/repo` shorthand) so the
  # marketplace clone doesn't fall back to SSH on machines without
  # GitHub SSH keys. New users without a configured ssh identity were
  # hitting `git@github.com: Permission denied (publickey)` here.
  if claude plugin marketplace add https://github.com/1-800-operator/operator.git </dev/null >/dev/null; then
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

# -- 7.6. Allowlist operator commands for desktop-app skill dispatch --------

# In the Claude Code desktop app (Mac/Windows), `!` blocks inside plugin
# skills don't surface an approval dialog when the command isn't pre-
# allowlisted — the Bash call silent-fails and the model goes quiet.
# Terminal CLI users see a prompt; desktop-app users see nothing. Since the
# desktop app is most users' default surface, /operator:dial and the other
# operator skills won't work out of the box without this allowlist entry.
#
# One entry covers every current operator skill (dial, status, hangup,
# doctor, recap) because operator self-daemonizes — there's no nohup wrapper
# in the skill bodies. Merge-in (preserves existing user entries), idempotent
# (skip if already present), soft-skip if claude isn't on PATH or the file
# is unparseable.

if command -v claude >/dev/null 2>&1; then
  bold "Allowlisting operator commands in ~/.claude/settings.json..."
  ALLOWLIST_RESULT="$(python3 - <<'PY' 2>&1
import json, os, sys
path = os.path.expanduser("~/.claude/settings.json")
# Entries needed for operator plugin skills + bundled MCP to work in
# the desktop app without silent-fail or permission prompts mid-meeting:
#   Bash(operator:*)                       dial/status/hangup/doctor/recap
#   Bash(claude plugin marketplace update) /operator:update
#   Bash(claude plugin update operator)    /operator:update
#   mcp__operator-meeting-record__*        bundled meeting-record MCP
#     (renamed from mcp__transcript__* in S238 — we also prune the dead
#     entry below for users upgrading from older installs)
# (Avoid apostrophes in this heredoc body — bash command-substitution
# parses quotes inside heredoc bodies and an unbalanced "'" breaks it.)
entries = [
    "Bash(operator:*)",
    "Bash(claude plugin marketplace update:*)",
    "Bash(claude plugin update operator:*)",
    "mcp__operator-meeting-record__*",
]
DEAD_ENTRIES = {"mcp__transcript__*", "mcp__transcript__list_meeting_record"}
try:
    with open(path) as f:
        cfg = json.load(f)
except FileNotFoundError:
    cfg = {}
except (json.JSONDecodeError, OSError) as e:
    print(f"skip:{e}"); sys.exit(0)
if not isinstance(cfg, dict):
    print("skip:settings-not-an-object"); sys.exit(0)
perms = cfg.setdefault("permissions", {})
if not isinstance(perms, dict):
    print("skip:permissions-not-an-object"); sys.exit(0)
allow = perms.setdefault("allow", [])
if not isinstance(allow, list):
    print("skip:allow-not-a-list"); sys.exit(0)
# Prune old transcript MCP entries from prior installs (server was
# renamed to operator-meeting-record in S238).
pruned = [e for e in allow if e in DEAD_ENTRIES]
allow[:] = [e for e in allow if e not in DEAD_ENTRIES]
added = []
for e in entries:
    if e not in allow:
        allow.append(e)
        added.append(e)
if not added and not pruned:
    print("present"); sys.exit(0)
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
parts = []
if added:
    parts.append(f"added:{','.join(added)}")
if pruned:
    parts.append(f"pruned:{','.join(pruned)}")
print(";".join(parts))
PY
)" || ALLOWLIST_RESULT="skip:python-failed"
  case "${ALLOWLIST_RESULT}" in
    added:*|pruned:*|added:*\;pruned:*)
      info "Updated ~/.claude/settings.json permissions.allow: ${ALLOWLIST_RESULT}"
      ;;
    present) info "operator allowlist entries already present in ~/.claude/settings.json — leaving untouched." ;;
    skip:*)  warn "Could not update ~/.claude/settings.json (${ALLOWLIST_RESULT#skip:}) — desktop-app users may need to add Bash(operator:*) + Bash(claude plugin marketplace update:*) + Bash(claude plugin update operator:*) + mcp__operator-meeting-record__* to permissions.allow manually." ;;
    *)       warn "Unexpected allowlist result: ${ALLOWLIST_RESULT}" ;;
  esac
  echo
fi

# -- 8. macOS audio helper (Operator.app) -----------------------------------

# Dial mode's dual-stream audio capture (mic + system) is delivered by a
# Swift helper that needs Apple-Dev signing + notarization to be allowed by
# macOS TCC for SCStream callbacks. There are two paths:
#
#   (a) Production: the wheel ships a pre-built, signed, notarized .app at
#       _1_800_operator/swift/Operator.app. We copy it into ~/.operator/bin/
#       (Granola-style — user never compiles or signs).
#
#   (b) From-source dev: the wheel doesn't ship the .app (e.g. you're
#       installing from a git checkout that hasn't been release-built yet).
#       We fall back to swiftc + ad-hoc-signed raw binary. THIS PATH CANNOT
#       DO SYSTEM AUDIO — SCStream callbacks are silently denied for ad-hoc
#       binaries on macOS 14+. Mic still works. To get system audio in dev,
#       run `scripts/build_signed_helper.sh` from a checkout (requires the
#       team's Developer-ID cert).
#
# Linux skips silently — dial is mac-only.

if [ "${OS}" = "macos" ]; then
  bold "Installing Operator audio helper (dial-mode dual-stream)..."
  TOOL_DIR="$(uv tool dir)/1-800-operator"
  PY_IN_TOOL="${TOOL_DIR}/bin/python"
  BIN_DIR="${HOME}/.operator/bin"
  mkdir -p "${BIN_DIR}"

  if [ ! -x "${PY_IN_TOOL}" ]; then
    err "Could not find tool venv python at ${PY_IN_TOOL} — skipping audio helper."
  else
    PKG_SWIFT_DIR="$(${PY_IN_TOOL} -c 'import _1_800_operator, pathlib; print(pathlib.Path(_1_800_operator.__file__).parent / "swift")')"
    PREBUILT_APP="${PKG_SWIFT_DIR}/Operator.app"
    INSTALLED_APP="${BIN_DIR}/Operator.app"

    if [ -d "${PREBUILT_APP}" ]; then
      # Path (a): production-shipped .app. Copy and we're done.
      rm -rf "${INSTALLED_APP}"
      cp -R "${PREBUILT_APP}" "${INSTALLED_APP}"
      info "Installed signed helper: ${INSTALLED_APP}"

      # Launch Services cleanup. Every `uv tool install --reinstall` extracts
      # the wheel into a fresh `~/.cache/uv/archive-v0/<hash>/` dir, and LS
      # auto-registers any `.app` it finds. Over a development cycle that's
      # dozens of stale copies all claiming `com.1-800-operator.audio-capture`.
      # When the TCC warmup dialog click attaches to "whichever copy LS
      # resolved first," the grant can land on a stale archive copy instead
      # of our canonical INSTALLED_APP, and the runtime helper silently fails
      # (S240 hit 36 stale registrations). Cleanup is idempotent + best-effort.
      LSREG="/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister"
      if [ -x "${LSREG}" ]; then
        n_stale=0
        while IFS= read -r p; do
          if [ -n "$p" ] && [ "$p" != "${INSTALLED_APP}" ]; then
            "${LSREG}" -u "$p" >/dev/null 2>&1 && n_stale=$((n_stale + 1))
          fi
        done < <("${LSREG}" -dump 2>/dev/null | awk '/^path:/{path=$2} /com\.1-800-operator\.audio-capture/{print path; path=""}' | sort -u)
        if [ "$n_stale" -gt 0 ]; then
          info "Unregistered $n_stale stale Launch Services entries for the helper bundle."
        fi
      fi


      # TCC warmup. The helper bundle carries its own TCC identity
      # (com.1-800-operator.audio-capture). When the dial flow exec's the
      # inner binary as a subprocess of operator (which is itself a child
      # of the user's terminal/IDE), macOS's responsible-process
      # attribution can land on the wrong app and silently deny without
      # ever surfacing the dialog. Invoking via `open -W -a` here forces
      # Launch Services to attribute the prompt to the helper bundle
      # itself, so the user sees the dialogs once at install time and
      # grants them cleanly — instead of hitting a broken-by-default dial
      # on first use. See debug spike 2026-05-15 for the attribution
      # validation (mic granted cleanly; screen recording hit Apple's
      # post-deny cooldown only because the test env was over-cycled).
      HELPER_BIN="${INSTALLED_APP}/Contents/MacOS/Operator"
      # Probe via _disclaimed_spawn so TCC attribution resolves to Operator.app
      # itself, not to the parent terminal/IDE through bash's responsibility
      # chain. Without disclaim, AVCaptureDevice.authorizationStatus and
      # CGPreflightScreenCaptureAccess answer against the responsible-process
      # chain and the probe lies — reporting "denied" even when System
      # Settings shows the helper as granted (see memory:
      # project_tcc_responsibility_chain_attribution.md).
      probe_helper() {
        "${PY_IN_TOOL}" - <<PYEOF 2>/dev/null
import sys
try:
    from _1_800_operator.pipeline._disclaimed_spawn import spawn_disclaimed, minimal_helper_env
    p = spawn_disclaimed(["${HELPER_BIN}", "--probe"], env=minimal_helper_env())
    out = p.stdout.read(4096).decode("utf-8", errors="replace").strip() if p.stdout else ""
    p.wait(timeout=5)
    print(out)
except Exception:
    print("{}")
PYEOF
      }
      PROBE_BEFORE="$(probe_helper)"
      [ -z "${PROBE_BEFORE}" ] && PROBE_BEFORE='{}'
      if echo "${PROBE_BEFORE}" | grep -q '"screen_recording":"ok"' \
        && echo "${PROBE_BEFORE}" | grep -q '"microphone":"ok"'; then
        info "Audio permissions already granted (Screen Recording + Microphone)"
      else
        bold "macOS will now request Screen Recording and Microphone permissions"
        info "  These are required for dial mode to capture meeting audio."
        info "  Click Allow on each dialog as it appears. (Take a few seconds — the"
        info "  helper will exit on its own once the prompts are dismissed.)"
        # `-W` waits for the launched app to exit. The helper requests
        # both perms in its init path, then waits on stdin which is
        # /dev/null here, so it exits via the 10s watchdog. `-n` opens
        # a fresh instance even if a stale one is around. 2>/dev/null
        # suppresses a benign Launch Services warning some setups emit.
        open -W -n -a "${INSTALLED_APP}" 2>/dev/null || true
        PROBE_AFTER="$(probe_helper)"
        [ -z "${PROBE_AFTER}" ] && PROBE_AFTER='{}'

        # Quit-and-Reopen recovery (macOS 14+ Screen Recording flow):
        # When the user toggles SR on inside System Settings, macOS demands
        # the helper "Quit and Reopen." That kills the running instance —
        # which install.sh's `open -W` had been waiting on, so `open -W`
        # returns. macOS then spawns a fresh helper instance, but it runs
        # detached from `open -W` and may not surface the mic dialog in
        # the foreground before exiting. Net result: SR=ok, mic=not_determined.
        #
        # Recovery: kill any leftover background helpers, launch a new
        # helper in the background (NOT `-W` — we don't want to block on
        # its exit), then poll the TCC state every 2s. The user sees the
        # mic dialog within ~1-2s of the relaunch; their click lands a
        # grant which the next poll cycle picks up. This is dramatically
        # faster than the two-stage `open -W` approach which had to wait
        # for the full helper-shutdown sequence after the user clicks.
        if echo "${PROBE_AFTER}" | grep -q '"screen_recording":"ok"' && \
           echo "${PROBE_AFTER}" | grep -q '"microphone":"not_determined"'; then
          pkill -f "${INSTALLED_APP}/Contents/MacOS/Operator" 2>/dev/null || true
          info "  Screen Recording granted ✓"
          bold "  Now requesting Microphone permission..."
          info "  → Look for the Microphone dialog (may appear behind System Settings"
          info "    if Settings is still in the foreground — switch to Operator if so)."
          info "  Click Allow when it appears."
          # Bring the helper to the foreground via `open -a` without -W (we
          # want to poll, not block). The launch is sync enough that the
          # mic dialog appears within ~1-3s on most machines.
          open -n -a "${INSTALLED_APP}" >/dev/null 2>&1 &
          disown 2>/dev/null || true

          # Poll up to 60s. Each iteration: 2s sleep + ~500ms probe.
          # Print a heartbeat every ~10s so the user knows install isn't
          # hung while they look for the dialog.
          POLL_START=$(date +%s)
          POLL_DEADLINE=$((POLL_START + 60))
          last_heartbeat=0
          while [ "$(date +%s)" -lt "${POLL_DEADLINE}" ]; do
            sleep 2
            PROBE_AFTER="$(probe_helper)"
            [ -z "${PROBE_AFTER}" ] && PROBE_AFTER='{}'
            if echo "${PROBE_AFTER}" | grep -q '"screen_recording":"ok"' && \
               echo "${PROBE_AFTER}" | grep -q '"microphone":"ok"'; then
              break
            fi
            elapsed=$(($(date +%s) - POLL_START))
            if [ $((elapsed / 10)) -gt "${last_heartbeat}" ]; then
              last_heartbeat=$((elapsed / 10))
              info "  ...still waiting for Microphone Allow click (${elapsed}s elapsed, 60s max)"
            fi
          done

          # Whether or not polling succeeded, clean up the background
          # helper so it doesn't linger past install.sh's exit.
          pkill -f "${INSTALLED_APP}/Contents/MacOS/Operator" 2>/dev/null || true
        fi

        if echo "${PROBE_AFTER}" | grep -q '"screen_recording":"ok"' \
          && echo "${PROBE_AFTER}" | grep -q '"microphone":"ok"'; then
          info "✓ Audio permissions granted (Screen Recording + Microphone)"
        else
          warn "Audio permissions not fully granted yet (probe: ${PROBE_AFTER})."
          warn "Dial mode will run, but captions may be silent until you grant access."
          warn "  Fix: System Settings → Privacy & Security → Screen Recording (and Microphone)"
          warn "       → '+' → ${INSTALLED_APP} → enable"
          warn "       Then re-run install.sh or 'operator doctor' to re-check."
        fi
      fi
    elif command -v swiftc >/dev/null 2>&1; then
      # Path (b): dev fallback. Mic-only; system audio will be denied by TCC.
      SWIFT_SRC="${PKG_SWIFT_DIR}/operator-audio-capture.swift"
      BIN_OUT="${PKG_SWIFT_DIR}/Operator"
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
        err "swiftc failed — dial will run chat-only."
      fi
    else
      warn "No prebuilt helper in wheel and swiftc not found — dial will run chat-only."
      warn "Install Xcode Command Line Tools (xcode-select --install) and re-run,"
      warn "or wait for the next release wheel which ships a prebuilt helper."
    fi
  fi
  echo
fi

# -- 8.5. AEC3 speaker-bleed cleaner (Rust binary) ---------------------------

# Dial mode runs a long-lived Rust binary that AEC3-cancels speaker bleed from
# the mic stream before it reaches whisper. Without it, transcripts of the
# user's mic include the remote audio playing through the user's speakers
# (when the user is on built-in speakers; headphone users are unaffected).
#
# Delivery: the GitHub Actions workflow at .github/workflows/build-aec3.yml
# builds aec3 on macos-14 (arm64) + macos-13 (x86_64), lipos them into a
# universal Mach-O binary, and attaches it to the GitHub release matching
# the operator version tag. install.sh downloads the universal binary from
# that release. If the download fails (CI in progress, release tag missing
# the asset, offline install) we fall back to building from Rust source via
# cargo — same hygiene flags as before (--locked --frozen). If cargo is
# also missing, we surface the existing warning and leave dial without the
# bleed defense (headphone users unaffected; built-in-speaker users see
# their own speaker output in transcripts).

if [ "${OS}" = "macos" ]; then
  bold "Installing aec3 (speaker-bleed cleaner)..."
  TOOL_DIR="$(uv tool dir)/1-800-operator"
  PY_IN_TOOL="${TOOL_DIR}/bin/python"
  BIN_DIR="${HOME}/.operator/bin"
  mkdir -p "${BIN_DIR}"

  AEC3_RELEASE_URL="https://github.com/1-800-operator/operator/releases/download/${OPERATOR_INSTALL_REF}/aec3-darwin-universal"
  AEC3_DEST="${BIN_DIR}/aec3"
  AEC3_TMP="$(mktemp -t aec3-XXXXXX)"

  if curl -fsSL --max-time 60 -o "${AEC3_TMP}" "${AEC3_RELEASE_URL}" 2>/dev/null && \
     file "${AEC3_TMP}" | grep -q "Mach-O"; then
    chmod +x "${AEC3_TMP}"
    mv "${AEC3_TMP}" "${AEC3_DEST}"
    info "Installed aec3 (prebuilt universal): ${AEC3_DEST}"
  else
    rm -f "${AEC3_TMP}"
    # Release asset unavailable — fall back to building from source.
    if [ ! -x "${PY_IN_TOOL}" ]; then
      err "Could not find tool venv python at ${PY_IN_TOOL} — skipping aec3 build."
    elif ! command -v cargo >/dev/null 2>&1; then
      warn "Prebuilt aec3 unavailable for ${OPERATOR_INSTALL_REF} and cargo not found — skipping aec3."
      warn "Dial will run without the speaker-bleed cleaner (mic transcripts may include"
      warn "remote audio playing through your speakers; headphone users are unaffected)."
      warn "To enable AEC: install Rust (https://rustup.rs/) and re-run install.sh,"
      warn "or wait for the prebuilt binary to be attached to the ${OPERATOR_INSTALL_REF} release."
    else
      info "Prebuilt aec3 unavailable — building from source (this takes ~30s)..."
      PKG_RUST_DIR="$(${PY_IN_TOOL} -c 'import _1_800_operator, pathlib; print(pathlib.Path(_1_800_operator.__file__).parent / "rust" / "aec3")')"
      if [ ! -f "${PKG_RUST_DIR}/Cargo.toml" ]; then
        warn "Rust source not present in wheel (${PKG_RUST_DIR}) — skipping aec3."
      else
        if cargo build --release --locked --frozen --manifest-path "${PKG_RUST_DIR}/Cargo.toml"; then
          cp "${PKG_RUST_DIR}/target/release/aec3" "${AEC3_DEST}"
          chmod +x "${AEC3_DEST}"
          info "Built and installed aec3 from source: ${AEC3_DEST}"
        else
          err "cargo build failed — dial will run without the speaker-bleed cleaner."
        fi
      fi
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
printf '    \033[1;95m/operator:dial\033[0m \033[2m<meet-url>\033[0m\n'
printf '    \033[2m(the operator plugin is already enabled — your meeting brain inherits this Claude Code session)\033[0m\n'
