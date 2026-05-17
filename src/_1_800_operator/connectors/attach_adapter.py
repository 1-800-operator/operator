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
    1. Probe CDP — if slip Chrome is still running with ≥1 tab, reuse
       it (preserves the user's other tabs). If Chrome is in the macOS
       menu-bar-only state (0 tabs), Playwright re-attach would fail
       on Browser.setDownloadBehavior, so evict + relaunch.
    2. Otherwise launch Chrome with --user-data-dir=SLIP_PROFILE_DIR,
       --remote-debugging-port=9222, and the meeting URL via `open -na`
    3. Wait for CDP endpoint
    4. `playwright.chromium.connect_over_cdp("http://localhost:9222")`
    5. Find or open the Meet tab (strict room-code match) — opens a
       new tab in the existing slip Chrome window on the reuse path
    6. Wait for the user to click 'Join now' (indefinite poll)
    7. Hand back to ChatRunner
    8. On leave(): disconnect CDP only — slip Chrome stays running so
       the user can stay in the meeting after claude detaches and
       keep working in any other tabs they opened.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from _1_800_operator import config

from .base import MeetingConnector
from .chat_dom_js import (
    DRAIN_CHAT_QUEUE_JS,
    DRAIN_SPEAKING_QUEUE_JS,
    GET_PARTICIPANT_NAMES_JS,
    GET_SELF_NAME_JS,
    INSTALL_CHAT_OBSERVER_JS,
    INSTALL_SPEAKING_OBSERVER_JS,
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

# AEC3 cleaner binary (S225 spike → step 5 will land a proper install). Same
# resolution pattern as the audio helper: production install wins over the
# in-tree dev build. None means AEC is unavailable — slip falls back to
# feeding raw mic frames straight to the M-leg AudioProcessor (transcripts
# will then include speaker bleed when system audio plays through speakers;
# users on headphones are unaffected).
_AEC_BINARY_INSTALLED = Path.home() / ".operator" / "bin" / "aec3"
_AEC_BINARY_DEV = (
    Path(__file__).resolve().parent.parent / "rust" / "aec3" / "target" / "release" / "aec3"
)

# Frame format from the helper: [1B tag 'S'|'M'][4B BE u32 length][N bytes Float32 16kHz mono PCM].
# 'S' = system audio (other participants), 'M' = mic (local user).
# Source of truth: src/_1_800_operator/swift/operator-audio-capture.swift.
_FRAME_TAG_SYSTEM = b"S"
_FRAME_TAG_MIC = b"M"
_FRAME_HEADER_LEN = 5  # 1 byte tag + 4 byte BE u32 length

# Speaker labels written into the meeting record. The mic leg gets the
# local user's Meet display name when we can scrape it from the self
# tile (data-self-name); falls back to "user" if the scrape fails. The
# system-audio leg gets "other" — that channel is one mixed PCM stream
# across all remote participants, so per-speaker attribution would need
# diarization (Whisper alone can't do it).
_SPEAKER_USER_FALLBACK = "user"
_SPEAKER_OTHER = "other"

# How often the speaking observer rescans the DOM for new participant
# tiles. Tile structure is stable across a meeting, but new tiles render
# whenever someone joins; the JS install is idempotent at the per-tile
# level, so calling it every few seconds is cheap. 2s is short enough
# that a late joiner who immediately starts talking gets attributed
# correctly within their first utterance, and long enough that the
# per-call DOM walk doesn't pile up.
_SPEAKING_RESCAN_INTERVAL_S = 2.0


class SlipAttachError(RuntimeError):
    """Raised when the slip-mode attach lifecycle fails fatally.

    Caught by _run_slip and presented to the user as a clean stderr
    message with a fix hint, not a stack trace.
    """


def _resolve_aec_binary() -> Path | None:
    """Return the path to the aec3 cleaner binary, or None if missing."""
    if _AEC_BINARY_INSTALLED.exists() and os.access(_AEC_BINARY_INSTALLED, os.X_OK):
        return _AEC_BINARY_INSTALLED
    if _AEC_BINARY_DEV.exists() and os.access(_AEC_BINARY_DEV, os.X_OK):
        return _AEC_BINARY_DEV
    return None


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
    """Kill any Chrome process holding CDP_PORT.

    slip always launches a fresh Chrome on --remote-debugging-port=9222,
    so anything already on that port must go: a leftover slip Chrome
    from a previous operator session, a debugger Chrome the user
    started for another tool, a stale spike — doesn't matter. We
    silently SIGTERM whichever Chrome it is rather than asking the
    user to run pkill. Identifies the PID via lsof, verifies it's a
    Chrome process via ps (refuses to evict non-Chrome processes),
    then SIGTERM, escalating to SIGKILL after 2s.

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
            log.info(f"AttachAdapter: evicting Chrome on port {CDP_PORT} pid={pid}")
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


def _cdp_endpoint_alive(timeout: float = 1.0) -> bool:
    """Check if CDP debug endpoint is already accepting connections.

    Used by _browser_session to decide between reuse (existing slip
    Chrome with ≥1 tab — open a new meeting tab in it), evict + launch
    (existing Chrome in zero-context state — re-attach would fail with
    Browser.setDownloadBehavior, see _cdp_page_count), or plain launch
    (port free).

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


def _cdp_page_count(timeout: float = 1.0) -> int:
    """Count tabs (CDP targets of type 'page') in the running Chrome.

    The "zero-contexts trap" is the menu-bar-only state on macOS where
    Chrome stays alive after every window closes. Playwright's
    connect_over_cdp issues Browser.setDownloadBehavior unconditionally,
    which Chrome refuses in zero-context state with "Browser context
    management is not supported" — verified in
    debug/14_30_cdp_reattach_spike. If page count > 0, re-attach works
    and we can reuse the existing slip Chrome (preserving any user tabs);
    if it's 0, we must evict + relaunch to escape the trap.

    Returns -1 on probe failure so callers can treat it as
    "indeterminate — fall back to safe path".
    """
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(
            f"{CDP_URL}/json/list", timeout=timeout
        ) as resp:
            targets = json.loads(resp.read().decode())
        return sum(1 for t in targets if t.get("type") == "page")
    except Exception as e:
        log.warning(f"AttachAdapter: /json/list probe failed: {e}")
        return -1


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
        # invoke from any thread (chat-runner main, provider callbacks),
        # all Playwright work runs on a dedicated browser thread; public
        # methods drop commands onto _chat_queue and block on a per-call
        # result queue.
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
        # after meeting entry. Stays None when the helper binary hasn't
        # been built. set_caption_callback may be invoked before or after
        # join(); the callback is late-bound when set post-join.
        self._caption_callback = None
        self._audio_helper_proc: subprocess.Popen | None = None
        self._audio_processors: dict[bytes, "object"] = {}
        self._audio_threads: list[threading.Thread] = []
        self._audio_stop = threading.Event()
        # AEC cleaner subprocess (S225 spike → step 3 integration). Stays
        # None when the aec3 binary isn't installed/built; in that case
        # mic frames go straight to the M-leg AudioProcessor and the
        # M transcript will contain speaker bleed when system audio is
        # playing — there is no bleed defense in the fallback path.
        self._aec_cleaner: "object | None" = None
        # Residual-bleed dedupe: small rolling buffer of recently-finalized
        # S-leg caption texts (normalized). When an M-leg caption is about
        # to fire, we check it against this list — if it fuzzy-matches a
        # recent S-leg entry, it's almost certainly residual bleed that AEC
        # didn't fully cancel and we drop it. See config.BLEED_DEDUPE_*.
        self._recent_s_captions: deque[tuple[float, str]] = deque(maxlen=16)
        self._recent_s_captions_lock = threading.Lock()
        # Pre-warm thread for faster-whisper-large-v3-turbo. Spawned at
        # join() start so the cold model load (1-2s warm cache, up to ~100s
        # first run when the ~1.5GB model downloads from HuggingFace) runs
        # in parallel with Chrome launch + lobby wait; _start_audio_pipeline
        # joins this thread before spawning the helper. None until first join().
        self._whisper_warmup_thread: threading.Thread | None = None
        # Latency anchors for the TIMING listening_ready line:
        #   _slip_start_at    — set at join() entry (≈ when operator slip fired)
        #   _meeting_entry_at — set when the in-call DOM appears (≈ when
        #                       participants see operator in the meeting)
        # Both monotonic clocks; the deltas land on the observer-install log.
        self._slip_start_at: float | None = None
        self._meeting_entry_at: float | None = None
        # Speaking-indicator state. Browser thread drains the DOM speaking
        # queue every _process_chat_queue cycle and updates these. Audio
        # utterance loops read them from their own threads — protected by
        # _speaking_lock. _last_s_speaker is the most-recent named speaker
        # on the system-audio leg; used to attribute [S] utterances even
        # when the DOM speaking indicator has already cleared by the time
        # Whisper finalizes (Whisper waits for silence, DOM clears sooner).
        self._speaking_lock = threading.Lock()
        self._speaking_participants: dict[str, str] = {}  # pid → name
        self._last_s_speaker: str = ""
        # Timeline of speaking events for interval-based attribution.
        # Each entry is (t, name, kind) where kind ∈ {"start", "stop"}.
        # _audio_utterance_loop attributes a Whisper segment by looking
        # up who was speaking at the segment's speech_start_time, not
        # who is speaking now — see _attribute_s_leg() and S234 spike
        # in debug/14_29_speaker_attribution_spike/. 512 entries ≈ 8min
        # of dense conversation, well past any plausible Whisper lag.
        self._speaking_history: deque[tuple[float, str, str]] = deque(maxlen=512)
        # Local runner's tile id, resolved at observer install time. The
        # JS observer skips this tile, but we also filter at drain time
        # in case a stale event slips through (e.g. tile DOM re-renders).
        self._local_participant_id: str = ""
        # Speaking-observer rescan cadence. The observer is installed once
        # at meeting entry, but new participants render new tiles after
        # that — without a rescan, late joiners never get an observer and
        # their speech falls through to _last_s_speaker (i.e. gets stamped
        # with whoever pipped most recently). Browser thread re-runs the
        # JS install no more than once per _SPEAKING_RESCAN_INTERVAL_S
        # to attach observers to any new tiles. JS is idempotent at the
        # per-tile level so this is cheap on the no-op case.
        self._last_speaking_rescan_at: float = 0.0

    # ------------------------------------------------------------------
    # MeetingConnector interface
    # ------------------------------------------------------------------

    def join(self, meeting_url):
        """Validate input, then spawn the browser thread that owns Playwright.

        Fire-and-forget: returns once the thread is started; failures
        and successes both surface via `self.join_status` (callers wait
        on `join_status.ready`). The previous design ran Playwright on
        the calling thread and raised SlipAttachError synchronously,
        which meant any off-thread caller hit greenlet errors.

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
                "tracked for a follow-up phase."
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
        self._slip_start_at = time.monotonic()
        self._meeting_entry_at = None
        # Pre-warm the whisper model in parallel with everything else the
        # join sequence does (Chrome eviction + launch + CDP attach + lobby
        # wait). The synchronous warm inside _start_audio_pipeline used to
        # gate the audio pipeline — and therefore the chat MutationObserver
        # install — for 3-20 seconds after meeting entry. Moving it here
        # means the model is usually already loaded by the time
        # _start_audio_pipeline runs, so listening latency drops to ~1s.
        self._whisper_warmup_thread = threading.Thread(
            target=self._warm_whisper,
            daemon=True,
            name="AttachAdapter-whisper-warm",
        )
        self._whisper_warmup_thread.start()
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
            # Three-way startup branch (verified by debug/14_30_cdp_reattach_spike):
            #   1. CDP alive AND ≥1 tab in Chrome → reuse. Skip launch;
            #      _find_or_open_meet_page below opens the meeting in a
            #      new tab inside the existing slip Chrome window. This
            #      preserves whatever other tabs the user opened during
            #      a previous meeting (looking something up, etc.).
            #   2. CDP alive AND 0 tabs (macOS menu-bar-only state) →
            #      evict + launch. Playwright's connect_over_cdp would
            #      fail with "Browser.setDownloadBehavior: Browser
            #      context management is not supported" otherwise. No
            #      user work to preserve in this state.
            #   3. CDP not alive → launch. Standard cold-start path.
            launch_needed = True
            if _cdp_endpoint_alive():
                pc = _cdp_page_count()
                if pc > 0:
                    log.info(
                        f"AttachAdapter: reusing existing slip Chrome ({pc} tab(s))"
                    )
                    launch_needed = False
                else:
                    log.info(
                        f"AttachAdapter: existing Chrome has {pc} tabs "
                        "(zero-context state) — evicting + relaunching"
                    )
                    _evict_other_chrome_on_cdp_port()
                    # Brief settle so the kernel releases the port before
                    # the new Chrome tries to bind.
                    time.sleep(0.5)
            if launch_needed:
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

            # Open the chat panel and install the MutationObserver
            # IMMEDIATELY after meeting entry — before audio pipeline,
            # before anything else. The observer's seed loop marks every
            # message already in the chat panel DOM as "already seen"
            # (so we don't re-reply to historical @claudes on rejoin), so
            # any user @-mention sent in the window between admission and
            # observer install is silently dropped. Pre-S221 that window
            # was 20s+ because the observer install was gated on audio
            # pipeline start (and whisper model load). Doing it here
            # collapses the window to ~300ms (chat-button click + textarea
            # render). read_chat still calls these defensively in case
            # the panel closes mid-meeting; both are idempotent.
            self._ensure_chat_open(self._page)
            self._install_chat_observer(self._page)
            self._install_speaking_observer(self._page)

            # Signal join success the moment the chat observer is watching.
            # ChatRunner blocks on join_status.ready before starting its
            # polling loop.
            js.signal_success()

            # Audio pipeline spawns off-thread so the browser thread can
            # enter the chat-queue processing loop immediately. The audio
            # path joins the whisper-warm thread (which can still be
            # mid-load) before spawning the helper subprocess; doing that
            # on the browser thread blocked _process_chat_queue, so even
            # though ChatRunner unblocked above, its read_chat() calls
            # piled up in _chat_queue waiting for the browser thread to
            # be free. S221 22:05 turn 1 showed poll_lag_ms=6573 from
            # exactly this. The audio-spawn thread is daemon so process
            # exit reaps it; _start_audio_pipeline checks _leave_event
            # before spawning the helper to avoid spawning into shutdown.
            threading.Thread(
                target=self._start_audio_pipeline,
                daemon=True,
                name="AttachAdapter-audio-spawn",
            ).start()

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
                    rejoin_btn = self._page.get_by_role("button", name="Rejoin")
                    if rejoin_btn.count() > 0 and rejoin_btn.first.is_visible():
                        log.info("AttachAdapter: user left the meeting — stopping audio")
                        self._leave_event.set()
                        break
                except Exception as e:
                    log.warning(f"AttachAdapter: liveness probe raised: {e}")
                    break
                # page.wait_for_timeout is the browser-thread-safe sleep —
                # it parks the greenlet without breaking sync_playwright's
                # event loop.
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

        May be called before OR after join(). The audio pipeline buffers
        utterances internally and delivers them through whichever
        callback is registered at the moment the utterance finalizes;
        late-bind is fine. Pass None to unregister.

        AttachAdapter's "captions" are local Whisper transcriptions of
        the helper's two PCM streams (system + mic), not Meet's caption
        DOM. Each call delivers one finalized utterance — no streaming
        partials. Slip wires this directly into MeetingRecord.
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
        user's own messages. Every meeting message goes through here —
        there is no unprefixed send path.
        """
        if not self._browser_alive.is_set():
            return None
        result_q: queue.Queue = queue.Queue()
        self._chat_queue.put(("send", message, result_q))
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

    def get_self_name(self):
        """Return the local user's Meet display name via the browser thread.

        Empty string if the scrape fails (browser dead, tile not yet
        rendered, Meet DOM shape changed). Callers degrade to a
        generic label.
        """
        if not self._browser_alive.is_set():
            return ""
        result_q: queue.Queue = queue.Queue()
        self._chat_queue.put(("self_name", None, result_q))
        try:
            return result_q.get(timeout=5) or ""
        except queue.Empty:
            return ""

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
        self._drain_speaking_queue(page)
        while True:
            try:
                cmd, args, result_q = self._chat_queue.get_nowait()
            except queue.Empty:
                return
            try:
                if cmd == "send":
                    result_q.put(self._do_send_chat(page, args))
                elif cmd == "read":
                    result_q.put(self._do_read_chat(page))
                elif cmd == "participant_count":
                    result_q.put(self._do_get_participant_count(page))
                elif cmd == "participant_names":
                    result_q.put(self._do_get_participant_names(page))
                elif cmd == "self_name":
                    result_q.put(self._do_get_self_name(page))
                else:
                    log.warning(f"AttachAdapter: unknown chat-queue command {cmd!r}")
                    result_q.put(None)
            except Exception as e:
                # Don't let a single command crash the browser session.
                # Surface the failure to the waiter via the sentinel
                # value its public wrapper expects on error.
                log.warning(f"AttachAdapter: chat-queue command {cmd!r} raised: {e}")
                fallback = [] if cmd in ("read", "participant_names") else (
                    0 if cmd == "participant_count"
                    else "" if cmd == "self_name"
                    else None
                )
                try:
                    result_q.put(fallback)
                except Exception:
                    pass

    def _do_send_chat(self, page, message):
        """Browser-thread send. Snapshot existing IDs, fill the textarea,
        send, poll every 50 ms (up to 1 s) for one new ID. Returns the
        new `data-message-id` or None on timeout (caller falls back to
        text-match dedup).

        The message is prefixed with self._reply_prefix (default
        '[🤖 Claude] ') so the room can distinguish claude's words from
        the user's own typing. Prefix lives in
        `bridges/claude.py:REPLY_PREFIX_SLIP`.
        """
        full_message = (
            f"{self._reply_prefix}{message}" if self._reply_prefix else message
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
        Slip is the only mode that hits this — the bot reads the user's
        own typing path, where Meet's optimistic placeholder fires
        before server confirmation; a separate-participant observer
        wouldn't see the placeholder at all.
        """
        # Read path never touches the panel state. The chat observer
        # survives close→reopen cycles (verified S224: the [data-panel-id]
        # container is the same node before and after a manual toggle,
        # and Meet keeps inserting div[data-message-id] into it while
        # the panel is hidden). So once installed at join, we just drain.
        self._install_chat_observer(page)
        try:
            messages = page.evaluate(DRAIN_CHAT_QUEUE_JS)
            # Stamp drain-time so chat_runner can attribute poll-lag (t_dom →
            # t_drained) separately from Python-side processing (t_drained →
            # turn dispatch). Same wall clock as JS Date.now(), in ms.
            t_drained_ms = int(time.time() * 1000)
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
                msg["t_drained"] = t_drained_ms
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

    def _do_get_self_name(self, page):
        try:
            return page.evaluate(GET_SELF_NAME_JS) or ""
        except Exception as e:
            log.warning(f"AttachAdapter: get_self_name failed: {e}")
            return ""

    def _install_speaking_observer(self, page):
        """Install the tile speaking-indicator MutationObserver. Browser thread only.

        The JS install is idempotent at the per-tile level: re-running it
        attaches observers to NEW tiles (late joiners) without touching
        existing ones. _maybe_rescan_speaking_observer re-invokes this on
        a cadence so late-arriving participants get wired up.
        """
        try:
            result = page.evaluate(INSTALL_SPEAKING_OBSERVER_JS) or {}
            total = result.get("total_observed", 0)
            added = result.get("added", 0)
            local_pid = result.get("local_pid", "") or ""
            self._local_participant_id = local_pid
            self._last_speaking_rescan_at = time.monotonic()
            log.info(
                f"AttachAdapter: speaking observer installed — "
                f"observing {total} remote tile(s) (added {added} this call); "
                f"local_pid={local_pid!r}"
            )
        except Exception as e:
            log.warning(f"AttachAdapter: speaking observer install failed: {e}")

    def _maybe_rescan_speaking_observer(self, page):
        """Re-run the install JS if the rescan interval has elapsed.

        Picks up late-joining participants by attaching observers to any
        new tiles. No-op on the JS side when nothing changed. Logs only
        when new tiles were actually wired up so the steady-state run
        doesn't spam the log.
        """
        now = time.monotonic()
        if now - self._last_speaking_rescan_at < _SPEAKING_RESCAN_INTERVAL_S:
            return
        self._last_speaking_rescan_at = now
        try:
            result = page.evaluate(INSTALL_SPEAKING_OBSERVER_JS) or {}
        except Exception as e:
            log.debug(f"AttachAdapter: speaking observer rescan raised: {e}")
            return
        added = result.get("added", 0)
        if added > 0:
            names = result.get("added_names") or []
            total = result.get("total_observed", 0)
            log.info(
                f"AttachAdapter: speaking observer rescan — "
                f"added {added} tile(s) ({names!r}); now observing {total}"
            )

    def _drain_speaking_queue(self, page):
        """Drain the DOM speaking-event queue and update _speaking_participants.

        Called from _process_chat_queue on the browser thread every ~200ms.
        Updates _last_s_speaker with the most-recent named speaker so
        _audio_utterance_loop can attribute [S] utterances even after the
        DOM indicator has cleared. Also runs the speaking-observer rescan
        on its own cadence so late-joining participants get wired up.
        """
        self._maybe_rescan_speaking_observer(page)
        try:
            events = page.evaluate(DRAIN_SPEAKING_QUEUE_JS) or []
        except Exception:
            return
        if not events:
            return
        with self._speaking_lock:
            for ev in events:
                pid = ev.get("participant_id") or ""
                name = (ev.get("name") or "").strip()
                speaking = ev.get("speaking", False)
                if not name:
                    continue
                # Belt-and-suspenders: the JS observer skips the local
                # tile, but a stale event could still reach Python if the
                # tile DOM re-rendered between install and observation.
                if pid and pid == self._local_participant_id:
                    continue
                # Use the event's own DOM timestamp (ms) when present —
                # it pre-dates the Python drain by 200ms+ and is the
                # correct anchor for chunk-start lookups.
                ev_t_ms = ev.get("t")
                ev_t = (ev_t_ms / 1000.0) if isinstance(ev_t_ms, (int, float)) else time.time()
                if speaking:
                    self._speaking_participants[pid] = name
                    self._last_s_speaker = name
                    self._speaking_history.append((ev_t, name, "start"))
                    log.debug(f"AttachAdapter: speaking start — {name!r}")
                else:
                    self._speaking_participants.pop(pid, None)
                    self._speaking_history.append((ev_t, name, "stop"))
                    log.debug(f"AttachAdapter: speaking stop  — {name!r}")

    def _attribute_s_leg(self, chunk_start: float, chunk_end: float, default: str) -> str:
        """Look up which named participant spoke during [chunk_start, chunk_end].

        Used by the S-leg utterance loop after Whisper finalizes a
        segment. Walks _speaking_history to reconstruct each speaker's
        [start, stop] intervals and returns the name with the largest
        overlap with the chunk window. Falls back to the most-recent
        speaker who stopped at or before chunk_start, then to default.

        Why this and not "who is speaking now": Whisper waits for
        silence before committing a segment, typically 300-1000ms after
        the speaker actually stopped. In back-to-back turns the *next*
        speaker has usually already grabbed the DOM speaking indicator
        by the time we attribute, so a snapshot-at-finalize read
        stamps the previous speaker's words with the new speaker's
        name. See debug/14_29_speaker_attribution_spike/.
        """
        with self._speaking_lock:
            events = list(self._speaking_history)
        # Reconstruct intervals from start/stop pairs. Still-open speakers
        # at the end of history get a sentinel +inf end (they're presumed
        # to still be speaking — which is fine for overlap math).
        open_starts: dict[str, float] = {}
        intervals: list[tuple[float, float, str]] = []
        for t, name, kind in events:
            if kind == "start":
                # If a previous start for this name never saw a stop, close
                # it at this new start (defensive — shouldn't happen, but
                # observer dedupe isn't perfect across rescans).
                if name in open_starts:
                    intervals.append((open_starts[name], t, name))
                open_starts[name] = t
            else:
                t0 = open_starts.pop(name, None)
                if t0 is not None:
                    intervals.append((t0, t, name))
        for name, t0 in open_starts.items():
            intervals.append((t0, float("inf"), name))

        best_name = ""
        best_overlap = 0.0
        for t0, t1, name in intervals:
            overlap = max(0.0, min(t1, chunk_end) - max(t0, chunk_start))
            if overlap > best_overlap:
                best_overlap = overlap
                best_name = name
        if best_name:
            return best_name
        # Fallback: most-recent stop at or before chunk_start.
        candidates = [(t1, name) for (t0, t1, name) in intervals if t1 <= chunk_start]
        if candidates:
            candidates.sort(reverse=True)
            return candidates[0][1]
        return default

    def leave(self):
        """Disconnect from CDP. Idempotent. Does NOT close slip Chrome.

        Signals the browser thread to exit and waits briefly for clean
        teardown. Audio pipeline shutdown + Playwright teardown happen
        inside the browser thread's finally block so all Playwright
        calls stay on the thread that owns them.

        Slip Chrome stays alive on purpose: the user may have opened
        their own tabs in it during the meeting (looking something up
        for the conversation), and `/operator:hangup` is meant to boot
        claude from the meeting — not end the meeting for the user. If
        the user closed the meeting tab manually, the other tabs they
        had open keep working. The next `operator slip` will reuse the
        same Chrome window (open the new meeting URL as a new tab) as
        long as at least one tab is still alive — see _browser_session
        for the reuse / zero-context branching.
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
        log.info("AttachAdapter: detached from slip Chrome (Chrome stays alive)")

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

        Polls every 1s. No timeout — lobby admission can take many minutes
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
                    self._meeting_entry_at = time.monotonic()
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
        """Open the chat panel if needed before sending. Uses textarea
        visibility as the live signal — if the textarea is visible, the
        panel is open and we leave it alone. Otherwise click the chat
        toggle. Drops a debug screenshot if the toggle can't be located.

        Don't be tempted to use the send-button `disabled` attribute as
        the predicate: Meet disables the send button whenever the
        textarea is empty (which is virtually always the case when we
        check), so it would falsely indicate "panel closed" while the
        panel is actually open — and the toggle click would then close
        an already-open panel. S224 footgun.

        Called at join time (to materialize the chat-message DOM so the
        observer can attach to a stable [data-panel-id] container) and
        before every send. Read path never calls this — the observer
        survives close→reopen and continues firing while the panel is
        hidden (verified S224).
        """
        try:
            textarea = page.locator('textarea[aria-label="Send a message"]')
            if textarea.count() > 0 and textarea.first.is_visible():
                return
        except Exception:
            pass
        try:
            chat_btn = page.get_by_role("button", name="Chat with everyone")
            chat_btn.wait_for(timeout=3000)
            chat_btn.click()
            log.info("AttachAdapter: clicked chat button — waiting for panel to render")
            page.locator('textarea[aria-label="Send a message"]').first.wait_for(
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
        """Inject the chat-panel MutationObserver."""
        if self._observer_installed:
            return
        try:
            page.evaluate(INSTALL_CHAT_OBSERVER_JS)
            attached = page.evaluate(OBSERVER_ATTACHED_CHECK_JS)
            if attached:
                self._observer_installed = True
                log.info("AttachAdapter: chat MutationObserver installed")
                # The observer is the first thing that lets operator notice
                # a new @mention. Log the two latencies a participant cares
                # about: (a) how long after the bot became visible in-call
                # (`_meeting_entry_at`) it can hear them, and (b) total
                # cold-start from /operator:slip firing.
                now = time.monotonic()
                parts = []
                if self._meeting_entry_at is not None:
                    parts.append(
                        f"ms_since_meeting_entry={int((now - self._meeting_entry_at) * 1000)}"
                    )
                if self._slip_start_at is not None:
                    parts.append(
                        f"ms_since_slip_start={int((now - self._slip_start_at) * 1000)}"
                    )
                if parts:
                    log.info(f"TIMING listening_ready {' '.join(parts)}")
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
        # path for the slip-Chrome-reuse case: existing Chrome with the
        # user's other tabs, no meeting tab yet. bring_to_front() so the
        # tab is foregrounded (matches the fresh-launch UX where
        # `open -na` brings Chrome to focus).
        log.info(f"AttachAdapter: no existing tab for {meeting_url} — opening new tab")
        try:
            page = self._browser.contexts[0].new_page()
            page.goto(meeting_url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.bring_to_front()
            except Exception as e:
                log.debug(f"AttachAdapter: bring_to_front non-fatal: {e}")
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

    def _warm_whisper(self) -> None:
        """Pre-load faster-whisper-large-v3-turbo ahead of meeting entry.

        Fired on a daemon thread at join() start so the cold model load
        (1-2s warm cache, up to ~100s on first run when ~1.5GB downloads
        from HuggingFace) runs in parallel with Chrome launch + lobby
        admission rather than gating the audio pipeline (and therefore
        the chat observer install) the moment the user admits the bot.
        Populates self._audio_processors; _start_audio_pipeline joins this
        thread and reuses whatever it finished loading.

        Best-effort: silent on non-mac, missing helper, or import failures
        — _start_audio_pipeline retries the warm synchronously in those
        cases (and surfaces the warning then).
        """
        if sys.platform != "darwin":
            return
        if _resolve_audio_helper() is None:
            return
        try:
            from _1_800_operator.pipeline.audio import AudioProcessor
        except ImportError:
            return
        try:
            log.info("AudioProcessor: warming faster-whisper-large-v3-turbo (async, one-time per process)…")
            self._audio_processors[_FRAME_TAG_SYSTEM] = AudioProcessor()
            self._audio_processors[_FRAME_TAG_MIC] = AudioProcessor()
        except Exception as e:
            log.warning(
                f"AttachAdapter: async whisper warm failed ({e}) — "
                f"_start_audio_pipeline will retry"
            )
            self._audio_processors.clear()

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
          processors['S']     --> _audio_utterance_loop("other")    --> caption_callback
          processors['M']     --> _audio_utterance_loop(<self-name>) --> caption_callback
                                                       (falls back to "user" if the
                                                        Meet self-tile scrape fails)
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

        # Wait for the async warm started in join(). If it succeeded,
        # self._audio_processors is already populated and we skip the
        # synchronous warm below. If it failed (or the warm thread was
        # never started — leave-before-join paths), fall through and warm
        # here.
        if self._whisper_warmup_thread is not None:
            self._whisper_warmup_thread.join(timeout=30)

        if not self._audio_processors:
            try:
                log.info("AudioProcessor: warming faster-whisper-large-v3-turbo (one-time per process)…")
                self._audio_processors[_FRAME_TAG_SYSTEM] = AudioProcessor()
                self._audio_processors[_FRAME_TAG_MIC] = AudioProcessor()
            except Exception as e:
                log.warning(f"AttachAdapter: AudioProcessor warmup failed ({e}) — chat-only mode")
                self._audio_processors.clear()
                return

        # Operator may have entered teardown while we were warming the
        # whisper model (cold load can take ~20s). Spawning the helper
        # after _leave_event is set would orphan a subprocess that
        # _stop_audio_pipeline can't see yet — bail before that happens.
        if self._leave_event.is_set():
            log.info("AttachAdapter: leave requested during audio warmup — skipping helper spawn")
            return

        # Debug WAV dumps: set OPERATOR_AUDIO_DEBUG=1 to write every utterance
        # as a WAV file before STT. Files land in /tmp/operator_audio_debug/S/
        # and /tmp/operator_audio_debug/M/.
        if os.environ.get("OPERATOR_AUDIO_DEBUG"):
            _debug_root = "/tmp/operator_audio_debug"
            for _tag, _proc in self._audio_processors.items():
                _tag_str = _tag.decode() if isinstance(_tag, bytes) else str(_tag)
                _proc.debug_dir = os.path.join(_debug_root, _tag_str)
                os.makedirs(_proc.debug_dir, exist_ok=True)
            log.info(f"AttachAdapter: OPERATOR_AUDIO_DEBUG → {_debug_root}")

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

        # Bring up the AEC3 cleaner so mic frames flow through it before
        # reaching the M-leg AudioProcessor. Best-effort: if the binary
        # isn't installed (step 5 hasn't landed) or fails to spawn, log
        # and continue without AEC — _audio_reader_loop routes mic frames
        # directly to m_proc and the M transcript will then include
        # speaker bleed (no bleed defense in the fallback path).
        m_proc = self._audio_processors.get(_FRAME_TAG_MIC)
        aec_binary = _resolve_aec_binary()
        if aec_binary is not None and m_proc is not None:
            try:
                from _1_800_operator.pipeline.aec_cleaner import AecCleaner
                self._aec_cleaner = AecCleaner(
                    binary_path=aec_binary,
                    on_clean_mic=m_proc.feed_audio,
                )
                self._aec_cleaner.start()
                log.info(f"AttachAdapter: AEC cleaner up ({aec_binary})")
            except Exception as e:
                log.warning(f"AttachAdapter: AEC cleaner start failed ({e}) — no bleed defense")
                self._aec_cleaner = None
        else:
            if aec_binary is None:
                log.info("AttachAdapter: aec3 binary not found — running without bleed defense")

        reader = threading.Thread(
            target=self._audio_reader_loop,
            name="AttachAdapter-audio-reader",
            daemon=True,
        )
        reader.start()
        self._audio_threads.append(reader)

        # Resolve the mic-leg speaker label by scraping the local user's
        # Meet display name from the self tile. Best-effort; falls back
        # to a generic "user" string when the scrape returns empty (slip
        # hasn't fully rendered the participant panel yet, Meet DOM
        # shifted, etc.). Done here rather than at join() because the
        # self tile reliably exists once we're past meeting-entry and
        # the audio pipeline is what consumes the value.
        mic_label = self.get_self_name() or _SPEAKER_USER_FALLBACK
        log.info(f"AttachAdapter: mic-leg speaker label = {mic_label!r}")

        for tag, label in (
            (_FRAME_TAG_SYSTEM, _SPEAKER_OTHER),
            (_FRAME_TAG_MIC, mic_label),
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
                aec = self._aec_cleaner
                if tag == _FRAME_TAG_SYSTEM:
                    # [S] always feeds the system-audio processor (whisper
                    # path for remote-participant transcripts). If AEC is
                    # alive it ALSO feeds the render side so the cleaner
                    # has the reference signal it needs to cancel bleed.
                    s_proc = self._audio_processors.get(_FRAME_TAG_SYSTEM)
                    if s_proc is not None:
                        s_proc.feed_audio(pcm)
                    if aec is not None:
                        aec.feed_render(pcm)
                elif tag == _FRAME_TAG_MIC:
                    # [M] goes through AEC when up; otherwise straight to
                    # the mic-leg processor (pre-AEC fallback). AEC's own
                    # on_clean_mic callback hands cleaned bytes back to
                    # m_proc.feed_audio, so the downstream whisper path
                    # is unchanged in shape.
                    if aec is not None:
                        aec.feed_capture(pcm)
                    else:
                        m_proc = self._audio_processors.get(_FRAME_TAG_MIC)
                        if m_proc is not None:
                            m_proc.feed_audio(pcm)
                else:
                    log.warning(f"AttachAdapter: unknown frame tag {tag!r} — dropping {length}B")
                    continue
        except Exception as e:
            log.warning(f"AttachAdapter: audio reader loop crashed: {e}")

    @staticmethod
    def _normalize_for_dedupe(text: str) -> str:
        """Lowercase, strip punctuation, collapse whitespace.

        Makes the SequenceMatcher comparison robust to minor whisper drift
        like trailing periods, capitalization, doubled spaces — without
        affecting what's actually delivered to the caption callback.
        """
        return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", text.lower())).strip()

    def _is_recent_s_caption(self, text: str) -> bool:
        """True if `text` fuzzy-matches an S-leg caption from the last few seconds."""
        needle = self._normalize_for_dedupe(text)
        if not needle:
            return False
        now = time.time()
        window = config.BLEED_DEDUPE_WINDOW_SECONDS
        threshold = config.BLEED_DEDUPE_SIMILARITY
        with self._recent_s_captions_lock:
            # Drop stale entries lazily so the deque doesn't fill with junk.
            while self._recent_s_captions and now - self._recent_s_captions[0][0] > window:
                self._recent_s_captions.popleft()
            candidates = [n for _, n in self._recent_s_captions]
        for c in candidates:
            if SequenceMatcher(None, needle, c).ratio() >= threshold:
                return True
        return False

    def _record_s_caption(self, text: str) -> None:
        """Add an S-leg caption to the rolling dedupe buffer."""
        normalized = self._normalize_for_dedupe(text)
        if not normalized:
            return
        with self._recent_s_captions_lock:
            self._recent_s_captions.append((time.time(), normalized))

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
                text, speech_start_time = proc.capture_next_utterance()
            except Exception as e:
                log.warning(f"AttachAdapter: utterance loop ({speaker_label}) raised: {e}")
                continue
            if not text:
                continue
            cb = self._caption_callback
            if cb is None:
                log.debug(f"AttachAdapter: utterance dropped (no callback) [{speaker_label}] {text!r}")
                continue
            # For the system-audio leg, attribute via the speaking-event
            # timeline using the chunk's actual speech window — NOT a
            # snapshot at finalize time. See _attribute_s_leg().
            effective_label = speaker_label
            if tag == _FRAME_TAG_SYSTEM and speech_start_time is not None:
                effective_label = self._attribute_s_leg(
                    chunk_start=speech_start_time,
                    chunk_end=time.time(),
                    default=speaker_label,
                )
            # Bleed dedupe: if this is the M leg and the same text just
            # came through the S leg, drop it as residual speaker bleed.
            if tag == _FRAME_TAG_MIC and self._is_recent_s_caption(text):
                log.info(
                    f"AttachAdapter: dropped M-leg caption (S-leg dedupe) {text!r}"
                )
                continue
            try:
                cb(effective_label, text, time.time())
            except Exception as e:
                log.warning(f"AttachAdapter: caption callback raised: {e}")
            # Record S-leg captions AFTER the callback so the dedupe lookup
            # by a near-simultaneous M-leg utterance sees this entry.
            if tag == _FRAME_TAG_SYSTEM:
                self._record_s_caption(text)

    def _stop_audio_pipeline(self) -> None:
        """Tear down the audio pipeline. Idempotent.

        Order matters: flip capturing=False so the utterance loops exit
        their next tick, set the stop event so the reader breaks out,
        then close helper stdin (which the helper watches for EOF and
        exits on). SIGTERM + a short wait is the fallback.
        """
        if (
            self._audio_helper_proc is None
            and not self._audio_processors
            and self._aec_cleaner is None
        ):
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
        # Helper is gone (or being killed) — the reader loop will EOF on
        # its next read. Stop the AEC cleaner only AFTER that path is
        # quiet so we don't drop frames the reader was still forwarding.
        # Any cleaned frames emitted during the EOF drain are pushed into
        # m_proc.feed_audio (no-ops past this point since capturing is
        # already False and the M utterance loop has exited).
        if self._aec_cleaner is not None:
            try:
                self._aec_cleaner.stop()
            except Exception as e:
                log.debug(f"AttachAdapter: AEC cleaner stop raised: {e}")
            self._aec_cleaner = None
        for t in self._audio_threads:
            t.join(timeout=1.5)
        self._audio_threads.clear()
        self._audio_processors.clear()
        log.info("AttachAdapter: audio pipeline torn down")
