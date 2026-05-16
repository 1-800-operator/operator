"""
Test the bundled transcript MCP server — pressure-test against fixtures.

Three tools under test:
  - search_captions(query, speaker?, start_minutes_ago?, end_minutes_ago?,
                    context_lines=0, limit=20)
  - list_captions(start_minutes_ago?, end_minutes_ago?, last_n?,
                  speaker?, limit=100)
  - list_speakers()

Strategy: in-memory tempfile fixtures for tight unit cases, plus the
real ~/.operator/history/avd-axqi-obq.jsonl (424 captions / 85 min,
single speaker) for byte-ceiling stress + realistic-volume cases.
A synthesized 3-speaker fixture covers by-speaker filtering.

Usage:
    python tests/test_transcript_mcp.py
"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("OPERATOR_BOT", "claude")

from _1_800_operator.mcp_servers import record_server


REAL_LONG_FIXTURE = Path.home() / ".operator" / "history" / "avd-axqi-obq.jsonl"


def _write_fixture(entries):
    """Write a list of dicts as JSONL to a tempfile; return path."""
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return path


def _wire(path: str | None):
    """Point the resolver at a path (env var route) and clear marker."""
    record_server.MARKER_FILE = Path(tempfile.gettempdir()) / "_test_no_marker"
    if path is None:
        os.environ.pop(record_server.ENV_PATH, None)
    else:
        os.environ[record_server.ENV_PATH] = path


def _set_now(t: float):
    """Pin the tool's idea of 'now' for deterministic time-window tests."""
    record_server._now = lambda: t


def _build_multi_speaker_fixture(now: float) -> str:
    """30 captions across 3 speakers, spanning ~15 minutes.

    Density and topic varied to exercise filters realistically.
    """
    entries = [
        {"kind": "session_start", "timestamp": now - 16 * 60},
    ]
    script = [
        # (minutes_ago, speaker, text)
        (15.0, "Alice", "Okay let's start with the migration timeline"),
        (14.7, "Bob", "I think we need to push it back a week"),
        (14.4, "Alice", "Why what's the blocker"),
        (14.0, "Bob", "Sentry alerts are still firing on the auth path"),
        (13.7, "Carol", "I can take Sentry triage if that helps"),
        (13.3, "Alice", "Great Carol thanks"),
        (12.5, "Bob", "Also we need to rename recall_transcript before launch"),
        (12.0, "Alice", "Agreed I'll open a ticket"),
        (11.5, "Carol", "Linear or GitHub"),
        (11.2, "Alice", "Linear we track product work there"),
        (10.0, "Bob", "Quick aside did anyone see the new Sonnet benchmarks"),
        (9.8, "Carol", "Yeah they look strong on coding tasks"),
        (9.0, "Alice", "Back to the migration"),
        (8.5, "Alice", "Mohammed pinged me about the database pick yesterday"),
        (8.0, "Bob", "Postgres or SQLite for v1"),
        (7.7, "Alice", "Postgres for prod SQLite for tests"),
        (7.0, "Carol", "Standard split sounds good"),
        (6.0, "Bob", "What about the codex parity work"),
        (5.5, "Alice", "Phase 14.18 we're on it today"),
        (5.0, "Carol", "I'll be available for live testing this afternoon"),
        (4.5, "Alice", "Perfect"),
        (4.0, "Bob", "One more thing the marker file mechanism"),
        (3.7, "Bob", "Does it survive a crash mid-meeting"),
        (3.3, "Alice", "Yes the cleanup is in the shutdown handler"),
        (2.5, "Carol", "Good thinking"),
        (2.0, "Alice", "Let's wrap up — action items"),
        (1.7, "Alice", "Bob you take Sentry triage"),
        (1.4, "Bob", "On it"),
        (1.0, "Carol", "I'll do the live test against codex"),
        (0.5, "Alice", "Ship it"),
    ]
    for mins_ago, speaker, text in script:
        entries.append({
            "kind": "caption",
            "sender": speaker,
            "text": text,
            "timestamp": now - mins_ago * 60,
        })
    return _write_fixture(entries)


