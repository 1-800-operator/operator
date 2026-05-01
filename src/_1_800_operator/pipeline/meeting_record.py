"""
MeetingRecord — per-meeting JSONL chat log that doubles as LLM history.

File path: ~/.operator/history/<meet_slug>.jsonl
One JSON object per line: {"timestamp": float, "sender": str, "text": str, "kind": "chat"}.

Append-only. Local-only. Users can delete ~/.operator/history/ freely.
"""
import json
import logging
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)

DEFAULT_ROOT = Path.home() / ".operator" / "history"


def slug_from_url(url: str) -> str:
    """Derive a stable meeting slug from a Google Meet URL.

    https://meet.google.com/pgy-qauk-frn → 'pgy-qauk-frn'. Returns
    'unknown-meeting' if the URL has no usable path.
    """
    if not url:
        return "unknown-meeting"
    try:
        path = urlparse(url).path.strip("/")
    except Exception:
        path = ""
    if not path:
        path = url.strip("/")
    clean = re.sub(r"[^A-Za-z0-9-]", "", path)
    return clean or "unknown-meeting"


class MeetingRecord:
    """Append-only JSONL transcript for a single meeting.

    If `slug` is given, writes to <root>/<slug>.jsonl. If `slug` is None,
    keeps entries in memory only (useful for tests and for runs without
    a stable meeting id).
    """

    def __init__(self, slug: str | None = None, root: Path | None = None,
                 meta: dict | None = None):
        self.slug = slug
        self._lock = threading.Lock()
        self._memory: list[dict] = []
        if slug is None:
            self.path = None
            log.info("MeetingRecord opened in-memory (no slug)")
            return
        self.root = root or DEFAULT_ROOT
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / f"{slug}.jsonl"
        # Write a one-time meta header on first open of a new file so the
        # record is self-describing: `head -1 file.jsonl` reveals the
        # meeting URL, slug, and when it was first joined. Rejoins of an
        # existing record leave the header alone.
        is_new = not self.path.exists() or self.path.stat().st_size == 0
        if is_new:
            header = {
                "kind": "meta",
                "created_at": time.time(),
                "slug": slug,
                **(meta or {}),
            }
            try:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(header, ensure_ascii=False) + "\n")
            except OSError as e:
                log.warning(f"MeetingRecord header write failed: {e}")
        # Session-boundary marker. tail() only replays entries after the
        # most recent marker, so the LLM never sees prior runs' assistant
        # answers and stops short-circuiting tool calls by echoing them.
        boundary = {"kind": "session_start", "timestamp": time.time()}
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(boundary, ensure_ascii=False) + "\n")
        except OSError as e:
            log.warning(f"MeetingRecord session_start write failed: {e}")
        log.info(f"MeetingRecord opened {self.path}")

    def append(self, sender: str, text: str, kind: str = "chat",
               timestamp: float | None = None) -> dict:
        entry = {
            "timestamp": timestamp if timestamp is not None else time.time(),
            "sender": sender,
            "text": text,
            "kind": kind,
        }
        with self._lock:
            if self.path is not None:
                line = json.dumps(entry, ensure_ascii=False)
                try:
                    with self.path.open("a", encoding="utf-8") as f:
                        f.write(line + "\n")
                except OSError as e:
                    log.warning(f"MeetingRecord append failed ({self.path}): {e}")
            self._memory.append(entry)
        return entry

    def tail(self, n: int) -> list[dict]:
        """Return the last n entries from the current session, oldest first.

        Scoped to entries after the most recent `session_start` marker —
        prior sessions leaked their assistant replies into the LLM prompt
        and caused the model to echo stale answers instead of calling tools.
        """
        if n <= 0:
            return []
        if self.path is None or not self.path.exists():
            with self._lock:
                return list(self._memory[-n:])
        try:
            with self.path.open("r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError as e:
            log.warning(f"MeetingRecord tail read failed: {e}")
            return []
        parsed: list[dict] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                parsed.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning(f"MeetingRecord skipping malformed line: {line[:80]!r}")
        start_idx = 0
        for i in range(len(parsed) - 1, -1, -1):
            if parsed[i].get("kind") == "session_start":
                start_idx = i + 1
                break
        return parsed[start_idx:][-n:]
