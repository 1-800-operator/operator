"""Google sign-in flow — backs `operator login claude`.

The bot needs a logged-in Chrome profile to join Meet. Detect an existing
session and offer continue / re-auth, or run the first-time sign-in flow.

Two artifacts are written on a successful sign-in:

  ~/.operator/auth_state.json   — Playwright storageState; seeds the
                                    Linux/Docker recovery path (see
                                    linux_adapter._auth_state_file).
  ~/.operator/google_account.json
                                  — small {"email": "..."} cache so future
                                    runs can show "✓ signed in as X"
                                    without re-scraping a Google page.

The persistent profile at ~/.operator/browser_profile/ is the source of
truth for the runtime — auth_state.json is only the recovery seed.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from _1_800_operator import config
from _1_800_operator.pipeline.chrome_preflight import CHROME_PATH

log = logging.getLogger(__name__)
console = Console()


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_SIGNIN_POLL_INTERVAL_S = 1.0
_SIGNIN_TIMEOUT_S = 300  # 5 min


@dataclass(frozen=True)
class DetectResult:
    detected: bool
    email: str | None  # None if detected but email cache absent (legacy profile)


def _auth_state_has_sid(path: Path) -> bool:
    """Inline of session.validate_auth_state — duplicated to avoid pulling
    in the connectors stack (Playwright, Chrome lifecycle) just to check a
    cookie file."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    return any(
        c.get("name") == "SID" and ".google.com" in c.get("domain", "")
        for c in state.get("cookies", [])
    )


def _read_account_email(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        email = data.get("email")
        return email if isinstance(email, str) and "@" in email else None
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def detect_google_session(
    auth_state_path: Path = Path(config.AUTH_STATE_FILE),
    account_file: Path = Path(config.GOOGLE_ACCOUNT_FILE),
) -> DetectResult:
    """Pure helper: does ~/.operator/auth_state.json carry a valid SID cookie?

    Returns DetectResult(detected=True, email=...) when the file exists and
    contains a .google.com SID. Email comes from the sibling cache file when
    present; None when the cache is missing (legacy profile, pre-14.10).
    """
    if not _auth_state_has_sid(auth_state_path):
        return DetectResult(False, None)
    return DetectResult(True, _read_account_email(account_file))


def _capture_email(page) -> str | None:
    """Pull the signed-in email from a live Google page in the same context.

    Navigates to myaccount.google.com (cheap when SID is already set — the
    page renders the account chip with the email in an aria-label) and
    pattern-matches an email out of the rendered DOM. Returns None on
    failure; non-fatal, the wizard just won't have an email to show next
    time.
    """
    try:
        page.goto("https://myaccount.google.com/", wait_until="domcontentloaded", timeout=15000)
    except Exception as e:
        log.warning(f"google_signin: myaccount nav failed during email capture: {e}")
        return None

    selectors = (
        'a[aria-label*="@"]',
        'div[aria-label*="@"]',
        '[data-email]',
    )
    for sel in selectors:
        try:
            handle = page.query_selector(sel)
            if not handle:
                continue
            for attr in ("aria-label", "data-email", "title"):
                val = handle.get_attribute(attr)
                if val:
                    m = _EMAIL_RE.search(val)
                    if m:
                        return m.group(0)
        except Exception:
            continue

    # Fallback: scan rendered text. Slower but resilient to selector drift.
    try:
        text = page.inner_text("body", timeout=5000)
        m = _EMAIL_RE.search(text)
        if m:
            return m.group(0)
    except Exception:
        pass
    return None


def _write_artifacts(context, page, account_file: Path, auth_state_path: Path) -> str | None:
    """After SID cookie is detected: capture email, persist both artifacts."""
    email = _capture_email(page)
    auth_state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        context.storage_state(path=str(auth_state_path))
        # auth_state.json carries .google.com session cookies (SID,
        # __Secure-1PSID, …). Lock it down so it isn't world-readable —
        # belt over umask 0o077 so existing-file overwrites stay tight.
        os.chmod(auth_state_path, 0o600)
    except Exception as e:
        log.warning(f"google_signin: storage_state write failed: {e}")
    if email:
        try:
            account_file.write_text(json.dumps({"email": email}), encoding="utf-8")
            os.chmod(account_file, 0o600)
        except OSError as e:
            log.warning(f"google_signin: account file write failed: {e}")
    return email


def _has_google_sid(context) -> bool:
    try:
        for c in context.cookies():
            if c.get("name") == "SID" and ".google.com" in c.get("domain", ""):
                return True
    except Exception:
        pass
    return False


def _launch_signin_flow(
    profile_dir: Path = Path(config.BROWSER_PROFILE_DIR),
    auth_state_path: Path = Path(config.AUTH_STATE_FILE),
    account_file: Path = Path(config.GOOGLE_ACCOUNT_FILE),
    *,
    sign_out_first: bool = False,
) -> str | None:
    """Open visible Chrome, wait for the user to sign in, persist artifacts.

    Returns the captured email (or None if capture failed but sign-in
    succeeded). Raises on Playwright failure or user-driven timeout.

    sign_out_first=True navigates through Google's logout endpoint first,
    used by the re-auth path so the user lands on the account picker
    instead of being auto-recognized into the existing account.
    """
    from playwright.sync_api import sync_playwright

    from _1_800_operator.pipeline.chrome_preflight import require_chrome_or_exit
    require_chrome_or_exit()

    # NB (session 178, T1.11): this flow uses real Google Chrome explicitly.
    # Runtime adapter (`macos_adapter.py:_browser_session`) currently
    # launches Playwright's bundled Chromium-for-Testing against the SAME
    # `~/.operator/browser_profile/` dir without `executable_path`. The two
    # binaries share most profile format, so Google Meet's session cookies
    # (SAPISID, __Secure-1PSID, etc. — not keychain-encrypted) round-trip
    # fine today. Risk for the future: if Google ever moves the auth
    # cookies into the keychain-encrypted slot (Chrome's "v10"/"v11"
    # scheme), or if Chrome's monthly update bumps a profile-DB schema
    # Chromium-for-Testing can't yet read, sign-in will silently fail at
    # `operator dial` time with no clear error. Fix when reproducible:
    # pass executable_path=str(CHROME_PATH) in the adapter so both ends
    # use the same binary. Deferred pre-launch; no observed failures.

    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            executable_path=str(CHROME_PATH),
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            start_url = (
                "https://accounts.google.com/Logout"
                if sign_out_first
                else "https://accounts.google.com/"
            )
            page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
            if sign_out_first:
                # After logout, Google redirects to a confirmation page;
                # walk the user to the signin page explicitly.
                try:
                    page.goto(
                        "https://accounts.google.com/ServiceLogin",
                        wait_until="domcontentloaded",
                        timeout=15000,
                    )
                except Exception:
                    pass

            console.print(
                "  [dim]A Chrome window has opened. Sign in with the Google account you "
                "want this bot to use.[/dim]"
            )
            console.print("  [dim]Waiting for sign-in… (Ctrl+C to abort)[/dim]")

            deadline = time.monotonic() + _SIGNIN_TIMEOUT_S
            while time.monotonic() < deadline:
                if _has_google_sid(context):
                    break
                time.sleep(_SIGNIN_POLL_INTERVAL_S)
            else:
                raise TimeoutError(
                    f"sign-in did not complete within {_SIGNIN_TIMEOUT_S}s"
                )

            email = _write_artifacts(context, page, account_file, auth_state_path)
            return email
        finally:
            try:
                context.close()
            except Exception:
                pass