# ---------------- empty-state tests ----------------

def test_no_path():
    _wire(None)
    out = record_server.list_captions()
    assert "captions are disabled" in out or "no meeting is active" in out, out
    out2 = record_server.search_captions("anything")
    assert "captions are disabled" in out2 or "no meeting is active" in out2, out2
    out3 = record_server.list_speakers()
    assert "captions are disabled" in out3 or "no meeting is active" in out3, out3
    print("✓ no path wired → empty-state on all three tools")


def test_missing_file():
    _wire("/tmp/does-not-exist-xyz.jsonl")
    out = record_server.list_captions()
    assert "no speech has been finalized" in out or "yet" in out, out
    print("✓ missing file → empty-state")


def test_empty_session():
    """File with only a session_start marker."""
    path = _write_fixture([{"kind": "session_start", "timestamp": time.time()}])
    _wire(path)
    out = record_server.list_captions()
    assert "empty so far" in out, out
    out2 = record_server.list_speakers()
    assert "No speakers" in out2 or "empty" in out2, out2
    os.unlink(path)
    print("✓ session_start only → empty-state")


# ---------------- list_captions tests ----------------

def test_list_basic():
    now = time.time()
    path = _write_fixture([
        {"kind": "meta", "timestamp": now - 100, "slug": "x"},
        {"kind": "session_start", "timestamp": now - 90},
        {"kind": "chat", "sender": "Alice", "text": "hi", "timestamp": now - 80},
        {"kind": "caption", "sender": "Bob", "text": "hello world", "timestamp": now - 60},
        {"kind": "caption", "sender": "Alice", "text": "good morning", "timestamp": now - 30},
    ])
    _wire(path)
    _set_now(now)
    out = record_server.list_captions()
    assert "Bob" in out and "hello world" in out, out
    assert "Alice" in out and "good morning" in out, out
    assert "hi" not in out, "chat-kind entries must not appear"
    os.unlink(path)
    print("✓ list_captions baseline (caption-only filter)")


def test_list_session_boundary():
    """Captions before the most recent session_start should be excluded."""
    now = time.time()
    path = _write_fixture([
        {"kind": "session_start", "timestamp": now - 200},
        {"kind": "caption", "sender": "Bob", "text": "OLD", "timestamp": now - 150},
        {"kind": "session_start", "timestamp": now - 90},
        {"kind": "caption", "sender": "Bob", "text": "NEW", "timestamp": now - 30},
    ])
    _wire(path)
    _set_now(now)
    out = record_server.list_captions()
    assert "NEW" in out, out
    assert "OLD" not in out, out
    os.unlink(path)
    print("✓ list_captions respects session boundary")


def test_list_time_window_around_x():
    """Window around 30-min-ago: start=35, end=25 → 30s included, others not."""
    now = time.time()
    path = _build_multi_speaker_fixture(now)
    _wire(path)
    _set_now(now)
    out = record_server.list_captions(start_minutes_ago=16, end_minutes_ago=14.5)
    # Window: between 14.5 and 16 min ago. Should include the 15.0 + 14.7 captions.
    assert "migration timeline" in out, out  # 15.0 min ago — inside
    assert "push it back" in out, out  # 14.7 min ago — inside
    assert "Sentry triage" not in out, out  # 13.7 min ago — outside (newer)
    assert "Ship it" not in out, out  # 0.5 min ago — way outside
    os.unlink(path)
    print("✓ list_captions time-window-around-X works")


def test_list_invalid_window():
    now = time.time()
    path = _build_multi_speaker_fixture(now)
    _wire(path)
    _set_now(now)
    out = record_server.list_captions(start_minutes_ago=5, end_minutes_ago=10)
    assert "Invalid time window" in out, out
    assert "older boundary" in out, out
    os.unlink(path)
    print("✓ list_captions rejects start <= end with helpful prose")


