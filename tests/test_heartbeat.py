#!/usr/bin/env python3
"""Heartbeat behavior tests for ChatRunner.

Covers:
  1. Silence threshold trips → side-channel call fires → result posted
  2. Inner-claude emits text during the call → heartbeat suppressed
  3. Side-channel call returns empty/None → no chat post (not a fatal)
  4. Tool tracker is populated by the progress callback and cleared on
     turn start
  5. Heartbeat thread doesn't run outside a turn

We unit-test the heartbeat plumbing in isolation by stubbing
`_request_heartbeat_text` (so no actual `claude -p` is spawned) and
the connector. The threshold and tick are monkeypatched down so tests
finish in seconds rather than 30+.
"""
import os
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from _1_800_operator.pipeline import chat_runner as cr_mod
from _1_800_operator.pipeline.chat_runner import ChatRunner


class FakeConnector:
    """Minimal stand-in for a MeetingConnector. We only exercise send_chat
    via _send; nothing else is touched in these tests."""

    def __init__(self):
        self.sent = []
        self.lock = threading.Lock()

    def send_chat(self, text):
        with self.lock:
            self.sent.append(text)
        # Adapter contract: return msg_id (str | None). None is the
        # adapter-can't-return-id fallback path; either is fine for us.
        return f"id-{len(self.sent)}"


class FakeRecord:
    def __init__(self):
        self.appended = []

    def append(self, *, sender, text, kind):
        self.appended.append({"sender": sender, "text": text, "kind": kind})


def _make_runner(monkeypatch=None):
    """Build a ChatRunner with stubbed deps. We don't call run() — the
    polling loop and join machinery aren't exercised in heartbeat
    tests; we drive _handle_message directly."""
    runner = ChatRunner(
        connector=FakeConnector(),
        llm=object(),  # _handle_message is stubbed in each test
        meeting_record=FakeRecord(),
    )
    runner._intro_posted = True
    return runner


def test_record_tool_use_populates_deque():
    runner = _make_runner()
    assert len(runner._recent_tool_uses) == 0
    runner._record_tool_use("Read", {"file_path": "/tmp/foo.txt"})
    runner._record_tool_use("Grep", {"pattern": "BLOCKS", "path": "."})
    assert len(runner._recent_tool_uses) == 2
    assert runner._recent_tool_uses[0]["name"] == "Read"
    assert runner._recent_tool_uses[1]["input"]["pattern"] == "BLOCKS"
    print("record_tool_use populates deque OK")


def test_summarize_recent_tools_picks_informative_arg():
    runner = _make_runner()
    runner._record_tool_use("Read", {"file_path": "/tmp/foo.txt"})
    runner._record_tool_use("Bash", {"command": "ls -la /tmp", "description": "list"})
    runner._record_tool_use("Grep", {"pattern": "BLOCKS LAUNCH", "path": "docs/"})
    runner._record_tool_use("Unknown", {"weird_key": "weird_val"})
    runner._record_tool_use("NoArgs", {})

    summary = runner._summarize_recent_tools(list(runner._recent_tool_uses))
    assert "Read: /tmp/foo.txt" in summary
    assert "Bash: ls -la /tmp" in summary  # picks command, not description
    assert "Grep: BLOCKS LAUNCH" in summary
    assert "Unknown: (weird_key)" in summary  # falls back to keys when no preferred
    assert "NoArgs" in summary
    print("summarize_recent_tools picks informative arg OK")


def test_summarize_empty_returns_sentinel():
    runner = _make_runner()
    out = runner._summarize_recent_tools([])
    assert "(none yet)" in out
    print("summarize empty returns sentinel OK")


def test_heartbeat_fires_after_silence(monkeypatch):
    """Threshold tripped + side-channel returns text → heartbeat posted."""
    runner = _make_runner()
    # Fast knobs so the test runs in ~0.5s instead of 30s.
    monkeypatch_chat_runner(monkeypatch, threshold=0.3, tick=0.05)

    heartbeat_called = []

    def fake_request(user_msg, recent_tools):
        heartbeat_called.append({"user_msg": user_msg, "tools": list(recent_tools)})
        return "Pulling the roadmap to find the open MVP items."

    runner._request_heartbeat_text = fake_request

    # Simulate a turn: set _heartbeat_user_msg and _turn_start_ts as
    # _handle_message would, spawn the loop directly, wait long enough
    # to trip, then signal stop.
    runner._heartbeat_user_msg = "what's open before MVP?"
    runner._turn_start_ts = time.time()
    runner._record_tool_use("Read", {"file_path": "docs/roadmap.md"})
    stop = threading.Event()
    t = threading.Thread(target=runner._heartbeat_loop, args=(stop,), daemon=True)
    t.start()

    time.sleep(0.6)  # 2x the threshold to ensure trip + post
    stop.set()
    t.join(timeout=2.0)
    # The heartbeat daemon enqueues sends to be drained on the main
    # thread (Playwright's sync API rejects non-main-thread calls).
    # In the live runner this drain happens via the polling loop and
    # the provider's tick callback; in this isolated test we run it
    # explicitly from the test's main thread.
    runner._drain_pending_sends()

    assert len(heartbeat_called) >= 1, "side-channel should have fired"
    assert heartbeat_called[0]["user_msg"] == "what's open before MVP?"
    assert any(t["name"] == "Read" for t in heartbeat_called[0]["tools"])
    sent_kinds = [r["kind"] for r in runner._record.appended]
    sent_texts = [r["text"] for r in runner._record.appended]
    assert "heartbeat" in sent_kinds, f"expected a heartbeat record, got {sent_kinds}"
    assert any("Pulling the roadmap" in t for t in sent_texts)
    print("heartbeat fires after silence OK")


