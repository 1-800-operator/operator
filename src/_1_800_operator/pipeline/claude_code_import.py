"""Claude Code installation preflight — used at boot.

Used by `_run_slip` and `operator doctor` to fail loudly before any
browser starts when the `claude` CLI is missing, logged out, or too old
for operator's PTY + hook-events flow.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess

# `claude auth status --json` is local-only (no network) and normally
# returns in <300 ms; 5 s is generous slack for a cold `node` warm-up
# on slow disks.
_CLAUDE_AUTH_TIMEOUT = 5.0
_CLAUDE_VERSION_TIMEOUT = 5.0

# Minimum Claude Code version operator's PTY + hook-events flow was
# validated against (debug/14_22_pty_spike ran on 2.1.141). Older builds
# may not emit the SessionStart / Stop hook events or the transcript
# shape the provider depends on, so operator refuses to launch on them.
_MIN_CLAUDE_CODE_VERSION = (2, 1, 141)
_MIN_CLAUDE_CODE_VERSION_STR = "2.1.141"


def _claude_code_version() -> tuple[int, ...] | None:
    """Parse `claude --version` into a comparable (major, minor, patch)
    tuple, or None if it can't be determined (timeout, non-zero exit,
    unparseable output).

    Callers fail OPEN on None — refusing a correctly-installed claude
    because the `--version` string format changed would be worse than
    proceeding on an unknown version.
    """
    try:
        r = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=_CLAUDE_VERSION_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", r.stdout)
    if not m:
        return None
    return tuple(int(g) for g in m.groups())


def _probe_claude_code(*, check_auth: bool = True) -> tuple[str, str]:
    """Check claude-code prereqs. Returns (status, detail).

    status:
      "ok"               — git + claude on PATH, claude new enough, and
                           (if check_auth) `claude auth status --json`
                           reports loggedIn=true.
      "prereq_missing"   — something's missing; detail names what.
      "version_too_old"  — claude is installed but below the floor
                           operator was validated against; detail says so.
    """
    if shutil.which("git") is None:
        return "prereq_missing", "git CLI not on PATH — install git first"
    if shutil.which("claude") is None:
        return "prereq_missing", "claude CLI not on PATH — install Claude Code first"
    ver = _claude_code_version()
    if ver is not None and ver < _MIN_CLAUDE_CODE_VERSION:
        got = ".".join(str(p) for p in ver)
        return "version_too_old", (
            f"Claude Code {got} is too old — operator needs "
            f">= {_MIN_CLAUDE_CODE_VERSION_STR}. Update via /plugin "
            f"(or reinstall from https://claude.ai/code)."
        )
    if not check_auth:
        return "ok", ""
    try:
        r = subprocess.run(
            ["claude", "auth", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=_CLAUDE_AUTH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return "prereq_missing", (
            f"`claude auth status` did not respond within {_CLAUDE_AUTH_TIMEOUT:.0f}s "
            f"— try running it manually and sign in"
        )
    except OSError as e:
        return "prereq_missing", f"could not run `claude auth status`: {e}"
    if r.returncode != 0:
        return "prereq_missing", "not logged in — run `claude auth login`"
    try:
        payload = json.loads(r.stdout)
    except json.JSONDecodeError:
        return "prereq_missing", "not logged in — run `claude auth login`"
    if not payload.get("loggedIn"):
        return "prereq_missing", "not logged in — run `claude auth login`"
    return "ok", ""


def claude_code_installed_and_logged_in() -> tuple[bool, str]:
    """Returns (ok, reason_if_not_ok).

    ok=True iff git + claude CLI are on PATH and `claude auth status
    --json` reports loggedIn: true.
    """
    status, detail = _probe_claude_code(check_auth=True)
    return status == "ok", detail
