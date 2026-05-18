"""
Unit tests for Component B — MeetingRecord (Boundary + race depth).

Covers the JSONL chat log at ~/.operator/history/<slug>.jsonl:
  1. slug_from_url — happy path + empty/malformed input
  2. New file — meta header + session_start marker written on first open
  3. Existing file rejoin — meta preserved, new session_start appended
  4. append — writes to file + memory, auto-timestamps when omitted
  5. tail(n) — scoped to entries after the most recent session_start
  6. tail(n) — n<=0, malformed lines, in-memory mode
  7. Race: concurrent appends from N threads — no torn lines, all entries present
  8. Race: tail() interleaved with appends — never returns a partial entry

Uses tempfile.TemporaryDirectory per test; no global state.

Run:
    source venv/bin/activate
    python tests/test_meeting_record.py
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import json
import tempfile
import threading
import time
from pathlib import Path

from _1_800_operator.pipeline.meeting_record import MeetingRecord, slug_from_url


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_lines(path: Path) -> list[dict]:
    """Read and parse every line of a JSONL file."""
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------------------
# Test 1: slug_from_url
# ---------------------------------------------------------------------------

def test_slug_from_url():
    """Happy path, empties, and malformed inputs all resolve safely."""
    assert slug_from_url("https://meet.google.com/pgy-qauk-frn") == "pgy-qauk-frn"
    assert slug_from_url("") == "unknown-meeting"
    assert slug_from_url(None) == "unknown-meeting"  # type: ignore[arg-type]
    # Strips unsafe characters
    assert slug_from_url("https://meet.google.com/abc_def!@#") == "abcdef"
    # Path-only fallback when urlparse yields no path
    assert slug_from_url("bare-slug") == "bare-slug"
    # All-unsafe chars → fallback
    assert slug_from_url("!@#$%") == "unknown-meeting"
    print("PASS  test_slug_from_url")


# ---------------------------------------------------------------------------
# Test 2: new file writes meta header + session_start
# ---------------------------------------------------------------------------

def test_new_file_writes_meta_and_session_start():
    """First open of a fresh slug writes a meta header, then a session_start marker."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rec = MeetingRecord(slug="abc-def-ghi", root=root, meta={"url": "https://meet.google.com/abc-def-ghi"})
        entries = read_lines(rec.path)
        assert len(entries) == 2, f"Expected meta + session_start, got: {entries}"
        assert entries[0]["kind"] == "meta"
        assert entries[0]["slug"] == "abc-def-ghi"
        assert entries[0]["url"] == "https://meet.google.com/abc-def-ghi"
        assert "created_at" in entries[0]
        assert entries[1]["kind"] == "session_start"
        assert "timestamp" in entries[1]
    print("PASS  test_new_file_writes_meta_and_session_start")


# ---------------------------------------------------------------------------
# Test 3: existing file rejoin preserves meta, adds new session_start
# ---------------------------------------------------------------------------

