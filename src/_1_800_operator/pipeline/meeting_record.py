"""
MeetingRecord — per-meeting JSONL chat log that doubles as LLM history.

File path: ~/.operator/history/<meet_slug>.jsonl
One JSON object per line: {"timestamp": float, "sender": str, "text": str, "kind": "chat"}.

Append-only. Local-only. Users can delete ~/.operator/history/ freely.

Permissions: the directory and JSONL files hold meeting transcripts that may
include sensitive chat / caption content. We harden them owner-only on every
open: mkdir mode=0o700 + defensive chmod on the dir, chmod 0o600 on the file.
This covers both fresh installs (umask already enforces these modes via
__main__.main's umask 0o077) AND legacy installs from before the umask fix
where the dir/files were created world-readable.
"""
import json
import logging
import os
import re
import threading
import time
from collections import deque
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
        # Kept on self so callers can read back meeting_url, mode, etc.
        # without re-parsing the JSONL header.
        self.meta = dict(meta or {})
        self._lock = threading.Lock()
        self._memory: list[dict] = []
        # In-memory chat tail — serves tail_chat() without re-reading the
        # JSONL on every LLM turn. Captions and meta entries are NOT mirrored
        # here; that's deliberate, see tail_chat() docstring.
        self._chat_tail: deque[dict] = deque(maxlen=200)
        if slug is None:
            self.path = None
            log.info("MeetingRecord opened in-memory (no slug)")
            return
        self.root = root or DEFAULT_ROOT
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Defensive chmod for legacy installs whose history dir predates
        # __main__.main's umask 0o077 (session 172 credential-hygiene fix).
        # mkdir's mode= is a no-op when the dir already exists; this is the
        # belt that retroactively tightens those.
        try:
            os.chmod(self.root, 0o700)
        except OSError as e:
            log.warning(f"MeetingRecord: chmod 0o700 on {self.root} failed: {e}")
        self.path = self.root / f"{slug}.jsonl"
        # Open-time writes, batched into one file open so header + session_start
        # land atomically (either both or neither) on every meeting open:
        # - meta header (first open of a new file only): self-describing,
        #   `head -1 file.jsonl` reveals meeting URL, slug, first-joined time.
        # - session_start marker (every open): tail() only replays entries
        #   after the most recent marker, so the LLM never sees prior runs'
        #   assistant answers and stops short-circuiting tool calls by echoing.
        is_new = not self.path.exists() or self.path.stat().st_size == 0
        boot_entries: list[dict] = []
        if is_new:
            boot_entries.append({
                "kind": "meta",
                "created_at": time.time(),
                "slug": slug,
                **(meta or {}),
            })
        boot_entries.append({"kind": "session_start", "timestamp": time.time()})
        with self.path.open("a", encoding="utf-8") as f:
            for entry in boot_entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        # Tighten file perms once on open. Covers brand-new files (where
        # the umask already produced 0o600 — chmod is a no-op then) and
        # legacy files from before the umask fix that may be world-readable.
        # chmod can legitimately fail on network FS that doesn't support mode
        # bits — keep the try/except, this isn't the same trust-the-OS case
        # as the file write.
        try:
            os.chmod(self.path, 0o600)
        except OSError as e:
            log.warning(f"MeetingRecord: chmod 0o600 on {self.path} failed: {e}")
        log.info(f"MeetingRecord opened {self.path}")

    def close(self, *, attended: list[str] | None = None,
              currently_present: list[str] | None = None,
              self_name: str = "") -> None:
        """Write meeting-end lifecycle events: an optional `participants_final`
        line (if `attended` is provided) followed by a `meeting_end` line.

        Read by `find_meetings` and `list_meetings` to surface attendee
        lists for post-meeting lookup without scanning the full event
        stream. The transient `.current_meeting_participants.json` snapshot
        covers live queries; `participants_final` is the durable record
        baked into the JSONL itself at meeting end.

        Idempotent: subsequent calls are no-ops (a second `meeting_end`
        would imply the meeting "ended twice"). In-memory mode (no path)
        is a no-op — post-meeting lookup needs disk-resident JSONLs.
        """
        if self.path is None:
            return
        with self._lock:
            if getattr(self, "_closed", False):
                return
            self._closed = True
            now = time.time()
            entries: list[dict] = []
            if attended is not None:
                entries.append({
                    "kind": "participants_final",
                    "timestamp": now,
                    "currently_present": list(currently_present or []),
                    "attended": list(attended),
                    "self_name": self_name or "",
                })
            entries.append({"kind": "meeting_end", "timestamp": now})
            try:
                with self.path.open("a", encoding="utf-8") as f:
                    for entry in entries:
                        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError as e:
                log.warning(f"MeetingRecord close write failed: {e}")

    def append(self, sender: str, text: str, kind: str = "chat",
               timestamp: float | None = None) -> dict:
        entry = {
            "timestamp": timestamp if timestamp is not None else time.time(),
            "sender": sender,
            "text": text,
            "kind": kind,
        }
        with self._lock:
            if self.path is None:
                self._memory.append(entry)
            else:
                line = json.dumps(entry, ensure_ascii=False)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            if kind == "chat":
                self._chat_tail.append(entry)
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

    def tail_chat(self, n: int) -> list[dict]:
        """Return the last n chat entries, oldest first. In-memory; no disk read.

        The hot path for LLM context: every chat turn calls this. Backed by
        a deque populated in append(), so it's O(n) with no I/O — the JSONL
        on disk can grow to MBs over a long meeting without affecting per-turn
        latency. Captions and other kinds are not mirrored here; consumers
        that want them should use tail() (file-backed, generic) or query the
        bundled transcript MCP server.
        """
        if n <= 0:
            return []
        with self._lock:
            return list(self._chat_tail)[-n:]