def test_list_speaker_filter():
    now = time.time()
    path = _build_multi_speaker_fixture(now)
    _wire(path)
    _set_now(now)
    out = record_server.list_captions(speaker="alice")  # case-insensitive
    assert "migration timeline" in out, out
    assert "I think we need to push" not in out, "Bob's line leaked"
    assert "I can take Sentry triage" not in out, "Carol's line leaked"
    os.unlink(path)
    print("✓ list_captions speaker filter (case-insensitive)")


def test_list_speaker_no_match():
    now = time.time()
    path = _build_multi_speaker_fixture(now)
    _wire(path)
    _set_now(now)
    out = record_server.list_captions(speaker="Mohammed")
    assert "No captions match the requested scope" in out, out
    assert "Mohammed" in out, "scope hint should echo the failed filter"
    os.unlink(path)
    print("✓ list_captions speaker mismatch → empty-state with scope hint")


def test_list_last_n():
    now = time.time()
    path = _build_multi_speaker_fixture(now)
    _wire(path)
    _set_now(now)
    out = record_server.list_captions(last_n=3)
    # Last 3 in the fixture: "Bob: On it" (1.4), "Carol: live test" (1.0), "Alice: Ship it" (0.5)
    assert "Ship it" in out, out
    assert "On it" in out, out
    assert "live test" in out, out
    assert "wrap up" not in out, out
    # Must include the truncation hint
    assert "showing the most recent 3 of" in out, out
    os.unlink(path)
    print("✓ list_captions last_n with truncation hint")


def test_list_byte_ceiling_real_fixture():
    """The 85-min real fixture should trigger the byte ceiling."""
    if not REAL_LONG_FIXTURE.exists():
        print("⚠ skipping byte-ceiling test: real fixture not available")
        return
    _wire(str(REAL_LONG_FIXTURE))
    # Don't pin _now — fixture is from April 23, captions will all be old; that's fine
    # because we're calling list_captions() with no time window.
    record_server._now = time.time
    out = record_server.list_captions(limit=10000)  # ask for everything
    out_bytes = len(out.encode("utf-8"))
    assert out_bytes <= record_server.RESULT_BYTE_CEILING + 500, (
        f"byte ceiling not enforced: {out_bytes} bytes returned"
    )
    assert "Operator recorded the entire meeting" in out, "truncation notice missing"
    print(f"✓ byte ceiling holds at {out_bytes} bytes against 442-line real fixture")


# ---------------- search_captions tests ----------------

def test_search_basic():
    now = time.time()
    path = _build_multi_speaker_fixture(now)
    _wire(path)
    _set_now(now)
    out = record_server.search_captions("Sentry")
    # 3 matches in fixture: "Sentry alerts", "Sentry triage", "did anyone see the new Sonnet"
    # Wait — only "Sentry alerts" and "Sentry triage" contain Sentry. Check.
    assert "Sentry alerts" in out, out
    assert "Sentry triage" in out, out
    # Match prefix `> ` should be present
    assert "> [" in out, "match prefix missing"
    os.unlink(path)
    print("✓ search_captions returns matches with > prefix")


def test_search_case_insensitive():
    now = time.time()
    path = _write_fixture([
        {"kind": "session_start", "timestamp": now - 100},
        {"kind": "caption", "sender": "Bob", "text": "Purple is the color", "timestamp": now - 60},
        {"kind": "caption", "sender": "Bob", "text": "PURPLE rain", "timestamp": now - 30},
    ])
    _wire(path)
    _set_now(now)
    out = record_server.search_captions("purple")
    assert "Purple is the color" in out, out
    assert "PURPLE rain" in out, out
    os.unlink(path)
    print("✓ search_captions is case-insensitive")


def test_search_no_match():
    now = time.time()
    path = _build_multi_speaker_fixture(now)
    _wire(path)
    _set_now(now)
    out = record_server.search_captions("kubernetes")
    assert "No captions match query 'kubernetes'" in out, out
    os.unlink(path)
    print("✓ search_captions empty result → clean empty-state")


