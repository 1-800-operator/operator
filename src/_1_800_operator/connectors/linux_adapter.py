"""
Linux local connector for Operator.

Wraps headless Playwright/Chromium meeting join into the MeetingConnector
interface. Requires Playwright's Chromium browser installed via
`python3 -m playwright install chromium`.
"""
import logging
import os
import queue
import threading
import time

from playwright.sync_api import sync_playwright
from _1_800_operator import config

from .base import MeetingConnector
from .session import JoinStatus, detect_page_state, validate_auth_state, inject_cookies, save_debug

log = logging.getLogger(__name__)

# Stealth config — validated in tests/probe_a2_stealth_meet.py (PASSES)
# Removes the two main bot-detection signals from headless Chrome:
#   1. "HeadlessChrome" substring in the User-Agent string
#   2. navigator.webdriver = true (standard automation flag)
STEALTH_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined,
});
"""


class LinuxAdapter(MeetingConnector):
    """MeetingConnector for headless local Linux using Playwright Chromium."""

    def __init__(self, user_data_dir=None, auth_state_file=None):
        super().__init__()
        if user_data_dir is None:
            user_data_dir = config.BROWSER_PROFILE_DIR
        if auth_state_file is None:
            auth_state_file = config.AUTH_STATE_FILE
        self._user_data_dir = user_data_dir
        self._auth_state_file = auth_state_file  # path to storage_state JSON from auth_export.py
        self._leave_event = threading.Event()
        self._browser_closed = threading.Event()
        self._browser_thread = None
        self._page = None  # kept for send_chat/read_chat; set/cleared by browser thread
        self._seen_message_ids = set()
        self._chat_queue = queue.Queue()

    # ------------------------------------------------------------------
    # MeetingConnector interface
    # ------------------------------------------------------------------

    def join(self, meeting_url):
        """Start a headless browser session and join the meeting.
        Returns immediately; browser runs in a background thread until leave()."""
        self._leave_event.clear()
        self._browser_closed.clear()
        self.join_status = JoinStatus()
        self._browser_thread = threading.Thread(
            target=self._browser_session,
            args=(meeting_url,),
            daemon=True,
            name="LinuxAdapter-browser",
        )
        self._browser_thread.start()
        log.info(f"LinuxAdapter: joining {meeting_url}")

    def send_chat(self, message):
        """Post a message to the Google Meet chat panel.
        Queues the request for the browser thread (Playwright is single-threaded)."""
        result_q = queue.Queue()
        self._chat_queue.put(("send", message, result_q))
        result_q.get(timeout=10)

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
        """Return participant count via browser thread.

        Without this override, ChatRunner's `saw_others`/1-on-1/alone-grace
        signals never fire on Linux because the base default is 0. Selectors
        match macOS (Meet's DOM is platform-agnostic).
        """
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
            page.wait_for_timeout(500)
        except Exception as e:
            log.debug(f"LinuxAdapter: could not open chat panel: {e}")
            try:
                os.makedirs(config.DEBUG_DIR, exist_ok=True, mode=0o700)
                _shot = os.path.join(config.DEBUG_DIR, "chat_btn_not_found.png")
                page.screenshot(path=_shot)
                try:
                    os.chmod(_shot, 0o600)
                except OSError:
                    pass
                log.debug(f"LinuxAdapter: saved debug screenshot to {_shot}")
            except Exception:
                pass

    def _do_send_chat(self, page, message):
        """Actual send_chat logic — must run on browser thread."""
        self._ensure_chat_open(page)
        try:
            input_box = page.locator('textarea[aria-label="Send a message"]')
            input_box.wait_for(timeout=5000)
            input_box.fill(message)
            input_box.press("Enter")
            log.info(f"LinuxAdapter: chat sent: {message!r}")
        except Exception as e:
            log.warning(f"LinuxAdapter: send_chat failed: {e}")

    def _do_read_chat(self, page):
        """Actual read_chat logic — must run on browser thread."""
        self._ensure_chat_open(page)

        new_messages = []
        try:
            msg_els = page.locator("div[data-message-id]")
            count = msg_els.count()
            for i in range(count):
                el = msg_els.nth(i)
                msg_id = el.get_attribute("data-message-id")
                if msg_id in self._seen_message_ids:
                    continue
                self._seen_message_ids.add(msg_id)
                # Extract text — prefer first div[jsname] (any value), fallback chain
                jsname_el = el.locator('div[jsname]')
                if jsname_el.count() > 0:
                    text = jsname_el.first.inner_text().strip()
                else:
                    text = el.evaluate("""el => {
                        const fc = el.children[0]?.childNodes[0];
                        return (fc && fc.textContent) ? fc.textContent.trim() : el.innerText.trim();
                    }""")
                # Extract sender — walk up to 4 parents, find sibling div whose text
                # matches "Name\nTimestamp". Avoids depending on obfuscated class names.
                sender = ""
                try:
                    sender = el.evaluate("""el => {
                        const TIME_RE = /\\d{1,2}:\\d{2}\\s*(AM|PM)/i;
                        let node = el;
                        for (let d = 0; d < 4; d++) {
                            node = node.parentElement;
                            if (!node) break;
                            for (const sib of node.children) {
                                const t = sib.innerText?.trim();
                                if (t && TIME_RE.test(t)) {
                                    const lines = t.split('\\n');
                                    return lines.length >= 2 ? lines[0] : '';
                                }
                            }
                        }
                        return '';
                    }""")
                except Exception:
                    pass
                new_messages.append({"id": msg_id, "sender": sender, "text": text})
        except Exception as e:
            log.warning(f"LinuxAdapter: read_chat failed: {e}")
        return new_messages

    def _do_get_participant_count(self, page):
        """Count participants via data-requested-participant-id elements."""
        try:
            return page.locator('[data-requested-participant-id]').count()
        except Exception as e:
            log.warning(f"LinuxAdapter: get_participant_count failed: {e}")
            return 0

    def _do_get_participant_names(self, page):
        """Best-effort participant name scrape via tile DOM. Same selector
        chain as macOS — Meet's tile structure is platform-agnostic."""
        try:
            return page.evaluate("""() => {
                const tiles = document.querySelectorAll('[data-requested-participant-id]');
                const names = [];
                const seen = new Set();
                tiles.forEach(t => {
                    let name = '';
                    const labelled = t.querySelector('[data-self-name]');
                    if (labelled) name = (labelled.textContent || '').trim();
                    if (!name) {
                        const aria = t.getAttribute('aria-label') || '';
                        if (aria && !aria.includes('More options') && !aria.includes('Menu')) {
                            name = aria.trim();
                        }
                    }
                    if (!name) {
                        const txt = (t.textContent || '').trim();
                        if (txt && txt.length < 60) name = txt;
                    }
                    if (name && !seen.has(name)) {
                        seen.add(name);
                        names.push(name);
                    }
                });
                return names;
            }""") or []
        except Exception as e:
            log.warning(f"LinuxAdapter: get_participant_names failed: {e}")
            return []

    def _process_chat_queue(self, page):
        """Drain the chat command queue. Called from browser thread's idle loop."""
        while not self._chat_queue.empty():
            try:
                cmd, args, result_q = self._chat_queue.get_nowait()
            except queue.Empty:
                break
            if cmd == "send":
                self._do_send_chat(page, args)
                result_q.put(None)
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

        Returns True if admitted, False on timeout or leave().
        """
        timeout_seconds = config.LOBBY_WAIT_SECONDS
        deadline = time.time() + timeout_seconds
        wait_start = time.time()
        last_status_log = wait_start
        chunk_ms = 5000  # how often to re-check _leave_event

        WAITING_ROOM_SEL = 'img[alt*="Please wait until a meeting host"]'

        # Phase 1: confirm the page has settled into the waiting room
        log.info("LinuxAdapter: waiting for lobby screen to appear...")
        try:
            page.wait_for_selector(WAITING_ROOM_SEL, state="visible", timeout=10_000)
            log.info("LinuxAdapter: lobby confirmed — watching for host to admit us "
                     f"(timeout={timeout_seconds}s)")
        except Exception:
            elapsed = time.time() - wait_start
            log.info(
                f"LinuxAdapter: lobby screen not detected after {elapsed:.1f}s "
                f"— assuming already admitted or different join flow"
            )
            return True

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
                    f"LinuxAdapter: admitted — lobby screen gone "
                    f"(event-driven, waited {elapsed:.1f}s total)"
                )
                return True
            except Exception:
                if page.is_closed():
                    log.info("LinuxAdapter: browser closed during admission wait — aborting")
                    return False

            if time.time() - last_status_log >= 30:
                elapsed = time.time() - wait_start
                log.info(f"LinuxAdapter: still in waiting room ({elapsed:.0f}s elapsed)")
                last_status_log = time.time()

        elapsed = time.time() - wait_start
        if self._leave_event.is_set():
            log.info(f"LinuxAdapter: admission wait cancelled (leave called after {elapsed:.0f}s)")
        else:
            log.warning(f"LinuxAdapter: admission timeout after {elapsed:.0f}s")
        return False

    def is_connected(self):
        """Return True if the browser session is still alive."""
        return not self._browser_closed.is_set()

    def leave(self):
        """Signal the browser session to close.
        Safe to call multiple times — only the first call does work."""
        if self._leave_event.is_set():
            return
        self._leave_event.set()
        if self._browser_thread and self._browser_thread.is_alive():
            log.info("LinuxAdapter: waiting for browser to close...")
            if not self._browser_closed.wait(timeout=10):
                log.warning("LinuxAdapter: browser close timed out (10s)")
        log.info("LinuxAdapter: left meeting")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _browser_session(self, meeting_url):
        """Run headless Playwright/Chromium session. Blocks until leave() is called."""
        # Lock the profile dir to owner-only — contents include Google session
        # cookies and shouldn't be listable by other users on shared hosts.
        # mode= on makedirs makes creation atomic-private; chmod is the
        # belt for the case where the dir already exists with looser perms.
        os.makedirs(self._user_data_dir, exist_ok=True, mode=0o700)
        try:
            os.chmod(self._user_data_dir, 0o700)
        except OSError as e:
            log.warning(f"LinuxAdapter: could not tighten perms on {config.relativize_home(self._user_data_dir)}: {e}")
        js = self.join_status
        browser = None
        try:
            with sync_playwright() as p:
                # Re-add --no-sandbox here if running as root (e.g. in a container).
                launch_args = [
                    "--use-fake-ui-for-media-stream",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    "--no-sandbox",  # required when running as root (droplet/server)
                    "--disable-features=WebRTCPipeWireCapturer",  # force PulseAudio for WebRTC; PipeWire not installed
                ]
                _use_auth = self._auth_state_file and os.path.isfile(self._auth_state_file)
                if not _use_auth and self._auth_state_file:
                    log.info(f"LinuxAdapter: {config.relativize_home(self._auth_state_file)} not found — using guest mode")
                if _use_auth:
                    # Authenticated path: launch + new_context with saved session.
                    # headless=False + DISPLAY (Xvfb) enables audio rendering —
                    # headless Chrome suppresses audio output entirely.
                    # Do NOT pass env= — Playwright replaces the full environment if
                    # you do, stripping XDG_RUNTIME_DIR and breaking PulseAudio discovery.
                    # DISPLAY is already set in os.environ by the caller (scripts/run_linux.py).
                    log.info(f"LinuxAdapter: loading auth state from {config.relativize_home(self._auth_state_file)}")
                    raw_browser = p.chromium.launch(
                        headless=False,
                        args=launch_args,
                    )
                    browser = raw_browser.new_context(
                        storage_state=self._auth_state_file,
                        user_agent=STEALTH_USER_AGENT,
                        viewport={"width": 1920, "height": 1080},
                    )
                    page = browser.new_page()
                    # Wrap close so leave() works the same way for both paths
                    browser._raw_browser = raw_browser
                else:
                    # Unauthenticated guest path: persistent context
                    browser = p.chromium.launch_persistent_context(
                        user_data_dir=self._user_data_dir,
                        headless=True,
                        user_agent=STEALTH_USER_AGENT,
                        viewport={"width": 1920, "height": 1080},
                        args=launch_args,
                    )
                    page = browser.pages[0] if browser.pages else browser.new_page()
                page.add_init_script(STEALTH_JS)
                self._page = page

                try:
                    page.goto(meeting_url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(8000)

                    # --- Session recovery ladder (authenticated path only) ---
                    recovered = False
                    if _use_auth:
                        state = detect_page_state(page)

                        if state == "logged_out":
                            log.warning("LinuxAdapter: session expired — attempting cookie recovery")
                            auth = validate_auth_state(self._auth_state_file)
                            if auth and inject_cookies(browser, auth):
                                page.reload(wait_until="domcontentloaded", timeout=30000)
                                page.wait_for_timeout(8000)
                                state = detect_page_state(page)
                                if state == "pre_join":
                                    log.info("LinuxAdapter: session recovered via cookie injection")
                                    recovered = True
                                else:
                                    log.error(f"LinuxAdapter: recovery failed — page state: {state}")
                                    save_debug(page, "recovery_fail")
                                    js.signal_failure("session_expired")
                                    return
                            else:
                                log.error("LinuxAdapter: no valid auth_state for recovery")
                                save_debug(page, "no_auth_state")
                                js.signal_failure("session_expired")
                                return

                        if state == "cant_join":
                            log.error("LinuxAdapter: 'can't join this video call'")
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
                            log.info("LinuxAdapter: already in meeting — skipping pre-join flow")
                    except Exception:
                        pass

                    # --- Pre-join screen actions ---

                    # Dismiss notifications popup if present
                    try:
                        not_now = page.get_by_role("button", name="Not now")
                        not_now.wait_for(timeout=3000)
                        not_now.click()
                        page.wait_for_timeout(500)
                        log.debug("LinuxAdapter: dismissed notifications popup")
                    except Exception:
                        pass

                    # Turn off camera and confirm before joining
                    save_debug(page, "pre_camera_toggle")
                    try:
                        cam_btn = page.get_by_role("button", name="Turn off camera")
                        cam_btn.wait_for(timeout=5000)
                        cam_btn.click()
                        log.info("LinuxAdapter: clicked 'Turn off camera'")
                        try:
                            page.wait_for_selector(
                                '[role="button"][data-is-muted="true"][aria-label*="camera"]',
                                timeout=3000,
                            )
                            log.info("LinuxAdapter: camera confirmed off (data-is-muted=true)")
                        except Exception:
                            log.warning("LinuxAdapter: camera toggle clicked but could not confirm off state")
                            save_debug(page, "camera_not_confirmed")
                    except Exception:
                        log.warning("LinuxAdapter: 'Turn off camera' button not found — camera may be on")
                        save_debug(page, "camera_btn_missing")

                    # Fill in guest name if present (unauthenticated join shows a name field)
                    try:
                        name_input = page.get_by_placeholder("Your name")
                        name_input.wait_for(timeout=3000)
                        name_input.fill("Operator")
                        page.wait_for_timeout(500)
                        log.debug("LinuxAdapter: filled guest name")
                    except Exception:
                        pass  # signed-in users don't see this field

                    # Race all join buttons — avoids 5s timeout per missing button
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
                                    log.debug(f"LinuxAdapter: clicked {label!r}")
                                    break
                        except Exception:
                            pass

                    if clicked_label is None:
                        save_debug(page, "join_fail")
                        log.warning("LinuxAdapter: could not find join button")
                        js.signal_failure("no_join_button")
                        return

                    if clicked_label == "Ask to join":
                        if not self._wait_for_admission(page):
                            save_debug(page, "admission_fail")
                            js.signal_failure("admission_timeout")
                            return

                    log.info("LinuxAdapter: joined meeting successfully")
                    js.signal_success(recovered=recovered)

                    log.info("LinuxAdapter: in meeting — holding browser open")

                    # Hold until leave() signals or 4-hour hard cap.
                    deadline = time.time() + 4 * 3600
                    last_health = time.time()
                    last_alert_check = time.time()
                    last_admit_check = time.time()
                    t_hold_start = time.time()
                    admit_diagnostic_saved = False
                    network_lost_at = None  # set when alert first detected, cleared on recovery
                    NETWORK_GRACE_SECONDS = 30  # exit only if alert persists this long
                    while not self._leave_event.is_set() and time.time() < deadline:
                        self._process_chat_queue(page)
                        page.wait_for_timeout(500)

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
                        import re as _re
                        if time.time() - last_admit_check >= 2:
                            last_admit_check = time.time()
                            try:
                                pill = page.get_by_text(
                                    _re.compile(r"^Admit\s+\d+\s+(guest|people)", _re.I)
                                ).first
                                if pill.count() > 0 and pill.is_visible():
                                    try:
                                        pill.hover()
                                        page.wait_for_timeout(400)
                                    except Exception as e:
                                        log.debug(f"LinuxAdapter: pill hover failed: {e}")

                                    if not admit_diagnostic_saved:
                                        save_debug(page, "admit_diagnostic")
                                        admit_diagnostic_saved = True

                                    admit_btn = page.get_by_role(
                                        "button", name=_re.compile(r"^Admit$", _re.I)
                                    ).first
                                    clicked = False
                                    if admit_btn.count() > 0 and admit_btn.is_visible():
                                        try:
                                            admit_btn.click(timeout=1000)
                                            log.info("LinuxAdapter: admitted via tray click")
                                            clicked = True
                                        except Exception as e:
                                            log.warning(f"LinuxAdapter: tray admit click failed: {e}")

                                    if not clicked:
                                        try:
                                            pill.focus()
                                            page.keyboard.press("ArrowDown")
                                            page.wait_for_timeout(200)
                                            page.keyboard.press("Enter")
                                            log.info("LinuxAdapter: admitted via keyboard path")
                                        except Exception as e:
                                            log.warning(f"LinuxAdapter: keyboard admit failed: {e}")
                                            save_debug(page, "admit_keyboard_fail")
                            except Exception as e:
                                log.debug(f"LinuxAdapter: admit poll error: {e}")

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
                                            "LinuxAdapter: network connection lost — "
                                            f"waiting up to {NETWORK_GRACE_SECONDS}s for recovery"
                                        )
                                    elif time.time() - network_lost_at >= NETWORK_GRACE_SECONDS:
                                        log.warning(
                                            f"LinuxAdapter: network lost for {NETWORK_GRACE_SECONDS}s — exiting"
                                        )
                                        print("\n⚠️  Operator: network connection lost — exiting.")
                                        break
                                else:
                                    if network_lost_at is not None:
                                        log.info("LinuxAdapter: network connection recovered — continuing")
                                    network_lost_at = None
                            except Exception:
                                pass  # page.is_closed() / inaccessible caught by the 30s check below

                        # Health check every 30s — page liveness and URL drift.
                        if time.time() - last_health >= 30:
                            last_health = time.time()
                            try:
                                if page.is_closed():
                                    log.warning("LinuxAdapter: health check — page closed unexpectedly, exiting")
                                    print("\n⚠️  Operator: browser page closed unexpectedly — exiting.")
                                    break
                                current_url = page.url
                                if "meet.google.com" not in current_url:
                                    log.warning(f"LinuxAdapter: health check — unexpected URL: {current_url}")
                            except Exception:
                                log.warning("LinuxAdapter: health check — page not accessible, exiting")
                                print("\n⚠️  Operator: browser became inaccessible — exiting.")
                                break

                finally:
                    # ── Clean leave — runs on ALL exit paths ──────────
                    self._page = None
                    try:
                        leave_btn = page.get_by_role("button", name="Leave call")
                        leave_btn.wait_for(timeout=2000)
                        leave_btn.click()
                        page.wait_for_timeout(500)
                        log.info("LinuxAdapter: clicked Leave call")
                    except Exception:
                        try:
                            page.goto("about:blank", timeout=3000)
                            log.info("LinuxAdapter: navigated away (Leave button not found)")
                        except Exception:
                            pass
                    def _close_browser():
                        try:
                            browser.close()
                            if hasattr(browser, "_raw_browser"):
                                browser._raw_browser.close()
                        except Exception:
                            pass
                    close_t = threading.Thread(target=_close_browser, daemon=True)
                    close_t.start()
                    close_t.join(timeout=5)
                    if close_t.is_alive():
                        log.warning("LinuxAdapter: browser.close() timed out (5s) — forcing exit")
                    else:
                        log.info("LinuxAdapter: browser closed")
                    # Drain pending chat queue commands so callers unblock immediately
                    while not self._chat_queue.empty():
                        try:
                            cmd, args, result_q = self._chat_queue.get_nowait()
                            if cmd == "read":
                                result_q.put([])
                            elif cmd == "participant_count":
                                result_q.put(0)
                            elif cmd == "participant_names":
                                result_q.put([])
                            else:
                                result_q.put(None)
                        except queue.Empty:
                            break
                    self._browser_closed.set()

        except Exception as e:
            log.error(f"LinuxAdapter: browser session error: {e}")
            if not js.ready.is_set():
                js.signal_failure(f"exception: {e}")
        finally:
            self._page = None
            if not self._browser_closed.is_set():
                self._browser_closed.set()