def test_existing_file_rejoin_preserves_meta():
    """Rejoining an existing record leaves meta untouched; a new session_start is appended."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rec1 = MeetingRecord(slug="abc-def-ghi", root=root, meta={"url": "first"})
        rec1.append("user", "hello")
        first_entries = read_lines(rec1.path)
        original_meta = first_entries[0]

        # Rejoin with different meta — header must not be rewritten
        rec2 = MeetingRecord(slug="abc-def-ghi", root=root, meta={"url": "second"})
        rec2.append("user", "again")
        second_entries = read_lines(rec2.path)

        assert second_entries[0] == original_meta, \
            f"Meta header was modified on rejoin: {second_entries[0]} vs {original_meta}"
        # Count session_start markers: should be exactly 2 (one per open)
        markers = [e for e in second_entries if e.get("kind") == "session_start"]
        assert len(markers) == 2, f"Expected 2 session_start markers, got {len(markers)}"
    print("PASS  test_existing_file_rejoin_preserves_meta")


# ---------------------------------------------------------------------------
# Test 4: append writes to file with auto-timestamp + honored overrides
# ---------------------------------------------------------------------------

def test_append_writes_file():
    """append() persists to JSONL; timestamp auto-populates when omitted; in-memory list stays empty in slug mode."""
    with tempfile.TemporaryDirectory() as tmp:
        rec = MeetingRecord(slug="t", root=Path(tmp))
        before = time.time()
        entry = rec.append("alice", "hi there")
        after = time.time()

        assert entry["sender"] == "alice"
        assert entry["text"] == "hi there"
        assert entry["kind"] == "chat"
        assert before <= entry["timestamp"] <= after

        # File mirror
        entries = read_lines(rec.path)
        chat_entries = [e for e in entries if e.get("kind") == "chat"]
        assert len(chat_entries) == 1 and chat_entries[0] == entry

        # In slug mode the in-memory list is unused — file is the single source of truth.
        assert rec._memory == []

        # Explicit timestamp + custom kind honored
        fixed = rec.append("bob", "system msg", kind="system", timestamp=123.0)
        assert fixed["timestamp"] == 123.0 and fixed["kind"] == "system"
    print("PASS  test_append_writes_file")


# ---------------------------------------------------------------------------
# Test 5: tail() scoped to entries after most recent session_start
# ---------------------------------------------------------------------------

def test_tail_scopes_to_latest_session():
    """tail(n) must not leak entries from prior sessions — the core correctness guarantee."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rec1 = MeetingRecord(slug="m", root=root)
        rec1.append("user", "old-1")
        rec1.append("assistant", "old-2")  # this would cause echo bugs if leaked

        rec2 = MeetingRecord(slug="m", root=root)  # new session_start appended
        rec2.append("user", "new-1")
        rec2.append("assistant", "new-2")

        got = rec2.tail(50)
        texts = [e.get("text") for e in got]
        assert texts == ["new-1", "new-2"], \
            f"tail leaked prior session entries: {texts}"

        # n smaller than session size truncates to last n
        assert [e["text"] for e in rec2.tail(1)] == ["new-2"]
    print("PASS  test_tail_scopes_to_latest_session")


# ---------------------------------------------------------------------------
# Test 6: tail() edges — n<=0, malformed lines, in-memory mode
# ---------------------------------------------------------------------------

def test_tail_edges():
    """n<=0 returns []; malformed JSON lines are skipped; in-memory mode tails from self._memory."""
    # n <= 0
    with tempfile.TemporaryDirectory() as tmp:
        rec = MeetingRecord(slug="m", root=Path(tmp))
        rec.append("u", "a")
        assert rec.tail(0) == []
        assert rec.tail(-3) == []

    # Malformed lines skipped, well-formed ones returned
    with tempfile.TemporaryDirectory() as tmp:
        rec = MeetingRecord(slug="m", root=Path(tmp))
        rec.append("u", "good-1")
        # Corrupt the file by appending a garbage line, then a valid one
        with rec.path.open("a", encoding="utf-8") as f:
            f.write("{not json\n")
        rec.append("u", "good-2")
        got = rec.tail(10)
        texts = [e.get("text") for e in got if e.get("kind") == "chat"]
        assert texts == ["good-1", "good-2"], f"Malformed line broke tail: {texts}"

    # In-memory mode (no slug): tails from self._memory
    rec = MeetingRecord()
    assert rec.path is None
    rec.append("u", "mem-1")
    rec.append("u", "mem-2")
    rec.append("u", "mem-3")
    got = rec.tail(2)
    assert [e["text"] for e in got] == ["mem-2", "mem-3"]
    print("PASS  test_tail_edges")


# ---------------------------------------------------------------------------
# Test 7: race — concurrent appends don't produce torn lines
# ---------------------------------------------------------------------------

def test_concurrent_appends_no_torn_lines():
    """10 threads × 20 appends — every line parses, every entry present."""
    with tempfile.TemporaryDirectory() as tmp:
        rec = MeetingRecord(slug="race", root=Path(tmp))
        N_THREADS = 10
        PER_THREAD = 20

        def worker(tid):
            for i in range(PER_THREAD):
                rec.append(f"t{tid}", f"msg-{tid}-{i}")

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Every line must parse cleanly — torn writes would raise here
        entries = read_lines(rec.path)
        chat_entries = [e for e in entries if e.get("kind") == "chat"]
        assert len(chat_entries) == N_THREADS * PER_THREAD, \
            f"Expected {N_THREADS * PER_THREAD} chat entries, got {len(chat_entries)}"

        # Every (tid, i) pair present exactly once
        expected = {f"msg-{t}-{i}" for t in range(N_THREADS) for i in range(PER_THREAD)}
        actual = {e["text"] for e in chat_entries}
        assert actual == expected, f"Missing: {expected - actual}; Extra: {actual - expected}"
    print("PASS  test_concurrent_appends_no_torn_lines")


