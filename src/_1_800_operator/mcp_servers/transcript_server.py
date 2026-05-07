"""Transcript MCP server — exposes the live meeting JSONL as tools.

Three verbs are exposed:

  - search_captions(query, speaker?, start_minutes_ago?, end_minutes_ago?,
                    context_lines=0, limit=20)
        Substring (case-insensitive) keyword search. Optional speaker
        filter and time window. Each match returns with ±N surrounding
        captions for context. Non-contiguous spans separated by blank
        lines.

  - list_captions(start_minutes_ago?, end_minutes_ago?, last_n?,
                  speaker?, limit=100)
        Chronological browse. Either a time window OR last_n captions.
        Optional speaker filter.

  - list_speakers()
        Speakers heard so far this session, with caption counts and
        time-since-last-spoke.

All tools return plain-text empty-state prose rather than raising, so
the model can relay them. A byte ceiling is enforced on every result so
the model's context can't be blown by an over-broad query — when the
ceiling trips, the result is trimmed and a clear hint is appended.

The meeting record path comes from one of two sources, in order:

  1. The marker file at ~/.operator/.current_meeting (written by the
     bot at meeting-join time, deleted at leave). Lets MCP registrations
     that don't get per-meeting env interpolation — e.g. a server the
     user added once via `claude mcp add` and reuses across meetings —
     still pick up the active meeting JSONL.

  2. The OPERATOR_MEETING_RECORD_PATH env var, set per-meeting by the
     bot's bundled MCP spawn path (claude_cli.py:_maybe_write_mcp_config).

If neither is set, or the file doesn't exist yet, tools return a friendly
empty-state string.

Run via:
    python -m _1_800_operator.mcp_servers.transcript_server
"""
import json
import os
import time
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

ENV_PATH = "OPERATOR_MEETING_RECORD_PATH"
MARKER_FILE = Path.home() / ".operator" / ".current_meeting"

RESULT_BYTE_CEILING = 12000
DEFAULT_LIST_LIMIT = 100
DEFAULT_SEARCH_LIMIT = 20

mcp = FastMCP("operator-transcript")


def _now() -> float:
    """Wall-clock now, factored for tests to monkeypatch."""
    return time.time()


def _resolve_record_path() -> Path | None:
    """Return the active meeting JSONL path, or None if unwired.

    Marker file wins over env var so a codex-style agent registering
    this MCP via static config can still pick up the active meeting.
    """
    if MARKER_FILE.exists():
        try:
            marker = MARKER_FILE.read_text(encoding="utf-8").strip()
            if marker:
                return Path(marker)
        except OSError:
            pass
    env_val = os.environ.get(ENV_PATH)
    if env_val:
        return Path(env_val)
    return None


def _format_caption(entry: dict, marker: str = "  ") -> str:
    ts = entry.get("timestamp")
    speaker = entry.get("sender") or "?"
    text = (entry.get("text") or "").strip()
    if isinstance(ts, (int, float)):
        clock = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        return f"{marker}[{clock} {speaker}] {text}"
    return f"{marker}[{speaker}] {text}"


def _read_captions(path: Path) -> list[dict]:
    """Return caption entries from the most recent session, oldest first."""
    try:
        with path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    parsed: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    start_idx = 0
    for i in range(len(parsed) - 1, -1, -1):
        if parsed[i].get("kind") == "session_start":
            start_idx = i + 1
            break
    return [e for e in parsed[start_idx:] if e.get("kind") == "caption"]


def _load_or_empty_state() -> tuple[list[dict] | None, str | None]:
    """Resolve path + load captions. Returns (captions, empty_state_msg).

    Exactly one of the two is non-None.
    """
    path = _resolve_record_path()
    if path is None:
        return None, (
            "No meeting transcript available — captions are disabled or no "
            "meeting is active."
        )
    if not path.exists():
        return None, (
            f"No transcript file at {path} yet — captions may be disabled, "
            "or no speech has been finalized in this session."
        )
    entries = _read_captions(path)
    if not entries:
        return None, "Transcript is empty so far this session — no speech finalized yet."
    return entries, None


def _apply_time_window(
    entries: list[dict],
    start_minutes_ago: float | None,
    end_minutes_ago: float | None,
) -> list[dict] | str:
    """Filter entries to the requested time window.

    Returns the filtered list, OR an error-state string if the window
    arguments are nonsensical (start <= end, since start is the older
    boundary).
    """
    if start_minutes_ago is not None and end_minutes_ago is not None:
        if start_minutes_ago <= end_minutes_ago:
            return (
                "Invalid time window — start_minutes_ago must be greater "
                "than end_minutes_ago (start is the older boundary, e.g. "
                "start=30, end=20 means 'between 30 and 20 minutes ago')."
            )
    now = _now()
    if start_minutes_ago is not None and start_minutes_ago > 0:
        cutoff = now - (start_minutes_ago * 60)
        entries = [e for e in entries if (e.get("timestamp") or 0) >= cutoff]
    if end_minutes_ago is not None and end_minutes_ago > 0:
        cutoff = now - (end_minutes_ago * 60)
        entries = [e for e in entries if (e.get("timestamp") or 0) <= cutoff]
    return entries