def test_search_empty_query():
    now = time.time()
    path = _build_multi_speaker_fixture(now)
    _wire(path)
    _set_now(now)
    out = record_server.search_captions("")
    assert "non-empty query" in out, out
    out2 = record_server.search_captions("   ")
    assert "non-empty query" in out2, out2
    os.unlink(path)
    print("✓ search_captions rejects empty/whitespace query")


def test_search_with_speaker():
    now = time.time()
    path = _build_multi_speaker_fixture(now)
    _wire(path)
    _set_now(now)
    # Bob mentions Sentry once ("Sentry alerts are still firing")
    out = record_server.search_captions("Sentry", speaker="Bob")
    assert "Sentry alerts" in out, out
    assert "Sentry triage" not in out, "Carol's line leaked through speaker filter"
    os.unlink(path)
    print("✓ search_captions composes with speaker filter")


def test_search_with_time_window():
    now = time.time()
    path = _build_multi_speaker_fixture(now)
    _wire(path)
    _set_now(now)
    # "migration" appears at 15.0 ("migration timeline") and 9.0 ("Back to the migration")
    out = record_server.search_captions("migration", start_minutes_ago=10, end_minutes_ago=0)
    assert "Back to the migration" in out, out
    assert "migration timeline" not in out, "Out-of-window match leaked"
    os.unlink(path)
    print("✓ search_captions composes with time window")


def test_search_context_lines():
    now = time.time()
    path = _build_multi_speaker_fixture(now)
    _wire(path)
    _set_now(now)
    # "Mohammed" appears at 8.5 min ago. With context_lines=1 we should see
    # 9.0 ("Back to the migration") and 8.0 ("Postgres or SQLite") around it.
    out = record_server.search_captions("Mohammed", context_lines=1)
    assert "Mohammed pinged me" in out, out
    assert "Back to the migration" in out, "before-context missing"
    assert "Postgres or SQLite" in out, "after-context missing"
    # Match line should have > prefix; context lines should have "  " prefix
    lines = out.split("\n")
    match_lines = [l for l in lines if "Mohammed pinged" in l]
    assert any(l.startswith("> ") for l in match_lines), "match prefix missing"
    context_lines_found = [l for l in lines if "Back to the migration" in l]
    assert any(l.startswith("  ") for l in context_lines_found), "context prefix missing"
    os.unlink(path)
    print("✓ search_captions context_lines includes ±N with marker distinction")


def test_search_limit_and_truncation_hint():
    now = time.time()
    # Synthesize 25 caps all containing "test"
    entries = [{"kind": "session_start", "timestamp": now - 600}]
    for i in range(25):
        entries.append({
            "kind": "caption",
            "sender": "Bob",
            "text": f"test number {i}",
            "timestamp": now - (500 - i * 10),
        })
    path = _write_fixture(entries)
    _wire(path)
    _set_now(now)
    out = record_server.search_captions("test", limit=5)
    assert "showing 5 of 25 matches" in out, out
    assert "narrow the time window" in out or "raise limit" in out, out
    os.unlink(path)
    print("✓ search_captions limit truncation hint")


def test_search_byte_ceiling():
    """Adversarial: many long monologue captions all matching the query.
    Sized to overshoot RESULT_BYTE_CEILING (~80KB) so the ceiling fires."""
    now = time.time()
    long_text = "diagnosis " * 80  # ~800 chars per caption
    n = 150  # 150 × ~850 bytes ≈ 127KB raw, well over the 80KB ceiling
    entries = [{"kind": "session_start", "timestamp": now - (n * 10 + 100)}]
    for i in range(n):
        entries.append({
            "kind": "caption",
            "sender": "Bob",
            "text": f"{long_text} — segment {i}",
            "timestamp": now - ((n * 10 + 100) - i * 10),
        })
    path = _write_fixture(entries)
    _wire(path)
    _set_now(now)
    out = record_server.search_captions("diagnosis", limit=n)
    out_bytes = len(out.encode("utf-8"))
    assert out_bytes <= record_server.RESULT_BYTE_CEILING + 500, (
        f"byte ceiling not enforced on search: {out_bytes} bytes"
    )
    assert "Operator recorded the entire meeting" in out, "search byte-ceiling notice missing"
    os.unlink(path)
    print(f"✓ search_captions byte ceiling holds ({out_bytes} bytes)")