# ---------------------------------------------------------------------------
# Test 8: race — tail() interleaved with appends never sees a partial entry
# ---------------------------------------------------------------------------

def test_history_dir_and_jsonl_are_owner_only():
    """~/.operator/history/ must be 0o700, and JSONL files 0o600 — meeting
    transcripts can hold sensitive chat / caption content. Belt-and-suspenders
    over umask: covers legacy installs whose history dir was created
    world-readable before __main__.main set umask 0o077.
    """
    import stat as _stat
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "history"
        # Pre-create the dir with looser perms to simulate the legacy
        # install case the defensive chmod is meant to retroactively fix.
        root.mkdir(mode=0o755)
        # Pre-create a JSONL file with looser perms too — simulates a
        # legacy meeting record file from before the umask fix.
        legacy_file = root / "legacy.jsonl"
        legacy_file.write_text('{"kind":"meta"}\n', encoding="utf-8")
        os.chmod(legacy_file, 0o644)
        assert _stat.S_IMODE(root.stat().st_mode) == 0o755
        assert _stat.S_IMODE(legacy_file.stat().st_mode) == 0o644

        # Reopen the legacy meeting — defensive chmods should retroactively
        # tighten the dir + the file we touch.
        rec = MeetingRecord(slug="legacy", root=root)
        assert _stat.S_IMODE(root.stat().st_mode) == 0o700, \
            f"history dir must be 0o700, got {oct(_stat.S_IMODE(root.stat().st_mode))}"
        assert _stat.S_IMODE(rec.path.stat().st_mode) == 0o600, \
            f"jsonl file must be 0o600, got {oct(_stat.S_IMODE(rec.path.stat().st_mode))}"

        # And a fresh-slug case lands at 0o600 from the start.
        rec2 = MeetingRecord(slug="fresh", root=root)
        rec2.append("alice", "hello")
        assert _stat.S_IMODE(rec2.path.stat().st_mode) == 0o600
    print("PASS  test_history_dir_and_jsonl_are_owner_only")


def test_tail_during_concurrent_appends():
    """While writers append, repeated tail() calls must never fail to parse or observe torn entries."""
    with tempfile.TemporaryDirectory() as tmp:
        rec = MeetingRecord(slug="race2", root=Path(tmp))
        stop = threading.Event()
        errors: list[Exception] = []

        def writer(tid):
            i = 0
            while not stop.is_set():
                rec.append(f"w{tid}", f"line-{tid}-{i}")
                i += 1

        def reader():
            while not stop.is_set():
                try:
                    got = rec.tail(100)
                    # Every entry returned must be a well-formed dict with the expected keys
                    for e in got:
                        if e.get("kind") == "chat":
                            assert "sender" in e and "text" in e and "timestamp" in e
                except Exception as exc:
                    errors.append(exc)
                    return

        writers = [threading.Thread(target=writer, args=(t,)) for t in range(4)]
        readers = [threading.Thread(target=reader) for _ in range(2)]
        for t in writers + readers:
            t.start()
        time.sleep(0.3)
        stop.set()
        for t in writers + readers:
            t.join()

        assert not errors, f"tail() observed a torn/partial entry: {errors[:3]}"
        # Sanity: something actually got written
        assert any(e.get("kind") == "chat" for e in read_lines(rec.path))
    print("PASS  test_tail_during_concurrent_appends")


# ---------------------------------------------------------------------------
# close() — meeting-end lifecycle events
# ---------------------------------------------------------------------------