def _apply_speaker_filter(entries: list[dict], speaker: str | None) -> list[dict]:
    """Case-insensitive substring match on sender field."""
    if not speaker:
        return entries
    needle = speaker.lower().strip()
    return [e for e in entries if needle in (e.get("sender") or "").lower()]


def _enforce_byte_ceiling(lines: list[str], total_count: int) -> str:
    """Join lines, trimming from the front if over RESULT_BYTE_CEILING.

    When trimmed, prepends a one-line truncation notice telling the
    model how many were dropped and how to narrow the query.
    """
    text = "\n".join(lines)
    if len(text.encode("utf-8")) <= RESULT_BYTE_CEILING:
        return text
    kept: list[str] = []
    running_bytes = 0
    for line in reversed(lines):
        line_bytes = len(line.encode("utf-8")) + 1
        if running_bytes + line_bytes > RESULT_BYTE_CEILING - 200:
            break
        kept.append(line)
        running_bytes += line_bytes
    kept.reverse()
    dropped = total_count - len(kept)
    notice = (
        f"(truncated to fit response size — showing the most recent "
        f"{len(kept)} of {total_count} captions; {dropped} older captions "
        f"omitted. Narrow the time window, add a speaker filter, or use "
        f"search_captions for a specific keyword.)"
    )
    return notice + "\n" + "\n".join(kept)


@mcp.tool()
def search_captions(
    query: str,
    speaker: str | None = None,
    start_minutes_ago: float | None = None,
    end_minutes_ago: float | None = None,
    context_lines: int = 0,
    limit: int = DEFAULT_SEARCH_LIMIT,
) -> str:
    """Search the spoken-caption transcript for a keyword.

    Use this for targeted lookups: "what did Alice say about the
    migration?", "did anyone mention Sentry?", "find where I said
    Mohammed". The match is case-insensitive substring.

    Args:
        query: Keyword or phrase to search for (case-insensitive substring).
        speaker: Optional speaker filter (case-insensitive substring on
            the speaker name). Use list_speakers to see who's spoken.
        start_minutes_ago: Older boundary of the time window (e.g. 30
            means "from 30 minutes ago"). Omit for no lower bound.
        end_minutes_ago: Newer boundary of the time window (e.g. 20
            means "up to 20 minutes ago"). Omit for "up to now".
        context_lines: Captions to include before AND after each match
            (like grep -A/-B). Default 0 = matches only.
        limit: Max number of MATCH lines (not total output lines) to
            return. Default 20.

    Returns:
        Plain-text matches, one caption per line, formatted as
        "{marker}[HH:MM:SS Speaker] text" where marker is "> " for
        match lines and "  " for context lines. Non-contiguous spans
        are separated by blank lines. Empty-state prose is returned
        as plain text rather than raising.
    """
    if not query or not query.strip():
        return "search_captions requires a non-empty query."

    entries, empty_state = _load_or_empty_state()
    if empty_state is not None:
        return empty_state

    windowed = _apply_time_window(entries, start_minutes_ago, end_minutes_ago)
    if isinstance(windowed, str):
        return windowed
    windowed = _apply_speaker_filter(windowed, speaker)

    if not windowed:
        scope_bits = []
        if speaker:
            scope_bits.append(f"speaker~='{speaker}'")
        if start_minutes_ago is not None or end_minutes_ago is not None:
            scope_bits.append(
                f"window=[{start_minutes_ago}min..{end_minutes_ago or 0}min ago]"
            )
        scope = (" with " + ", ".join(scope_bits)) if scope_bits else ""
        return f"No captions in scope{scope} — nothing to search."

    needle = query.lower()
    match_indices = [
        i for i, e in enumerate(windowed) if needle in (e.get("text") or "").lower()
    ]

    if not match_indices:
        return f"No captions match query '{query}' in the requested scope."

    total_matches = len(match_indices)
    capped_indices = match_indices[:limit]
    truncated = total_matches > limit

    context_lines = max(0, int(context_lines))
    include_idx: set[int] = set()
    for mi in capped_indices:
        for j in range(max(0, mi - context_lines), min(len(windowed), mi + context_lines + 1)):
            include_idx.add(j)

    sorted_idx = sorted(include_idx)
    lines: list[str] = []
    prev = None
    match_set = set(capped_indices)
    for idx in sorted_idx:
        if prev is not None and idx != prev + 1:
            lines.append("")
        marker = "> " if idx in match_set else "  "
        lines.append(_format_caption(windowed[idx], marker=marker))
        prev = idx

    if truncated:
        lines.append("")
        lines.append(
            f"(showing {len(capped_indices)} of {total_matches} matches — "
            f"narrow the time window or speaker filter, or raise limit to see more.)"
        )

    return _enforce_byte_ceiling(lines, total_count=len(sorted_idx))


