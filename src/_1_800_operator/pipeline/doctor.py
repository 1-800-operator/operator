"""`operator doctor` — diagnostic checker (Phase 14.19.5).

Analogous to `brew doctor` / `flutter doctor`. Prints a checklist of
world-readiness items, marks each pass/fail, and emits the exact fix
command for failures. No interactive prompts, no fixups — purely
read-only diagnostics. Exits 0 if everything is green, 1 if any check
fails (so CI / scripts can gate on it).

Composition: each check is a tiny pure function returning
`CheckResult(name, status, fix)`. `run_doctor()` calls them in order
and renders. Easy to extend — Phase 14.20 will add a Screen &
System Audio Recording check when slip caption capture ships.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from _1_800_operator.pipeline.chrome_preflight import (
    CHROME_PATH,
    INSTALL_URL as CHROME_INSTALL_URL,
    chrome_installed,
)
from _1_800_operator.pipeline.install_preflight import chromium_installed
from _1_800_operator.pipeline.readiness import _probe_claude_code

_AUTH_STATE_FILE = Path.home() / ".operator" / "auth_state.json"


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str   # one-line status (green or red)
    fix: str      # exact command(s) to run; empty when ok


def _check_claude() -> CheckResult:
    """claude CLI on PATH + logged in via `claude auth status --json`."""
    if shutil.which("claude") is None:
        return CheckResult(
            name="claude CLI",
            ok=False,
            detail="not on PATH",
            fix="install Claude Code: https://docs.anthropic.com/claude/docs/claude-code",
        )
    status, detail = _probe_claude_code(check_auth=True)
    if status == "ok":
        return CheckResult("claude CLI", True, "installed and logged in", "")
    return CheckResult(
        name="claude CLI",
        ok=False,
        detail=detail,
        fix="claude auth login",
    )


def _check_chrome() -> CheckResult:
    """Real Google Chrome.app present (macOS) — required by slip + sign-in."""
    if chrome_installed():
        return CheckResult(
            "Google Chrome",
            True,
            f"installed at {CHROME_PATH}",
            "",
        )
    return CheckResult(
        name="Google Chrome",
        ok=False,
        detail="not installed",
        fix=f"brew install --cask google-chrome  (or download from {CHROME_INSTALL_URL})",
    )


def _check_chromium() -> CheckResult:
    """Playwright Chromium runtime — required by dial/deploy headless flow."""
    if chromium_installed():
        return CheckResult(
            "Playwright Chromium",
            True,
            "installed",
            "",
        )
    import sys
    return CheckResult(
        name="Playwright Chromium",
        ok=False,
        detail="not installed",
        fix=f"{sys.executable} -m playwright install chromium",
    )


def _check_git() -> CheckResult:
    """git on PATH — required by claude_cli's worktree + repo ops."""
    if shutil.which("git") is None:
        return CheckResult(
            name="git",
            ok=False,
            detail="not on PATH",
            fix="install git from https://git-scm.com/  (or `xcode-select --install`)",
        )
    try:
        r = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, timeout=2
        )
        version = r.stdout.strip() or "(unknown version)"
    except (subprocess.TimeoutExpired, OSError):
        version = "(installed)"
    return CheckResult("git", True, version, "")


def _check_auth_state() -> CheckResult:
    """`~/.operator/auth_state.json` — required only for dial/deploy.

    Slip launches its own dedicated Chrome under `~/.operator/slip_profile/`
    and never reads auth_state.json. Doctor reports this absence as a
    warning (yellow) rather than a hard fail so a slip-only user isn't
    pushed to set up dial sign-in they don't need.
    """
    if _AUTH_STATE_FILE.exists():
        return CheckResult(
            name="Google sign-in (dial/deploy)",
            ok=True,
            detail=f"auth_state.json present at {_AUTH_STATE_FILE}",
            fix="",
        )
    return CheckResult(
        name="Google sign-in (dial/deploy)",
        ok=False,
        detail="not signed in — only needed for dial/deploy (slip is independent)",
        fix="operator login claude",
    )


def run_doctor() -> int:
    """Run every check, print a checklist, return shell exit code.

    Exit codes:
      0 — every check passed
      1 — at least one check failed

    Output format mirrors `brew doctor` / `flutter doctor`: one line
    per check, ✓ or ✗ glyph, name + detail, indented fix on failures.
    """
    checks = [
        _check_claude(),
        _check_chrome(),
        _check_chromium(),
        _check_git(),
        _check_auth_state(),
    ]

    print()
    print("operator doctor")
    print("───────────────")
    failures = 0
    for c in checks:
        glyph = "✓" if c.ok else "✗"
        print(f"  {glyph} {c.name}: {c.detail}")
        if not c.ok:
            failures += 1
            print(f"      fix: {c.fix}")
    print()

    if failures == 0:
        print("All checks passed. You're ready to dial/deploy/slip.")
        return 0
    word = "issue" if failures == 1 else "issues"
    print(f"{failures} {word} above. Fix and re-run `operator doctor`.")
    return 1
