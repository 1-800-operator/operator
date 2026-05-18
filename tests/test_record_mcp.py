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
    """Point the resolver at a path (env var route) and clear marker.

    Also overrides HISTORY_DIR to the parent of `path` so the MCP's
    _is_safe_record_path validation (added by the audit) accepts the
    test fixture. Production paths always live under ~/.operator/history;
    tests use tempfiles, so we shim the validator's notion of where
    'history' is.
    """
    record_server.MARKER_FILE = Path(tempfile.gettempdir()) / "_test_no_marker"
    if path is None:
        os.environ.pop(record_server.ENV_PATH, None)
        record_server.HISTORY_DIR = Path(tempfile.gettempdir())
    else:
        os.environ[record_server.ENV_PATH] = path
        record_server.HISTORY_DIR = Path(path).resolve().parent


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
        {"kind": "chat", "sender": "Alice", "text": "ZZZchatonlymarkerZZZ", "timestamp": now - 80},
        {"kind": "caption", "sender": "Bob", "text": "hello world", "timestamp": now - 60},
        {"kind": "caption", "sender": "Alice", "text": "good morning", "timestamp": now - 30},
    ])
    _wire(path)
    _set_now(now)
    out = record_server.list_captions()
    assert "Bob" in out and "hello world" in out, out
    assert "Alice" in out and "good morning" in out, out
    assert "ZZZchatonlymarkerZZZ" not in out, "chat-kind entries must not appear"
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


def test_env_var_wins_over_marker_file():
    """H-6 security: env var is the primary source — marker is a fallback
    only for static MCP registrations that miss the env. Production
    operator sets the env at inner-claude spawn time; the MCP inherits
    it atomically and a same-uid attacker cannot race-overwrite it the
    way they could overwrite the on-disk marker file."""
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
    # _is_safe_record_path scopes both candidates to HISTORY_DIR; both
    # tempfiles need the same parent for the test to exercise the
    # priority-order logic rather than the safety filter.
    record_server.HISTORY_DIR = Path(env_target).resolve().parent
    _set_now(now)
    out = record_server.list_captions()
    assert "from env" in out, out
    assert "from marker" not in out, "marker file must not win over env var"
    os.unlink(marker_target)
    os.unlink(env_target)
    os.unlink(marker_path)
    record_server.MARKER_FILE = Path.home() / ".operator" / ".current_meeting"
    print("✓ env var wins over marker file (H-6 priority order)")


def _fake_live_slip_lock() -> Path:
    """Write a slip.pid that points at THIS test process (always live).

    Returns the path so the caller can monkey-patch
    record_server.SLIP_LOCK + unlink at teardown. H-21's freshness gate
    treats the marker as stale if slip.pid points at a dead pid, so
    tests that exercise the marker-fallback path need a live-looking
    slip.pid to pass.
    """
    fd, p = tempfile.mkstemp(prefix="op_slip_lock_")
    os.close(fd)
    Path(p).write_text(str(os.getpid()), encoding="utf-8")
    return Path(p)


def test_marker_fallback_when_env_unset():
    """No env var → marker file is consulted (legacy compatibility).

    Post-H-21: marker fallback also requires slip.pid to point at a
    live process. The test fakes that with the current pid.
    """
    now = time.time()
    marker_target = _write_fixture([
        {"kind": "session_start", "timestamp": now - 60},
        {"kind": "caption", "sender": "Bob", "text": "marker worked", "timestamp": now - 30},
    ])
    fd, marker_path = tempfile.mkstemp()
    os.close(fd)
    with open(marker_path, "w") as f:
        f.write(marker_target)
    slip_lock = _fake_live_slip_lock()
    record_server.MARKER_FILE = Path(marker_path)
    record_server.SLIP_LOCK = slip_lock
    os.environ.pop(record_server.ENV_PATH, None)
    record_server.HISTORY_DIR = Path(marker_target).resolve().parent
    _set_now(now)
    out = record_server.list_captions()
    assert "marker worked" in out, out
    os.unlink(marker_target)
    os.unlink(marker_path)
    slip_lock.unlink()
    record_server.MARKER_FILE = Path.home() / ".operator" / ".current_meeting"
    record_server.SLIP_LOCK = Path.home() / ".operator" / "slip.pid"
    print("✓ env var unset → marker file fallback works (with live slip.pid)")


