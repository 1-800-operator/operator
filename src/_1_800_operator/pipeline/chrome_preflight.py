"""Pre-launch check for Google Chrome on macOS.

Both the wizard sign-in step (`pipeline/google_signin.py`) and the macOS
adapter (`connectors/macos_adapter.py`) hard-code the system Chrome binary
because Chrome profiles aren't compatible across binaries (Chrome-for-
Testing vs real Chrome — session 159 hard-won knowledge). If the user
doesn't have Chrome installed, both paths fail deep inside Playwright with
an opaque error. This module surfaces that as a single human line at the
top of any command that would otherwise hit it.

Linux uses bundled Chromium via `connectors/linux_adapter.py`, so the check
is a no-op there. The terminal `try` connector doesn't touch a browser at
all, so it skips the check too.
"""
from __future__ import annotations

import sys
from pathlib import Path

CHROME_PATH = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
INSTALL_URL = "https://www.google.com/chrome/"


def chrome_installed() -> bool:
    """True on non-darwin (no system-Chrome dependency) or when the binary exists."""
    if sys.platform != "darwin":
        return True
    return CHROME_PATH.exists()


def require_chrome_or_exit() -> None:
    """Print one line + install URL and exit 2 if Chrome is missing on macOS."""
    if chrome_installed():
        return
    print("Google Chrome is required but not installed.", file=sys.stderr)
    print(f"Install it from {INSTALL_URL} and re-run.", file=sys.stderr)
    sys.exit(2)


def require_signed_in_or_exit() -> None:
    """Exit 2 with a clear hint if the persistent Chrome profile is missing.

    The profile at `~/.operator/browser_profile/` is created and populated
    during the wizard's Google sign-in step. Without it, the headless
    Chromium at dial time has no Google session and `meet.new` redirects
    to a sign-in page that the bot can't fill in — the dial then hangs
    until Playwright's 30s navigation timeout. Catching this up front
    saves the user 30 seconds and a misleading "did not redirect" error.
    """
    from _1_800_operator import config
    profile = Path(config.BROWSER_PROFILE_DIR)
    if profile.is_dir():
        return
    print(
        "Google sign-in not done — `~/.operator/browser_profile/` is missing.",
        file=sys.stderr,
    )
    print(
        "Run `operator setup` and complete the sign-in step, then retry.",
        file=sys.stderr,
    )
    sys.exit(2)
