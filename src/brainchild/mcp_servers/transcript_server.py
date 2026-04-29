"""Transcript MCP server — exposes the live meeting JSONL as a tool.

Spawned by the `claude` agent (track A) via Claude Code's --mcp-config so
the model can fetch spoken captions on demand instead of seeing them in
the prompt every turn.

Reads the meeting record path from the env var set at spawn time:

    OPERATOR_MEETING_RECORD_PATH=/abs/path/to/<slug>.jsonl

If the env var is missing or the file doesn't exist yet, the tool returns
a friendly empty-state string rather than erroring — captions might be
disabled, or the meeting may not have produced any speech yet.

Run via:
    python -m brainchild.mcp_servers.transcript_server
"""
import json
import os
import time
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

ENV_PATH = "OPERATOR_MEETING_RECORD_PATH"

mcp = FastMCP("operator-transcript")


def _format_entry(entry: dict) -> str:
    ts = entry.get("timestamp")
    speaker = entry.get("sender") or "?"
    text = (entry.get("text") or "").strip()
    if isinstance(ts, (int, float)):
        clock = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        return f"[{clock} {speaker}] {text}"
    return f"[{speaker}] {text}"


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


@mcp.tool()
def recall_transcript(
    minutes_back: float | None = None,
    last_n: int | None = None,
) -> str:
    """Return the spoken-caption transcript of the current meeting.

    Captions are finalized lines of speech captured by Google Meet's live
    captions, attributed by speaker. Use this when a chat message asks
    about something said aloud (e.g. "what name did I just say?",
    "summarize the discussion", "what did we decide?").

    Args:
        minutes_back: If set, return only captions from the last N minutes.
        last_n: If set, return only the last N caption lines. If both are
            set, the more restrictive bound wins. If neither is set,
            returns the full transcript for this session.

    Returns:
        Plain-text transcript, one caption per line, formatted as
        "[HH:MM:SS Speaker] text". Empty-state strings are returned as
        prose rather than raising, so the model can relay them.
    """
    path_str = os.environ.get(ENV_PATH)
    if not path_str:
        return (
            "No meeting transcript available — captions are disabled or the "
            "meeting record was not wired."
        )
    path = Path(path_str)
    if not path.exists():
        return (
            f"No transcript file at {path} yet — captions may be disabled, or "
            "no speech has been finalized in this session."
        )

    entries = _read_captions(path)
    if not entries:
        return "Transcript is empty so far this session — no speech finalized yet."

    if minutes_back is not None and minutes_back > 0:
        cutoff = time.time() - (minutes_back * 60)
        entries = [e for e in entries if (e.get("timestamp") or 0) >= cutoff]

    if last_n is not None and last_n > 0:
        entries = entries[-last_n:]

    if not entries:
        return "No captions match the requested window."

    return "\n".join(_format_entry(e) for e in entries)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
