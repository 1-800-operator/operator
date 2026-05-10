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
import queue
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
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
from .session import JoinStatus, save_debug, _is_real_meet_room


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

# operator-audio-capture lives at one of two paths. Production is the
# signed+notarized .app produced by scripts/build_signed_helper.sh — only
# this path can capture system audio (SCStream callbacks are silently
# denied for ad-hoc-signed binaries on macOS 14+). Dev fallback is the
# raw swiftc-built artifact in-tree, used for mic-only iteration when no
# Developer-ID cert is available. Production wins when both exist; mirrors
# doctor.py:_AUDIO_HELPER_INSTALLED.
_AUDIO_HELPER_INSTALLED = (
    Path.home() / ".operator" / "bin" / "operator-audio-capture.app"
    / "Contents" / "MacOS" / "operator-audio-capture"
)
_AUDIO_HELPER_DEV = Path(__file__).resolve().parent.parent / "swift" / "operator-audio-capture"

# Frame format from the helper: [1B tag 'S'|'M'][4B BE u32 length][N bytes Float32 16kHz mono PCM].
# 'S' = system audio (other participants), 'M' = mic (local user).
# Source of truth: src/_1_800_operator/swift/operator-audio-capture.swift.
_FRAME_TAG_SYSTEM = b"S"
_FRAME_TAG_MIC = b"M"
_FRAME_HEADER_LEN = 5  # 1 byte tag + 4 byte BE u32 length

# Speaker labels written into the meeting record. "user" matches what
# transcript_server.py / llm.py expect for the local-side speaker; the
# remote side gets "other" (we don't have per-participant attribution
# from system audio — Whisper alone can't diarize Meet's mixed stream).
_SPEAKER_USER = "user"
_SPEAKER_OTHER = "other"


class SlipAttachError(RuntimeError):
    """Raised when the slip-mode attach lifecycle fails fatally.

    Caught by _run_slip and presented to the user as a clean stderr
    message with a fix hint, not a stack trace.
    """


def _resolve_audio_helper() -> Path | None:
    """Return the path to operator-audio-capture, or None if missing.

    Production install (~/.operator/bin/) wins over in-tree dev build
    when both exist. None means audio capture is unavailable; AttachAdapter
    skips spawning and runs in chat-only mode (warning logged, no crash —
    audio is an enhancement, not a hard requirement for slip).
    """
    if _AUDIO_HELPER_INSTALLED.exists() and os.access(_AUDIO_HELPER_INSTALLED, os.X_OK):
        return _AUDIO_HELPER_INSTALLED
    if _AUDIO_HELPER_DEV.exists() and os.access(_AUDIO_HELPER_DEV, os.X_OK):
        return _AUDIO_HELPER_DEV
    return None


def _evict_other_chrome_on_cdp_port() -> bool:
    """Kill the non-slip Chrome process holding CDP_PORT, if any.

    slip launches its own dedicated Chrome with --remote-debugging-port=9222.
    If port 9222 is already held by some other Chrome (a leftover spike,
    a debugger window from another tool, a stale instance from a crashed
    slip session) we silently SIGTERM that Chrome ourselves rather than
    asking the user to run pkill. Identifies the PID via lsof, verifies
    it's a Chrome process via ps, then sends SIGTERM. Escalates to
    SIGKILL after 2s if it doesn't exit.

    Returns True if a Chrome was evicted; False if no Chrome was found
    on the port or eviction failed.
    """
    try:
        r = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{CDP_PORT}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return False
        pids = [int(p) for p in r.stdout.split() if p.strip().isdigit()]
    except Exception as e:
        log.warning(f"AttachAdapter: lsof failed during eviction: {e}")
        return False

    evicted_any = False
    for pid in pids:
        try:
            ps_r = subprocess.run(
                ["ps", "-o", "command=", "-p", str(pid)],
                capture_output=True, text=True, timeout=2,
            )
            command = ps_r.stdout.strip()
            if "Google Chrome" not in command:
                # Whatever's on 9222 isn't Chrome — leave it alone, slip
                # will fail downstream with a clearer launch error.
                log.warning(
                    f"AttachAdapter: pid {pid} on port {CDP_PORT} is not "
                    f"Chrome ({command[:80]!r}) — not evicting"
                )
                continue
            log.info(f"AttachAdapter: evicting non-slip Chrome pid={pid}")
            os.kill(pid, 15)  # SIGTERM
            # Wait up to 2s for graceful exit, escalate to SIGKILL if needed
            for _ in range(20):
                try:
                    os.kill(pid, 0)  # check if alive
                    time.sleep(0.1)
                except OSError:
                    evicted_any = True
                    break
            else:
                try:
                    os.kill(pid, 9)  # SIGKILL
                    log.warning(f"AttachAdapter: SIGKILL'd pid={pid} after SIGTERM timeout")
                    evicted_any = True
                except Exception as e:
                    log.warning(f"AttachAdapter: SIGKILL failed pid={pid}: {e}")
        except ProcessLookupError:
            # Already gone
            evicted_any = True
        except Exception as e:
            log.warning(f"AttachAdapter: eviction failed pid={pid}: {e}")
    return evicted_any


