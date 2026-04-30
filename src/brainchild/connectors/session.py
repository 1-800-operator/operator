"""
Session recovery utilities for Google Meet connectors.

Provides detection of logged-out / revoked-session states, cookie injection
from auth_state.json, and a JoinStatus primitive for browser→runner signalling.
"""
import json
import logging
import os
import threading

from brainchild import config

log = logging.getLogger(__name__)


class JoinStatus:
    """Thread-safe join result communicated from browser thread to runner."""

    def __init__(self):
        self.ready = threading.Event()
        self.success = False
        self.failure_reason = None   # str | None
        self.session_recovered = False

    def signal_success(self, recovered=False):
        self.success = True
        self.session_recovered = recovered
        self.ready.set()

    def signal_failure(self, reason):
        self.success = False
        self.failure_reason = reason
        self.ready.set()


def _chrome_lock_is_live(lock_path):
    """Return True if the SingletonLock symlink points to a running process."""
    try:
        target = os.readlink(lock_path)   # e.g. "mymac-12345"
        pid = int(target.rsplit("-", 1)[-1])
        os.kill(pid, 0)                   # signal 0 = existence check only
        return True
    except (OSError, ValueError):
        return False


def _write_brainchild_pid(lock_path):
    """Write the current process PID to a file alongside the SingletonLock.

    Called at the start of each browser session so --force can find and
    terminate the Brainchild Python process, not just Chrome.
    """
    pid_file = os.path.join(os.path.dirname(lock_path), ".brainchild.pid")
    try:
        with open(pid_file, "w") as f:
            f.write(str(os.getpid()))
    except OSError as e:
        log.warning(f"session: could not write brainchild PID file: {e}")


def _chrome_kill_and_clear(lock_path):
    """Kill the Brainchild session (Python process + Chrome) and clear the lock.

    Kills the Python brainchild process first via the PID file (its SIGTERM
    handler triggers a clean shutdown including browser.close()).  Chrome is
    killed as a fallback in case the PID file is absent or the process is
    already dead.  Safe to call on a stale or already-gone lock.
    """
    import signal as _signal
    import time as _time

    pid_file = os.path.join(os.path.dirname(lock_path), ".brainchild.pid")

    # ── 1. Kill the Brainchild Python process ──────────────────────────
    brainchild_pid = None
    try:
        with open(pid_file) as f:
            brainchild_pid = int(f.read().strip())
        if brainchild_pid == os.getpid():
            brainchild_pid = None          # never kill ourselves
    except (FileNotFoundError, ValueError):
        pass

    if brainchild_pid:
        try:
            reason_file = os.path.join(os.path.dirname(lock_path), ".brainchild.kill_reason")
            with open(reason_file, "w") as f:
                f.write("Terminated: killed by another Brainchild instance (--force)")
        except OSError:
            pass
        try:
            os.kill(brainchild_pid, _signal.SIGTERM)
            for _ in range(30):          # wait up to 3 s for clean exit
                _time.sleep(0.1)
                try:
                    os.kill(brainchild_pid, 0)
                except OSError:
                    break                # gone
            else:
                try:
                    os.kill(brainchild_pid, _signal.SIGKILL)
                except OSError:
                    pass
        except OSError:
            pass                         # already gone

    # ── 2. Kill Chrome as a fallback ─────────────────────────────────
    # Handles the case where the PID file was absent or Python didn't
    # close Chrome before exiting.
    try:
        target = os.readlink(lock_path)
        chrome_pid = int(target.rsplit("-", 1)[-1])
        try:
            os.kill(chrome_pid, _signal.SIGTERM)
        except OSError:
            pass
        for _ in range(20):              # wait up to 2 s
            _time.sleep(0.1)
            try:
                os.kill(chrome_pid, 0)
            except OSError:
                break
        else:
            try:
                os.kill(chrome_pid, _signal.SIGKILL)
            except OSError:
                pass
    except (OSError, ValueError):
        pass

    # ── 3. Remove lock and PID file ───────────────────────────────────
    for path in (lock_path, pid_file):
        try:
            if os.path.islink(path) or os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def detect_page_state(page):
    """Classify what Google Meet is showing after navigation.

    Returns one of: "pre_join", "logged_out", "cant_join", "unknown".
    """
    url = page.url

    # Redirected to Google sign-in
    if "accounts.google.com" in url:
        log.info(f"session: detected logged-out state (URL: {url})")
        return "logged_out"

    # Check for "can't join" text on the page
    try:
        cant_join = page.locator("text=You can't join this video call")
        if cant_join.count() > 0:
            # Distinguish auth failure from host controls
            # by checking if browser has Google session cookies
            try:
                cookies = page.context.cookies()
                has_session = any(
                    c.get("name") == "SID" and ".google.com" in c.get("domain", "")
                    for c in cookies
                )
            except Exception:
                has_session = False

            if not has_session:
                log.info("session: 'can't join' but no Google session cookie — treating as logged_out")
                return "logged_out"

            log.info("session: detected 'can't join' state (authenticated — likely host controls)")
            return "cant_join"
    except Exception:
        pass

    # Check for join buttons — indicates normal pre-join screen
    for label in ["Join now", "Ask to join", "Switch here"]:
        try:
            btn = page.get_by_role("button", name=label)
            if btn.count() > 0:
                return "pre_join"
        except Exception:
            continue

    # Check for re-auth challenge on meet.google.com itself
    try:
        sign_in = page.locator("text=Sign in")
        if sign_in.count() > 0:
            log.info("session: detected sign-in prompt on Meet page")
            return "logged_out"
    except Exception:
        pass

    log.info(f"session: unknown page state (URL: {url})")
    return "unknown"


