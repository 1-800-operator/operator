"""
CDP-attach connector for `operator dial` mode.

Dial launches a SEPARATE Chrome window under operator's own profile dir
(~/.operator/dial_profile/), opens the meeting URL there, and CDP-attaches
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
    - Dial Chrome is a dedicated meeting window — different from main
      browser. User signs into Google in this profile once (operator's
      own first-run flow); cookies persist across dial sessions.
    - Meeting joins as the user (same Google identity), so the room
      sees one participant entry "User Name". claude posts chat with
      a marker prefix so user vs. claude is distinguishable.
    - User must run dial BEFORE joining the meeting in main Chrome —
      otherwise the same identity is in the meeting twice. JIT
      preflights / friendly notices handle this.

Lifecycle:
    1. Probe CDP — if dial Chrome is still running with ≥1 tab, reuse
       it (preserves the user's other tabs). If Chrome is in the macOS
       menu-bar-only state (0 tabs), Playwright re-attach would fail
       on Browser.setDownloadBehavior, so evict + relaunch.
    2. Otherwise launch Chrome with --user-data-dir=DIAL_PROFILE_DIR,
       --remote-debugging-port=9222, and the meeting URL via `open -na`
    3. Wait for CDP endpoint
    4. `playwright.chromium.connect_over_cdp("http://localhost:9222")`
    5. Find or open the Meet tab (strict room-code match) — opens a
       new tab in the existing dial Chrome window on the reuse path
    6. Wait for the user to click 'Join now' (indefinite poll)
    7. Hand back to ChatRunner
    8. On leave(): disconnect CDP only — dial Chrome stays running so
       the user can stay in the meeting after claude detaches and
       keep working in any other tabs they opened.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import secrets
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from .cdp_ws import CDPError, CDPTarget

from _1_800_operator import config

from .base import MeetingConnector
from .chat_dom_js import (
    DRAIN_CHAT_QUEUE_JS,
    DRAIN_GCHAT_QUEUE_JS,
    DRAIN_SPEAKING_QUEUE_JS,
    GCHAT_CLICK_SEND_JS,
    GCHAT_INSERT_JS,
    GET_PARTICIPANT_NAMES_JS,
    GET_SELF_NAME_JS,
    INSTALL_CHAT_OBSERVER_JS,
    INSTALL_GCHAT_OBSERVER_JS,
    INSTALL_SPEAKING_OBSERVER_JS,
    OBSERVER_ATTACHED_CHECK_JS,
    SNAPSHOT_MESSAGE_IDS_JS,
)

# A Meet attached to a Google Chat space renders chat inside a cross-origin
# chat.google.com iframe rather than the in-page [data-panel-id] panel. We
# detect that frame by this URL marker and install the gchat observer into
# it instead of the classic page observer.
_GCHAT_FRAME_MARKER = "chat.google.com"
from .session import JoinStatus, save_debug, _is_real_meet_room


log = logging.getLogger(__name__)

CDP_PORT = 9222
CDP_URL = f"http://localhost:{CDP_PORT}"
CHROME_BINARY_MACOS = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
# Operator-owned dial profile — never touches the user's main Chrome.
# Stays signed in across dial sessions (cookies / Google session
# persist on disk like any Chrome profile dir). First-run sign-in is
# handled by _run_dial in __main__.py.
DIAL_PROFILE_DIR = os.path.expanduser("~/.operator/dial_profile")
# Per-Chrome-instance nonce that gates which Origin header Chrome accepts
# on CDP WebSocket upgrades. Written when Chrome is freshly launched, read
# when reusing a running dial Chrome (S239 reuse path). Lives inside the
# dial profile dir so it's tied to that Chrome instance's lifetime.
#
# SECURITY: this replaces the previous --remote-allow-origins=* (which
# accepted ANY Origin, letting any webpage the user visited mount a
# cross-origin attack against the dial Chrome holding their Google
# session). With a 128-bit random nonce, a webpage can't guess the
# expected Origin. Residual: a same-uid local process can read this file;
# accepted, since same-uid attackers already have broad capability.
CDP_ORIGIN_FILE = os.path.join(DIAL_PROFILE_DIR, ".cdp_origin")

# Developer-only audio-debug toggle. When True, every utterance gets
# written as a WAV under ~/.operator/debug/audio/. Devs flip this in their
# working copy when iterating on audio; it must stay False on main so
# end-user installs never accidentally persist raw meeting audio to disk.
_AUDIO_DEBUG_WAV = False
# Chrome can take 20+s to bring up the debug server on a profile with
# extensions or syncing data. 30s is generous; failure beyond that
# points at a real problem (port collision, Chrome crash, OS issue).
CDP_READY_TIMEOUT_SECONDS = 30

# The Operator audio helper lives at one of two paths. Production is the
# signed+notarized .app produced by scripts/build_signed_helper.sh — only
# this path can capture system audio (the Core Audio Tap TCC service binds
# to the helper's code-signature identity; ad-hoc signatures aren't stable
# across rebuilds and the user would re-prompt every time). Dev fallback is
# the raw swiftc-built artifact in-tree, used for mic-only iteration when
# no Developer-ID cert is available. Production wins when both exist;
# mirrors doctor.py:_AUDIO_HELPER_INSTALLED.
_AUDIO_HELPER_INSTALLED = (
    Path.home() / ".operator" / "bin" / "Operator.app"
    / "Contents" / "MacOS" / "Operator"
)
_AUDIO_HELPER_DEV = Path(__file__).resolve().parent.parent / "swift" / "Operator"

# AEC3 cleaner binary (S225 spike → step 5 will land a proper install). Same
# resolution pattern as the audio helper: production install wins over the
# in-tree dev build. None means AEC is unavailable — dial falls back to
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
_FRAME_TAG_EVENT = b"E"  # control event payload (UTF-8 JSON) for whisper_worker stdin
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

# Worker-respawn circuit breaker. After _RESPAWN_BREAKER_THRESHOLD
# respawn attempts inside _RESPAWN_BREAKER_WINDOW_S, give up for the
# remainder of the meeting. Sized for "transient crash deserves a few
# tries, permanent crash should stop fast" — a healthy worker that dies
# once gets respawned cleanly; a broken-on-startup worker only burns 3
# spawn cycles before the breaker trips.
_RESPAWN_BREAKER_THRESHOLD = 3
_RESPAWN_BREAKER_WINDOW_S = 10.0


class DialAttachError(RuntimeError):
    """Raised when the dial-mode attach lifecycle fails fatally.

    Caught by _run_dial and presented to the user as a clean stderr
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
    """Return the path to the Operator audio helper, or None if missing.

    Production install (~/.operator/bin/) wins over in-tree dev build
    when both exist. None means audio capture is unavailable; AttachAdapter
    skips spawning and runs in chat-only mode (warning logged, no crash —
    audio is an enhancement, not a hard requirement for dial).
    """
    if _AUDIO_HELPER_INSTALLED.exists() and os.access(_AUDIO_HELPER_INSTALLED, os.X_OK):
        return _AUDIO_HELPER_INSTALLED
    if _AUDIO_HELPER_DEV.exists() and os.access(_AUDIO_HELPER_DEV, os.X_OK):
        return _AUDIO_HELPER_DEV
    return None


def _chrome_user_data_dir_on_cdp_port() -> str | None:
    """Return the --user-data-dir of the Chrome process listening on
    CDP_PORT, or None if we can't determine it.

    Used by _browser_session's reuse path to verify the Chrome on 9222
    is OUR dial Chrome before attaching. The Origin-nonce check (security
    C-1) catches foreign Chromes with default Origin lockdown — they
    refuse the WebSocket upgrade. The residual is a foreign Chrome
    launched with --remote-allow-origins=* (a developer's Puppeteer dev
    session, another LLM browser tool), which accepts our Origin header
    and would let us silently attach + open the meeting URL in the wrong
    profile (different Google identity, no dial cookies). Verifying the
    process's --user-data-dir matches DIAL_PROFILE_DIR closes that.

    Best-effort: returns None on lsof/ps failure or if --user-data-dir
    isn't found in argv. Caller treats None as "foreign / unknown" and
    evicts.
    """
    try:
        r = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{CDP_PORT}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        pids = [int(p) for p in r.stdout.split() if p.strip().isdigit()]
    except Exception:
        return None
    for pid in pids:
        try:
            ps_r = subprocess.run(
                ["ps", "-o", "command=", "-p", str(pid)],
                capture_output=True, text=True, timeout=2,
            )
            command = ps_r.stdout.strip()
        except Exception:
            continue
        # We launch Chrome with --user-data-dir=<path> (equals form, see
        # _launch_dial_chrome). A foreign Chrome that launched with
        # space-separated form (--user-data-dir <path>) won't match here
        # — caller treats unknown as foreign and evicts, which is
        # exactly what we want.
        for token in command.split():
            if token.startswith("--user-data-dir="):
                return token[len("--user-data-dir="):]
    return None


