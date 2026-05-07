"""
macOS connector for Operator.

Wraps Playwright/Chrome meeting join into the MeetingConnector interface.
"""
import os
import logging
import queue
import re
import threading
import time

from playwright.sync_api import sync_playwright
from _1_800_operator import config

from .base import MeetingConnector
from .captions_js import CAPTION_OBSERVER_JS, enable_captions, filter_caption
from .chat_dom_js import (
    DRAIN_CHAT_QUEUE_JS,
    GET_PARTICIPANT_NAMES_JS,
    INSTALL_CHAT_OBSERVER_JS,
    OBSERVER_ATTACHED_CHECK_JS,
    SNAPSHOT_MESSAGE_IDS_JS,
)
from .session import JoinStatus, detect_page_state, validate_auth_state, inject_cookies, save_debug, _chrome_lock_is_live, _chrome_kill_and_clear, _write_operator_pid, _is_real_meet_room

log = logging.getLogger(__name__)

BROWSER_PROFILE = config.BROWSER_PROFILE_DIR


class MacOSAdapter(MeetingConnector):
    """MeetingConnector for macOS using Playwright + real Chrome."""

    def __init__(self, auth_state_file=None, force=False):
        super().__init__()
        if auth_state_file is None:
            auth_state_file = config.AUTH_STATE_FILE
        self._auth_state_file = auth_state_file
        self._force = force
        self._leave_event = threading.Event()
        self._browser_closed = threading.Event()
        # Set while the browser thread is holding a live, open page; cleared on
        # close-detection or teardown. is_connected() ANDs this with not-closed
        # so callers see a sub-second signal when the page dies, instead of
        # waiting up to 30s for the active health probe to catch it.
        self._browser_alive = threading.Event()
        self._browser_thread = None
        self._page = None
        self._chat_queue = queue.Queue()  # (command, args, result_queue)
        self._observer_installed = False
        self._caption_callback = None  # fn(speaker, text, timestamp); set via set_caption_callback
        self._js_time_offset = None    # performance.now() → wall-clock calibration
        self._resolved_url = None
        self._url_resolved = threading.Event()

    # ------------------------------------------------------------------
    # MeetingConnector interface
    # ------------------------------------------------------------------

    def join(self, meeting_url):
        """Start a browser session and join the meeting. Returns immediately;
        browser runs in a background thread until leave() is called.

        Pass meeting_url=None to open a fresh meet.new and resolve the URL
        at runtime — the caller can then retrieve it via wait_for_resolved_url().
        """
        from _1_800_operator.pipeline.chrome_preflight import require_chrome_or_exit
        require_chrome_or_exit()

        self._leave_event.clear()
        self._browser_closed.clear()
        self._browser_alive.clear()
        self.join_status = JoinStatus()
        self._resolved_url = None
        self._url_resolved.clear()
        self._browser_thread = threading.Thread(
            target=self._browser_session,
            args=(meeting_url,),
            daemon=True,
            name="MacOSAdapter-browser",
        )
        self._browser_thread.start()
        log.info(f"MacOSAdapter: joining {meeting_url or '<meet.new>'}")

    def wait_for_resolved_url(self, timeout=45):
        """Block until the browser thread has resolved the meeting URL.

        Returns the URL on success, None on timeout. For meet.new flows, this
        is the real meet.google.com URL produced by redirect. For direct-URL
        flows, this returns immediately with the URL passed to join().
        """
        if self._url_resolved.wait(timeout=timeout):
            return self._resolved_url
        return None

    def send_chat(self, message):
        """Post a message to the Google Meet chat panel.

        Queues the request for the browser thread (Playwright is single-threaded).
        Returns the new message's `data-message-id` if the DOM read-back lands
        within ~1s; returns None on timeout or error so the caller can fall
        back to text-match dedup.
        """
        result_q = queue.Queue()
        self._chat_queue.put(("send", message, result_q))
        try:
            return result_q.get(timeout=10)
        except queue.Empty:
            return None

    def read_chat(self):
        """Return new chat messages since last call.
        Queues the request for the browser thread."""
        result_q = queue.Queue()
        self._chat_queue.put(("read", None, result_q))
        try:
            return result_q.get(timeout=10)
        except queue.Empty:
            return []

    def get_participant_count(self):
        """Return participant count via browser thread."""
        result_q = queue.Queue()
        self._chat_queue.put(("participant_count", None, result_q))
        try:
            return result_q.get(timeout=5)
        except queue.Empty:
            return 0

    def get_participant_names(self):
        """Return participant display names via browser thread (best effort)."""
        result_q = queue.Queue()
        self._chat_queue.put(("participant_names", None, result_q))
        try:
            return result_q.get(timeout=5)
        except queue.Empty:
            return []

    def is_connected(self):
        """Return True if the browser thread is holding a live, open page.

        Cross-thread safe: only reads Event flags. The actual page probes
        (page.is_closed(), page.url) run on the browser thread that owns the
        Playwright objects — Playwright's sync API is single-thread, so we
        can't probe from outside _browser_session.
        """
        return self._browser_alive.is_set() and not self._browser_closed.is_set()

    def set_caption_callback(self, fn):
        """Register fn(speaker, text, timestamp) for caption DOM updates.

        May be called BEFORE or AFTER join(). The JS bridge is exposed at
        browser startup whenever config.CAPTIONS_ENABLED is true (regardless
        of whether a callback is set yet), so a late-bound callback still
        receives captions cleanly. Captions that arrive while no callback is
        registered are silently dropped. Pass None to unregister.

        Late-bind lets callers register the finalizer after the meeting slug
        is known — useful when the bot opens `meet.new` and the real slug is
        only assigned post-navigation, not at browser-startup time.
        """
        self._caption_callback = fn

    def _on_caption_from_js(self, speaker, text, js_timestamp):
        """JS → Python caption bridge. Runs on the browser thread."""
        cleaned = filter_caption(speaker, text)
        if cleaned is None:
            return
        py_now = time.time()
        if self._js_time_offset is None:
            self._js_time_offset = py_now - js_timestamp / 1000.0
        timestamp = self._js_time_offset + js_timestamp / 1000.0
        log.info(f"caption: [{speaker}] {cleaned[:80]}")
        if self._caption_callback:
            try:
                self._caption_callback(speaker, cleaned, timestamp)
            except Exception as e:
                log.warning(f"caption callback raised: {e}")

    # --- Browser-thread chat implementations (called from _process_chat_queue) ---

    def _ensure_chat_open(self, page):
        """Open the chat panel if it isn't already open. Must run on browser thread."""
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
            log.info("MacOSAdapter: clicked chat button — waiting for panel to render")
            # Wait for the panel to actually render instead of a fixed sleep.
            # This prevents a race where the observer install runs before the
            # textarea is in the DOM, causing a silent no-op installation.
            page.locator('textarea[aria-label="Send a message"]').wait_for(
                state="visible", timeout=2000
            )
            log.info("MacOSAdapter: chat panel open")
        except Exception as e:
            log.debug(f"MacOSAdapter: could not open chat panel: {e}")
            try:
                os.makedirs(config.DEBUG_DIR, exist_ok=True, mode=0o700)
                _shot = os.path.join(config.DEBUG_DIR, "chat_btn_not_found.png")
                page.screenshot(path=_shot)
                os.chmod(_shot, 0o600)
                log.debug(f"MacOSAdapter: saved debug screenshot to {_shot}")
            except Exception:
                pass

    def _do_send_chat(self, page, message):
        """Actual send_chat logic — must run on browser thread.

        Returns the new message's `data-message-id` so ChatRunner can mark
        it seen by ID rather than text-match. Snapshots the existing IDs
        before send, then polls (every 50ms, up to 1s) for one new ID to
        appear. Returns None if the read-back times out — the caller falls
        back to text-match dedup in that case.
        """
        self._ensure_chat_open(page)
        try:
            pre_ids = set(page.evaluate(SNAPSHOT_MESSAGE_IDS_JS))
            input_box = page.locator('textarea[aria-label="Send a message"]')
            input_box.wait_for(timeout=5000)
            input_box.fill(message)
            input_box.press("Enter")
            log.info(f"MacOSAdapter: chat sent: {message!r}")
            for _ in range(20):
                current = set(page.evaluate(SNAPSHOT_MESSAGE_IDS_JS))
                new_ids = current - pre_ids
                if new_ids:
                    return next(iter(new_ids))
                time.sleep(0.05)
            log.debug("MacOSAdapter: send_chat ID-readback timed out — caller will fall back to text-match dedup")
            return None
        except Exception as e:
            log.warning(f"MacOSAdapter: send_chat failed: {e}")
            return None

    def _install_chat_observer(self, page):
        """Inject a MutationObserver that queues new chat messages in JS.

        The observer watches for new div[data-message-id] elements and
        stores them in window.__operatorChatQueue. _do_read_chat drains
        this queue instead of scanning the full DOM each time.
        """
        if self._observer_installed:
            return
        try:
            page.evaluate(INSTALL_CHAT_OBSERVER_JS)
            # Verify the observer actually attached. The JS function returns
            # early (no-op) if the textarea or its panel container isn't in
            # the DOM yet — page.evaluate() won't throw, so we check the
            # result explicitly and only mark installed on confirmed success.
            attached = page.evaluate(OBSERVER_ATTACHED_CHECK_JS)
            if attached:
                self._observer_installed = True
                log.info("MacOSAdapter: chat MutationObserver installed")
            else:
                log.warning("MacOSAdapter: chat observer not attached (textarea or panel container not in DOM) — will retry next poll")
        except Exception as e:
            log.warning(f"MacOSAdapter: failed to install chat observer: {e}")

    def _do_read_chat(self, page):
        """Drain the JS-side chat queue populated by the MutationObserver."""
        self._ensure_chat_open(page)
        self._install_chat_observer(page)

        try:
            messages = page.evaluate(DRAIN_CHAT_QUEUE_JS)
            if messages:
                log.debug(f"MacOSAdapter: observer drained {len(messages)} new messages")
            return messages
        except Exception as e:
            log.warning(f"MacOSAdapter: read_chat failed: {e}")
            return []

    def _do_get_participant_count(self, page):
        """Count participants via data-requested-participant-id elements."""
        try:
            return page.locator('[data-requested-participant-id]').count()
        except Exception as e:
            log.warning(f"MacOSAdapter: get_participant_count failed: {e}")
            return 0

    def _do_get_participant_names(self, page):
        """Best-effort participant name scrape via tile DOM.

        Meet renders one tile per participant with `data-requested-participant-id`.
        We try a few selectors per tile because Meet's class names rotate; we
        also fall back to textContent. Returns a deduped list of plausible
        names. Returns [] on any failure — callers must degrade gracefully.
        """
        try:
            return page.evaluate(GET_PARTICIPANT_NAMES_JS) or []
        except Exception as e:
            log.warning(f"MacOSAdapter: get_participant_names failed: {e}")
            return []

    def _process_chat_queue(self, page):
        """Drain the chat command queue. Called from browser thread's idle loop."""
        while not self._chat_queue.empty():
            try:
                cmd, args, result_q = self._chat_queue.get_nowait()
            except queue.Empty:
                break
            if cmd == "send":
                new_id = self._do_send_chat(page, args)
                result_q.put(new_id)
            elif cmd == "read":
                messages = self._do_read_chat(page)
                result_q.put(messages)
            elif cmd == "participant_count":
                count = self._do_get_participant_count(page)
                result_q.put(count)
            elif cmd == "participant_names":
                names = self._do_get_participant_names(page)
                result_q.put(names)

    # ── Waiting room ─────────────────────────────────────────────────

    def _wait_for_admission(self, page):
        """Wait for the host to admit us, with event-driven detection.

        Phase 1: wait up to 10s for the waiting room image to appear — confirms
        the page has settled into the lobby state.
        Phase 2: watch for that image to disappear — fires immediately when the
        host clicks 'Let in', with no polling lag.

        Returns "admitted", "cancelled", or "timeout".
        """
        timeout_seconds = config.LOBBY_WAIT_SECONDS
        deadline = time.time() + timeout_seconds
        wait_start = time.time()
        last_status_log = wait_start
        chunk_ms = 1000  # how often to re-check _leave_event

        WAITING_ROOM_SEL = 'img[alt*="Please wait until a meeting host"]'

        # Phase 1: confirm the page has settled into the waiting room
        log.info("MacOSAdapter: waiting for lobby screen to appear...")
        try:
            page.wait_for_selector(WAITING_ROOM_SEL, state="visible", timeout=10_000)
            log.info("MacOSAdapter: lobby confirmed — watching for host to admit us "
                     f"(timeout={timeout_seconds}s)")
        except Exception:
            elapsed = time.time() - wait_start
            log.info(
                f"MacOSAdapter: lobby screen not detected after {elapsed:.1f}s "
                f"— assuming already admitted or different join flow"
            )
            return "admitted"

        # Phase 2: event-driven watch for the lobby to go away = admitted
        while not self._leave_event.is_set() and time.time() < deadline:
            remaining_ms = int((deadline - time.time()) * 1000)
            chunk = min(chunk_ms, max(remaining_ms, 0))
            if chunk <= 0:
                break

            try:
                page.wait_for_selector(WAITING_ROOM_SEL, state="detached", timeout=chunk)
                elapsed = time.time() - wait_start
                log.info(
                    f"MacOSAdapter: admitted — lobby screen gone "
                    f"(event-driven, waited {elapsed:.1f}s total)"
                )
                return "admitted"
            except Exception:
                if page.is_closed():
                    log.info("MacOSAdapter: browser closed during admission wait — aborting")
                    return "cancelled"

            if time.time() - last_status_log >= 30:
                elapsed = time.time() - wait_start
                log.info(f"MacOSAdapter: still in waiting room ({elapsed:.0f}s elapsed)")
                last_status_log = time.time()

        elapsed = time.time() - wait_start
        if self._leave_event.is_set():
            log.info(f"MacOSAdapter: admission wait cancelled (leave called after {elapsed:.0f}s)")
            return "cancelled"
        else:
            log.warning(f"MacOSAdapter: admission timeout after {elapsed:.0f}s")
            return "timeout"

    def leave(self):
        """Signal the browser session to close.
        Safe to call multiple times — only the first call does work."""
        if self._leave_event.is_set():
            return
        self._leave_event.set()
        if self._browser_thread and self._browser_thread.is_alive():
            log.info("MacOSAdapter: waiting for browser to close...")
            if not self._browser_closed.wait(timeout=10):
                log.warning("MacOSAdapter: browser close timed out (10s)")
            # Give sync_playwright.__exit__ a short window to unwind cleanly.
            # If the greenlet event loop wedges on a dead CDP socket (can happen
            # after Chrome self-exits), force-kill the Node driver directly —
            # that unblocks the kqueue.select(None) and lets the thread exit.
            self._browser_thread.join(timeout=2)
            if self._browser_thread.is_alive():
                self._kill_playwright_drivers()
                self._browser_thread.join(timeout=2)
            if self._browser_thread.is_alive():
                log.warning("MacOSAdapter: browser thread still stuck after driver SIGKILL")
                try:
                    import sys as _sys, traceback as _tb
                    frame = _sys._current_frames().get(self._browser_thread.ident)
                    if frame is not None:
                        stack = "".join(_tb.format_stack(frame))
                        log.warning(f"MacOSAdapter: stuck browser-thread stack:\n{stack}")
                except Exception:
                    pass
        log.info("MacOSAdapter: left meeting")

    def _kill_playwright_drivers(self):
        """SIGKILL any Playwright Node driver child processes.
        Used to unblock the greenlet event loop when sync_playwright.__exit__
        hangs waiting on a dead CDP socket."""
        import os as _os
        import signal as _signal
        import subprocess as _sp
        try:
            r = _sp.run(
                ["pgrep", "-P", str(_os.getpid())],
                capture_output=True, text=True, timeout=2,
            )
            child_pids = [int(p) for p in r.stdout.split() if p.strip().isdigit()]
        except Exception as e:
            log.warning(f"MacOSAdapter: pgrep failed: {e}")
            return
        for cpid in child_pids:
            try:
                cmd_r = _sp.run(
                    ["ps", "-o", "command=", "-p", str(cpid)],
                    capture_output=True, text=True, timeout=1,
                )
                cmd = cmd_r.stdout.strip()
            except Exception:
                cmd = ""
            if "playwright" in cmd and "run-driver" in cmd:
                try:
                    _os.kill(cpid, _signal.SIGKILL)
                    log.warning(f"MacOSAdapter: force-killed stuck Playwright driver pid={cpid}")
                except ProcessLookupError:
                    pass
                except Exception as e:
                    log.warning(f"MacOSAdapter: failed to kill driver pid={cpid}: {e}")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _browser_session(self, meeting_url):
        """Run Playwright browser session. Blocks until leave() is called."""
        singleton_lock = os.path.join(BROWSER_PROFILE, "SingletonLock")
        if os.path.islink(singleton_lock) or os.path.exists(singleton_lock):
            if _chrome_lock_is_live(singleton_lock):
                if self._force:
                    log.info("MacOSAdapter: --force: killing existing session")
                    _chrome_kill_and_clear(singleton_lock)
                else:
                    log.error(
                        "MacOSAdapter: another Operator session is already running — "
                        "stop that session before starting a new one"
                    )
                    self.join_status.signal_failure("already_running")
                    return
            else:
                os.remove(singleton_lock)
                log.info("MacOSAdapter: removed stale SingletonLock")

        _write_operator_pid(singleton_lock)
        js = self.join_status
        browser = None
        t_start = time.monotonic()
        # Lock the profile dir to owner-only — contents include Google session
        # cookies and shouldn't be listable by other users on shared hosts.
        # mode= on makedirs makes creation atomic-private; chmod is the
        # belt for the case where the dir already exists with looser perms.
        os.makedirs(BROWSER_PROFILE, exist_ok=True, mode=0o700)
        os.chmod(BROWSER_PROFILE, 0o700)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch_persistent_context(
                    user_data_dir=BROWSER_PROFILE,
                    headless=True,
                    args=[
                        "--use-fake-ui-for-media-stream",
                        # Synthesize a fake mic/cam so Chromium never tries to
                        # access the real device — sidesteps the macOS-level
                        # "allow microphone access?" prompt that the
                        # `-ui-` flag alone can't bypass. Operator is chat-only;
                        # there's no audio capture path that would need a real
                        # mic anyway.
                        "--use-fake-device-for-media-stream",
                        "--mute-audio",
                    ],
                )
                t_browser = time.monotonic()
                log.info(f"TIMING browser_launch={t_browser - t_start:.1f}s")
                page = browser.pages[0] if browser.pages else browser.new_page()
                self._page = page

                # Expose the caption bridge BEFORE navigation so the
                # MutationObserver can find window.__onCaption the instant it
                # attaches. Gated on the global captions_enabled flag (not on
                # whether a callback is currently set) so callers can late-bind
                # set_caption_callback after join() — callers that open
                # `meet.new` only know the real slug once the browser has
                # navigated, so the finalizer is wired up post-hoc.
                if config.CAPTIONS_ENABLED:
                    try:
                        page.expose_function("__onCaption", self._on_caption_from_js)
                        log.info("MacOSAdapter: caption bridge exposed")
                    except Exception as e:
                        log.warning(f"MacOSAdapter: expose_function failed: {e}")

                try:
                    if meeting_url is None:
                        log.info("MacOSAdapter: opening meet.new for fresh meeting")
                        page.goto("https://meet.new", wait_until="domcontentloaded", timeout=30000)
                        # meet.new normally redirects straight to a real meeting URL.
                        # On a fresh persistent profile (first-time mic/cam state)
                        # Meet may instead sit on `/new` and show a
                        # "Do you want people to see and hear you in the meeting?"
                        # interstitial with a "Continue without microphone and
                        # camera" button. Poll for both: if the interstitial
                        # appears first, click it to unblock the redirect.
                        deadline = time.monotonic() + 30.0
                        redirected = False
                        while time.monotonic() < deadline:
                            if _is_real_meet_room(page.url):
                                redirected = True
                                break
                            try:
                                decline = page.get_by_text("Continue without microphone and camera")
                                if decline.count() > 0 and decline.first.is_visible():
                                    decline.first.click()
                                    log.info("MacOSAdapter: dismissed pre-redirect interstitial (Continue without microphone and camera)")
                                    page.wait_for_timeout(500)
                                    continue
                            except Exception:
                                pass
                            page.wait_for_timeout(500)
                        if not redirected:
                            log.error("MacOSAdapter: meet.new did not redirect to a meeting URL: timeout 30s")
                            # Snapshot the page so we can see what Meet was showing
                            # at the timeout (blank page? sign-in interstitial?
                            # workspace consent? anti-automation challenge?).
                            try:
                                save_debug(page, "meet_new_no_redirect")
                            except Exception as _e:
                                log.warning(f"MacOSAdapter: save_debug failed: {_e}")
                            js.signal_failure("meet_new_no_redirect")
                            return
                        meeting_url = page.url
                        log.info(f"MacOSAdapter: meet.new resolved to {meeting_url}")
                    else:
                        page.goto(meeting_url, wait_until="domcontentloaded", timeout=30000)
                    self._resolved_url = meeting_url
                    self._url_resolved.set()
                    t_nav = time.monotonic()
                    log.info(f"TIMING navigation={t_nav - t_browser:.1f}s")
                    # Event-driven: wait for a pre-join or in-meeting element instead of sleeping 8s.
                    # `Leave call` covers the meet.new case where the creator is auto-joined and
                    # the pre-join screen is skipped entirely — without it we wait the full 15s
                    # timeout for elements that will never appear.
                    try:
                        page.wait_for_selector(
                            'button:has-text("Join now"), '
                            'button:has-text("Ask to join"), '
                            'button[aria-label*="Turn off camera"], '
                            'button[aria-label*="Turn on camera"], '
                            'button[aria-label*="Sign in"], '
                            'button[aria-label*="Leave call"]',
                            timeout=15000,
                        )
                    except Exception:
                        log.warning("MacOSAdapter: no pre-join element detected — proceeding anyway")
                    log.info(f"TIMING pre_join_ready={time.monotonic() - t_nav:.1f}s")

                    # --- Session recovery ladder ---
                    t_state = time.monotonic()
                    state = detect_page_state(page)
                    log.info(f"TIMING detect_page_state={time.monotonic() - t_state:.1f}s (state={state})")
                    recovered = False

                    if state == "logged_out":
                        log.warning("MacOSAdapter: session expired — attempting cookie recovery")
                        auth = validate_auth_state(self._auth_state_file)
                        if auth and inject_cookies(browser, auth):
                            page.reload(wait_until="domcontentloaded", timeout=30000)
                            try:
                                page.wait_for_selector(
                                    'button:has-text("Join now"), '
                                    'button:has-text("Ask to join"), '
                                    'button[aria-label*="Turn off camera"]',
                                    timeout=15000,
                                )
                            except Exception:
                                pass
                            state = detect_page_state(page)
                            if state == "pre_join":
                                log.info("MacOSAdapter: session recovered via cookie injection")
                                recovered = True
                            else:
                                log.error(f"MacOSAdapter: recovery failed — page state: {state}")
                                save_debug(page, "recovery_fail")
                                js.signal_failure("session_expired")
                                return
                        else:
                            log.error("MacOSAdapter: no valid auth_state for recovery")
                            save_debug(page, "no_auth_state")
                            js.signal_failure("session_expired")
                            return

                    if state == "cant_join":
                        log.error("MacOSAdapter: 'can't join this video call'")
                        save_debug(page, "cant_join")
                        js.signal_failure("cant_join")
                        return

                    # meet.new auto-joins the creator, so the pre-join screen
                    # never appears. Detect the in-meeting Leave-call button
                    # and skip camera toggle + join-button race when set.
                    already_in_meeting = False
                    try:
                        leave_btn = page.get_by_role("button", name="Leave call")
                        if leave_btn.count() > 0 and leave_btn.first.is_visible():
                            already_in_meeting = True
                            log.info("MacOSAdapter: already in meeting — skipping pre-join flow")
                    except Exception:
                        pass

                    # --- Pre-join screen actions ---

                    # Dismiss the device-permission modal if it's covering
                    # the pre-join controls. Variants: "Continue without
                    # microphone and camera" link (has-text), or a camera-only
                    # "Do you want people to see you" dialog with no decline
                    # link (close via aria-label="Close"). Try the link first,
                    # fall back to Escape, which dismisses any open dialog
                    # without affecting the empty pre-join state.
                    try:
                        decline = page.get_by_text("Continue without microphone and camera")
                        if decline.count() > 0 and decline.first.is_visible():
                            decline.first.click()
                            log.info("MacOSAdapter: dismissed device modal via decline link")
                            page.wait_for_timeout(500)
                        else:
                            page.keyboard.press("Escape")
                            log.debug("MacOSAdapter: sent Escape to dismiss any pre-join modal")
                            page.wait_for_timeout(500)
                    except Exception as e:
                        log.debug(f"MacOSAdapter: pre-join modal dismiss skipped: {e}")

                    # Turn off camera and confirm before joining. The pre-join "Turn off camera"
                    # button only exists on the pre-join screen — when meet.new auto-joins us,
                    # we're already past it and the wait would burn 5s for nothing.
                    t_prejoin = time.monotonic()
                    if already_in_meeting:
                        log.info("TIMING camera_toggle=0.0s (already_in)")
                    else:
                        save_debug(page, "pre_camera_toggle")
                        cam_off = page.get_by_role("button", name="Turn off camera")
                        try:
                            cam_off.wait_for(timeout=5000)
                            cam_off.click()
                            log.info("MacOSAdapter: clicked 'Turn off camera'")
                            try:
                                page.wait_for_selector(
                                    '[role="button"][data-is-muted="true"][aria-label*="camera"]',
                                    timeout=3000,
                                )
                                log.info("MacOSAdapter: camera confirmed off (data-is-muted=true)")
                            except Exception:
                                log.warning("MacOSAdapter: camera toggle clicked but could not confirm off state")
                                save_debug(page, "camera_not_confirmed")
                        except Exception:
                            log.warning("MacOSAdapter: 'Turn off camera' button not found — camera may be on")
                            save_debug(page, "camera_btn_missing")
                        log.info(f"TIMING camera_toggle={time.monotonic() - t_prejoin:.1f}s")

                    # Race all join buttons — avoids 5s timeout per missing button
                    t_join = time.monotonic()
                    clicked_label = None
                    if already_in_meeting:
                        clicked_label = "already_in"
                    else:
                        join_now = page.get_by_role("button", name="Join now")
                        ask_join = page.get_by_role("button", name="Ask to join")
                        switch_here = page.get_by_role("button", name="Switch here")
                        try:
                            join_now.or_(ask_join).or_(switch_here).wait_for(timeout=10000)
                            for label, btn in [("Join now", join_now), ("Ask to join", ask_join), ("Switch here", switch_here)]:
                                if btn.is_visible():
                                    btn.click()
                                    clicked_label = label
                                    log.debug(f"MacOSAdapter: clicked {label!r}")
                                    break
                        except Exception:
                            pass

                    if clicked_label is None:
                        save_debug(page, "join_fail")
                        log.warning("MacOSAdapter: could not find join button")
                        js.signal_failure("no_join_button")
                        return

                    log.info(f"TIMING join_click={time.monotonic() - t_join:.1f}s ({clicked_label})")

                    if clicked_label == "Ask to join":
                        admission = self._wait_for_admission(page)
                        if admission != "admitted":
                            save_debug(page, "admission_fail")
                            js.signal_failure(f"admission_{admission}")
                            return

                    log.info("MacOSAdapter: joined meeting successfully")
                    js.signal_success(recovered=recovered)
                    self._browser_alive.set()

                    # Event-driven: wait for in-meeting UI instead of sleeping 3s
                    t_in_meeting = time.monotonic()
                    try:
                        page.wait_for_selector(
                            'button[aria-label*="Leave call"]',
                            timeout=5000,
                        )
                    except Exception:
                        log.warning("MacOSAdapter: in-meeting indicator not detected — proceeding anyway")
                    log.info(f"TIMING in_meeting_wait={time.monotonic() - t_in_meeting:.1f}s")

                    # Enable captions + inject the observer when captions are
                    # enabled in config — independent of callback registration
                    # so late-bound callbacks receive captions without
                    # restarting the browser. Graceful degrade: if
                    # captions can't be enabled (unsupported language,
                    # permissions, etc.) we still hold the meeting open for chat.
                    if config.CAPTIONS_ENABLED:
                        t_cap = time.monotonic()
                        if enable_captions(page):
                            try:
                                page.evaluate(CAPTION_OBSERVER_JS)
                                log.info(f"TIMING caption_observer_inject={time.monotonic() - t_cap:.1f}s")
                            except Exception as e:
                                log.warning(f"MacOSAdapter: observer inject failed: {e}")
                        else:
                            log.warning("MacOSAdapter: captions unavailable — continuing without transcript")

                    log.info("MacOSAdapter: in meeting — holding browser open")
                    log.info(f"TIMING total_join={time.monotonic() - t_start:.1f}s")

                    # Hold until leave() signals or 4-hour hard cap.
                    # Loop every 500ms to service chat queue promptly.
                    deadline = time.time() + 4 * 3600
                    last_health = time.time()
                    last_alert_check = time.time()
                    last_admit_check = time.time()
                    t_hold_start = time.time()
                    admit_diagnostic_saved = False
                    last_admit_attempt = None  # (pill_text, participant_count) when we last tried — used to suppress retries on stale pill
                    network_lost_at = None  # set when alert first detected, cleared on recovery
                    NETWORK_GRACE_SECONDS = 30  # exit only if alert persists this long
                    while not self._leave_event.is_set() and time.time() < deadline:
                        self._process_chat_queue(page)
                        page.wait_for_timeout(500)

                        # Per-tick page-close probe. page.is_closed() reads
                        # cached state (no CDP round-trip) so it's cheap to
                        # call every 500ms. Catches the cleanly-closed case
                        # immediately; frozen-WebSocket zombies still wait
                        # for the active probe at the 10s health check below.
                        if page.is_closed():
                            self._browser_alive.clear()
                            log.warning("MacOSAdapter: page closed mid-meeting — exiting")
                            break

                        # Admission poll every 2s. Meet renders a top-right
                        # "Admit N guest(s)" element — accessibility text says
                        # "Press Down Arrow to open the hover tray and Escape
                        # to close", so it's a hover-tray widget, not a plain
                        # button. Flow: locate the pill by its visible text,
                        # real-hover it (Playwright moves the mouse, which
                        # fires the handlers Google's widget binds), then
                        # click the Admit button that appears in the tray.
                        # Keyboard path is a fallback (focus + ArrowDown +
                        # Enter) per the a11y hint.
                        if time.time() - last_admit_check >= 2:
                            last_admit_check = time.time()
                            try:
                                pill = page.get_by_text(
                                    re.compile(r"^Admit\s+\d+\s+(guest|people)", re.I)
                                ).first
                                if pill.count() > 0 and pill.is_visible():
                                    try:
                                        pill_text = pill.inner_text(timeout=500).strip()
                                    except Exception:
                                        pill_text = "<unreadable>"
                                    current_count = self._do_get_participant_count(page)

                                    # Cooldown: Meet leaves the "Admit N guest" pill widget
                                    # sticky in the DOM after the guest is admitted (visible
                                    # text persists). Skip if pill text + participant count
                                    # both match our last attempt — otherwise we'd spam
                                    # admit clicks every 2s on a phantom knocker.
                                    if (last_admit_attempt is not None
                                            and last_admit_attempt[0] == pill_text
                                            and last_admit_attempt[1] == current_count):
                                        pass
                                    else:
                                        log.info(f"MacOSAdapter: admit pill detected text={pill_text!r} count={current_count}")

                                        def _wait_count_increase(start, timeout_ms=3000):
                                            deadline_v = time.monotonic() + (timeout_ms / 1000)
                                            while time.monotonic() < deadline_v:
                                                new = self._do_get_participant_count(page)
                                                if new > start:
                                                    return new
                                                page.wait_for_timeout(150)
                                            return None

                                        try:
                                            pill.hover()
                                            page.wait_for_timeout(400)
                                        except Exception as e:
                                            log.debug(f"MacOSAdapter: pill hover failed: {e}")

                                        if not admit_diagnostic_saved:
                                            save_debug(page, "admit_diagnostic")
                                            admit_diagnostic_saved = True

                                        admit_btn = page.get_by_role(
                                            "button", name=re.compile(r"^Admit$", re.I)
                                        ).first
                                        btn_count = admit_btn.count()
                                        btn_visible = btn_count > 0 and admit_btn.is_visible()
                                        log.info(f"MacOSAdapter: admit_btn count={btn_count} visible={btn_visible}")

                                        tray_click_succeeded = False
                                        if btn_visible:
                                            try:
                                                admit_btn.click(timeout=1000)
                                                tray_click_succeeded = True
                                            except Exception as e:
                                                log.warning(f"MacOSAdapter: tray admit click raised: {e}")

                                        if tray_click_succeeded:
                                            new_count = _wait_count_increase(current_count)
                                            if new_count is not None:
                                                log.info(f"MacOSAdapter: admitted via tray click (verified — participant count {current_count} → {new_count})")
                                                save_debug(page, "post_admit_success")
                                                last_admit_attempt = (pill_text, new_count)
                                            else:
                                                log.warning(f"MacOSAdapter: tray click — no count change in 3s; treating pill as stale")
                                                last_admit_attempt = (pill_text, current_count)
                                        else:
                                            try:
                                                pill.focus()
                                                page.keyboard.press("ArrowDown")
                                                page.wait_for_timeout(200)
                                                page.keyboard.press("Enter")
                                                new_count = _wait_count_increase(current_count)
                                                if new_count is not None:
                                                    log.info(f"MacOSAdapter: admitted via keyboard path (verified — participant count {current_count} → {new_count})")
                                                    save_debug(page, "post_admit_success")
                                                    last_admit_attempt = (pill_text, new_count)
                                                else:
                                                    log.warning(f"MacOSAdapter: keyboard path — no count change in 3s (text={pill_text!r}); treating pill as stale")
                                                    last_admit_attempt = (pill_text, current_count)
                                            except Exception as e:
                                                log.warning(f"MacOSAdapter: keyboard admit failed: {e}")
                                                save_debug(page, "admit_keyboard_fail")
                            except Exception as e:
                                log.debug(f"MacOSAdapter: admit poll error: {e}")

                        # Network-loss alert check every 5s.
                        # Polls role="alert" (ARIA standard — stable across Meet UI updates).
                        # Tracks first-detection time so the 30s grace period starts from
                        # when the network actually dropped, not from our polling cycle.
                        if time.time() - last_alert_check >= 5:
                            last_alert_check = time.time()
                            try:
                                alert_text = page.evaluate("""() => {
                                    const el = document.querySelector('[role="alert"]');
                                    return el ? el.innerText.trim() : '';
                                }""")
                                if "lost your network" in alert_text.lower():
                                    if network_lost_at is None:
                                        network_lost_at = time.time()
                                        log.warning(
                                            "MacOSAdapter: network connection lost — "
                                            f"waiting up to {NETWORK_GRACE_SECONDS}s for recovery"
                                        )
                                    elif time.time() - network_lost_at >= NETWORK_GRACE_SECONDS:
                                        log.warning(
                                            f"MacOSAdapter: network lost for {NETWORK_GRACE_SECONDS}s — exiting"
                                        )
                                        print("\n⚠️  Operator: network connection lost — exiting.")
                                        break
                                else:
                                    if network_lost_at is not None:
                                        log.info("MacOSAdapter: network connection recovered — continuing")
                                    network_lost_at = None
                            except Exception:
                                pass  # page.is_closed() / inaccessible caught by the 30s check below

                        # Active health probe every 10s — catches frozen-WebSocket
                        # zombies that page.is_closed() can't detect (Mac sleep
                        # / network blip leaves the cached close state False
                        # while the underlying transport is dead). page.url
                        # touches the CDP transport and raises on an
                        # inaccessible page.
                        if time.time() - last_health >= 10:
                            last_health = time.time()
                            try:
                                if page.is_closed():
                                    self._browser_alive.clear()
                                    log.warning("MacOSAdapter: health check — page closed unexpectedly, exiting")
                                    print("\n⚠️  Operator: browser page closed unexpectedly — exiting.")
                                    break
                                current_url = page.url
                                if "meet.google.com" not in current_url:
                                    log.warning(f"MacOSAdapter: health check — unexpected URL: {current_url}")
                            except Exception:
                                self._browser_alive.clear()
                                log.warning("MacOSAdapter: health check — page not accessible, exiting")
                                print("\n⚠️  Operator: browser became inaccessible — exiting.")
                                break

                finally:
                    # ── Clean leave — runs on ALL exit paths ──────────
                    # Click Leave call so Meet's server registers the
                    # disconnect immediately (avoids ~60s ghost session).
                    self._page = None
                    try:
                        leave_btn = page.get_by_role("button", name="Leave call")
                        leave_btn.wait_for(timeout=2000)
                        leave_btn.click()
                        page.wait_for_timeout(500)
                        log.info("MacOSAdapter: clicked Leave call")
                    except Exception:
                        pass
                    try:
                        page.goto("about:blank", timeout=3000)
                    except Exception:
                        pass
                    # Intentionally no explicit browser.close() — on persistent
                    # contexts it can hang waiting for CDP responses that never
                    # arrive (Chrome may have self-exited after Leave). The
                    # sync_playwright __exit__ below handles teardown reliably.
                    # Drain any pending chat queue commands so callers unblock
                    # immediately instead of waiting for their full timeout.
                    while not self._chat_queue.empty():
                        try:
                            cmd, args, result_q = self._chat_queue.get_nowait()
                            if cmd == "read":
                                result_q.put([])
                            elif cmd == "participant_count":
                                result_q.put(0)
                            else:
                                result_q.put(None)
                        except queue.Empty:
                            break
                    self._browser_alive.clear()
                    self._browser_closed.set()

        except Exception as e:
            log.error(f"MacOSAdapter: browser session error: {e}")
            if not js.ready.is_set():
                js.signal_failure(f"exception: {e}")
        finally:
            # PID file may not exist if _write_operator_pid was never reached
            # (early failure). Anything other than FileNotFoundError on a file
            # we own would be unexpected and worth surfacing.
            pid_file = os.path.join(BROWSER_PROFILE, ".operator.pid")
            try:
                os.remove(pid_file)
            except FileNotFoundError:
                pass
            self._browser_alive.clear()
            if not self._browser_closed.is_set():
                self._browser_closed.set()