def _cdp_belongs_to_slip() -> bool:
    """True iff the Chrome currently listening on CDP_PORT was launched
    against SLIP_PROFILE_DIR.

    Without this check, slip would happily attach to any Chrome that
    happens to have port 9222 open — a leftover validation spike, a
    DevTools-debugger Chrome the user started for some other reason,
    or a different operator install. Attaching to the wrong Chrome
    fails downstream with confusing Playwright errors.

    Verification path: lsof finds the PID listening on 9222, ps reads
    that PID's command line, we check for SLIP_PROFILE_DIR in the
    args. Returns False on any failure (lsof missing, ps weirdness,
    permission errors, etc.) — the caller will then launch a fresh
    slip Chrome, which is the safe fallback.
    """
    try:
        r = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{CDP_PORT}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode != 0:
            return False
        for line in r.stdout.splitlines()[1:]:  # skip header
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                pid = int(parts[1])
            except ValueError:
                continue
            ps_r = subprocess.run(
                ["ps", "-o", "command=", "-p", str(pid)],
                capture_output=True, text=True, timeout=2,
            )
            # Match the exact `--user-data-dir=PATH` flag we pass in
            # _launch_slip_chrome — a plain `PATH in stdout` substring match
            # would false-positive if the user has a sibling profile dir
            # (e.g. ~/.operator/slip_profile_backup) whose path contains ours.
            if f"--user-data-dir={SLIP_PROFILE_DIR}" in ps_r.stdout:
                return True
        return False
    except Exception as e:
        log.warning(f"AttachAdapter: _cdp_belongs_to_slip check failed: {e}")
        return False


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
    # mode= on makedirs only fires at creation; chmod is the belt for the
    # case where the dir already exists with looser perms. The slip profile
    # holds Google session cookies — owner-only matters on shared hosts.
    os.makedirs(SLIP_PROFILE_DIR, exist_ok=True, mode=0o700)
    os.chmod(SLIP_PROFILE_DIR, 0o700)
    args = [
        "open", "-na", "Google Chrome", "--args",
        f"--remote-debugging-port={CDP_PORT}",
        # Required by Chrome 111+ to allow CDP WebSocket connections from
        # localhost. Without this, some browser-level CDP methods reject
        # with "Browser context management is not supported" or similar.
        "--remote-allow-origins=*",
        f"--user-data-dir={SLIP_PROFILE_DIR}",
        # Silence first-run / default-browser nags so slip Chrome lands
        # the user directly on the meeting URL.
        "--no-first-run",
        "--no-default-browser-check",
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
        "Slip Chrome didn't come up in time. Try running slip again."
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
        # Browser thread + chat command queue. Playwright's sync API is
        # single-threaded by contract: only the thread that opened the
        # context may call its methods. To keep AttachAdapter safe to
        # invoke from any thread (chat-runner main, provider permission
        # pump, heartbeat daemon), all Playwright work runs on a
        # dedicated browser thread; public methods drop commands onto
        # _chat_queue and block on a per-call result queue. Mirrors the
        # design already in MacOSAdapter and LinuxAdapter — slip mode
        # used to be the odd one out (greenlet errors when off-thread
        # callers reached page.evaluate directly).
        self._chat_queue: queue.Queue = queue.Queue()
        self._browser_thread: threading.Thread | None = None
        self._leave_event = threading.Event()
        # Mirrors browser-session liveness for is_connected() so callers
        # don't have to touch Playwright objects from arbitrary threads.
        # Set when the page+browser are live; cleared when the session
        # is winding down. _browser_closed is set at the very end of
        # _browser_session so leave() can wait for clean teardown.
        self._browser_alive = threading.Event()
        self._browser_closed = threading.Event()
        # Audio pipeline (14.20.4) — populated by _start_audio_pipeline()
        # after meeting entry. Stays None on Linux (mac-only helper) or
        # when the helper binary hasn't been built. set_caption_callback
        # may be invoked before or after join(); the late-bound callback
        # path matches macos_adapter's contract.
        self._caption_callback = None
        self._audio_helper_proc: subprocess.Popen | None = None
        self._audio_processors: dict[bytes, "object"] = {}
        self._audio_threads: list[threading.Thread] = []
        self._audio_stop = threading.Event()

    # ------------------------------------------------------------------
    # MeetingConnector interface
    # ------------------------------------------------------------------

    def join(self, meeting_url):
        """Validate input, then spawn the browser thread that owns Playwright.

        Fire-and-forget: returns once the thread is started; failures
        and successes both surface via `self.join_status` (callers wait
        on `join_status.ready`). The previous design ran Playwright on
        the calling thread and raised SlipAttachError synchronously,
        which meant any off-thread caller (PermissionChatHandler on the
        provider's pump thread, heartbeat daemon, etc.) hit greenlet
        errors. Mirrors the contract already in MacOSAdapter /
        LinuxAdapter.

        Synchronous validation is kept on the calling thread because
        these checks don't touch Playwright and surfacing them through
        join_status would defer obvious user errors to a background
        thread.
        """
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

        self._leave_event.clear()
        self._browser_alive.clear()
        self._browser_closed.clear()
        self._observer_installed = False
        self._browser_thread = threading.Thread(
            target=self._browser_session,
            args=(meeting_url,),
            daemon=True,
            name="AttachAdapter-browser",
        )
        self._browser_thread.start()
        log.info(f"AttachAdapter: joining {meeting_url}")

    def _browser_session(self, meeting_url):
        """Browser-thread entry point. Owns the entire Playwright lifecycle.

        Runs on a dedicated daemon thread spawned by join(). All
        Playwright sync API calls happen here; public methods (send_chat,
        read_chat, get_participant_*) round-trip via `_chat_queue` so
        callers from any thread are decoupled from the single-threaded
        sync API constraint. Exits when leave() sets `_leave_event` or
        when the browser disconnects.
        """
        js = self.join_status
        try:
            # CDP probe + Chrome launch — these are subprocess-level
            # operations (no Playwright) but we keep them on the browser
            # thread so the entire session lifecycle lives in one place
            # and so a slow CDP probe doesn't block the calling thread.
            took_reuse_path = False
            if _cdp_endpoint_alive() and _cdp_belongs_to_slip():
                log.info(f"AttachAdapter: CDP at {CDP_URL} is slip Chrome — reusing")
                took_reuse_path = True
            else:
                if _cdp_endpoint_alive():
                    # Some other Chrome is hogging the port. Evict it
                    # silently — the user shouldn't have to know.
                    _evict_other_chrome_on_cdp_port()
                    # Brief settle so the kernel releases the port
                    # before we try to bind.
                    time.sleep(0.5)
                self._chrome_proc = _launch_slip_chrome(meeting_url)
                try:
                    _wait_for_cdp_ready()
                except SlipAttachError:
                    js.signal_failure("cdp_not_ready")
                    return

            self._playwright = sync_playwright().start()
            try:
                self._browser = self._playwright.chromium.connect_over_cdp(CDP_URL)
            except Exception as e:
                # Reuse path failure → almost always a zombie slip
                # Chrome: the process is alive and the CDP socket is
                # open, but every window has been closed so there is no
                # BrowserContext for Playwright to manage. The canonical
                # symptom is `Browser.setDownloadBehavior … Browser
                # context management is not supported`. Self-heal by
                # evicting the zombie Chrome and relaunching once. We
                # only retry on the reuse path: a connect failure
                # immediately after a fresh launch is a real problem
                # (port held by something un-killable, OS-level issue)
                # that we should surface rather than paper over.
                if took_reuse_path:
                    log.warning(
                        f"AttachAdapter: reused slip Chrome rejected "
                        f"connect_over_cdp ({e}); evicting zombie Chrome "
                        "and relaunching"
                    )
                    self._teardown_playwright()
                    _evict_other_chrome_on_cdp_port()
                    time.sleep(0.5)
                    self._chrome_proc = _launch_slip_chrome(meeting_url)
                    try:
                        _wait_for_cdp_ready()
                    except SlipAttachError:
                        js.signal_failure("cdp_not_ready")
                        return
                    self._playwright = sync_playwright().start()
                    try:
                        self._browser = self._playwright.chromium.connect_over_cdp(CDP_URL)
                    except Exception as e2:
                        self._teardown_playwright()
                        js.signal_failure("cdp_attach_failed")
                        log.error(
                            f"AttachAdapter: connect_over_cdp failed after "
                            f"relaunch: {e2}"
                        )
                        return
                    log.info("AttachAdapter: attached after zombie-Chrome recovery")
                else:
                    self._teardown_playwright()
                    js.signal_failure("cdp_attach_failed")
                    log.error(f"AttachAdapter: connect_over_cdp failed: {e}")
                    return

            self._page = self._find_or_open_meet_page(meeting_url)
            if self._page is None:
                self._teardown_playwright()
                js.signal_failure("meet_tab_open_failed")
                return
            log.info(f"AttachAdapter: attached to Meet tab at {self._page.url}")

            # Mark the session live so off-thread is_connected() short-
            # circuits to True before signalling join_status — the
            # _wait_for_meeting_entry loop also reads it.
            self._browser_alive.set()

            # Block here until the user has actually entered the meeting.
            # Lobby admission is user-paced and can take many minutes.
            if not self._wait_for_meeting_entry(self._page):
                js.signal_failure("chrome_closed_before_entry")
                return

            # Audio is best-effort — meeting entry already succeeded, so
            # failure to spawn the helper must NOT fail the join.
            self._start_audio_pipeline()

            js.signal_success()

            # Main loop: drain queued chat commands, watch for browser
            # death. 200 ms cadence balances responsiveness for queued
            # reads/sends against CPU spend on idle meetings.
            while not self._leave_event.is_set():
                self._process_chat_queue(self._page)
                try:
                    if self._page.is_closed():
                        log.warning("AttachAdapter: page closed mid-meeting — exiting")
                        break
                    if not self._browser.is_connected():
                        log.warning(
                            "AttachAdapter: browser disconnected mid-meeting — exiting"
                        )
                        break
                except Exception as e:
                    log.warning(f"AttachAdapter: liveness probe raised: {e}")
                    break
                # page.wait_for_timeout is the browser-thread-safe sleep —
                # it parks the greenlet without breaking sync_playwright's
                # event loop. Plain time.sleep would also work but stays
                # consistent with MacOSAdapter's main loop.
                try:
                    self._page.wait_for_timeout(200)
                except Exception:
                    # Page died during the wait. Loop top will re-check.
                    pass
        except Exception as e:
            log.error(
                f"AttachAdapter: browser session crashed: {e}", exc_info=True,
            )
            if not js.ready.is_set():
                js.signal_failure(f"browser_session_crashed: {type(e).__name__}")
        finally:
            self._browser_alive.clear()
            self._stop_audio_pipeline()
            self._teardown_playwright()
            self._browser_closed.set()

    def set_caption_callback(self, fn):
        """Register fn(speaker, text, timestamp) for finalized utterances.

        Mirrors macos_adapter contract — may be called before OR after
        join(). The audio pipeline buffers utterances internally and
        delivers them through whichever callback is registered at the
        moment the utterance finalizes; late-bind is fine. Pass None to
        unregister.

        AttachAdapter's "captions" are local Whisper transcriptions of
        the helper's two PCM streams (system + mic), not Meet's caption
        DOM. Each call delivers one finalized utterance — no streaming
        partials, so callers don't need TranscriptFinalizer's silence /
        prefix-strip logic. dial wires this through TranscriptFinalizer;
        slip wires a direct write into MeetingRecord.
        """
        self._caption_callback = fn

    def send_chat(self, message):
        """Post a message to chat. Queues the request for the browser thread.

        Returns the new `data-message-id` from the post, or None on
        timeout / failure (caller falls back to text-match dedup).
        Returns None immediately when called before the browser thread
        is alive — same fallback shape.

        Slip-mode prefix-strip: prepends self._reply_prefix
        (`[🤖 Claude] ` per `bridges/claude.py:REPLY_PREFIX_SLIP`) so
        meeting participants can tell the bot's replies apart from the
        user's own messages. Use `send_chat_raw` to post without the
        prefix (operator-voice status messages already carry their
        own `[☎️ Operator] ` prefix).
        """
        if not self._browser_alive.is_set():
            return None
        result_q: queue.Queue = queue.Queue()
        self._chat_queue.put(("send", message, result_q))
        try:
            return result_q.get(timeout=10)
        except queue.Empty:
            return None

    def send_chat_raw(self, message):
        """Post a message verbatim — no slip reply-prefix prepended.

        Used by ChatRunner for operator-voice status lines (connection
        drops, throttled tool narration, permission-denial hints) which
        already carry the `[☎️ Operator] ` prefix from
        `bridges/claude.py:REPLY_PREFIX_OPERATOR`. Posting them through
        `send_chat` would double-prefix them with the slip bot prefix,
        confusing the visual contract that meeting participants rely on
        (Claude voice vs. Operator voice).
        """
        if not self._browser_alive.is_set():
            return None
        result_q: queue.Queue = queue.Queue()
        self._chat_queue.put(("send_raw", message, result_q))
        try:
            return result_q.get(timeout=10)
        except queue.Empty:
            return None

    def read_chat(self):
        """Drain new chat messages. Queues the request for the browser thread."""
        if not self._browser_alive.is_set():
            return []
        result_q: queue.Queue = queue.Queue()
        self._chat_queue.put(("read", None, result_q))
        try:
            return result_q.get(timeout=10)
        except queue.Empty:
            return []

    def get_participant_count(self):
        """Return participant count via the browser thread."""
        if not self._browser_alive.is_set():
            return 0
        result_q: queue.Queue = queue.Queue()
        self._chat_queue.put(("participant_count", None, result_q))
        try:
            return result_q.get(timeout=5)
        except queue.Empty:
            return 0

    def get_participant_names(self):
        """Return participant display names via the browser thread."""
        if not self._browser_alive.is_set():
            return []
        result_q: queue.Queue = queue.Queue()
        self._chat_queue.put(("participant_names", None, result_q))
        try:
            return result_q.get(timeout=5)
        except queue.Empty:
            return []

    def is_connected(self):
        """Cross-thread-safe liveness check.

        Reads only threading.Event flags maintained by the browser
        thread — no Playwright access from the caller's thread.
        Returns True iff the browser thread is holding a live page and
        hasn't started its teardown.
        """
        return self._browser_alive.is_set() and not self._browser_closed.is_set()

    # --- Browser-thread chat implementations (called from _process_chat_queue) ---

    def _process_chat_queue(self, page):
        """Drain the chat command queue. Called from the browser thread."""
        while True:
            try:
                cmd, args, result_q = self._chat_queue.get_nowait()
            except queue.Empty:
                return
            try:
                if cmd == "send":
                    result_q.put(self._do_send_chat(page, args))
                elif cmd == "send_raw":
                    result_q.put(self._do_send_chat(page, args, prepend_prefix=False))
                elif cmd == "read":
                    result_q.put(self._do_read_chat(page))
                elif cmd == "participant_count":
                    result_q.put(self._do_get_participant_count(page))
                elif cmd == "participant_names":
                    result_q.put(self._do_get_participant_names(page))
                else:
                    log.warning(f"AttachAdapter: unknown chat-queue command {cmd!r}")
                    result_q.put(None)
            except Exception as e:
                # Don't let a single command crash the browser session.
                # Surface the failure to the waiter via the sentinel
                # value its public wrapper expects on error.
                log.warning(f"AttachAdapter: chat-queue command {cmd!r} raised: {e}")
                fallback = [] if cmd in ("read", "participant_names") else (
                    0 if cmd == "participant_count" else None
                )
                try:
                    result_q.put(fallback)
                except Exception:
                    pass

    def _do_send_chat(self, page, message, prepend_prefix: bool = True):
        """Browser-thread implementation. Mirrors MacOSAdapter._do_send_chat:
        snapshot existing IDs, fill the textarea, send, poll every 50 ms
        (up to 1 s) for one new ID. Returns the new `data-message-id`
        or None on timeout (caller falls back to text-match dedup).

        slip-mode quirk: the message is prefixed with self._reply_prefix
        (e.g. '[🤖 Claude] ') so the room can distinguish claude's words
        from the user's own typing. Empty prefix (dial/deploy) means no
        marker. Prefix value lives in `bridges/claude.py:REPLY_PREFIX_SLIP`.

        `prepend_prefix=False` is used by `send_chat_raw` for operator-voice
        status messages — they carry their own `[☎️ Operator] ` prefix and
        must not be double-prefixed with the slip bot prefix.
        """
        full_message = (
            f"{self._reply_prefix}{message}" if (prepend_prefix and self._reply_prefix) else message
        )
        self._ensure_chat_open(page)
        try:
            pre_ids = set(page.evaluate(SNAPSHOT_MESSAGE_IDS_JS))
            input_box = page.locator('textarea[aria-label="Send a message"]')
            input_box.wait_for(timeout=5000)
            input_box.fill(full_message)
            input_box.press("Enter")
            log.info(f"AttachAdapter: chat sent: {full_message!r}")
            for _ in range(20):
                current = set(page.evaluate(SNAPSHOT_MESSAGE_IDS_JS))
                new_ids = current - pre_ids
                if new_ids:
                    return next(iter(new_ids))
                time.sleep(0.05)
            log.debug(
                "AttachAdapter: send_chat ID-readback timed out — caller will "
                "fall back to text-match dedup"
            )
            return None
        except Exception as e:
            log.warning(f"AttachAdapter: send_chat failed: {e}")
            return None

    def _do_read_chat(self, page):
        """Browser-thread implementation. Drains the JS-side chat queue.

        Slip-mode prefix-strip: send_chat prepends self._reply_prefix
        (`[🤖 Claude] ` per `bridges/claude.py:REPLY_PREFIX_SLIP`) so the
        room can distinguish claude's words from the user's typing. The
        DOM observer reads back the prefixed text. ChatRunner's
        _own_messages dedup set stores the UN-prefixed text. Without
        normalization the text-match dedup misses, the bot's own
        messages get treated as new user input, and a self-reply
        cascade kicks off. Strip the prefix here so the text passed
        upstream matches what was added to _own_messages.

        Slip-only optimistic-ID filter: when the slip-mode user types a
        message in their own Chrome, Meet renders an optimistic
        placeholder element with a numeric local-timestamp ID
        (e.g. `1778216640038`) before the server confirms. ~5–10 s
        later Meet swaps in a real element with the canonical ID
        (`spaces/<spaceId>/messages/<msgId>`). The MutationObserver
        fires on both, so without filtering ChatRunner sees the same
        user message twice under different IDs and dispatches two LLM
        turns. Drop placeholder-shaped IDs here — the canonical always
        follows under normal Meet delivery; we accept the rare
        canonical-never-arrives case (network drop) as silent message
        loss rather than risk a double turn on every user message.
        Other adapters (dial/deploy/Linux) don't hit this because the
        bot is a separate participant observing only server-confirmed
        traffic, so this filter lives here, not in chat_runner.
        """
        self._ensure_chat_open(page)
        self._install_chat_observer(page)
        try:
            messages = page.evaluate(DRAIN_CHAT_QUEUE_JS)
            if messages:
                log.debug(
                    f"AttachAdapter: observer drained {len(messages)} new messages"
                )
            filtered = []
            for msg in messages:
                mid = msg.get("id") or ""
                if not mid.startswith("spaces/"):
                    log.debug(
                        f"AttachAdapter: dropping placeholder-id message "
                        f"id={mid!r} text={msg.get('text', '')[:40]!r} "
                        "(awaiting canonical)"
                    )
                    continue
                filtered.append(msg)
            messages = filtered
            if self._reply_prefix and messages:
                for msg in messages:
                    text = msg.get("text", "")
                    if text.startswith(self._reply_prefix):
                        msg["text"] = text[len(self._reply_prefix):]
            return messages
        except Exception as e:
            log.warning(f"AttachAdapter: read_chat failed: {e}")
            return []

    def _do_get_participant_count(self, page):
        try:
            return page.locator('[data-requested-participant-id]').count()
        except Exception as e:
            log.warning(f"AttachAdapter: get_participant_count failed: {e}")
            return 0

    def _do_get_participant_names(self, page):
        try:
            return page.evaluate(GET_PARTICIPANT_NAMES_JS) or []
        except Exception as e:
            log.warning(f"AttachAdapter: get_participant_names failed: {e}")
            return []

    def leave(self):
        """Disconnect from CDP. Does NOT quit Chrome — the user's browser
        keeps running with all its tabs (including the Meet tab claude
        was attached to). Idempotent.

        Signals the browser thread to exit and waits briefly for clean
        teardown. Audio pipeline shutdown + Playwright teardown happen
        inside the browser thread's finally block so all Playwright
        calls stay on the thread that owns them.
        """
        if self._leave_event.is_set():
            return
        self._leave_event.set()
        if self._browser_thread and self._browser_thread.is_alive():
            log.info("AttachAdapter: waiting for browser thread to exit...")
            if not self._browser_closed.wait(timeout=10):
                log.warning("AttachAdapter: browser-thread close timed out (10s)")
            self._browser_thread.join(timeout=2)
            if self._browser_thread.is_alive():
                log.warning(
                    "AttachAdapter: browser thread still alive after 12s; "
                    "abandoning (daemon thread will exit with the process)"
                )
        else:
            # Edge case: leave() called before join() ever spawned the
            # thread (e.g. early validation failure path). Nothing to
            # tear down beyond what the failure path already cleaned.
            self._stop_audio_pipeline()
            self._teardown_playwright()
        log.info("AttachAdapter: detached from Chrome (Chrome left running)")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _wait_for_meeting_entry(self, page):
        """Block until the user has entered the meeting.

        Detects entry by requiring BOTH the 'Leave call' button AND the
        'Chat with everyone' button to be visible. The reason for the
        two-signal AND: Meet renders the in-call control bar (including
        Leave call) the moment a user clicks 'Ask to join', even while
        the page is still on the 'Please wait until a meeting host
        brings you into the call' lobby screen. A 'Leave call'-only
        check therefore false-positives during the lobby wait — bot
        declares 'Joined' while the host hasn't admitted yet, then the
        chat-runner spins forever trying to open a chat panel that
        doesn't exist in the lobby DOM. The 'Chat with everyone' button
        is the discriminator: it does NOT render in the green-room
        pre-join state, and it does NOT render in the lobby waiting
        state — only in the actual in-call DOM. Confirmed via DOM dumps
        of all three states (session 205 repro). Bonus: chat_runner is
        about to open chat anyway, so waiting for the chat button is in
        the spirit of the detector.

        Polls every 1s, matching the cadence in macos_adapter._wait_for
        _admission. No timeout — lobby admission can take many minutes
        (host on another call, large meetings with multiple admits,
        etc.). User-paced waits shouldn't have a clock; the user can
        Ctrl+C anytime if they want to abort. The only fatal signal is
        Chrome being closed, which we detect via is_connected.

        Returns True on entry, False if Chrome was closed mid-wait.
        Progress logged to /tmp/operator.log every 30s.
        """
        print(
            "\nWaiting for you to join the meeting in Chrome — click 'Join now'…",
            file=sys.stderr, flush=True,
        )
        last_log = time.monotonic()
        while not self._leave_event.is_set():
            try:
                leave_btn = page.get_by_role("button", name="Leave call")
                chat_btn = page.get_by_role("button", name="Chat with everyone")
                leave_visible = (
                    leave_btn.count() > 0 and leave_btn.first.is_visible()
                )
                chat_visible = (
                    chat_btn.count() > 0 and chat_btn.first.is_visible()
                )
                if leave_visible and chat_visible:
                    log.info("AttachAdapter: meeting entry detected")
                    print("Joined — claude is listening.\n", file=sys.stderr, flush=True)
                    return True
            except Exception:
                pass
            # We're on the browser thread here, so probe Playwright
            # directly — public is_connected() reads cached threading
            # events that aren't updated mid-wait.
            try:
                if page.is_closed() or not self._browser.is_connected():
                    log.warning("AttachAdapter: Chrome closed during meeting-entry wait")
                    return False
            except Exception:
                log.warning("AttachAdapter: liveness probe failed during meeting-entry wait")
                return False
            now = time.monotonic()
            if now - last_log > 30:
                log.info("AttachAdapter: still waiting for meeting entry…")
                last_log = now
            time.sleep(1.0)
        # leave_event tripped while we were waiting for entry — caller
        # is shutting down before the user joined. Surface as a clean
        # not-entered signal so _browser_session takes the failure path
        # and tears Playwright down cleanly.
        log.info("AttachAdapter: leave requested before meeting entry")
        return False

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
                os.chmod(_shot, 0o600)
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

    # ------------------------------------------------------------------
    # Audio pipeline (14.20.4)
    # ------------------------------------------------------------------

    def _start_audio_pipeline(self) -> None:
        """Spawn operator-audio-capture and wire it through two AudioProcessors.

        Best-effort — any failure here logs a warning and leaves the
        connector in chat-only mode. The reasons audio might not come up:
          - Linux (helper is Mac-only)
          - Helper binary not built (install.sh hasn't run, or dev tree
            without a manual swiftc)
          - Helper exits early on TCC denial (Screen Recording / Mic) —
            the ten-second watchdog and exit codes 4/5 surface this; the
            user is told to run `operator doctor` for the fix copy

        Layout of the spawned pipeline:
          helper stdout --> _audio_reader_loop --> processors[tag].feed_audio
          processors['S']     --> _audio_utterance_loop("other") --> caption_callback
          processors['M']     --> _audio_utterance_loop("user")  --> caption_callback
        """
        if sys.platform != "darwin":
            return
        helper = _resolve_audio_helper()
        if helper is None:
            log.warning(
                "AttachAdapter: operator-audio-capture not found — slip will run "
                "chat-only (no transcript). Run install.sh to build the helper."
            )
            return

        try:
            from _1_800_operator.pipeline.audio import AudioProcessor
        except ImportError as e:
            log.warning(f"AttachAdapter: AudioProcessor import failed ({e}) — chat-only mode")
            return

        try:
            log.info("AudioProcessor: warming mlx-whisper-base (one-time per process)…")
            self._audio_processors[_FRAME_TAG_SYSTEM] = AudioProcessor()
            self._audio_processors[_FRAME_TAG_MIC] = AudioProcessor()
        except Exception as e:
            log.warning(f"AttachAdapter: AudioProcessor warmup failed ({e}) — chat-only mode")
            self._audio_processors.clear()
            return

        # Helper stderr → /tmp/operator.log (append). Same destination as
        # operator's own logs so users have one place to look. Health line
        # ("10s health: [S]=… cb / [M]=… cb"), TCC fatals, and shutdown
        # totals all land here.
        #
        # Spawned via posix_spawn with `responsibility_spawnattrs_setdisclaim`
        # (see _disclaimed_spawn.py). Without this, the helper inherits the
        # parent IDE/terminal's TCC responsibility chain — Cursor's
        # ToDesktop-wrapped Electron build silently denies SCStream audio
        # even when the helper itself is granted Screen Recording. Disclaim
        # makes the helper its own responsible process so TCC keys decisions
        # against the helper's own code-signature identifier, regardless of
        # who launched it. Validated against Cursor/Terminal.app spawn paths
        # in 14.20.4.
        try:
            from _1_800_operator.pipeline._disclaimed_spawn import spawn_disclaimed
            # spawn_disclaimed dup2's our fd into the child; close the parent
            # handle right after so we don't keep an extra fd to the log
            # open for the lifetime of the connector.
            with open("/tmp/operator.log", "ab") as stderr_sink:
                self._audio_helper_proc = spawn_disclaimed(
                    [str(helper)],
                    stderr_fd=stderr_sink.fileno(),
                )
        except OSError as e:
            log.warning(f"AttachAdapter: spawning {helper} failed ({e}) — chat-only mode")
            self._audio_processors.clear()
            return

        # Mark processors capturing BEFORE the utterance threads start so
        # the loop conditions don't immediately exit.
        for proc in self._audio_processors.values():
            proc.capturing = True

        reader = threading.Thread(
            target=self._audio_reader_loop,
            name="AttachAdapter-audio-reader",
            daemon=True,
        )
        reader.start()
        self._audio_threads.append(reader)

        for tag, label in (
            (_FRAME_TAG_SYSTEM, _SPEAKER_OTHER),
            (_FRAME_TAG_MIC, _SPEAKER_USER),
        ):
            t = threading.Thread(
                target=self._audio_utterance_loop,
                args=(tag, label),
                name=f"AttachAdapter-utterance-{label}",
                daemon=True,
            )
            t.start()
            self._audio_threads.append(t)

        log.info(f"AttachAdapter: audio pipeline up (helper={helper}, pid={self._audio_helper_proc.pid})")

    def _audio_reader_loop(self) -> None:
        """Parse framed PCM from helper stdout, dispatch to the right processor.

        Exits cleanly on EOF (helper closed stdout, typically after stdin
        close or fatal TCC error). Exits cleanly on _audio_stop set.
        Malformed frames (unknown tag, oversized length) are logged and
        the stream is abandoned — recovering from a desync would require
        re-syncing on a known sentinel, which the protocol doesn't have.
        Helper restarting is the recovery path; we don't do that here.
        """
        proc = self._audio_helper_proc
        if proc is None or proc.stdout is None:
            return
        # Cap frame size to a sane upper bound. The helper emits ~40ms
        # chunks (~5KB at 16kHz Float32). Anything > 1MB means the stream
        # is corrupted; bail.
        MAX_FRAME_BYTES = 1 << 20
        try:
            while not self._audio_stop.is_set():
                header = proc.stdout.read(_FRAME_HEADER_LEN)
                if len(header) < _FRAME_HEADER_LEN:
                    log.info("AttachAdapter: audio reader EOF — helper exited")
                    break
                tag = header[0:1]
                (length,) = struct.unpack(">I", header[1:5])
                if length == 0 or length > MAX_FRAME_BYTES:
                    log.warning(f"AttachAdapter: bogus frame length {length} — abandoning audio stream")
                    break
                pcm = proc.stdout.read(length)
                if len(pcm) < length:
                    log.info("AttachAdapter: audio reader truncated read — helper exited mid-frame")
                    break
                target = self._audio_processors.get(tag)
                if target is None:
                    log.warning(f"AttachAdapter: unknown frame tag {tag!r} — dropping {length}B")
                    continue
                target.feed_audio(pcm)
        except Exception as e:
            log.warning(f"AttachAdapter: audio reader loop crashed: {e}")

    def _audio_utterance_loop(self, tag: bytes, speaker_label: str) -> None:
        """Drain finalized utterances from one processor, fire caption callback.

        Loop exits when the processor flips capturing=False (set by
        _stop_audio_pipeline). Each utterance is delivered exactly once;
        the callback is captured by reference at call time so a late
        set_caption_callback also works.
        """
        proc = self._audio_processors.get(tag)
        if proc is None:
            return
        while proc.capturing and not self._audio_stop.is_set():
            try:
                text = proc.capture_next_utterance()
            except Exception as e:
                log.warning(f"AttachAdapter: utterance loop ({speaker_label}) raised: {e}")
                continue
            if not text:
                continue
            cb = self._caption_callback
            if cb is None:
                log.debug(f"AttachAdapter: utterance dropped (no callback) [{speaker_label}] {text!r}")
                continue
            try:
                cb(speaker_label, text, time.time())
            except Exception as e:
                log.warning(f"AttachAdapter: caption callback raised: {e}")

    def _stop_audio_pipeline(self) -> None:
        """Tear down the audio pipeline. Idempotent.

        Order matters: flip capturing=False so the utterance loops exit
        their next tick, set the stop event so the reader breaks out,
        then close helper stdin (which the helper watches for EOF and
        exits on). SIGTERM + a short wait is the fallback.
        """
        if self._audio_helper_proc is None and not self._audio_processors:
            return
        for proc in self._audio_processors.values():
            proc.capturing = False
        self._audio_stop.set()
        proc_handle = self._audio_helper_proc
        if proc_handle is not None:
            try:
                if proc_handle.stdin is not None:
                    proc_handle.stdin.close()
            except Exception as e:
                log.debug(f"AttachAdapter: closing helper stdin raised: {e}")
            try:
                proc_handle.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                log.warning("AttachAdapter: helper didn't exit on stdin close — terminating")
                try:
                    proc_handle.terminate()
                    proc_handle.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc_handle.kill()
                except Exception:
                    pass
            except Exception as e:
                log.debug(f"AttachAdapter: helper wait raised: {e}")
            self._audio_helper_proc = None
        for t in self._audio_threads:
            t.join(timeout=1.5)
        self._audio_threads.clear()
        self._audio_processors.clear()
        log.info("AttachAdapter: audio pipeline torn down")
