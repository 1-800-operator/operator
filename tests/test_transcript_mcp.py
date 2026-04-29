"""
Test the bundled transcript MCP server.

Exercises the recall_transcript tool function directly against fixture
JSONL files (full MCP wire-protocol coverage is the SDK's job, validated
by tests/test_mcp_client.py). Focus: filtering, windowing, empty states.

Usage:
    python tests/test_transcript_mcp.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BRAINCHILD_BOT", "claude")

from brainchild.mcp_servers import transcript_server


def _write_fixture(entries):
    """Write a list of dicts as JSONL to a tempfile; return path."""
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return path


def test_no_env_var():
    os.environ.pop(transcript_server.ENV_PATH, None)
    out = transcript_server.recall_transcript()
    assert "captions are disabled" in out, out
    print("✓ no env var → empty-state prose")


def test_missing_file():
    os.environ[transcript_server.ENV_PATH] = "/tmp/does-not-exist-xyz.jsonl"
    out = transcript_server.recall_transcript()
    assert "no speech has been finalized" in out or "yet" in out, out
    print("✓ missing file → empty-state prose")


def test_empty_session():
    """File with only a session_start marker — no captions yet."""
    path = _write_fixture([{"kind": "session_start", "timestamp": time.time()}])
    os.environ[transcript_server.ENV_PATH] = path
    out = transcript_server.recall_transcript()
    assert "empty so far" in out, out
    os.unlink(path)
    print("✓ session_start only → empty-state prose")


def test_basic_recall():
    now = time.time()
    path = _write_fixture([
        {"kind": "meta", "timestamp": now - 100, "slug": "x"},
        {"kind": "session_start", "timestamp": now - 90},
        {"kind": "chat", "sender": "Alice", "text": "hi", "timestamp": now - 80},
        {"kind": "caption", "sender": "Bob", "text": "hello world", "timestamp": now - 60},
        {"kind": "caption", "sender": "Alice", "text": "good morning", "timestamp": now - 30},
    ])
    os.environ[transcript_server.ENV_PATH] = path
    out = transcript_server.recall_transcript()
    assert "Bob" in out and "hello world" in out, out
    assert "Alice" in out and "good morning" in out, out
    assert "hi" not in out, "chat-kind entries must not appear in transcript output"
    os.unlink(path)
    print("✓ caption filter excludes chat entries")


def test_session_boundary():
    """Captions before the most recent session_start should be excluded."""
    now = time.time()
    path = _write_fixture([
        {"kind": "session_start", "timestamp": now - 200},
        {"kind": "caption", "sender": "Bob", "text": "OLD SESSION", "timestamp": now - 150},
        {"kind": "session_start", "timestamp": now - 90},
        {"kind": "caption", "sender": "Bob", "text": "NEW SESSION", "timestamp": now - 30},
    ])
    os.environ[transcript_server.ENV_PATH] = path
    out = transcript_server.recall_transcript()
    assert "NEW SESSION" in out, out
    assert "OLD SESSION" not in out, "prior session captions leaked through"
    os.unlink(path)
    print("✓ session boundary respected")


def test_minutes_back():
    now = time.time()
    path = _write_fixture([
        {"kind": "session_start", "timestamp": now - 600},
        {"kind": "caption", "sender": "Bob", "text": "ten minutes ago", "timestamp": now - 600},
        {"kind": "caption", "sender": "Bob", "text": "thirty seconds ago", "timestamp": now - 30},
    ])
    os.environ[transcript_server.ENV_PATH] = path
    out = transcript_server.recall_transcript(minutes_back=2)
    assert "thirty seconds ago" in out, out
    assert "ten minutes ago" not in out, out
    os.unlink(path)
    print("✓ minutes_back window")


def test_last_n():
    now = time.time()
    path = _write_fixture([
        {"kind": "session_start", "timestamp": now - 100},
        {"kind": "caption", "sender": "Bob", "text": "first", "timestamp": now - 90},
        {"kind": "caption", "sender": "Bob", "text": "second", "timestamp": now - 60},
        {"kind": "caption", "sender": "Bob", "text": "third", "timestamp": now - 30},
    ])
    os.environ[transcript_server.ENV_PATH] = path
    out = transcript_server.recall_transcript(last_n=2)
    assert "second" in out and "third" in out, out
    assert "first" not in out, out
    os.unlink(path)
    print("✓ last_n window")


def test_format():
    now = time.time()
    path = _write_fixture([
        {"kind": "session_start", "timestamp": now - 60},
        {"kind": "caption", "sender": "Jojo Shapiro", "text": "my name is Mohammed", "timestamp": now - 30},
    ])
    os.environ[transcript_server.ENV_PATH] = path
    out = transcript_server.recall_transcript()
    # Expected format: "[HH:MM:SS Speaker] text"
    assert out.startswith("["), out
    assert "Jojo Shapiro" in out, out
    assert "my name is Mohammed" in out, out
    os.unlink(path)
    print("✓ format includes timestamp + speaker + text")


if __name__ == "__main__":
    test_no_env_var()
    test_missing_file()
    test_empty_session()
    test_basic_recall()
    test_session_boundary()
    test_minutes_back()
    test_last_n()
    test_format()
    print("\nAll transcript MCP tests passed.")
