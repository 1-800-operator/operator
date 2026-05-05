"""
CDP-attach connector for `operator slip` mode (Phase 14.19.3).

Where dial/deploy launch a fresh persistent-context Chrome under a separate
user data dir (operator's own profile), slip attaches to the user's
*existing* Chrome — same profile, same tabs, same logged-in identity. The
user's typing, claude's typing, and meeting participants all coexist in
one Meet tab; claude's replies are prefixed with a marker so the room can
tell what's the user and what's claude.

Lifecycle:
    1. Detect Chrome running (pgrep)
    2. Prompt the user to confirm closing Chrome (interactive yes/no)
    3. Graceful quit via AppleScript (5s timeout) → pkill fallback
    4. Wait for the process to actually exit
    5. Relaunch Chrome with `--remote-debugging-port=9222` against the
       user's actual profile dir, opening the Meet URL in a new tab
    6. `playwright.chromium.connect_over_cdp("http://localhost:9222")`
    7. Locate the Meet tab among open pages
    8. (Future commits) install chat observer / send / read / participants
    9. On leave(): disconnect from CDP but do NOT quit Chrome — the user's
       browser remains running with all their tabs intact.

Phase 14.19.3a scope (this file's first cut): lifecycle through step 7.
Chat observer + send/read/participants land in 14.19.3b. _run_slip wiring
in __main__.py lands in 14.19.3c.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from .base import MeetingConnector


log = logging.getLogger(__name__)

CDP_PORT = 9222
CDP_URL = f"http://localhost:{CDP_PORT}"
CHROME_BINARY_MACOS = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_USER_DATA_DIR_MACOS = os.path.expanduser("~/Library/Application Support/Google/Chrome")
GRACEFUL_QUIT_TIMEOUT_SECONDS = 5
PKILL_FALLBACK_TIMEOUT_SECONDS = 5
RELAUNCH_READY_TIMEOUT_SECONDS = 15
MEET_TAB_FIND_TIMEOUT_SECONDS = 10


class SlipAttachError(RuntimeError):
    """Raised when the slip-mode attach lifecycle fails fatally.

    Caught by _run_slip and presented to the user as a clean stderr
    message with a fix hint, not a stack trace.
    """


def _is_meet_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return "meet.google.com" in (parsed.netloc or "")


def _chrome_is_running() -> bool:
    """True iff a Google Chrome process is currently alive on this Mac.

    Uses `pgrep -x "Google Chrome"` to match only the main browser process,
    not Chrome Helpers / renderers / GPU process / Login items.
    """
    try:
        r = subprocess.run(
            ["pgrep", "-x", "Google Chrome"],
            capture_output=True, text=True, timeout=2,
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception as e:
        log.warning(f"AttachAdapter: pgrep Chrome failed: {e}")
        return False


def _chrome_quit_graceful() -> bool:
    """Ask Chrome to quit via AppleScript. Returns True if Chrome exits
    within GRACEFUL_QUIT_TIMEOUT_SECONDS, False otherwise.

    AppleScript `quit` triggers Chrome's normal exit path: tab state is
    persisted, no "restore tabs?" prompt on next launch, no LevelDB
    corruption. Falls back to pkill on timeout.
    """
    try:
        subprocess.run(
            ["osascript", "-e", 'tell application "Google Chrome" to quit'],
            capture_output=True, timeout=2,
        )
    except Exception as e:
        log.warning(f"AttachAdapter: osascript quit failed: {e}")
        return False

    deadline = time.monotonic() + GRACEFUL_QUIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not _chrome_is_running():
            return True
        time.sleep(0.2)
    return False


def _chrome_kill_force() -> bool:
    """SIGKILL Chrome via pkill. Used only when graceful quit times out.

    May leave Chrome in 'crashed' state — next launch will show
    'Restore tabs?'. Acceptable as a fallback because the alternative is
    a hung slip command.
    """
    try:
        subprocess.run(
            ["pkill", "-x", "-9", "Google Chrome"],
            capture_output=True, timeout=2,
        )
    except Exception as e:
        log.warning(f"AttachAdapter: pkill Chrome failed: {e}")
        return False

    deadline = time.monotonic() + PKILL_FALLBACK_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not _chrome_is_running():
            return True
        time.sleep(0.2)
    return False


def _confirm_chrome_quit_with_user() -> bool:
    """Interactive prompt asking the user to consent to Chrome quit.

    Returns True if the user typed y/yes (case-insensitive). Anything
    else (n/no/empty/Ctrl+D) returns False so the caller can exit
    cleanly without touching Chrome. The prompt is explicit about
    impact: all Chrome windows will close.
    """
    sys.stderr.write(
        "\nOperator needs to relaunch Chrome with debugging enabled so claude can\n"
        "slip into your meeting. This will close all open Chrome windows; tabs\n"
        "will be restored when Chrome reopens.\n\n"
        "Continue? [y/N] "
    )
    sys.stderr.flush()
    try:
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        sys.stderr.write("\n")
        return False
    return answer in ("y", "yes")


def _launch_chrome_with_debug_port(meeting_url: str) -> subprocess.Popen:
    """Spawn Chrome with the remote debugging port enabled, the user's
    real profile, and the meeting URL as the initial tab.

    Returns the Popen handle. Caller is responsible for waiting until
    the CDP endpoint is reachable (via _wait_for_cdp_ready).
    """
    if not os.path.exists(CHROME_BINARY_MACOS):
        raise SlipAttachError(
            f"Could not find Google Chrome at {CHROME_BINARY_MACOS!r}. "
            "Install Chrome from https://www.google.com/chrome/ and re-run."
        )
    args = [
        CHROME_BINARY_MACOS,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={CHROME_USER_DATA_DIR_MACOS}",
        meeting_url,
    ]
    log.info(f"AttachAdapter: launching Chrome with CDP port {CDP_PORT}")
    return subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _wait_for_cdp_ready(timeout_seconds: int = RELAUNCH_READY_TIMEOUT_SECONDS) -> None:
    """Block until the CDP endpoint accepts a TCP connection.

    Chrome publishes the debugging port a fraction of a second after
    process launch — polling beats sleeping a fixed duration. Raises
    SlipAttachError on timeout.
    """
    import socket
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("localhost", CDP_PORT), timeout=0.5):
                return
        except (ConnectionRefusedError, socket.timeout, OSError):
            time.sleep(0.1)
    raise SlipAttachError(
        f"Chrome CDP endpoint at {CDP_URL} did not become ready within "
        f"{timeout_seconds}s. Chrome may have failed to launch — check "
        "that no other process is using port 9222 and try again."
    )


class AttachAdapter(MeetingConnector):
    """MeetingConnector for slip mode — CDP-attached to user's Chrome.

    Chat methods (send/read/participants) raise NotImplementedError until
    Phase 14.19.3b lands the chat-observer wiring. is_connected and
    leave are functional from this commit onward.
    """

    def __init__(self, reply_prefix: str = ""):
        super().__init__()
        self._reply_prefix = reply_prefix
        self._playwright = None
        self._browser = None
        self._page = None
        self._chrome_proc = None

    # ------------------------------------------------------------------
    # MeetingConnector interface
    # ------------------------------------------------------------------

    def join(self, meeting_url):
        if sys.platform != "darwin":
            raise SlipAttachError(
                "slip mode is currently macOS-only. Linux support is "
                "tracked for a follow-up phase. Use `operator dial claude` "
                "or `operator deploy claude <url>` on Linux."
            )
        if not meeting_url:
            raise SlipAttachError(
                "slip mode requires a meeting URL. Run "
                "`operator slip claude <https://meet.google.com/xxx-xxxx-xxx>`."
            )
        if not _is_meet_url(meeting_url):
            raise SlipAttachError(
                f"slip mode requires a Google Meet URL; got {meeting_url!r}."
            )

        if _chrome_is_running():
            if not _confirm_chrome_quit_with_user():
                raise SlipAttachError(
                    "slip mode requires closing Chrome. Aborted at user request."
                )
            log.info("AttachAdapter: quitting Chrome (graceful)")
            if not _chrome_quit_graceful():
                log.warning("AttachAdapter: graceful quit timed out — falling back to pkill")
                if not _chrome_kill_force():
                    raise SlipAttachError(
                        "Could not close Chrome — it is still running after both "
                        "graceful quit and force kill. Quit Chrome manually and re-run."
                    )

        self._chrome_proc = _launch_chrome_with_debug_port(meeting_url)
        _wait_for_cdp_ready()

        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            self._teardown_playwright()
            raise SlipAttachError(
                f"Failed to attach to Chrome via CDP at {CDP_URL}: {e}. "
                "Chrome may have launched without the debugging port — try again."
            )

        self._page = self._find_meet_page(meeting_url)
        if self._page is None:
            self._teardown_playwright()
            raise SlipAttachError(
                f"Could not find a Meet tab pointing at {meeting_url!r} in the "
                "relaunched Chrome. The tab may have failed to load — open it "
                "manually and re-run, or pass --force to retry."
            )
        log.info(f"AttachAdapter: attached to Meet tab at {self._page.url}")

    def send_chat(self, message):
        raise NotImplementedError(
            "Phase 14.19.3b will wire the chat observer + send/read."
        )

    def read_chat(self):
        raise NotImplementedError(
            "Phase 14.19.3b will wire the chat observer + send/read."
        )

    def get_participant_count(self):
        raise NotImplementedError(
            "Phase 14.19.3b will wire participant scraping."
        )

    def is_connected(self):
        if self._browser is None:
            return False
        try:
            return self._browser.is_connected()
        except Exception:
            return False

    def leave(self):
        """Disconnect from CDP. Does NOT quit Chrome — the user's browser
        keeps running with all its tabs (including the Meet tab claude
        was attached to). Idempotent.
        """
        self._teardown_playwright()
        log.info("AttachAdapter: detached from Chrome (Chrome left running)")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _find_meet_page(self, meeting_url):
        """Locate the Meet tab among the relaunched Chrome's open pages.

        Polls every 250ms up to MEET_TAB_FIND_TIMEOUT_SECONDS — the tab
        we asked Chrome to open isn't always immediately discoverable
        via CDP (Chrome's startup race). Match by hostname rather than
        exact URL because Meet appends query strings post-redirect.
        """
        target_host = urlparse(meeting_url).netloc
        deadline = time.monotonic() + MEET_TAB_FIND_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            for context in self._browser.contexts:
                for page in context.pages:
                    try:
                        host = urlparse(page.url).netloc
                    except Exception:
                        continue
                    if host == target_host:
                        return page
            time.sleep(0.25)
        return None

    def _teardown_playwright(self):
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception as e:
                log.debug(f"AttachAdapter: browser.close raised: {e}")
            self._browser = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception as e:
                log.debug(f"AttachAdapter: playwright.stop raised: {e}")
            self._playwright = None
        self._page = None
