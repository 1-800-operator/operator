"""
CDP-attach connector for `operator slip` mode.

Slip launches a SEPARATE Chrome window under operator's own profile dir
(~/.operator/slip_profile/), opens the meeting URL there, and CDP-attaches
to it. The user's main Chrome is NEVER touched — original tabs / browsing
session / current meetings stay intact.

The original design (attach to user's existing Chrome) is not technically
viable on modern Chrome: starting around Chrome 121, Chromium silently
disables `--remote-debugging-port` when the `--user-data-dir` matches
the user's logged-in default profile. The flag is accepted into argv but
the TCP listener is never created. This is a security mitigation against
malware harvesting OAuth tokens via DevTools (Chromium issue 40066423,
unbypassable by design). Using a fresh profile dir sidesteps the
restriction entirely.

The user-perceived UX:
    - Slip Chrome is a dedicated meeting window — different from main
      browser. User signs into Google in this profile once (operator's
      own first-run flow); cookies persist across slip sessions.
    - Meeting joins as the user (same Google identity), so the room
      sees one participant entry "User Name". claude posts chat with
      a marker prefix so user vs. claude is distinguishable.
    - User must run slip BEFORE joining the meeting in main Chrome —
      otherwise the same identity is in the meeting twice. JIT
      preflights / friendly notices handle this.

Lifecycle:
    1. Probe CDP — if a prior slip session left slip Chrome running,
       skip launch and reuse it
    2. Otherwise launch Chrome with --user-data-dir=SLIP_PROFILE_DIR,
       --remote-debugging-port=9222, and the meeting URL via `open -na`
    3. Wait for CDP endpoint
    4. `playwright.chromium.connect_over_cdp("http://localhost:9222")`
    5. Find or open the Meet tab (strict room-code match)
    6. Wait for the user to click 'Join now' (indefinite poll)
    7. Hand back to ChatRunner
    8. On leave(): disconnect CDP only — slip Chrome stays running so
       the user can keep the meeting going after claude detaches.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from _1_800_operator import config

from .base import MeetingConnector
from .chat_dom_js import (
    DRAIN_CHAT_QUEUE_JS,
    GET_PARTICIPANT_NAMES_JS,
    INSTALL_CHAT_OBSERVER_JS,
    OBSERVER_ATTACHED_CHECK_JS,
    SNAPSHOT_MESSAGE_IDS_JS,
)
from .session import JoinStatus, save_debug
# Reuse the strict Meet-room URL check from macos_adapter (matches the
# `abc-defg-hij` room-code pattern, rejects /landing, /lookup, /new, etc).
# Cross-adapter import of a private helper is a temporary smell — if a
# third adapter ever needs this, promote _is_real_meet_room to session.py.
from .macos_adapter import _is_real_meet_room


log = logging.getLogger(__name__)

CDP_PORT = 9222
CDP_URL = f"http://localhost:{CDP_PORT}"
CHROME_BINARY_MACOS = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
# Operator-owned slip profile — never touches the user's main Chrome.
# Stays signed in across slip sessions (cookies / Google session
# persist on disk like any Chrome profile dir). First-run sign-in is
# handled by _run_slip in __main__.py.
SLIP_PROFILE_DIR = os.path.expanduser("~/.operator/slip_profile")
# Chrome can take 20+s to bring up the debug server on a profile with
# extensions or syncing data. 30s is generous; failure beyond that
# points at a real problem (port collision, Chrome crash, OS issue).
CDP_READY_TIMEOUT_SECONDS = 30


class SlipAttachError(RuntimeError):
    """Raised when the slip-mode attach lifecycle fails fatally.

    Caught by _run_slip and presented to the user as a clean stderr
    message with a fix hint, not a stack trace.
    """


def _cdp_endpoint_alive(timeout: float = 1.0) -> bool:
    """Check if CDP debug endpoint is already accepting connections.

    Used to short-circuit the Chrome quit/relaunch dance when Chrome was
    already started with --remote-debugging-port=9222 — typically because
    a prior slip session left it that way. Re-running slip should not
    require re-quitting Chrome.

    A successful TCP accept on localhost:9222 means *some* Chrome process
    is exposing CDP; we still verify connect_over_cdp works downstream
    before committing to the attach path.
    """
    import socket
    try:
        with socket.create_connection(("localhost", CDP_PORT), timeout=timeout):
            return True
    except (ConnectionRefusedError, socket.timeout, OSError):
        return False


def _launch_slip_chrome(meeting_url: str) -> subprocess.Popen:
    """Spawn slip's dedicated Chrome window with debug port + meeting URL.

    Uses `open -na 'Google Chrome' --args ...` (macOS-canonical pattern;
    `-n` forces a new instance, `--args` propagates flags reliably).
    The user-data-dir is operator-owned (SLIP_PROFILE_DIR), separate
    from the user's main Chrome profile — sidesteps Chrome's silent
    debug-port disable for the default profile.

    Returns the Popen handle of the `open` command itself, which exits
    after dispatching. The actual Chrome process is owned by
    LaunchServices.

    First-run behavior: if SLIP_PROFILE_DIR doesn't exist yet, Chrome
    creates it on launch. The user lands on the meeting URL, will see
    Google's sign-in prompt (slip profile has no cookies yet), can
    sign in once, and the profile persists for future runs.
    """
    if not os.path.exists(CHROME_BINARY_MACOS):
        raise SlipAttachError(
            f"Could not find Google Chrome at {CHROME_BINARY_MACOS!r}. "
            "Install Chrome from https://www.google.com/chrome/ and re-run."
        )
    os.makedirs(SLIP_PROFILE_DIR, exist_ok=True, mode=0o700)
    args = [
        "open", "-na", "Google Chrome", "--args",
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={SLIP_PROFILE_DIR}",
        meeting_url,
    ]
    log.info(f"AttachAdapter: launching slip Chrome via: {' '.join(args)}")
    return subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _wait_for_cdp_ready(timeout_seconds: int = CDP_READY_TIMEOUT_SECONDS) -> None:
    """Block until the CDP endpoint accepts a TCP connection.

    Chrome publishes the debugging port shortly after process launch.
    Polling at 100ms beats a fixed sleep. Raises SlipAttachError on
    timeout — by which point Chrome has either crashed or is bound up
    on something we can't disambiguate from here.
    """
    import socket
    log.info(f"AttachAdapter: waiting for CDP endpoint at {CDP_URL} (timeout {timeout_seconds}s)")
    t_start = time.monotonic()
    deadline = t_start + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("localhost", CDP_PORT), timeout=0.5):
                log.info(f"AttachAdapter: CDP ready after {time.monotonic() - t_start:.1f}s")
                return
        except (ConnectionRefusedError, socket.timeout, OSError):
            time.sleep(0.1)
    log.warning(f"AttachAdapter: CDP timeout after {timeout_seconds}s")
    raise SlipAttachError(
        f"Slip Chrome's CDP endpoint at {CDP_URL} did not become ready "
        f"within {timeout_seconds}s. Chrome may have crashed during launch — "
        "try `pkill -f operator-slip-chrome` then re-run. If the problem "
        "persists, delete ~/.operator/slip_profile/ to start with a fresh "
        "profile."
    )


class AttachAdapter(MeetingConnector):
    """MeetingConnector for slip mode — CDP-attached to slip's dedicated
    Chrome window.

    Each slip session launches Chrome with --user-data-dir=SLIP_PROFILE_DIR
    and CDP-attaches via Playwright. The user's main Chrome is never
    touched. Re-running slip while a slip Chrome is still alive (the
    user just hit Ctrl+C and is firing it back up) reuses the existing
    Chrome via the CDP probe — no second window, no relaunch.

    First-run: SLIP_PROFILE_DIR is created on launch; Chrome lands the
    user on the meeting URL with no Google session. They sign in once,
    cookies persist, and subsequent slip runs skip the sign-in.
    """

    def __init__(self, reply_prefix: str = ""):
        super().__init__()
        self._reply_prefix = reply_prefix
        self._playwright = None
        self._browser = None
        self._page = None
        self._chrome_proc = None
        self._observer_installed = False

    # ------------------------------------------------------------------
    # MeetingConnector interface
    # ------------------------------------------------------------------

    def join(self, meeting_url):
        # Populate join_status — ChatRunner inspects connector.join_status
        # to decide whether the join succeeded (matches macos_adapter
        # contract). Both signal_success() and signal_failure() set
        # the underlying threading.Event, so callers blocking on it
        # never deadlock no matter which branch we take.
        self.join_status = JoinStatus()
        js = self.join_status

        if sys.platform != "darwin":
            js.signal_failure("linux_unsupported")
            raise SlipAttachError(
                "slip mode is currently macOS-only. Linux support is "
                "tracked for a follow-up phase. Use `operator dial claude` "
                "or `operator deploy claude <url>` on Linux."
            )
        if not meeting_url:
            js.signal_failure("missing_url")
            raise SlipAttachError(
                "slip mode requires a meeting URL. Run "
                "`operator slip claude <https://meet.google.com/xxx-xxxx-xxx>`."
            )
        if not _is_real_meet_room(meeting_url):
            js.signal_failure("not_meet_room_url")
            raise SlipAttachError(
                f"slip mode requires a Google Meet room URL like "
                f"`https://meet.google.com/abc-defg-hij`; got {meeting_url!r}."
            )

        # Probe CDP first — if a prior slip session left slip Chrome
        # running with the debug port open, skip launch entirely.
        # Re-running slip after Ctrl+C should be instant.
        if _cdp_endpoint_alive():
            log.info(f"AttachAdapter: CDP already alive at {CDP_URL} — reusing existing slip Chrome")
        else:
            self._chrome_proc = _launch_slip_chrome(meeting_url)
            try:
                _wait_for_cdp_ready()
            except SlipAttachError:
                js.signal_failure("cdp_not_ready")
                raise

        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            self._teardown_playwright()
            js.signal_failure("cdp_attach_failed")
            raise SlipAttachError(
                f"Failed to attach to Chrome via CDP at {CDP_URL}: {e}. "
                "Chrome may have launched without the debugging port — try again."
            )

        self._page = self._find_or_open_meet_page(meeting_url)
        if self._page is None:
            self._teardown_playwright()
            js.signal_failure("meet_tab_open_failed")
            raise SlipAttachError(
                f"Could not find or open a Meet tab for {meeting_url!r}. "
                "Open it manually in Chrome and re-run."
            )
        log.info(f"AttachAdapter: attached to Meet tab at {self._page.url}")

        # The user's Chrome opened the meeting URL but they may still be in
        # the green room (pre-join screen) or stuck in the host-admission
        # lobby. Block until they've actually entered the meeting —
        # otherwise ChatRunner's first read_chat poll fires against an
        # empty DOM and slip appears hung. The wait is indefinite; lobby
        # admission can take many minutes, the user can Ctrl+C anytime,
        # and the only fatal signal is Chrome being closed (which the
        # wait loop detects via is_connected).
        if not self._wait_for_meeting_entry(self._page):
            self._teardown_playwright()
            js.signal_failure("chrome_closed_before_entry")
            raise SlipAttachError(
                "Chrome was closed before you joined the meeting. Re-run "
                "`operator slip claude <url>` when you're ready."
            )

        js.signal_success()

    def send_chat(self, message):
        """Post a message to chat with the slip-mode reply prefix.

        Mirrors MacOSAdapter._do_send_chat: snapshot existing IDs,
        fill the textarea, send, poll every 50ms (up to 1s) for one
        new ID. Returns the new data-message-id, or None on timeout
        (caller falls back to text-match dedup).

        slip-mode quirk: the message is prefixed with self._reply_prefix
        (e.g. '🤖 ') so the room can distinguish claude's words from the
        user's own typing. Empty prefix (dial/deploy) means no marker.

        Direct call — no queue/thread bridging needed because slip runs
        playwright on the same thread that drives ChatRunner. The
        queue dance in MacOSAdapter exists to bridge main → browser
        thread under launch_persistent_context; CDP-attach has no
        background thread.
        """
        if self._page is None:
            return None
        full_message = f"{self._reply_prefix}{message}" if self._reply_prefix else message
        self._ensure_chat_open(self._page)
        try:
            pre_ids = set(self._page.evaluate(SNAPSHOT_MESSAGE_IDS_JS))
            input_box = self._page.locator('textarea[aria-label="Send a message"]')
            input_box.wait_for(timeout=5000)
            input_box.fill(full_message)
            input_box.press("Enter")
            log.info(f"AttachAdapter: chat sent: {full_message!r}")
            for _ in range(20):
                current = set(self._page.evaluate(SNAPSHOT_MESSAGE_IDS_JS))
                new_ids = current - pre_ids
                if new_ids:
                    return next(iter(new_ids))
                time.sleep(0.05)
            log.debug("AttachAdapter: send_chat ID-readback timed out — caller will fall back to text-match dedup")
            return None
        except Exception as e:
            log.warning(f"AttachAdapter: send_chat failed: {e}")
            return None

    def read_chat(self):
        """Drain the JS-side chat queue. Mirrors MacOSAdapter._do_read_chat."""
        if self._page is None:
            return []
        self._ensure_chat_open(self._page)
        self._install_chat_observer(self._page)
        try:
            messages = self._page.evaluate(DRAIN_CHAT_QUEUE_JS)
            if messages:
                log.debug(f"AttachAdapter: observer drained {len(messages)} new messages")
            return messages
        except Exception as e:
            log.warning(f"AttachAdapter: read_chat failed: {e}")
            return []

    def get_participant_count(self):
        """Count participants via data-requested-participant-id elements."""
        if self._page is None:
            return 0
        try:
            return self._page.locator('[data-requested-participant-id]').count()
        except Exception as e:
            log.warning(f"AttachAdapter: get_participant_count failed: {e}")
            return 0

    def get_participant_names(self):
        """Best-effort participant name scrape. Mirrors MacOSAdapter."""
        if self._page is None:
            return []
        try:
            return self._page.evaluate(GET_PARTICIPANT_NAMES_JS) or []
        except Exception as e:
            log.warning(f"AttachAdapter: get_participant_names failed: {e}")
            return []

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

    def _wait_for_meeting_entry(self, page):
        """Block until the user has entered the meeting.

        Detects entry via the 'Leave call' button — only present in-meeting,
        never in the green room. Same selector macos_adapter uses for its
        `already_in_meeting` short-circuit (~line 743). Polls every 1s,
        matching the cadence in macos_adapter._wait_for_admission.

        No timeout — lobby admission can take many minutes (host on another
        call, large meetings with multiple admits, etc.). User-paced waits
        shouldn't have a clock; the user can Ctrl+C anytime if they want
        to abort. The only fatal signal is Chrome being closed, which we
        detect via is_connected.

        Returns True on entry, False if Chrome was closed mid-wait.
        Progress logged to /tmp/operator.log every 30s.
        """
        print(
            "\nWaiting for you to join the meeting in Chrome — click 'Join now'…",
            file=sys.stderr, flush=True,
        )
        last_log = time.monotonic()
        while True:
            try:
                leave_btn = page.get_by_role("button", name="Leave call")
                if leave_btn.count() > 0 and leave_btn.first.is_visible():
                    log.info("AttachAdapter: meeting entry detected")
                    print("Joined — claude is listening.\n", file=sys.stderr, flush=True)
                    return True
            except Exception:
                pass
            if not self.is_connected():
                log.warning("AttachAdapter: Chrome closed during meeting-entry wait")
                return False
            now = time.monotonic()
            if now - last_log > 30:
                log.info("AttachAdapter: still waiting for meeting entry…")
                last_log = now
            time.sleep(1.0)

    def _ensure_chat_open(self, page):
        """Open the chat panel if it isn't already.

        Mirrors MacOSAdapter._ensure_chat_open exactly — same Meet DOM,
        same selectors, same debug-screenshot fallback. Idempotent.
        """
        try:
            textarea = page.locator('textarea[aria-label="Send a message"]')
            if textarea.count() > 0 and textarea.is_visible():
                return  # already open
        except Exception:
            pass
        try:
            chat_btn = page.get_by_role("button", name="Chat with everyone")
            chat_btn.wait_for(timeout=3000)
            chat_btn.click()
            log.info("AttachAdapter: clicked chat button — waiting for panel to render")
            page.locator('textarea[aria-label="Send a message"]').wait_for(
                state="visible", timeout=2000
            )
            log.info("AttachAdapter: chat panel open")
        except Exception as e:
            log.debug(f"AttachAdapter: could not open chat panel: {e}")
            try:
                os.makedirs(config.DEBUG_DIR, exist_ok=True, mode=0o700)
                _shot = os.path.join(config.DEBUG_DIR, "chat_btn_not_found.png")
                page.screenshot(path=_shot)
                try:
                    os.chmod(_shot, 0o600)
                except OSError:
                    pass
                log.debug(f"AttachAdapter: saved debug screenshot to {_shot}")
            except Exception:
                pass

    def _install_chat_observer(self, page):
        """Inject the MutationObserver. Mirrors MacOSAdapter._install_chat_observer."""
        if self._observer_installed:
            return
        try:
            page.evaluate(INSTALL_CHAT_OBSERVER_JS)
            attached = page.evaluate(OBSERVER_ATTACHED_CHECK_JS)
            if attached:
                self._observer_installed = True
                log.info("AttachAdapter: chat MutationObserver installed")
            else:
                log.warning("AttachAdapter: chat observer not attached (textarea or panel container not in DOM) — will retry next poll")
        except Exception as e:
            log.warning(f"AttachAdapter: failed to install chat observer: {e}")

    def _find_or_open_meet_page(self, meeting_url):
        """Find an existing Meet tab for this exact room, or open a new one.

        Strict match on the room-code path (e.g. `/abc-defg-hij`) — not
        just the meet.google.com host. If the user has another Meet tab
        open for a different room, attaching to the wrong one would be
        a silent disaster.

        Polls briefly for fresh-launch races (Chrome was just relaunched
        with the URL as argv — tab might lag CDP attach by a few hundred
        ms). If still not found, opens a new tab via Playwright. Covers
        both relaunch and attach-to-existing-debug-Chrome cases without
        a parallel code path.
        """
        target_path = urlparse(meeting_url).path.rstrip('/')

        # Pass 1: scan for an existing exact-room match. Brief poll (~3s)
        # handles the post-relaunch race where Chrome's tab list hasn't
        # propagated to CDP yet.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            for context in self._browser.contexts:
                for page in context.pages:
                    try:
                        if urlparse(page.url).path.rstrip('/') == target_path:
                            log.info(f"AttachAdapter: found existing Meet tab at {page.url}")
                            return page
                    except Exception:
                        continue
            time.sleep(0.25)

        # Pass 2: not found — open a new tab with the URL. This is the
        # path for "attach to existing debug Chrome where this room
        # isn't currently open."
        log.info(f"AttachAdapter: no existing tab for {meeting_url} — opening new tab")
        try:
            page = self._browser.contexts[0].new_page()
            page.goto(meeting_url, wait_until="domcontentloaded", timeout=30000)
            return page
        except Exception as e:
            log.warning(f"AttachAdapter: failed to open new tab: {e}")
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