def test_close_writes_participants_final_and_meeting_end():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rec = MeetingRecord(slug="abc", root=root, meta={"meet_url": "https://meet.google.com/abc"})
        rec.append(sender="alice", text="hello")
        rec.close(
            attended=["alice", "bob"],
            currently_present=["bob"],
            self_name="operator",
        )
        entries = read_lines(rec.path)
        kinds = [e.get("kind") for e in entries]
        assert kinds[-2:] == ["participants_final", "meeting_end"], kinds
        final = entries[-2]
        assert final["attended"] == ["alice", "bob"], final
        assert final["currently_present"] == ["bob"], final
        assert final["self_name"] == "operator", final
        print("  close: writes participants_final + meeting_end: OK")


def test_close_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        rec = MeetingRecord(slug="abc", root=Path(tmp), meta={})
        rec.close(attended=["alice"])
        rec.close(attended=["alice"])  # second call should be a no-op
        rec.close()
        kinds = [e.get("kind") for e in read_lines(rec.path)]
        assert kinds.count("meeting_end") == 1, kinds
        assert kinds.count("participants_final") == 1, kinds
        print("  close: idempotent across repeat calls: OK")


def test_close_without_attended_skips_participants_final():
    with tempfile.TemporaryDirectory() as tmp:
        rec = MeetingRecord(slug="abc", root=Path(tmp), meta={})
        rec.close()  # no attended kwarg
        kinds = [e.get("kind") for e in read_lines(rec.path)]
        assert "participants_final" not in kinds, kinds
        assert kinds[-1] == "meeting_end", kinds
        print("  close: no attended → skip participants_final, still write meeting_end: OK")


def test_close_in_memory_mode_is_noop():
    rec = MeetingRecord(slug=None)  # in-memory mode
    rec.close(attended=["alice"])
    # No path, no crash, no entries appended.
    assert rec.path is None
    print("  close: in-memory mode is a no-op: OK")


def test_append_after_close_is_dropped_silently():
    """H-25: between meeting_record.close() and connector.leave() in the
    shutdown sequence, the audio caption thread is still alive and can
    fire _on_utterance, which calls append() on the already-closed
    record. Pre-fix this wrote the late caption AFTER the meeting_end
    marker — corrupting the seal that find_meetings / list_meetings rely
    on to bound a session.

    Post-fix append() checks _closed and silently returns (the in-memory
    entry dict is still returned for caller compatibility, but nothing is
    written to disk and nothing is added to _chat_tail).
    """
    with tempfile.TemporaryDirectory() as tmp:
        rec = MeetingRecord(slug="abc", root=Path(tmp), meta={})
        rec.append(sender="alice", text="hello before close")
        rec.close(attended=["alice"])
        # Snapshot what's on disk after the seal.
        kinds_before = [e.get("kind") for e in read_lines(rec.path)]
        assert kinds_before[-1] == "meeting_end", kinds_before

        # Now simulate a late caption finalizing post-close.
        rec.append(sender="alice", text="late caption after close",
                   kind="caption")
        rec.append(sender="bob", text="late chat after close")

        # Disk state must be unchanged — no entries past meeting_end.
        kinds_after = [e.get("kind") for e in read_lines(rec.path)]
        assert kinds_after == kinds_before, (
            f"append after close should not write to disk; "
            f"before={kinds_before} after={kinds_after}"
        )
        # _chat_tail must not have grown either.
        assert all(
            "late chat after close" not in (e.get("text") or "")
            for e in rec._chat_tail
        ), "post-close chat should not enter _chat_tail"
    print("  append: post-close calls are dropped silently (seal preserved): OK")


# ---------------------------------------------------------------------------
# Run all
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_slug_from_url,
        test_new_file_writes_meta_and_session_start,
        test_existing_file_rejoin_preserves_meta,
        test_append_writes_file,
        test_tail_scopes_to_latest_session,
        test_tail_edges,
        test_concurrent_appends_no_torn_lines,
        test_history_dir_and_jsonl_are_owner_only,
        test_tail_during_concurrent_appends,
        test_close_writes_participants_final_and_meeting_end,
        test_close_is_idempotent,
        test_close_without_attended_skips_participants_final,
        test_close_in_memory_mode_is_noop,
        test_append_after_close_is_dropped_silently,
    ]
    failures = []
    for t in tests:
        try:
            t()
        except Exception as e:
            import traceback
            print(f"FAIL  {t.__name__}: {e}")
            traceback.print_exc()
            failures.append(t.__name__)

    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
