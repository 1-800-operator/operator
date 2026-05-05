"""Install-time preflight that runs at the top of `operator setup`.

Bridges the two install paths:

- **`curl | sh`** runs `install.sh` which provisions Playwright Chromium,
  seeds `~/.operator/.env`, and prints the Chrome.app nudge before the
  user ever invokes `operator`. By the time they hit `operator setup`,
  every check below is already a no-op.
- **`uv tool install git+...`** (no curl) skips the shell script entirely.
  The user lands at `operator setup` with the Python package installed
  but Chromium missing, no `.env`, and no Chrome.app warning. Without
  this preflight, the wizard would crash 30 steps later inside Playwright
  with an opaque error, and the user would have to reverse-engineer
  install.sh to get going.

This module is the bridge: detect each missing dependency, do the silent
ones automatically (`.env` seed — placeholders are harmless), prompt for
the slow one (Chromium download is ~170 MB, deserves consent), and
print the existing Chrome.app warning. Idempotent on re-run.

Single entry point: `run_install_preflight()`.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from _1_800_operator.pipeline.chrome_preflight import (
    chrome_installed,
    INSTALL_URL as CHROME_INSTALL_URL,
)


_ENV_FILE = Path.home() / ".operator" / ".env"

_ENV_TEMPLATE = """\
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
"""


def _playwright_browsers_root() -> Path:
    """Return the directory Playwright stores browser builds under.

    Honors `PLAYWRIGHT_BROWSERS_PATH` if set (Playwright's escape hatch);
    otherwise falls back to the platform default. Used by `chromium_installed`.
    """
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def chromium_installed() -> bool:
    """True iff Playwright has at least one `chromium-*` build cached.

    We don't care about the revision — Playwright's runtime will pick the
    one matching its installed version. We're answering the binary
    question: has `playwright install chromium` been run successfully on
    this machine? If not, our connectors will fail deep inside Playwright
    with `BrowserType.launch: Executable doesn't exist at ...`.
    """
    root = _playwright_browsers_root()
    if not root.exists():
        return False
    return any(child.name.startswith("chromium-") for child in root.iterdir())


def seed_env_file() -> bool:
    """Create `~/.operator/.env` with commented placeholders if missing.

    Returns True iff the file was newly created. Mode 600 so other users
    on a shared box can't read the API keys the user is about to paste in.
    Does nothing if the file already exists — never overwrites real keys.
    """
    if _ENV_FILE.exists():
        return False
    _ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    _ENV_FILE.write_text(_ENV_TEMPLATE)
    _ENV_FILE.chmod(0o600)
    return True


def _confirm(prompt: str) -> bool:
    """Yes/no prompt that defaults to yes on plain Enter.

    Returns False on EOF (non-interactive shell — better to skip than to
    hang waiting for input that will never come).
    """
    try:
        answer = input(f"  {prompt} [Y/n] ").strip().lower()
    except EOFError:
        return False
    return answer in ("", "y", "yes")


def install_chromium() -> int:
    """Run `playwright install chromium` and stream its output.

    We invoke `python -m playwright` rather than the bare `playwright`
    binary so the call hits the Playwright bundled with *this* Python
    install, regardless of PATH ordering. Returns the subprocess exit code.
    """
    print()
    print("  Downloading Playwright Chromium runtime (~170 MB)…")
    print()
    return subprocess.call(
        [sys.executable, "-m", "playwright", "install", "chromium"]
    )


def run_install_preflight() -> None:
    """Top-of-`operator setup` preflight.

    Three checks, each independent, all idempotent:

      1. Seed `~/.operator/.env` if missing.
      2. Offer to install Playwright Chromium if missing.
      3. Print the Chrome.app warning on macOS if Chrome is missing —
         non-blocking, since the user can install Chrome between
         `operator setup` and their first `operator dial`.

    Silent when everything is already in place; that's the curl-installer
    happy path. Prints rich-flavored output via plain `print` so it works
    before the wizard's Console is initialized.
    """
    seeded = seed_env_file()
    if seeded:
        print(f"  ✓ Seeded {_ENV_FILE} with API-key placeholders (mode 600).")

    if not chromium_installed():
        print()
        print("  Playwright Chromium is not installed on this machine.")
        print("  Operator drives Chrome/Chromium for the meeting connector;")
        print("  without it, the first `operator dial` will fail.")
        if _confirm("Install it now?"):
            rc = install_chromium()
            if rc != 0:
                print()
                print(
                    f"  ✗ playwright install chromium exited {rc}. "
                    "You can re-run it manually:"
                )
                print(f"    {sys.executable} -m playwright install chromium")
                print()
        else:
            print()
            print("  Skipped. Run before your first meeting:")
            print(f"    {sys.executable} -m playwright install chromium")
            print()

    if sys.platform == "darwin" and not chrome_installed():
        print()
        print("  ⚠ Google Chrome not found in /Applications.")
        print(
            "  Operator drives a real Chrome (not bundled Chromium) for "
            "Google Meet sign-in."
        )
        print("  Install it before your first meeting:")
        print("    brew install --cask google-chrome")
        print(f"    (or download from {CHROME_INSTALL_URL})")
        print()