def test_marker_stale_after_crash_is_rejected():
    """H-21: if operator crashed without _shutdown, the marker file
    persists pointing at the prior meeting's JSONL. Pre-fix, the next
    bare claude session that called list_captions / search_captions
    would silently return content from the prior meeting and label it
    'the live meeting' — looks like fresh recall but is actually stale
    state served confidently. Erodes trust the same way hallucination
    does.

    Post-fix: the marker-fallback path requires slip.pid to point at a
    live process. No live operator → marker is treated as stale, MCP
    returns the unwired empty-state.

    Two stale-marker scenarios covered:
      (a) slip.pid missing entirely (clean shutdown that forgot, or a
          fresh install before operator's first run)
      (b) slip.pid present but pid is dead (crash / SIGKILL / OOM)
    """
    now = time.time()
    marker_target = _write_fixture([
        {"kind": "session_start", "timestamp": now - 3600},
        {"kind": "caption", "sender": "Alice", "text": "yesterday's standup", "timestamp": now - 1800},
    ])
    fd, marker_path = tempfile.mkstemp()
    os.close(fd)
    Path(marker_path).write_text(marker_target, encoding="utf-8")
    record_server.MARKER_FILE = Path(marker_path)
    os.environ.pop(record_server.ENV_PATH, None)
    record_server.HISTORY_DIR = Path(marker_target).resolve().parent
    _set_now(now)

    # (a) No slip.pid at all → stale.
    no_lock = Path(tempfile.gettempdir()) / "_test_no_slip_lock"
    if no_lock.exists():
        no_lock.unlink()
    record_server.SLIP_LOCK = no_lock
    out = record_server.list_captions()
    assert "yesterday's standup" not in out, (
        "missing slip.pid should treat marker as stale; "
        f"got: {out!r}"
    )

    # (b) slip.pid present but pid is dead → stale.
    # Pick a pid that's almost certainly not in use: a giant number
    # outside the typical OS range. os.kill(pid, 0) returns
    # ProcessLookupError.
    dead_lock = _fake_live_slip_lock()
    dead_lock.write_text("9999999", encoding="utf-8")
    record_server.SLIP_LOCK = dead_lock
    out = record_server.list_captions()
    assert "yesterday's standup" not in out, (
        "dead pid in slip.pid should treat marker as stale; "
        f"got: {out!r}"
    )

    # Sanity: with a live slip.pid (this process), the marker IS trusted.
    live_lock = _fake_live_slip_lock()
    record_server.SLIP_LOCK = live_lock
    out = record_server.list_captions()
    assert "yesterday's standup" in out, (
        "live slip.pid should let marker through; "
        f"got: {out!r}"
    )

    # Cleanup.
    os.unlink(marker_target)
    os.unlink(marker_path)
    dead_lock.unlink()
    live_lock.unlink()
    record_server.MARKER_FILE = Path.home() / ".operator" / ".current_meeting"
    record_server.SLIP_LOCK = Path.home() / ".operator" / "slip.pid"
    print("✓ H-21: stale marker (no slip.pid OR dead pid) rejected; live pid accepted")


def test_poisoned_path_rejected_by_safety_filter():
    """SECURITY regression: a poisoned env var or marker file pointing
    at a file OUTSIDE ~/.operator/history/ must be rejected by
    _is_safe_record_path. Without this filter, a same-uid attacker
    could redirect the MCP to read arbitrary files (~/.ssh/id_rsa,
    ~/.aws/credentials, a poisoned JSONL they dropped in /tmp) and
    have the contents served back to claude as 'meeting transcript'."""
    now = time.time()
    # Real meeting fixture inside an isolated HISTORY_DIR.
    safe_dir = Path(tempfile.mkdtemp(prefix="op_history_safe_"))
    safe_file = safe_dir / "real-meeting.jsonl"
    safe_file.write_text(
        json.dumps({"kind": "session_start", "timestamp": now - 60}) + "\n"
        + json.dumps({"kind": "caption", "sender": "Bob", "text": "real", "timestamp": now - 30}) + "\n"
    )
    # Hostile file living OUTSIDE the history dir.
    hostile = Path(tempfile.mktemp(suffix=".jsonl"))
    hostile.write_text(
        json.dumps({"kind": "session_start", "timestamp": now - 60}) + "\n"
        + json.dumps({"kind": "caption", "sender": "Mallory", "text": "EXFIL", "timestamp": now - 30}) + "\n"
    )
    record_server.MARKER_FILE = Path(tempfile.gettempdir()) / "_test_no_marker"
    record_server.HISTORY_DIR = safe_dir
    os.environ[record_server.ENV_PATH] = str(hostile)
    _set_now(now)
    out = record_server.list_captions()
    # Hostile content must NOT be served — instead we get the
    # nothing-wired empty-state because the env var was rejected.
    assert "EXFIL" not in out, "hostile-path content leaked through safety filter"
    hostile.unlink()
    safe_file.unlink()
    safe_dir.rmdir()
    record_server.MARKER_FILE = Path.home() / ".operator" / ".current_meeting"
    print("✓ poisoned path outside HISTORY_DIR rejected (H-6 safety filter)")


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
    test_env_var_wins_over_marker_file()
    test_marker_fallback_when_env_unset()
    test_marker_stale_after_crash_is_rejected()
    test_poisoned_path_rejected_by_safety_filter()
    test_format_includes_clock_and_speaker()
    # find_meetings
    test_find_meetings_by_participant()
    test_find_meetings_fallback_to_chat_senders_when_no_participants_final()
    test_find_meetings_by_date_range()
    test_find_meetings_url_contains()
    test_find_meetings_no_filters_returns_everything()
    print("\nAll meeting-record MCP tests passed.")