@mcp.tool()
def list_captions(
    start_minutes_ago: float | None = None,
    end_minutes_ago: float | None = None,
    last_n: int | None = None,
    speaker: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
) -> str:
    """Return spoken captions in chronological order.

    Use this for browse-style queries: "what did we just say?",
    "what were we discussing 30 minutes ago?", "everything Alice said".
    For a keyword lookup, prefer search_captions instead — it's faster
    to read and won't blow context on a long meeting.

    Args:
        start_minutes_ago: Older boundary of the time window (e.g. 30
            means "from 30 minutes ago"). Omit for no lower bound (full
            session).
        end_minutes_ago: Newer boundary of the time window (e.g. 20
            means "up to 20 minutes ago"). Omit for "up to now".
        last_n: Return only the last N captions in the filtered window.
            Combine with start_minutes_ago to get "last 20 captions in
            the last 30 minutes". If both last_n and limit are set, the
            more restrictive bound wins.
        speaker: Optional speaker filter (case-insensitive substring on
            speaker name). Use list_speakers to see who's spoken.
        limit: Hard cap on captions returned. Default 100. The byte
            ceiling on response size is a separate, additional cap.

    Returns:
        Plain-text captions, one per line, formatted as
        "[HH:MM:SS Speaker] text". When truncated by byte ceiling or
        limit, a hint is appended.
    """
    entries, empty_state = _load_or_empty_state()
    if empty_state is not None:
        return empty_state

    windowed = _apply_time_window(entries, start_minutes_ago, end_minutes_ago)
    if isinstance(windowed, str):
        return windowed
    windowed = _apply_speaker_filter(windowed, speaker)

    if not windowed:
        scope_bits = []
        if speaker:
            scope_bits.append(f"speaker~='{speaker}'")
        if start_minutes_ago is not None or end_minutes_ago is not None:
            scope_bits.append(
                f"window=[{start_minutes_ago}min..{end_minutes_ago or 0}min ago]"
            )
        scope = (" with " + ", ".join(scope_bits)) if scope_bits else ""
        return f"No captions match the requested scope{scope}."

    full_count = len(windowed)
    if last_n is not None and last_n > 0:
        windowed = windowed[-last_n:]

    if limit is not None and limit > 0 and len(windowed) > limit:
        windowed = windowed[-limit:]

    lines = [_format_caption(e, marker="") for e in windowed]

    if len(windowed) < full_count:
        lines.append("")
        lines.append(
            f"(showing {len(windowed)} of {full_count} captions in scope — "
            f"raise last_n/limit, or narrow filters to see different captions.)"
        )

    return _enforce_byte_ceiling(lines, total_count=full_count)


@mcp.tool()
def list_speakers() -> str:
    """Return the speakers heard so far this meeting session.

    Useful before applying a speaker filter on search_captions or
    list_captions — speaker names are case-insensitive substrings; this
    tool shows you what's actually in the data.

    Returns:
        Plain-text list, one speaker per line, formatted as
        "  <name> — <count> captions, last spoke <relative time>".
    """
    entries, empty_state = _load_or_empty_state()
    if empty_state is not None:
        return empty_state

    counts: dict[str, int] = {}
    last_seen: dict[str, float] = {}
    for e in entries:
        name = e.get("sender") or "?"
        counts[name] = counts.get(name, 0) + 1
        ts = e.get("timestamp")
        if isinstance(ts, (int, float)):
            if name not in last_seen or ts > last_seen[name]:
                last_seen[name] = ts

    if not counts:
        return "No speakers yet — no speech finalized in this session."

    now = _now()
    sorted_names = sorted(counts.keys(), key=lambda n: -counts[n])
    lines = [f"Speakers in this session ({len(sorted_names)} total):"]
    for name in sorted_names:
        ago = now - last_seen.get(name, now)
        if ago < 60:
            ago_str = f"{int(ago)}s ago"
        elif ago < 3600:
            ago_str = f"{int(ago / 60)} min ago"
        else:
            ago_str = f"{int(ago / 3600)}h{int((ago % 3600) / 60)}m ago"
        lines.append(f"  {name} — {counts[name]} captions, last spoke {ago_str}")

    return "\n".join(lines)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