def validate_auth_state(path):
    """Load auth_state.json and check it contains a .google.com SID cookie.

    Returns the parsed dict on success, None on failure.
    Only validates structure — server-side revocation is caught after injection.
    """
    if not path:
        return None
    try:
        with open(path) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log.warning(f"session: cannot load auth state from {path}: {e}")
        return None

    cookies = state.get("cookies", [])
    has_sid = any(
        c.get("name") == "SID" and ".google.com" in c.get("domain", "")
        for c in cookies
    )
    if not has_sid:
        log.warning("session: auth_state.json has no .google.com SID cookie")
        return None

    log.info(f"session: auth_state.json valid ({len(cookies)} cookies)")
    return state


def inject_cookies(context, auth_state):
    """Inject .google.com cookies from auth_state into a Playwright context.

    Returns True on success, False on failure.
    """
    cookies = [
        c for c in auth_state.get("cookies", [])
        if ".google.com" in c.get("domain", "")
    ]
    if not cookies:
        log.warning("session: no .google.com cookies to inject")
        return False

    try:
        context.add_cookies(cookies)
        log.info(f"session: injected {len(cookies)} .google.com cookies")
        return True
    except Exception as e:
        log.error(f"session: cookie injection failed: {e}")
        return False


def save_debug(page, label="debug"):
    """Save a screenshot and HTML dump for diagnosis."""
    debug_dir = config.DEBUG_DIR
    os.makedirs(debug_dir, exist_ok=True, mode=0o700)
    try:
        os.chmod(debug_dir, 0o700)
    except OSError:
        pass
    png_path = os.path.join(debug_dir, f"{label}.png")
    html_path = os.path.join(debug_dir, f"{label}.html")
    try:
        page.screenshot(path=png_path, full_page=True)
        try:
            os.chmod(png_path, 0o600)
        except OSError:
            pass
        log.info(f"session: screenshot saved to {debug_dir}/{label}.png")
    except Exception as e:
        log.warning(f"session: screenshot failed: {e}")
    try:
        with open(html_path, "w") as f:
            f.write(page.content())
        try:
            os.chmod(html_path, 0o600)
        except OSError:
            pass
        log.info(f"session: HTML saved to {debug_dir}/{label}.html")
    except Exception as e:
        log.warning(f"session: HTML dump failed: {e}")
