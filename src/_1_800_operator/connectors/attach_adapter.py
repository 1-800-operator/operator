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
CHROME_USER_DATA_DIR_MACOS = os.path.expanduser("~/Library/Application Support/Google/Chrome")
GRACEFUL_QUIT_TIMEOUT_SECONDS = 5
PKILL_FALLBACK_TIMEOUT_SECONDS = 5
# Full user profile with many tabs / extensions can take 20+s to spin up
# the debug server. 30s is generous without being painful when something
# is genuinely wrong.
RELAUNCH_READY_TIMEOUT_SECONDS = 30


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

    Uses `open -na 'Google Chrome' --args ...` instead of direct binary
    exec via subprocess.Popen — macOS LaunchServices can intercept a
    direct binary launch and merge it with an existing/quitting Chrome
    process, silently dropping the --remote-debugging-port flag in the
    process. `open -na` forces a brand-new instance and reliably
    propagates flags through `--args`.

    The returned Popen handle is the `open` command itself, which exits
    quickly after dispatching the launch. The actual Chrome process is
    owned by LaunchServices; track it via _chrome_is_running, not this
    handle.
    """
    if not os.path.exists(CHROME_BINARY_MACOS):
        raise SlipAttachError(
            f"Could not find Google Chrome at {CHROME_BINARY_MACOS!r}. "
            "Install Chrome from https://www.google.com/chrome/ and re-run."
        )
    args = [
        "open", "-na", "Google Chrome", "--args",
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={CHROME_USER_DATA_DIR_MACOS}",
        meeting_url,
    ]
    log.info(f"AttachAdapter: launching Chrome via: {' '.join(args)}")
    return subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _wait_for_cdp_ready(timeout_seconds: int = RELAUNCH_READY_TIMEOUT_SECONDS) -> None:
    """Block until the CDP endpoint accepts a TCP connection.

    Chrome publishes the debugging port shortly after process launch —
    polling beats sleeping a fixed duration. Raises SlipAttachError on
    timeout, with a diagnostic line in /tmp/operator.log capturing
    whether Chrome is actually running (disambiguates "Chrome failed
    to launch" from "Chrome launched but didn't bind 9222").
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
    chrome_alive = _chrome_is_running()
    log.warning(
        f"AttachAdapter: CDP timeout after {timeout_seconds}s; "
        f"chrome_running={chrome_alive}"
    )
    if chrome_alive:
        hint = (
            "Chrome is running but did not expose the debug port. Quit Chrome "
            "fully (Cmd+Q, not just close window) and re-run."
        )
    else:
        hint = (
            "Chrome did not stay running. Try launching Chrome manually first "
            "(open -na 'Google Chrome'), then re-run slip."
        )
    raise SlipAttachError(
        f"Chrome CDP endpoint at {CDP_URL} did not become ready within "
        f"{timeout_seconds}s. {hint}"
    )


class AttachAdapter(MeetingConnector):
    """MeetingConnector for slip mode — CDP-attached to user's Chrome.

    Chat methods (send/read/participants) and Chrome lifecycle (join/
    is_connected/leave) all functional from 14.19.3b onward. Wired into
    `operator slip` in 14.19.3c (which builds the LLM/MCP/ChatRunner
    pipeline and hands the adapter to ChatRunner.run()).
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

        # Probe CDP first — if a prior slip session left Chrome running with
        # the debug port open, skip the quit/relaunch dance entirely. The
        # user shouldn't have to re-quit their browser on the second slip.
        if _cdp_endpoint_alive():
            log.info(f"AttachAdapter: CDP already alive at {CDP_URL} — attaching to existing Chrome")
        elif _chrome_is_running():
            # Chrome running but no debug port — must quit + relaunch.
            if not _confirm_chrome_quit_with_user():
                js.signal_failure("user_declined_chrome_quit")
                raise SlipAttachError(
                    "slip mode requires closing Chrome. Aborted at user request."
                )
            log.info("AttachAdapter: quitting Chrome (graceful)")
            if not _chrome_quit_graceful():
                log.warning("AttachAdapter: graceful quit timed out — falling back to pkill")
                if not _chrome_kill_force():
                    js.signal_failure("chrome_quit_failed")
                    raise SlipAttachError(
                        "Could not close Chrome — it is still running after both "
                        "graceful quit and force kill. Quit Chrome manually and re-run."
                    )
            self._chrome_proc = _launch_chrome_with_debug_port(meeting_url)
            try:
                _wait_for_cdp_ready()
            except SlipAttachError:
                js.signal_failure("cdp_not_ready")
                raise
        else:
            # Chrome not running at all — fresh launch with debug port.
            self._chrome_proc = _launch_chrome_with_debug_port(meeting_url)
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