def _pid_still_owns_port(pid: int, port: int) -> bool:
    """True iff `pid` still has a listening TCP socket on `port`.

    Used to close a TOCTOU window in the eviction path: between the
    initial lsof that named the PID and the kill that targets it, the
    process can exit and the kernel can recycle the PID to an unrelated
    same-uid process. Re-asking lsof "does PID still hold this port?"
    closes the window — if False, we don't kill (the original Chrome
    is already gone; whatever PID we'd kill now isn't ours to kill).
    """
    try:
        r = subprocess.run(
            ["lsof", "-nP", "-p", str(pid), f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=2,
        )
    except (subprocess.TimeoutExpired, OSError):
        # Can't verify → fail safe (don't kill).
        return False
    if r.returncode != 0:
        return False
    return str(pid) in r.stdout.split()


def _evict_other_chrome_on_cdp_port() -> bool:
    """Kill any Chrome process holding CDP_PORT.

    dial always launches a fresh Chrome on --remote-debugging-port=9222,
    so anything already on that port must go: a leftover dial Chrome
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
            # Re-verify the PID still holds CDP_PORT immediately before
            # we kill. Closes a TOCTOU window between the lsof above and
            # this kill — the original PID can exit between the two and
            # the kernel can recycle it to an unrelated same-uid process.
            # Without this check, a same-uid attacker spawning short-lived
            # workers could race us into SIGKILLing arbitrary processes
            # of their choosing on every operator dial.
            if not _pid_still_owns_port(pid, CDP_PORT):
                log.warning(
                    f"AttachAdapter: pid {pid} no longer holds port "
                    f"{CDP_PORT} (likely exited + PID recycled) — not "
                    f"evicting"
                )
                continue
            ps_r = subprocess.run(
                ["ps", "-o", "command=", "-p", str(pid)],
                capture_output=True, text=True, timeout=2,
            )
            command = ps_r.stdout.strip()
            if "Google Chrome" not in command:
                # Whatever's on 9222 isn't Chrome — leave it alone, dial
                # will fail downstream with a clearer launch error.
                log.warning(
                    f"AttachAdapter: pid {pid} on port {CDP_PORT} is not "
                    f"Chrome ({command[:80]!r}) — not evicting"
                )
                continue
            log.info(f"AttachAdapter: evicting Chrome on port {CDP_PORT} pid={pid}")
            os.kill(pid, 15)  # SIGTERM
            # Dial Chrome dies in ~100ms on SIGTERM (empirically measured
            # in debug/eviction_spike). 500ms is 5× headroom; if it
            # doesn't die in that, escalate to SIGKILL. Modern Chrome
            # cleanly reclaims its own stale SingletonLock on next
            # launch (spike-verified) so no profile cleanup needed.
            for _ in range(10):  # 10 × 50ms = 500ms
                try:
                    os.kill(pid, 0)  # check if alive
                    time.sleep(0.05)
                except OSError:
                    evicted_any = True
                    break
            else:
                # Before escalating to SIGKILL, re-verify once more —
                # the process could've exited + recycled during the wait.
                # Same TOCTOU shape as above.
                if not _pid_still_owns_port(pid, CDP_PORT):
                    log.info(
                        f"AttachAdapter: pid {pid} released port "
                        f"{CDP_PORT} during SIGTERM wait — not escalating"
                    )
                    evicted_any = True
                    continue
                try:
                    os.kill(pid, 9)  # SIGKILL
                    log.warning(
                        f"AttachAdapter: Chrome pid={pid} didn't exit on "
                        f"SIGTERM in 500ms — SIGKILL'd. May have left stale "
                        f"state in {DIAL_PROFILE_DIR}; Chrome usually "
                        f"recovers on next launch."
                    )
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

    Used by _browser_session to decide between reuse (existing dial
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
    and we can reuse the existing dial Chrome (preserving any user tabs);
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


def _new_cdp_origin() -> str:
    """Generate a fresh 128-bit-random Origin URL used to gate CDP access.

    The string is opaque to humans — its only job is to be unguessable
    by a webpage trying to mount a cross-origin CDP attack against the
    dial Chrome holding the user's Google session.
    """
    return f"http://operator-{secrets.token_hex(16)}.local"


def _write_cdp_origin(origin: str) -> None:
    """Persist a freshly-generated origin for later reuse by sibling
    operator processes that attach to the same Chrome instance."""
    os.makedirs(DIAL_PROFILE_DIR, exist_ok=True, mode=0o700)
    # Owner-only — the file IS the secret that gates CDP access.
    fd = os.open(
        CDP_ORIGIN_FILE,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        os.write(fd, origin.encode("utf-8"))
    finally:
        os.close(fd)
    # Belt-and-suspenders against pre-existing loose perms (write above
    # only applies at create time; if the file existed, perms persist).
    os.chmod(CDP_ORIGIN_FILE, 0o600)


def _read_cdp_origin() -> str | None:
    """Read the persisted origin for the currently-running dial Chrome,
    or None if it doesn't exist / is unreadable. Caller decides whether
    to fall back to generating a new one."""
    try:
        with open(CDP_ORIGIN_FILE, "rb") as f:
            data = f.read().strip()
    except (FileNotFoundError, OSError):
        return None
    if not data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _launch_dial_chrome(meeting_url: str, cdp_origin: str) -> subprocess.Popen:
    """Spawn dial's dedicated Chrome window with debug port + meeting URL.

    Uses `open -na 'Google Chrome' --args ...` (macOS-canonical pattern;
    `-n` forces a new instance, `--args` propagates flags reliably).
    The user-data-dir is operator-owned (DIAL_PROFILE_DIR), separate
    from the user's main Chrome profile — sidesteps Chrome's silent
    debug-port disable for the default profile.

    `cdp_origin` is the per-launch random Origin URL Chrome will accept
    on CDP WebSocket upgrades. Playwright must connect with the same
    Origin header. Caller (`_browser_session`) generates + persists it
    via _new_cdp_origin / _write_cdp_origin before invoking us.

    Returns the Popen handle of the `open` command itself, which exits
    after dispatching. The actual Chrome process is owned by
    LaunchServices.

    First-run behavior: if DIAL_PROFILE_DIR doesn't exist yet, Chrome
    creates it on launch. The user lands on the meeting URL, will see
    Google's sign-in prompt (dial profile has no cookies yet), can
    sign in once, and the profile persists for future runs.
    """
    if not os.path.exists(CHROME_BINARY_MACOS):
        raise DialAttachError(
            f"Could not find Google Chrome at {CHROME_BINARY_MACOS!r}. "
            "Install Chrome from https://www.google.com/chrome/ and re-run."
        )
    # mode= on makedirs only fires at creation; chmod is the belt for the
    # case where the dir already exists with looser perms. The dial profile
    # holds Google session cookies — owner-only matters on shared hosts.
    os.makedirs(DIAL_PROFILE_DIR, exist_ok=True, mode=0o700)
    os.chmod(DIAL_PROFILE_DIR, 0o700)
    args = [
        "open", "-na", "Google Chrome", "--args",
        f"--remote-debugging-port={CDP_PORT}",
        # Chrome 111+ requires --remote-allow-origins to permit CDP
        # WebSocket upgrades. Previously this was `*` — that lets every
        # webpage the user visits mount a cross-origin attack against
        # the dial Chrome's Google session cookies. We now pin to a
        # per-launch random Origin URL; Playwright sends the matching
        # Origin header when it connects.
        f"--remote-allow-origins={cdp_origin}",
        f"--user-data-dir={DIAL_PROFILE_DIR}",
        # Silence first-run / default-browser nags so dial Chrome lands
        # the user directly on the meeting URL.
        "--no-first-run",
        "--no-default-browser-check",
        # Pin Meet UI language to en-US regardless of the dial Chrome's
        # Google account locale. Many of operator's DOM selectors match
        # against English aria-labels ("Leave call", "Chat with everyone",
        # "Send a message", "Reframe", "Backgrounds and effects" — the
        # local-tile predicate), and those silently mis-fire on
        # non-English locales. Single-line lockdown beats fixing each
        # selector for every locale.
        "--lang=en-US",
        meeting_url,
    ]
    log.info(f"AttachAdapter: launching dial Chrome via: {' '.join(args)}")
    return subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _wait_for_cdp_ready(timeout_seconds: int = CDP_READY_TIMEOUT_SECONDS) -> None:
    """Block until the CDP endpoint accepts a TCP connection.

    Chrome publishes the debugging port shortly after process launch.
    Polling at 100ms beats a fixed sleep. Raises DialAttachError on
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
    raise DialAttachError(
        "Dial Chrome didn't come up in time. Try running dial again."
    )


class AttachAdapter(MeetingConnector):
    """MeetingConnector for dial mode — CDP-attached to dial's dedicated
    Chrome window.

    Each dial session launches Chrome with --user-data-dir=DIAL_PROFILE_DIR
    and CDP-attaches via Playwright. The user's main Chrome is never
    touched. Re-running dial while a dial Chrome is still alive (the
    user just hit Ctrl+C and is firing it back up) reuses the existing
    Chrome via the CDP probe — no second window, no relaunch.

    First-run: DIAL_PROFILE_DIR is created on launch; Chrome lands the
    user on the meeting URL with no Google session. They sign in once,
    cookies persist, and subsequent dial runs skip the sign-in.
    """

    def __init__(self, reply_prefix: str = "", jsonl_path: "str | Path | None" = None):
        super().__init__()
        self._reply_prefix = reply_prefix
        self._reply_prefix_re = self._compile_reply_prefix_re(reply_prefix)
        # Path to the meeting JSONL. When provided, audio captions land in
        # the JSONL via the whisper_worker subprocess (S244 — drain decoupled
        # from main shutdown). When None, falls back to the legacy in-process
        # AudioProcessor path (caption_callback delivers captions to the
        # caller). The fallback is exercised by tests + the Linux/no-helper
        # path where no audio is captured anyway.
        from pathlib import Path as _Path
        self._jsonl_path: _Path | None = _Path(jsonl_path) if jsonl_path else None
        self._audio_worker_proc: subprocess.Popen | None = None
        # Latest known attended/self_name snapshot — ChatRunner pushes
        # this every polling tick via update_pending_shutdown_payload(). On
        # the page-close path the browser thread tears down the audio
        # pipeline before __main__._shutdown can call send_audio_worker_shutdown,
        # so the seal event would race the EOF. Using a buffered "always
        # current" snapshot lets _stop_audio_pipeline send the event right
        # before closing worker stdin, guaranteeing it arrives.
        self._pending_shutdown_payload: "dict | None" = None
        # `_audio_worker_pid` is set at spawn time and NEVER cleared, even
        # after _stop_audio_pipeline drops the Popen handle. The shutdown
        # safety-net reaper queries this so it can exclude the worker from
        # its pgrep -P kill list — otherwise the worker (which legitimately
        # outlives main to drain residual audio) gets SIGTERM/SIGKILL'd
        # before it can write the JSONL seal. start_new_session=True only
        # changes the session/pgroup, not the parent PID, so pgrep -P still
        # sees the worker as a child.
        self._audio_worker_pid: int | None = None
        self._audio_worker_lock = threading.Lock()
        # Separate from _audio_worker_lock to avoid reentrancy when
        # _maybe_respawn_worker (called from _send_worker_frame) needs
        # to send mic_label + speaker-history replay events, each of
        # which goes through _send_worker_frame and acquires the write lock.
        self._audio_worker_respawn_lock = threading.Lock()
        self._audio_worker_shutdown_sent = False
        # Respawn circuit breaker — protects against respawn storms when the
        # worker crashes immediately on every spawn (broken module, missing
        # dep, etc.). Without this, ~100 audio frames/sec would each trigger
        # a fresh spawn-and-die cycle for the whole meeting. After
        # _RESPAWN_BREAKER_THRESHOLD attempts inside _RESPAWN_BREAKER_WINDOW_S,
        # disable further respawn and log loudly. Captions are lost for the
        # rest of the meeting, but the process stays sane.
        self._respawn_attempts: deque[float] = deque(maxlen=16)
        self._respawn_disabled = False
        self._playwright = None
        self._browser = None
        self._page = None
        self._chrome_proc = None
        self._observer_installed = False
        # Which chat surface the installed observer is watching:
        # "classic" (in-page Meet panel) or "iframe" (Google Chat space
        # embed). Set when the observer attaches; gates the drain target
        # and the spaces/-id placeholder filter in _do_read_chat.
        self._chat_surface = None
        # Direct CDP-target client for the Google Chat OOPIF (the space
        # embed). Playwright's connect_over_cdp doesn't stitch that
        # cross-origin iframe into page.frames, so we reach it by its own
        # target websocket instead (see cdp_ws.CDPTarget). Lazy-connected
        # on first iframe op, closed in _teardown_playwright.
        self._iframe_cdp: CDPTarget | None = None
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
        # Audio pipeline (S244): the main process owns the Swift audio
        # helper + AEC3 cleaner; the whisper_worker subprocess owns
        # AudioProcessor, speaker attribution, bleed dedupe, and caption
        # writes to the JSONL. _audio_helper_proc and _aec_cleaner are the
        # only audio state main retains. The whisper-worker fields are
        # initialized in the whisper_worker block above.
        self._audio_helper_proc: subprocess.Popen | None = None
        self._audio_threads: list[threading.Thread] = []
        self._audio_stop = threading.Event()
        self._aec_cleaner: "object | None" = None
        # Latency anchors for the TIMING listening_ready line:
        #   _dial_start_at    — set at join() entry (≈ when operator dial fired)
        #   _meeting_entry_at — set when the in-call DOM appears (≈ when
        #                       participants see operator in the meeting)
        # Both monotonic clocks; the deltas land on the observer-install log.
        self._dial_start_at: float | None = None
        self._meeting_entry_at: float | None = None
        # Speaking-indicator state. Browser thread drains the DOM speaking
        # queue every _process_chat_queue cycle and updates these.
        # _speaking_participants is the live "who is speaking right now"
        # set, written under _speaking_lock. Audio-leg attribution itself
        # happens inside whisper_worker, which holds its own timeline
        # replica fed by [E] events — main no longer needs a most-recent
        # speaker cache.
        self._speaking_lock = threading.Lock()
        self._speaking_participants: dict[str, str] = {}  # pid → name
        # Timeline of speaking events for interval-based attribution.
        # Each entry is (t, name, kind) where kind ∈ {"start", "stop"}.
        # Mirrored to the whisper_worker subprocess via [E] events so its
        # utterance loops can look up "who was speaking at this segment's
        # speech_start_time" rather than "who is speaking now" — Whisper
        # finalizes ~300-1000ms after the speaker stops, by which point
        # the DOM indicator may have moved to the next speaker. See
        # debug/14_29_speaker_attribution_spike/ for the original S234
        # spike. 512 entries ≈ 8min of dense conversation, well past any
        # plausible Whisper lag.
        self._speaking_history: deque[tuple[float, str, str]] = deque(maxlen=512)
        # Local runner's tile id, resolved at observer install time. The
        # JS observer skips this tile, but we also filter at drain time
        # in case a stale event slips through (e.g. tile DOM re-renders).
        self._local_participant_id: str = ""
        # Speaking-observer rescan cadence. The observer is installed once
        # at meeting entry, but new participants render new tiles after
        # that — without a rescan, late joiners never get an observer and
        # their speech goes unattributed. Browser thread re-runs the JS
        # install no more than once per _SPEAKING_RESCAN_INTERVAL_S to
        # attach observers to any new tiles. JS is idempotent at the
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
        the calling thread and raised DialAttachError synchronously,
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
            raise DialAttachError(
                "dial mode is currently macOS-only. Linux support is "
                "tracked for a follow-up phase."
            )
        if not meeting_url:
            js.signal_failure("missing_url")
            raise DialAttachError(
                "dial mode requires a meeting URL. Run "
                "`operator dial claude <https://meet.google.com/xxx-xxxx-xxx>`."
            )
        if not _is_real_meet_room(meeting_url):
            js.signal_failure("not_meet_room_url")
            raise DialAttachError(
                f"dial mode requires a Google Meet room URL like "
                f"`https://meet.google.com/abc-defg-hij`; got {meeting_url!r}."
            )

        self._leave_event.clear()
        self._browser_alive.clear()
        self._browser_closed.clear()
        self._observer_installed = False
        self._chat_surface = None
        self._close_iframe_cdp()
        self._dial_start_at = time.monotonic()
        self._meeting_entry_at = None
        # Silent-breakage detector for the speaking observer. Meet's
        # obfuscated "speaking" class (currently BlxGDf) rotates roughly
        # quarterly. When that happens, the observer silently fires
        # ZERO events and speaker attribution falls back to "Unknown"
        # without telling anyone. We log a one-time warning if zero
        # speaking events have been seen N minutes into a meeting that
        # has remote participants. Fires once per meeting, then suppresses.
        self._speaking_events_seen = 0
        self._speaking_breakage_warned = False
        # Optional forensic dump of per-tile DOM state at every speaker-observer
        # fire. Gated behind OPERATOR_DEBUG_SPEAKER_SNAPSHOTS=1 — off-path costs
        # zero. The JS observer always captures the snapshot (cheap, only walks
        # tiles); Python writes it to disk only when this flag is set. Use to
        # correlate misattribution incidents against a screen recording.
        self._speaker_snapshot_debug = os.environ.get(
            "OPERATOR_DEBUG_SPEAKER_SNAPSHOTS", ""
        ).strip() in ("1", "true", "yes")
        self._speaker_snapshot_path: Path | None = None
        if self._speaker_snapshot_debug and self._jsonl_path is not None:
            try:
                os.makedirs(config.DEBUG_DIR, exist_ok=True, mode=0o700)
                self._speaker_snapshot_path = (
                    Path(config.DEBUG_DIR)
                    / f"speaker_snapshots_{self._jsonl_path.stem}.jsonl"
                )
                log.info(
                    f"AttachAdapter: speaker snapshot debug ON → "
                    f"{self._speaker_snapshot_path}"
                )
            except Exception as e:
                log.warning(
                    f"AttachAdapter: speaker snapshot debug setup failed: {e}"
                )
                self._speaker_snapshot_path = None
        # S244: spawn the whisper_worker subprocess now so its model load
        # runs in parallel with Chrome launch + lobby wait. The spawn
        # itself is fire-and-forget (Popen returns in milliseconds); the
        # worker process loads faster-whisper-large-v3-turbo (~2s warm
        # cache, up to ~100s on first run with the 1.5GB download) on its
        # own. Frames pushed before warmup completes buffer in the worker
        # stdin pipe — no readiness wait needed in main.
        if sys.platform == "darwin" and _resolve_audio_helper() is not None:
            self._spawn_audio_worker()
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
        # Per-phase stamps for the TIMING browser_join line. Anchored on
        # entry to this thread, so subtracting `_dial_start_at` would
        # include a tiny dispatch lag (join() → thread start, usually <5ms).
        _t_bs_entry = time.monotonic()
        _t_after_cdp_probe = _t_bs_entry
        _t_after_chrome_launch = _t_bs_entry
        _t_after_playwright = _t_bs_entry
        _t_after_cdp_attach = _t_bs_entry
        _t_after_meet_tab = _t_bs_entry
        _launched = False
        try:
            # Three-way startup branch (verified by debug/14_30_cdp_reattach_spike):
            #   1. CDP alive AND ≥1 tab in Chrome → reuse. Skip launch;
            #      _find_or_open_meet_page below opens the meeting in a
            #      new tab inside the existing dial Chrome window. This
            #      preserves whatever other tabs the user opened during
            #      a previous meeting (looking something up, etc.).
            #   2. CDP alive AND 0 tabs (macOS menu-bar-only state) →
            #      evict + launch. Playwright's connect_over_cdp would
            #      fail with "Browser.setDownloadBehavior: Browser
            #      context management is not supported" otherwise. No
            #      user work to preserve in this state.
            #   3. CDP not alive → launch. Standard cold-start path.
            # Fine-grained timing for cdp_probe — when cdp_probe_ms balloons,
            # the cost is almost always eviction (SIGTERM + 2s wait loop +
            # 0.5s settle), not the cheap socket/page-count/uds checks.
            # Each slot starts at -1 (not stamped); the emit step joins
            # them in order, so a skipped phase contributes 0 to the
            # breakdown rather than smearing into the next.
            _t_socket_done = -1.0
            _t_page_count_done = -1.0
            _t_uds_done = -1.0
            _t_eviction_done = -1.0
            _t_settle_done = -1.0
            _evicted = False

            launch_needed = True
            if _cdp_endpoint_alive():
                _t_socket_done = time.monotonic()
                pc = _cdp_page_count()
                _t_page_count_done = time.monotonic()
                if pc > 0:
                    # Verify the Chrome on 9222 is OUR dial Chrome. The
                    # Origin-nonce check (security C-1) handles foreign
                    # Chromes with default Origin lockdown; checking
                    # --user-data-dir closes the residual where a foreign
                    # Chrome launched with --remote-allow-origins=* would
                    # accept our Origin header and let us silently attach
                    # in the wrong profile.
                    owner_uds = _chrome_user_data_dir_on_cdp_port()
                    _t_uds_done = time.monotonic()
                    is_dial = (
                        owner_uds is not None
                        and os.path.realpath(owner_uds)
                            == os.path.realpath(DIAL_PROFILE_DIR)
                    )
                    if is_dial:
                        log.info(
                            f"AttachAdapter: reusing existing dial Chrome "
                            f"({pc} tab(s))"
                        )
                        launch_needed = False
                    else:
                        log.warning(
                            f"AttachAdapter: Chrome on port {CDP_PORT} has "
                            f"user-data-dir={owner_uds!r} (expected "
                            f"{DIAL_PROFILE_DIR!s}) — foreign Chrome, "
                            "evicting + relaunching"
                        )
                        _evict_other_chrome_on_cdp_port()
                        _evicted = True
                        _t_eviction_done = time.monotonic()
                        # No fixed settle — port is free at Chrome death
                        # (spike-verified). _launch_dial_chrome + _wait_for_cdp_ready
                        # below will see the freed port immediately.
                        _t_settle_done = time.monotonic()
                else:
                    log.info(
                        f"AttachAdapter: existing Chrome has {pc} tabs "
                        "(zero-context state) — evicting + relaunching"
                    )
                    _evict_other_chrome_on_cdp_port()
                    _evicted = True
                    _t_eviction_done = time.monotonic()
                    _t_settle_done = time.monotonic()
            else:
                _t_socket_done = time.monotonic()
            _t_after_cdp_probe = time.monotonic()
            # Walk the stamps in order; for each unstamped slot, fall back
            # to the prior stamp so the field reads 0 instead of mangling
            # the next field's delta.
            def _ms(prev: float, cur: float) -> int:
                if cur < 0:
                    return 0
                return int((cur - prev) * 1000)
            _stamps = [_t_bs_entry]
            for _s in (_t_socket_done, _t_page_count_done, _t_uds_done,
                       _t_eviction_done, _t_settle_done):
                _stamps.append(_s if _s >= 0 else _stamps[-1])
            log.info(
                f"TIMING cdp_probe "
                f"socket_ms={_ms(_stamps[0], _t_socket_done)} "
                f"page_count_ms={_ms(_stamps[1], _t_page_count_done)} "
                f"uds_ms={_ms(_stamps[2], _t_uds_done)} "
                f"evict_ms={_ms(_stamps[3], _t_eviction_done)} "
                f"settle_ms={_ms(_stamps[4], _t_settle_done)} "
                f"evicted={_evicted}"
            )
            if launch_needed:
                _launched = True
                # Fresh Chrome → fresh CDP origin. The new nonce is what
                # Chrome will accept on the WebSocket upgrade; Playwright
                # must send it as the Origin header below.
                cdp_origin = _new_cdp_origin()
                _write_cdp_origin(cdp_origin)
                self._chrome_proc = _launch_dial_chrome(
                    meeting_url, cdp_origin
                )
                try:
                    _wait_for_cdp_ready()
                except DialAttachError:
                    js.signal_failure("cdp_not_ready")
                    return
            else:
                # Reuse path — Chrome was launched by a prior operator
                # session, which persisted its nonce to CDP_ORIGIN_FILE.
                # Read it; we need the same value as the Origin header.
                cdp_origin = _read_cdp_origin()
                if cdp_origin is None:
                    # File missing / unreadable. Can't connect without
                    # the matching Origin — abandon reuse, fall back to
                    # evict + fresh launch.
                    log.warning(
                        "AttachAdapter: CDP origin file missing for "
                        "reused Chrome — evicting + relaunching"
                    )
                    _evict_other_chrome_on_cdp_port()
                    cdp_origin = _new_cdp_origin()
                    _write_cdp_origin(cdp_origin)
                    self._chrome_proc = _launch_dial_chrome(
                        meeting_url, cdp_origin
                    )
                    try:
                        _wait_for_cdp_ready()
                    except DialAttachError:
                        js.signal_failure("cdp_not_ready")
                        return

            _t_after_chrome_launch = time.monotonic()
            self._playwright = sync_playwright().start()
            _t_after_playwright = time.monotonic()
            try:
                self._browser = self._playwright.chromium.connect_over_cdp(
                    CDP_URL,
                    headers={"Origin": cdp_origin},
                )
            except Exception as e:
                self._teardown_playwright()
                js.signal_failure("cdp_attach_failed")
                log.error(f"AttachAdapter: connect_over_cdp failed: {e}")
                return
            _t_after_cdp_attach = time.monotonic()

            self._page = self._find_or_open_meet_page(meeting_url)
            if self._page is None:
                self._teardown_playwright()
                js.signal_failure("meet_tab_open_failed")
                return
            _t_after_meet_tab = time.monotonic()
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
            _t_after_entry = time.monotonic()
            log.info(
                f"TIMING browser_join "
                f"cdp_probe_ms={int((_t_after_cdp_probe - _t_bs_entry) * 1000)} "
                f"chrome_launch_ms={int((_t_after_chrome_launch - _t_after_cdp_probe) * 1000)} "
                f"playwright_ms={int((_t_after_playwright - _t_after_chrome_launch) * 1000)} "
                f"cdp_attach_ms={int((_t_after_cdp_attach - _t_after_playwright) * 1000)} "
                f"meet_tab_ms={int((_t_after_meet_tab - _t_after_cdp_attach) * 1000)} "
                f"lobby_wait_ms={int((_t_after_entry - _t_after_meet_tab) * 1000)} "
                f"total_ms={int((_t_after_entry - _t_bs_entry) * 1000)} "
                f"launched={_launched}"
            )

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

    def send_chat(self, message):
        """Post a message to chat. Queues the request for the browser thread.

        Returns the new `data-message-id` from the post, or None on
        timeout / failure (caller falls back to text-match dedup).
        Returns None immediately when called before the browser thread
        is alive — same fallback shape.

        Dial-mode prefix-strip: prepends self._reply_prefix
        (`[🤖 Claude] ` per `bridges/claude.py:REPLY_PREFIX_DIAL`) so
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
        `bridges/claude.py:REPLY_PREFIX_DIAL`.
        """
        full_message = (
            f"{self._reply_prefix}{message}" if self._reply_prefix else message
        )
        # Google Chat space embed: send through the OOPIF's CDP target — the
        # in-page textarea path doesn't exist there. No data-message-id
        # readback (returns None → caller's text-match dedup handles the bot's
        # own message, same as the classic timeout fallback; the read path
        # strips self._reply_prefix so it isn't re-dispatched).
        if self._chat_surface == "iframe":
            if self._iframe_send(full_message):
                log.info(f"AttachAdapter: chat sent (iframe): {full_message!r}")
            else:
                log.warning(f"AttachAdapter: iframe send failed: {full_message!r}")
            return None
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

    @staticmethod
    def _compile_reply_prefix_re(prefix: str):
        """Build an emoji-tolerant regex matching the reply prefix at line start.

        send_chat prepends self._reply_prefix ('[🤖 Claude] '). The Google
        Chat iframe surface drops the 🤖 emoji when the DOM is read back, so
        the bot's own reply returns as '[ Claude] …'. A literal
        str.startswith(prefix) check misses that, the bot re-reads its own
        words as a fresh 'You' message, and an echo loop kicks off (observed
        live S250). Make each non-ASCII char in the prefix optional and runs
        of whitespace flexible so the match holds whether or not the emoji
        survived rendering. Returns None when there is no prefix.
        """
        if not prefix:
            return None
        parts = []
        for ch in prefix:
            if ch.isspace():
                parts.append(r"\s*")
            elif not ch.isascii():
                parts.append(re.escape(ch) + "?")
            else:
                parts.append(re.escape(ch))
        return re.compile("^" + "".join(parts))

    def _do_read_chat(self, page):
        """Browser-thread implementation. Drains the JS-side chat queue.

        Dial-mode prefix-strip: send_chat prepends self._reply_prefix
        (`[🤖 Claude] ` per `bridges/claude.py:REPLY_PREFIX_DIAL`) so the
        room can distinguish claude's words from the user's typing. The
        DOM observer reads back the prefixed text. ChatRunner's
        _own_messages dedup set stores the UN-prefixed text. Without
        normalization the text-match dedup misses, the bot's own
        messages get treated as new user input, and a self-reply
        cascade kicks off. Strip the prefix here so the text passed
        upstream matches what was added to _own_messages.

        Dial-only optimistic-ID filter: when the dial-mode user types a
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
        Dial is the only mode that hits this — the bot reads the user's
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
            # Drain from whichever surface the observer attached to. The
            # iframe queue lives on the frame's own window, so it's drained
            # via the frame, not the page.
            if self._chat_surface == "iframe":
                # Self-heal: the OOPIF is replaced with a fresh window when the
                # chat panel closes/reopens, dropping our observer. Re-install
                # if it's gone before draining so messages aren't silently lost.
                if self._iframe_evaluate(OBSERVER_ATTACHED_CHECK_JS) is not True:
                    self._iframe_evaluate(INSTALL_GCHAT_OBSERVER_JS)
                messages = self._iframe_evaluate(DRAIN_GCHAT_QUEUE_JS) or []
            else:
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
                # The spaces/ placeholder filter only applies to classic Meet
                # chat, where Meet emits an optimistic placeholder id before
                # the canonical spaces/... id. Google Chat iframe messages key
                # on data-topic-id (e.g. "MFivfrcBGcI") — a different
                # namespace with no placeholder phase — so don't drop them.
                if self._chat_surface != "iframe" and not mid.startswith("spaces/"):
                    log.debug(
                        f"AttachAdapter: dropping placeholder-id message "
                        f"id={mid!r} text={msg.get('text', '')[:40]!r} "
                        "(awaiting canonical)"
                    )
                    continue
                msg["t_drained"] = t_drained_ms
                filtered.append(msg)
            messages = filtered
            # Keep the bot's own replies out of the dispatch stream.
            # send_chat prepends self._reply_prefix; _reply_prefix_re matches
            # it emoji-tolerantly (the iframe surface drops the 🤖). The
            # iframe surface returns no data-message-id (see _do_send_chat),
            # so this prefix match is its ONLY echo defense — own replies are
            # dropped outright. The downstream text-match dedup can't save it:
            # Meet labels own messages 'You', and that fallback is gated on an
            # empty sender. The classic surface strips-and-forwards as before
            # (its message-id dedup is primary), now via the same regex so an
            # emoji-drop there can't reintroduce the loop.
            if self._reply_prefix_re and messages:
                kept = []
                for msg in messages:
                    m = self._reply_prefix_re.match(msg.get("text", "") or "")
                    if m:
                        if self._chat_surface == "iframe":
                            continue  # bot's own echo — drop it entirely
                        msg["text"] = msg["text"][m.end():]
                    kept.append(msg)
                messages = kept
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
        Each drained event is pushed to the whisper_worker subprocess
        (S244) as an [E] event for its in-worker S-leg attribution. Also
        runs the speaking-observer rescan on its own cadence so
        late-joining participants get wired up.
        """
        self._maybe_rescan_speaking_observer(page)
        try:
            events = page.evaluate(DRAIN_SPEAKING_QUEUE_JS) or []
        except Exception:
            return
        # Silent-breakage check: 10 min into the meeting, if we've still
        # seen ZERO speaking events, the obfuscated "speaking" class in
        # chat_dom_js.py:INSTALL_SPEAKING_OBSERVER_JS has likely been
        # rotated by a Meet release. Warn ONCE so the dev can update it.
        # Fires regardless of whether THIS poll had events — if events
        # have ever arrived, _speaking_events_seen > 0 and we skip.
        if (
            not self._speaking_breakage_warned
            and self._speaking_events_seen == 0
            and self._meeting_entry_at is not None
            and (time.monotonic() - self._meeting_entry_at) > 600
        ):
            log.warning(
                "AttachAdapter: 10+ min into meeting with ZERO speaking events — "
                "Meet's obfuscated speaking-indicator class may have rotated. "
                "Check chat_dom_js.py:INSTALL_SPEAKING_OBSERVER_JS (currently looks for "
                "class 'BlxGDf')."
            )
            self._speaking_breakage_warned = True
        if not events:
            return
        self._speaking_events_seen += len(events)
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
                if self._speaker_snapshot_path is not None:
                    try:
                        with self._speaker_snapshot_path.open("a") as f:
                            f.write(json.dumps({
                                "t": ev_t,
                                "event": {
                                    "participant_id": pid,
                                    "name": name,
                                    "speaking": speaking,
                                },
                                "local_pid": self._local_participant_id,
                                "snapshot": ev.get("snapshot") or [],
                            }) + "\n")
                    except Exception as e:
                        log.debug(
                            f"AttachAdapter: speaker snapshot write failed: {e}"
                        )
                if speaking:
                    self._speaking_participants[pid] = name
                    self._speaking_history.append((ev_t, name, "start"))
                    log.debug(f"AttachAdapter: speaking start — {name!r}")
                    # S244: mirror to the whisper_worker subprocess so its
                    # S-leg attribution lookup has the same timeline.
                    self._send_worker_event({"type": "speaker_start", "name": name, "t": ev_t})
                else:
                    self._speaking_participants.pop(pid, None)
                    self._speaking_history.append((ev_t, name, "stop"))
                    log.debug(f"AttachAdapter: speaking stop  — {name!r}")
                    self._send_worker_event({"type": "speaker_stop", "name": name, "t": ev_t})

    def leave(self):
        """Disconnect from CDP. Idempotent. Does NOT close dial Chrome.

        Signals the browser thread to exit and waits briefly for clean
        teardown. Audio pipeline shutdown + Playwright teardown happen
        inside the browser thread's finally block so all Playwright
        calls stay on the thread that owns them.

        Dial Chrome stays alive on purpose: the user may have opened
        their own tabs in it during the meeting (looking something up
        for the conversation), and `/operator:hangup` is meant to boot
        claude from the meeting — not end the meeting for the user. If
        the user closed the meeting tab manually, the other tabs they
        had open keep working. The next `operator dial` will reuse the
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
        log.info("AttachAdapter: detached from dial Chrome (Chrome stays alive)")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _wait_for_meeting_entry(self, page):
        """Block until the user has entered the meeting.

        Detects entry by the visibility of the 'Chat with everyone'
        button. Meet renders the in-call control bar (including 'Leave
        call') the moment the user clicks 'Ask to join', so 'Leave call'
        false-positives during the lobby wait. 'Chat with everyone' is
        the discriminator: it does NOT render in the green-room pre-join
        state, and it does NOT render in the lobby waiting state — only
        in the actual in-call DOM. Confirmed via DOM dumps of all three
        states (session 205 repro).

        Previously this was an AND of both buttons; the Leave-call check
        was redundant once chat-button presence proved we're in-call.
        Host-disabled-chat: the chat button still renders (verified
        S243), the input field is greyed at send-time — that's a clean
        send-time failure rather than an entry-detection hang.

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
        diag_30s_written = False
        wait_start = time.monotonic()
        while not self._leave_event.is_set():
            try:
                chat_btn = page.get_by_role("button", name="Chat with everyone")
                if chat_btn.count() > 0 and chat_btn.first.is_visible():
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
                    save_debug(page, label=f"lobby_exit_chrome_closed_{int(time.time())}")
                    return False
            except Exception:
                log.warning("AttachAdapter: liveness probe failed during meeting-entry wait")
                return False
            now = time.monotonic()
            if now - last_log > 30:
                log.info("AttachAdapter: still waiting for meeting entry…")
                last_log = now
            # Diagnostic: capture page state at the 30s mark of the wait.
            # If the user never gets admitted we'll be staring at this when
            # we want to know what screen the dial Chrome was actually on
            # (green room / lobby / sign-in / in-call but no chat button).
            # Written once per join — overwrite-safe (label includes unix ts).
            if not diag_30s_written and now - wait_start > 30:
                save_debug(page, label=f"lobby_wait_30s_{int(time.time())}")
                diag_30s_written = True
            time.sleep(1.0)
        # leave_event tripped while we were waiting for entry — caller
        # is shutting down before the user joined. Surface as a clean
        # not-entered signal so _browser_session takes the failure path
        # and tears Playwright down cleanly.
        log.info("AttachAdapter: leave requested before meeting entry")
        save_debug(page, label=f"lobby_exit_leave_requested_{int(time.time())}")
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

    def _discover_gchat_target_ws(self):
        """Return the chat.google.com OOPIF's CDP debugger ws URL, or None.

        Playwright's connect_over_cdp does NOT expose this cross-origin
        iframe in page.frames (verified S250), so we discover it from the
        browser's /json target list and talk to it over its own target
        websocket. Re-queried each call — the target id changes when the
        chat panel closes/reopens or the space view changes.
        """
        try:
            with urllib.request.urlopen(f"{CDP_URL}/json", timeout=2) as resp:
                targets = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None
        for t in targets:
            if t.get("type") == "iframe" and _GCHAT_FRAME_MARKER in (t.get("url") or ""):
                return t.get("webSocketDebuggerUrl")
        return None

    def _ensure_iframe_cdp(self):
        """Return a connected CDPTarget for the Google Chat OOPIF, or None.

        Discovers the target + connects + enables Runtime on first use. The
        target id rotates on panel close/reopen, so callers that hit a
        transport error should _close_iframe_cdp() and call again to
        re-discover.
        """
        if self._iframe_cdp is not None:
            return self._iframe_cdp
        ws_url = self._discover_gchat_target_ws()
        if not ws_url:
            return None
        try:
            cdp = CDPTarget(ws_url)
            cdp.connect()
            cdp.call("Runtime.enable")
        except CDPError as e:
            log.debug(f"AttachAdapter: iframe CDP connect failed: {e}")
            self._close_iframe_cdp()
            return None
        self._iframe_cdp = cdp
        return cdp

    def _iframe_evaluate(self, arrow_fn_src):
        """Evaluate JS in the Google Chat OOPIF via its CDP target websocket.

        On transport failure the connection is dropped + the target
        re-discovered + retried once. Returns the JS value, or None if the
        iframe can't be reached.
        """
        for _attempt in (1, 2):
            cdp = self._ensure_iframe_cdp()
            if cdp is None:
                return None
            try:
                return cdp.evaluate(arrow_fn_src)
            except CDPError as e:
                log.debug(f"AttachAdapter: iframe CDP evaluate failed: {e}")
                self._close_iframe_cdp()
        return None

    def _iframe_send(self, text):
        """Post `text` into the Google Chat OOPIF. Returns True on success.

        Insert via GCHAT_INSERT_JS (execCommand + InputEvent — preserves the
        emoji prefix and enables Google's Send button), then poll-click Send
        until it enables. Retries once on transport failure with a fresh
        connection.
        """
        for _attempt in (1, 2):
            cdp = self._ensure_iframe_cdp()
            if cdp is None:
                return False
            try:
                if cdp.evaluate(GCHAT_INSERT_JS, text) is not True:
                    return False
                # Send enables once the editor registers the inserted text.
                for _ in range(20):
                    if cdp.evaluate(GCHAT_CLICK_SEND_JS) is True:
                        return True
                    time.sleep(0.05)
                return False
            except CDPError as e:
                log.debug(f"AttachAdapter: iframe send failed: {e}")
                self._close_iframe_cdp()
        return False

    def _close_iframe_cdp(self):
        if self._iframe_cdp is not None:
            self._iframe_cdp.close()
            self._iframe_cdp = None

    def _install_chat_observer(self, page):
        """Inject the chat MutationObserver — classic panel or Chat iframe.

        Prefers the Google Chat iframe when the meeting is space-attached;
        falls back to the in-page Meet chat panel otherwise. Sets
        _chat_surface so the drain path knows which surface to read and
        whether the spaces/-id placeholder filter applies.
        """
        if self._observer_installed:
            return
        gchat_ws = self._discover_gchat_target_ws()
        try:
            if gchat_ws is not None:
                self._iframe_evaluate(INSTALL_GCHAT_OBSERVER_JS)
                attached = self._iframe_evaluate(OBSERVER_ATTACHED_CHECK_JS) is True
                surface, target_desc = "iframe", "Google Chat iframe observer"
            else:
                page.evaluate(INSTALL_CHAT_OBSERVER_JS)
                attached = page.evaluate(OBSERVER_ATTACHED_CHECK_JS)
                surface, target_desc = "classic", "chat MutationObserver"
            if attached:
                self._observer_installed = True
                self._chat_surface = surface
                log.info(f"AttachAdapter: {target_desc} installed")
                # The observer is the first thing that lets operator notice
                # a new @mention. Log the two latencies a participant cares
                # about: (a) how long after the bot became visible in-call
                # (`_meeting_entry_at`) it can hear them, and (b) total
                # cold-start from /operator:dial firing.
                now = time.monotonic()
                parts = []
                if self._meeting_entry_at is not None:
                    parts.append(
                        f"ms_since_meeting_entry={int((now - self._meeting_entry_at) * 1000)}"
                    )
                if self._dial_start_at is not None:
                    parts.append(
                        f"ms_since_dial_start={int((now - self._dial_start_at) * 1000)}"
                    )
                if parts:
                    log.info(f"TIMING listening_ready {' '.join(parts)}")
            elif gchat_ws is not None:
                log.warning("AttachAdapter: Google Chat iframe observer not attached (message list not in DOM yet) — will retry next poll")
            else:
                log.warning("AttachAdapter: chat observer not attached (textarea or panel container not in DOM) — will retry next poll")
        except Exception as e:
            log.warning(f"AttachAdapter: failed to install chat observer: {e}")

    def _find_or_open_meet_page(self, meeting_url):
        """Find an existing Meet tab for this exact room, or open a new one.

        Matches on the room-code segment (`xxx-yyyy-zzz`) appearing
        anywhere in the URL — not a strict path equality. The looser
        match survives Meet's transient URL states (auth bounces,
        `?authuser=N` query params, `/_meet/...` shells during sign-in
        redirects). Room codes are 10-char patterns specific enough that
        accidental matches against unrelated tabs are vanishingly
        unlikely. Falls back to the full URL path if no room-code is
        found in the meeting_url (defensive).

        Polls briefly for fresh-launch races (Chrome was just relaunched
        with the URL as argv — tab might lag CDP attach by a few hundred
        ms). If still not found, opens a new tab via Playwright. Covers
        both relaunch and attach-to-existing-debug-Chrome cases without
        a parallel code path.
        """
        # Meet room codes are 3-4-3 lowercase letter groups separated by
        # dashes (e.g. `aux-hdto-nen`). Use the FIRST match from the
        # meeting_url; if there's none (unusual URL shape), fall back to
        # full-path equality.
        room_code_match = re.search(r"\b[a-z]{3,4}-[a-z]{3,4}-[a-z]{3,4}\b", meeting_url)
        room_code = room_code_match.group(0) if room_code_match else None
        target_path_fallback = urlparse(meeting_url).path.rstrip('/')

        def _tab_matches(page_url):
            if room_code:
                return room_code in page_url
            return urlparse(page_url).path.rstrip('/') == target_path_fallback

        # Pass 1: scan for an existing match. Brief poll (~3s) handles
        # the post-relaunch race where Chrome's tab list hasn't
        # propagated to CDP yet.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            for context in self._browser.contexts:
                for page in context.pages:
                    try:
                        if _tab_matches(page.url):
                            log.info(f"AttachAdapter: found existing Meet tab at {page.url}")
                            return page
                    except Exception:
                        continue
            time.sleep(0.25)

        # Pass 2: not found — open a new tab with the URL. This is the
        # path for the dial-Chrome-reuse case: existing Chrome with the
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
        self._close_iframe_cdp()
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

    # ------------------------------------------------------------------
    # whisper_worker subprocess (S244)
    # ------------------------------------------------------------------

    def _spawn_audio_worker(self) -> None:
        """Spawn the whisper_worker subprocess. Idempotent.

        Worker runs in its own session group (start_new_session=True) so the
        shutdown safety-net's pgrep -P doesn't see it — the worker survives
        main's exit and drains its residual audio backlog independently.
        Verified via debug/14_32_shutdown_drain_spike/spike3_detached_child.py
        across normal exit / SIGTERM / SIGKILL.

        Stderr → /tmp/operator.log (same destination as the audio helper).
        Stdin is the audio + event channel. Stdout is /dev/null — the
        worker writes captions directly to the meeting JSONL.
        """
        if self._audio_worker_proc is not None:
            return
        if self._jsonl_path is None:
            return
        try:
            with open("/tmp/operator.log", "ab") as stderr_sink:
                self._audio_worker_proc = subprocess.Popen(
                    [
                        sys.executable, "-m",
                        "_1_800_operator.pipeline.whisper_worker",
                        "--jsonl", str(self._jsonl_path),
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_sink,
                    start_new_session=True,
                )
            self._audio_worker_pid = self._audio_worker_proc.pid
            log.info(
                f"AttachAdapter: whisper_worker spawned "
                f"(pid={self._audio_worker_proc.pid}, jsonl={self._jsonl_path})"
            )
        except OSError as e:
            log.warning(
                f"AttachAdapter: failed to spawn whisper_worker ({e}) — "
                f"falling back to in-process audio"
            )
            self._audio_worker_proc = None

    def _send_worker_frame(self, tag: bytes, payload: bytes) -> bool:
        """Write [tag][len_be][payload] to worker stdin.

        Returns True on success, False if the worker is gone / pipe broken.
        Thread-safe: stdin writes are serialized via _audio_worker_lock so
        the reader loop ([S]/[M] frames) and speaker observer ([E] events)
        don't interleave.

        If the worker has died (proc.poll() returns non-None), attempts
        one respawn before giving up — captions then resume on the new
        worker. State that's reachable from main (speaker timeline, mic
        label) is replayed; in-worker dedupe / partial buffers are lost.
        """
        proc = self._audio_worker_proc
        if proc is None or proc.stdin is None:
            return False
        if proc.poll() is not None:
            self._maybe_respawn_worker(dead_proc=proc)
            proc = self._audio_worker_proc
            if proc is None or proc.stdin is None or proc.poll() is not None:
                return False
        try:
            header = tag + struct.pack(">I", len(payload))
            with self._audio_worker_lock:
                proc.stdin.write(header)
                proc.stdin.write(payload)
                proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError) as e:
            log.warning(f"AttachAdapter: worker stdin write failed ({e}) — worker may be dead")
            return False

    def _maybe_respawn_worker(self, dead_proc: "subprocess.Popen") -> None:
        """Respawn the whisper_worker if it died mid-meeting. Idempotent
        under concurrent callers — first thread in wins, others see the
        already-new proc and bail.

        Replays speaker timeline + mic_label so attribution + M-leg
        labeling work for captions transcribed after respawn. The S-leg
        bleed-dedupe window (worker-local) is reset; first M-leg captions
        after respawn may pass through residual S-leg bleed. Acceptable
        degradation given the alternative is silent caption loss.
        """
        with self._audio_worker_respawn_lock:
            # Another thread may have respawned already.
            if self._audio_worker_proc is not dead_proc:
                return
            if self._respawn_disabled:
                return
            now = time.monotonic()
            while (
                self._respawn_attempts
                and (now - self._respawn_attempts[0]) > _RESPAWN_BREAKER_WINDOW_S
            ):
                self._respawn_attempts.popleft()
            self._respawn_attempts.append(now)
            if len(self._respawn_attempts) > _RESPAWN_BREAKER_THRESHOLD:
                log.error(
                    f"AttachAdapter: whisper_worker respawn storm — "
                    f"{len(self._respawn_attempts)} attempts in "
                    f"{_RESPAWN_BREAKER_WINDOW_S:.0f}s. Disabling further "
                    f"respawn; captions will be lost for the rest of this meeting."
                )
                self._respawn_disabled = True
                self._audio_worker_proc = None
                return
            old_pid = dead_proc.pid
            exit_code = dead_proc.returncode
            log.warning(
                f"AttachAdapter: whisper_worker (pid={old_pid}) died mid-meeting "
                f"(exit={exit_code}) — respawning"
            )
            self._audio_worker_proc = None
            self._audio_worker_shutdown_sent = False
            self._spawn_audio_worker()
            if not self.has_audio_worker:
                log.warning("AttachAdapter: respawn failed — captions will be lost")
                return
            # Replay mic_label so M-leg captions use the right speaker.
            try:
                mic_label = self.get_self_name() or _SPEAKER_USER_FALLBACK
                self._send_worker_event({"type": "mic_label", "name": mic_label})
            except Exception as e:
                log.debug(f"AttachAdapter: respawn mic_label send failed: {e}")
            # Replay speaker timeline so S-leg attribution survives the
            # respawn. Snapshot under lock so the live observer can't
            # mutate while we copy.
            with self._speaking_lock:
                history = list(self._speaking_history)
            for t, name, kind in history:
                event_type = "speaker_start" if kind == "start" else "speaker_stop"
                self._send_worker_event({"type": event_type, "name": name, "t": t})
            log.info(
                f"AttachAdapter: respawn done (new_pid={self._audio_worker_pid}, "
                f"replayed {len(history)} speaker events)"
            )

    def _send_worker_event(self, msg: dict) -> bool:
        """Send a control event ([E] tag, JSON payload) to the worker."""
        try:
            payload = json.dumps(msg).encode("utf-8")
        except (TypeError, ValueError) as e:
            log.warning(f"AttachAdapter: bad worker event payload {msg!r}: {e}")
            return False
        return self._send_worker_frame(_FRAME_TAG_EVENT, payload)

    @property
    def has_audio_worker(self) -> bool:
        """True if the whisper_worker subprocess was spawned successfully.

        Caller checks this to decide whether to call meeting_record.close()
        themselves (no-worker fallback) or skip it (worker handles seal).
        """
        proc = self._audio_worker_proc
        return proc is not None and proc.stdin is not None

    def update_pending_shutdown_payload(
        self,
        attended: "list[str]",
        currently_present: "list[str]",
        self_name: str,
    ) -> None:
        """Buffer the latest attended/self_name snapshot for the worker shutdown event.

        Called by ChatRunner each polling tick. _stop_audio_pipeline uses
        this to emit a final shutdown event right before closing worker
        stdin — covers the page-close race where _shutdown's own
        send_audio_worker_shutdown call would arrive after EOF.
        """
        self._pending_shutdown_payload = {
            "type": "shutdown",
            "attended": list(attended or []),
            "currently_present": list(currently_present or []),
            "self_name": self_name or "",
        }

    def send_audio_worker_shutdown(self, attended: list[str], currently_present: list[str], self_name: str) -> bool:
        """Tell the worker to seal the JSONL with participants_final +
        meeting_end after it drains. Called once by __main__._shutdown
        before connector.leave(). Returns True if the event was sent.

        Idempotent — safe to call twice (subsequent calls are no-ops).
        """
        if self._audio_worker_shutdown_sent:
            return True
        if not self.has_audio_worker:
            return False
        ok = self._send_worker_event({
            "type": "shutdown",
            "attended": list(attended or []),
            "currently_present": list(currently_present or []),
            "self_name": self_name or "",
        })
        if ok:
            self._audio_worker_shutdown_sent = True
        return ok

    def _start_audio_pipeline(self) -> None:
        """Bring up the Swift audio helper + AEC3, wire both into the
        whisper_worker subprocess.

        Best-effort — any failure here logs a warning and leaves the
        connector in chat-only mode. Reasons audio might not come up:
          - Linux (helper is Mac-only)
          - Helper binary not built (install.sh hasn't run)
          - Helper exits early on TCC denial (System Audio Recording / Mic) —
            user is told to run `operator doctor`
          - The whisper_worker subprocess failed to spawn at join() time

        Layout (S244):
          helper stdout  --> _audio_reader_loop --> worker stdin ([S] frames)
          helper stdout  --> _audio_reader_loop --> AEC3 stdin   ([M] frames)
          AEC3 stdout    --> _aec_to_worker     --> worker stdin ([M] frames)
        Speaker DOM observer events also stream to worker stdin ([E] frames)
        for in-worker S-leg attribution.
        """
        if sys.platform != "darwin":
            return
        helper = _resolve_audio_helper()
        if helper is None:
            log.warning(
                "AttachAdapter: Operator audio helper not found — dial will run "
                "chat-only (no transcript). Run install.sh to build the helper."
            )
            return
        if not self.has_audio_worker:
            log.warning("AttachAdapter: whisper_worker not available — chat-only mode")
            return

        # Operator may have entered teardown while the worker was
        # warming up. Spawning the helper after _leave_event is set
        # would orphan a subprocess _stop_audio_pipeline can't see — bail.
        if self._leave_event.is_set():
            log.info("AttachAdapter: leave requested during audio warmup — skipping helper spawn")
            return

        # Helper stderr → /tmp/operator.log (append). Same destination as
        # operator's own logs so users have one place to look.
        #
        # Spawned via posix_spawn with `responsibility_spawnattrs_setdisclaim`
        # so helper TCC identity is its own bundle id, independent of the
        # parent IDE/terminal's responsibility chain. Without this, Cursor's
        # ToDesktop Electron build silently denies audio capture even when
        # the helper itself is granted System Audio Recording.
        try:
            from _1_800_operator.pipeline._disclaimed_spawn import (
                spawn_disclaimed, minimal_helper_env,
            )
            with open("/tmp/operator.log", "ab") as stderr_sink:
                self._audio_helper_proc = spawn_disclaimed(
                    [str(helper)],
                    env=minimal_helper_env(),
                    stderr_fd=stderr_sink.fileno(),
                )
        except OSError as e:
            log.warning(f"AttachAdapter: spawning {helper} failed ({e}) — chat-only mode")
            return

        # AEC3 cleaner: routes cleaned mic frames to the worker as [M].
        # If aec3 binary is missing, raw [M] frames are routed straight to
        # worker by _audio_reader_loop (no bleed defense in that path).
        def _aec_to_worker(pcm: bytes) -> None:
            self._send_worker_frame(_FRAME_TAG_MIC, pcm)
        aec_binary = _resolve_aec_binary()
        if aec_binary is not None:
            try:
                from _1_800_operator.pipeline.aec_cleaner import AecCleaner
                self._aec_cleaner = AecCleaner(
                    binary_path=aec_binary,
                    on_clean_mic=_aec_to_worker,
                )
                self._aec_cleaner.start()
                log.info(f"AttachAdapter: AEC cleaner up ({aec_binary})")
            except Exception as e:
                log.warning(f"AttachAdapter: AEC cleaner start failed ({e}) — no bleed defense")
                self._aec_cleaner = None
        else:
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
        # to a generic "user" string when the scrape returns empty.
        mic_label = self.get_self_name() or _SPEAKER_USER_FALLBACK
        log.info(f"AttachAdapter: mic-leg speaker label = {mic_label!r}")
        self._send_worker_event({"type": "mic_label", "name": mic_label})
        log.info(
            f"AttachAdapter: audio pipeline up "
            f"(helper={helper}, pid={self._audio_helper_proc.pid}, "
            f"worker pid={self._audio_worker_pid})"
        )

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
                # S244: frames flow main → whisper_worker via the worker's
                # stdin. [S] also feeds AEC's render side as the reference
                # signal for echo cancellation; [M] goes through AEC's
                # capture side and its on_clean_mic callback re-emits the
                # cleaned [M] back to the worker. If AEC is down, raw [M]
                # goes straight to the worker (no bleed defense).
                if tag == _FRAME_TAG_SYSTEM:
                    self._send_worker_frame(_FRAME_TAG_SYSTEM, pcm)
                    if aec is not None:
                        aec.feed_render(pcm)
                elif tag == _FRAME_TAG_MIC:
                    if aec is not None:
                        aec.feed_capture(pcm)
                    else:
                        self._send_worker_frame(_FRAME_TAG_MIC, pcm)
                else:
                    log.warning(f"AttachAdapter: unknown frame tag {tag!r} — dropping {length}B")
                    continue
        except Exception as e:
            log.warning(f"AttachAdapter: audio reader loop crashed: {e}")

    def _stop_audio_pipeline(self) -> None:
        """Tear down the audio pipeline. Idempotent.

        Order matters: set the stop event so the reader breaks out, close
        helper stdin (helper watches for EOF and exits on it), drain AEC
        cleaner, join the reader thread, then send the buffered shutdown
        event to the worker and close its stdin.

        S244 — worker subprocess: receives EOF on its stdin AFTER helper
        + AEC + reader have drained. The worker then transcribes its
        residual buffer, writes participants_final + meeting_end, and
        exits at its own pace (out of our process). We do NOT wait for
        the worker — that's the whole point of the subprocess-decoupled
        drain. The shutdown safety-net reaper excludes the worker pid so
        it isn't SIGKILL'd before it drains.
        """
        if (
            self._audio_helper_proc is None
            and self._aec_cleaner is None
            and self._audio_worker_proc is None
        ):
            return
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
        # Helper is gone — the reader loop will EOF on its next read.
        # Stop the AEC cleaner only AFTER the reader path is quiet so we
        # don't drop frames it was still forwarding.
        if self._aec_cleaner is not None:
            try:
                self._aec_cleaner.stop()
            except Exception as e:
                log.debug(f"AttachAdapter: AEC cleaner stop raised: {e}")
            self._aec_cleaner = None
        for t in self._audio_threads:
            t.join(timeout=1.5)
        self._audio_threads.clear()
        # Send the latest known attended/self_name snapshot to the worker
        # as a shutdown event RIGHT BEFORE closing its stdin. This covers
        # the page-close race: when the browser thread runs
        # _stop_audio_pipeline autonomously (user closed Meet tab),
        # __main__._shutdown's own send_audio_worker_shutdown call would
        # arrive after EOF and the seal would land with empty attended.
        # Buffered payload comes from ChatRunner._refresh_roster_file via
        # update_pending_shutdown_payload, so it's always within one poll
        # tick of fresh.
        worker_proc = self._audio_worker_proc
        if worker_proc is not None and self._pending_shutdown_payload is not None:
            try:
                self._send_worker_event(self._pending_shutdown_payload)
            except Exception as e:
                log.debug(f"AttachAdapter: shutdown event flush raised: {e}")
        # Close the worker's stdin AFTER helper + AEC + reader have all
        # drained. The worker sees EOF, transcribes residual audio, writes
        # participants_final + meeting_end, exits. We do NOT wait.
        if worker_proc is not None:
            try:
                if worker_proc.stdin is not None:
                    worker_proc.stdin.close()
            except (BrokenPipeError, OSError) as e:
                log.debug(f"AttachAdapter: closing worker stdin raised: {e}")
            log.info(
                f"AttachAdapter: whisper_worker (pid={worker_proc.pid}) handed off — "
                f"draining residual audio + sealing JSONL out-of-process"
            )
            # Clear the handle so subsequent ops can't accidentally write
            # to a closed pipe. The Popen object stays alive (the OS owns
            # the child process); GC of self drops our last reference.
            self._audio_worker_proc = None
        log.info("AttachAdapter: audio pipeline torn down")
