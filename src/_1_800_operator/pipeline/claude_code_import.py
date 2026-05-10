"""Claude Code installation preflight — used at boot.

Used by `_run_slip` to fail loudly before any browser starts when the
`claude` CLI is missing or not logged in.
"""
from __future__ import annotations

import json
import shutil
import subprocess

# `claude auth status --json` is local-only (no network) and normally
# returns in <300 ms; 5 s is generous slack for a cold `node` warm-up
# on slow disks.
_CLAUDE_AUTH_TIMEOUT = 5.0


def _probe_claude_code(*, check_auth: bool = True) -> tuple[str, str]:
    """Check claude-code prereqs. Returns (status, detail).

    status:
      "ok"              — git + claude both on PATH, and (if check_auth)
                          `claude auth status --json` reports loggedIn=true.
      "prereq_missing"  — something's missing; detail names what.
    """
    if shutil.which("git") is None:
        return "prereq_missing", "git CLI not on PATH — install git first"
    if shutil.which("claude") is None:
        return "prereq_missing", "claude CLI not on PATH — install Claude Code first"
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