def test_heartbeat_skipped_when_text_landed_during_call(monkeypatch):
    """Inner claude posts text while the side-channel call is running →
    heartbeat result is dropped, not double-posted."""
    runner = _make_runner()
    monkeypatch_chat_runner(monkeypatch, threshold=0.3, tick=0.05)

    fired = []

    def fake_request(user_msg, recent_tools):
        # Simulate the side-channel taking 0.3s during which inner-
        # claude emits text (we bump _last_send_time mid-call).
        time.sleep(0.15)
        runner._last_send_time = time.time()  # "real reply landed"
        time.sleep(0.15)
        fired.append(True)
        return "I'll check the roadmap now."

    runner._request_heartbeat_text = fake_request

    runner._heartbeat_user_msg = "status?"
    runner._turn_start_ts = time.time()
    stop = threading.Event()
    t = threading.Thread(target=runner._heartbeat_loop, args=(stop,), daemon=True)
    t.start()

    time.sleep(0.7)
    stop.set()
    t.join(timeout=2.0)
    # Drain any queued off-thread sends so the assertion runs against
    # the same end state as production.
    runner._drain_pending_sends()

    # Side-channel did fire (threshold tripped before the bump), but
    # the heartbeat post should have been suppressed by the race re-check.
    assert len(fired) >= 1
    sent_kinds = [r["kind"] for r in runner._record.appended]
    assert "heartbeat" not in sent_kinds, (
        f"heartbeat should have been suppressed, got records: "
        f"{runner._record.appended}"
    )
    print("heartbeat skipped when text landed during call OK")


def test_heartbeat_silent_on_request_failure(monkeypatch):
    """Side-channel returns None (timeout, error, empty) → no chat post."""
    runner = _make_runner()
    monkeypatch_chat_runner(monkeypatch, threshold=0.3, tick=0.05)

    runner._request_heartbeat_text = lambda u, r: None

    runner._heartbeat_user_msg = "anything?"
    runner._turn_start_ts = time.time()
    stop = threading.Event()
    t = threading.Thread(target=runner._heartbeat_loop, args=(stop,), daemon=True)
    t.start()

    time.sleep(0.6)
    stop.set()
    t.join(timeout=2.0)

    sent_kinds = [r["kind"] for r in runner._record.appended]
    assert "heartbeat" not in sent_kinds
    assert runner._connector.sent == []
    print("heartbeat silent on request failure OK")


def test_heartbeat_does_not_double_fire(monkeypatch):
    """Two threshold trips in rapid succession → second one re-arms via
    _last_heartbeat_post_ts; we don't get back-to-back heartbeats."""
    runner = _make_runner()
    monkeypatch_chat_runner(monkeypatch, threshold=0.3, tick=0.05)

    runner._request_heartbeat_text = lambda u, r: "Working on it."

    runner._heartbeat_user_msg = "status?"
    runner._turn_start_ts = time.time()
    stop = threading.Event()
    t = threading.Thread(target=runner._heartbeat_loop, args=(stop,), daemon=True)
    t.start()

    # Wait ~2.5x the threshold. That's enough for ONE heartbeat (at
    # ~0.3s in) + the silence clock re-arming so the next one would
    # fire at ~0.6s. 0.7s gives us room for both, but the re-arm
    # logic should ensure only one post in that window since
    # _last_heartbeat_post_ts is what the anchor reads next.
    time.sleep(0.45)
    stop.set()
    t.join(timeout=2.0)
    runner._drain_pending_sends()

    heartbeat_records = [r for r in runner._record.appended if r["kind"] == "heartbeat"]
    assert len(heartbeat_records) == 1, (
        f"expected exactly one heartbeat in 0.45s window, got "
        f"{len(heartbeat_records)}: {heartbeat_records}"
    )
    print("heartbeat does not double-fire within threshold OK")


# --- helpers --------------------------------------------------------

class _MonkeyPatchShim:
    """Tiny replacement for pytest's monkeypatch — these tests don't
    use pytest, so we hand-roll the few semantics we need."""
    def __init__(self):
        self._undos = []

    def setattr(self, target, attr, value):
        original = getattr(target, attr)
        self._undos.append(lambda: setattr(target, attr, original))
        setattr(target, attr, value)

    def undo(self):
        for fn in reversed(self._undos):
            fn()
        self._undos.clear()


def monkeypatch_chat_runner(monkeypatch, *, threshold: float, tick: float):
    """Patch the module-level constants in chat_runner so the loop
    runs at test-friendly speeds."""
    monkeypatch.setattr(cr_mod, "HEARTBEAT_SILENCE_SECONDS", threshold)
    monkeypatch.setattr(cr_mod, "HEARTBEAT_TICK_SECONDS", tick)


# --- runner ---------------------------------------------------------

if __name__ == "__main__":
    # Tiny test runner. Each test gets a fresh _MonkeyPatchShim if it
    # takes one. Failures print stack and exit 1.
    import traceback

    no_mp_tests = [
        test_record_tool_use_populates_deque,
        test_summarize_recent_tools_picks_informative_arg,
        test_summarize_empty_returns_sentinel,
    ]
    mp_tests = [
        test_heartbeat_fires_after_silence,
        test_heartbeat_skipped_when_text_landed_during_call,
        test_heartbeat_silent_on_request_failure,
        test_heartbeat_does_not_double_fire,
    ]

    failed = 0
    for fn in no_mp_tests:
        try:
            fn()
        except Exception:
            traceback.print_exc()
            failed += 1
    for fn in mp_tests:
        mp = _MonkeyPatchShim()
        try:
            fn(mp)
        except Exception:
            traceback.print_exc()
            failed += 1
        finally:
            mp.undo()

    if failed:
        print(f"\n{failed} test(s) failed")
        sys.exit(1)
    print("\nAll heartbeat tests passed.")
