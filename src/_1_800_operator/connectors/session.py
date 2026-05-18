"""
Session utilities for the Meet connector.

The JoinStatus primitive for browser→runner signalling, a Meet-room URL
matcher, and on-failure debug artifact dumps.
"""
import logging
import os
import re
import threading
from urllib.parse import urlparse

from _1_800_operator import config

log = logging.getLogger(__name__)

# Meet room codes look like `abc-defg-hij` — three lowercase letter groups
# separated by hyphens. Used to distinguish a real meeting URL from the
# `/new` interstitial (which may carry query strings like `?authuser=0&hs=178`).
_MEET_ROOM_RE = re.compile(r"^/[a-z]{3,}-[a-z]{3,}-[a-z]{3,}/?$")
_MEET_CODE_RE = re.compile(r"^[a-z]{3,}-[a-z]{3,}-[a-z]{3,}$")


def _is_real_meet_room(url: str) -> bool:
    """True iff `url` is a meet.google.com room URL like /abc-defg-hij.

    Rejects /new, /landing, /lookup, missing path, and non-meet hosts.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if "meet.google.com" not in (parsed.netloc or ""):
        return False
    return bool(_MEET_ROOM_RE.match(parsed.path or ""))


def normalize_meet_url(raw: str | None) -> str | None:
    """Canonicalize a user-pasted Meet URL to `https://meet.google.com/<code>`.

    Tolerates the shapes a user is likely to paste from the browser:
    surrounding whitespace, missing scheme, `?authuser=N`/`?hs=…` query
    strings, `#fragment`, trailing slash. Validates that the path is a
    Meet room code (xxx-xxxx-xxx style); returns None for anything else
    so the caller can reject the input before daemonizing.
    """
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    if not s.startswith(("http://", "https://")):
        s = "https://" + s
    try:
        parsed = urlparse(s)
    except Exception:
        return None
    if "meet.google.com" not in (parsed.netloc or "").lower():
        return None
    code = (parsed.path or "").strip("/")
    if not _MEET_CODE_RE.match(code):
        return None
    return f"https://meet.google.com/{code}"


class JoinStatus:
    """Thread-safe join result communicated from browser thread to runner."""

    def __init__(self):
        self.ready = threading.Event()
        self.success = False
        self.failure_reason = None   # str | None

    def signal_success(self):
        self.success = True
        self.ready.set()

    def signal_failure(self, reason):
        self.success = False
        self.failure_reason = reason
        self.ready.set()


def save_debug(page, label="debug"):
    """Save a screenshot and HTML dump for diagnosis."""
    debug_dir = config.DEBUG_DIR
    os.makedirs(debug_dir, exist_ok=True, mode=0o700)
    os.chmod(debug_dir, 0o700)
    png_path = os.path.join(debug_dir, f"{label}.png")
    html_path = os.path.join(debug_dir, f"{label}.html")
    try:
        page.screenshot(path=png_path, full_page=True)
        os.chmod(png_path, 0o600)
        log.info(f"session: screenshot saved to {debug_dir}/{label}.png")
    except Exception as e:
        log.warning(f"session: screenshot failed: {e}")
    try:
        with open(html_path, "w") as f:
            f.write(page.content())
        os.chmod(html_path, 0o600)
        log.info(f"session: HTML saved to {debug_dir}/{label}.html")
    except Exception as e:
        log.warning(f"session: HTML dump failed: {e}")