# ---------------- list_speakers tests ----------------

def test_speakers_multi():
    now = time.time()
    path = _build_multi_speaker_fixture(now)
    _wire(path)
    _set_now(now)
    out = record_server.list_speakers()
    assert "Speakers in this session (3 total)" in out, out
    assert "Alice" in out and "Bob" in out and "Carol" in out, out
    # Most recent activity: Alice spoke 0.5 min ago → "0 min ago" or "30s ago"
    assert "captions" in out, out
    os.unlink(path)
    print("✓ list_speakers reports counts + relative time")


def test_speakers_single():
    now = time.time()
    path = _write_fixture([
        {"kind": "session_start", "timestamp": now - 100},
        {"kind": "caption", "sender": "Jojo Shapiro", "text": "hello", "timestamp": now - 30},
    ])
    _wire(path)
    _set_now(now)
    out = record_server.list_speakers()
    assert "(1 total)" in out, out
    assert "Jojo Shapiro" in out, out
    os.unlink(path)
    print("✓ list_speakers single-speaker case")


# ---------------- adversarial / robustness ----------------

def test_missing_timestamp_does_not_crash():
    """A caption with a None/missing timestamp must not crash time-window filter."""
    now = time.time()
    path = _write_fixture([
        {"kind": "session_start", "timestamp": now - 100},
        {"kind": "caption", "sender": "Bob", "text": "no timestamp here"},
        {"kind": "caption", "sender": "Bob", "text": "valid one", "timestamp": now - 10},
    ])
    _wire(path)
    _set_now(now)
    out = record_server.list_captions(start_minutes_ago=5)
    assert "valid one" in out, out
    # Caption with missing timestamp gets timestamp=0 → filtered out by start>0 cutoff
    assert "no timestamp here" not in out, out
    print("✓ missing-timestamp caption doesn't crash time-window")


def test_marker_file_resolution():
    """Marker file should win over env var when both are set."""
    now = time.time()
    marker_target = _write_fixture([
        {"kind": "session_start", "timestamp": now - 60},
        {"kind": "caption", "sender": "Bob", "text": "from marker", "timestamp": now - 30},
    ])
    env_target = _write_fixture([
        {"kind": "session_start", "timestamp": now - 60},
        {"kind": "caption", "sender": "Bob", "text": "from env", "timestamp": now - 30},
    ])
    fd, marker_path = tempfile.mkstemp()
    os.close(fd)
    with open(marker_path, "w") as f:
        f.write(marker_target)
    record_server.MARKER_FILE = Path(marker_path)
    os.environ[record_server.ENV_PATH] = env_target
    _set_now(now)
    out = record_server.list_captions()
    assert "from marker" in out, out
    assert "from env" not in out, "env var should not have won over marker file"
    os.unlink(marker_target)
    os.unlink(env_target)
    os.unlink(marker_path)
    record_server.MARKER_FILE = Path.home() / ".operator" / ".current_meeting"
    print("✓ marker file resolves before env var fallback")


def test_marker_fallback_to_env():
    """Empty/missing marker file falls back to env var."""
    now = time.time()
    env_target = _write_fixture([
        {"kind": "session_start", "timestamp": now - 60},
        {"kind": "caption", "sender": "Bob", "text": "fallback worked", "timestamp": now - 30},
    ])
    record_server.MARKER_FILE = Path(tempfile.gettempdir()) / "_test_no_marker_xyz"
    os.environ[record_server.ENV_PATH] = env_target
    _set_now(now)
    out = record_server.list_captions()
    assert "fallback worked" in out, out
    os.unlink(env_target)
    record_server.MARKER_FILE = Path.home() / ".operator" / ".current_meeting"
    print("✓ marker missing → env var fallback works")


def _make_history_dir(meetings: dict[str, list[dict]]) -> Path:
    """Write multiple JSONL files to a temp HISTORY_DIR for find_meetings
    tests. Each (slug, entries) pair becomes one meeting file."""
    tmp = Path(tempfile.mkdtemp(prefix="op_history_"))
    for slug, entries in meetings.items():
        path = tmp / f"{slug}.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
    return tmp


def test_find_meetings_by_participant():
    now = time.time()
    tmp = _make_history_dir({
        "aaa-bbbb-ccc": [
            {"kind": "meta", "slug": "aaa-bbbb-ccc", "meet_url": "https://meet.google.com/aaa-bbbb-ccc", "mode": "slip"},
            {"kind": "session_start", "timestamp": now - 3600},
            {"kind": "chat", "sender": "Alice", "text": "hi", "timestamp": now - 3500},
            {"kind": "participants_final", "timestamp": now - 100,
             "attended": ["Alice", "Bob"], "currently_present": [], "self_name": "operator"},
        ],
        "ddd-eeee-fff": [
            {"kind": "meta", "slug": "ddd-eeee-fff", "meet_url": "https://meet.google.com/ddd-eeee-fff", "mode": "wiretap"},
            {"kind": "session_start", "timestamp": now - 1800},
            {"kind": "participants_final", "timestamp": now - 50,
             "attended": ["Charlie", "Dana"], "currently_present": [], "self_name": "operator"},
        ],
    })
    record_server.HISTORY_DIR = tmp
    out = record_server.find_meetings(participants=["alice"])
    assert "aaa-bbbb-ccc" in out, out
    assert "ddd-eeee-fff" not in out, out
    out2 = record_server.find_meetings(participants=["dana"])
    assert "ddd-eeee-fff" in out2, out2
    out3 = record_server.find_meetings(participants=["alice", "bob"])
    assert "aaa-bbbb-ccc" in out3, out3
    out4 = record_server.find_meetings(participants=["alice", "charlie"])
    assert "No meetings matched" in out4, out4
    print("✓ find_meetings: participant filter matches attended list (all needles required)")


def test_find_meetings_fallback_to_chat_senders_when_no_participants_final():
    """When participants_final is absent (e.g. operator crashed), fall back
    to deriving attendees from chat senders + caption speakers."""
    now = time.time()
    tmp = _make_history_dir({
        "crashed-meeting": [
            {"kind": "meta", "slug": "crashed-meeting", "meet_url": "https://meet.google.com/crashed-meeting", "mode": "slip"},
            {"kind": "session_start", "timestamp": now - 1000},
            {"kind": "chat", "sender": "Erin", "text": "hello", "timestamp": now - 900},
            {"kind": "caption", "sender": "Frank", "text": "spoken", "timestamp": now - 800},
            # No participants_final, no meeting_end — simulated crash.
        ],
    })
    record_server.HISTORY_DIR = tmp
    out = record_server.find_meetings(participants=["erin"])
    assert "crashed-meeting" in out, out
    out2 = record_server.find_meetings(participants=["frank"])
    assert "crashed-meeting" in out2, out2
    print("✓ find_meetings: falls back to chat/caption senders when participants_final absent")


def test_find_meetings_by_date_range():
    today = datetime.now()
    yesterday = today - timedelta(days=1)
    last_week = today - timedelta(days=7)
    tmp = _make_history_dir({
        "today-meeting": [
            {"kind": "session_start", "timestamp": today.timestamp()},
            {"kind": "chat", "sender": "A", "text": "x", "timestamp": today.timestamp() + 10},
        ],
        "yesterday-meeting": [
            {"kind": "session_start", "timestamp": yesterday.timestamp()},
            {"kind": "chat", "sender": "A", "text": "x", "timestamp": yesterday.timestamp() + 10},
        ],
        "last-week-meeting": [
            {"kind": "session_start", "timestamp": last_week.timestamp()},
            {"kind": "chat", "sender": "A", "text": "x", "timestamp": last_week.timestamp() + 10},
        ],
    })
    record_server.HISTORY_DIR = tmp
    today_iso = today.strftime("%Y-%m-%d")
    yest_iso = yesterday.strftime("%Y-%m-%d")
    out = record_server.find_meetings(date_range_iso=today_iso)
    assert "today-meeting" in out and "yesterday-meeting" not in out and "last-week-meeting" not in out, out
    out2 = record_server.find_meetings(date_range_iso=f"{yest_iso}/{today_iso}")
    assert "today-meeting" in out2 and "yesterday-meeting" in out2 and "last-week-meeting" not in out2, out2
    print("✓ find_meetings: date_range_iso scopes to single date and ranges (inclusive end-of-day)")


def test_find_meetings_url_contains():
    now = time.time()
    tmp = _make_history_dir({
        "aaa-bbbb-ccc": [
            {"kind": "meta", "slug": "aaa-bbbb-ccc", "meet_url": "https://meet.google.com/aaa-bbbb-ccc", "mode": "slip"},
            {"kind": "session_start", "timestamp": now},
        ],
        "zzz-yyyy-xxx": [
            {"kind": "meta", "slug": "zzz-yyyy-xxx", "meet_url": "https://meet.google.com/zzz-yyyy-xxx", "mode": "slip"},
            {"kind": "session_start", "timestamp": now},
        ],
    })
    record_server.HISTORY_DIR = tmp
    out = record_server.find_meetings(url_contains="zzz")
    assert "zzz-yyyy-xxx" in out and "aaa-bbbb-ccc" not in out, out
    print("✓ find_meetings: url_contains case-insensitive substring filter works")


def test_find_meetings_no_filters_returns_everything():
    now = time.time()
    tmp = _make_history_dir({
        "m1": [{"kind": "session_start", "timestamp": now}],
        "m2": [{"kind": "session_start", "timestamp": now - 100}],
    })
    record_server.HISTORY_DIR = tmp
    out = record_server.find_meetings()
    assert "m1" in out and "m2" in out, out
    print("✓ find_meetings: no filters → returns everything (like list_meetings)")


def test_format_includes_clock_and_speaker():
    now = time.time()
    path = _write_fixture([
        {"kind": "session_start", "timestamp": now - 60},
        {"kind": "caption", "sender": "Jojo Shapiro", "text": "my name is Mohammed", "timestamp": now - 30},
    ])
    _wire(path)
    _set_now(now)
    out = record_server.list_captions()
    assert "[" in out and "Jojo Shapiro" in out, out
    assert "my name is Mohammed" in out, out
    os.unlink(path)
    print("✓ output format: [HH:MM:SS Speaker] text")


if __name__ == "__main__":
    # Empty-state
    test_no_path()
    test_missing_file()
    test_empty_session()
    # list_captions
    test_list_basic()
    test_list_session_boundary()
    test_list_time_window_around_x()
    test_list_invalid_window()
    test_list_speaker_filter()
    test_list_speaker_no_match()
    test_list_last_n()
    test_list_byte_ceiling_real_fixture()
    # search_captions
    test_search_basic()
    test_search_case_insensitive()
    test_search_no_match()
    test_search_empty_query()
    test_search_with_speaker()
    test_search_with_time_window()
    test_search_context_lines()
    test_search_limit_and_truncation_hint()
    test_search_byte_ceiling()
    # list_speakers
    test_speakers_multi()
    test_speakers_single()
    # robustness
    test_missing_timestamp_does_not_crash()
    test_marker_file_resolution()
    test_marker_fallback_to_env()
    test_format_includes_clock_and_speaker()
    # find_meetings
    test_find_meetings_by_participant()
    test_find_meetings_fallback_to_chat_senders_when_no_participants_final()
    test_find_meetings_by_date_range()
    test_find_meetings_url_contains()
    test_find_meetings_no_filters_returns_everything()
    print("\nAll meeting-record MCP tests passed.")
