#!/usr/bin/env python3
"""Operator-voice narration tests for ChatRunner.

Covers the narration callbacks that replaced the heartbeat side-channel
in Phase 14.22.3 (S211). The heartbeat used to spawn a side-channel
`claude -p` invocation with an operator-authored prompt — that pattern
got stripped wholesale because it carried harness identity at the spawn
layer (see `memory/project_anthropic_detection_vector.md`). The new
narration is operator-side only: stream-reading + chat-posting, zero
spawn-signature impact.

  1. _narrate_tool_use posts `[☎️ Operator] running <tool>: <args>` with
     a TOOL_NARRATION_THROTTLE_SECONDS throttle (rapid tool chains
     collapse to one line; long-running tools get periodic re-posts).
  2. _narrate_tool_use skips internal tools (ToolSearch).
  3. _narrate_denial posts the `--yolo` hint, deduped per
     tool_use_id within a turn.
  4. _narrate_connection posts switchboard-voice status on EOF +
     retry events (suppresses the "reconnecting" event since "dropped"
     already implies it).
  5. _narrate_failure posts directly in operator voice — no LLM call,
     no operator-authored prompt fed into `claude -p`.

The file name is kept (`test_heartbeat.py`) to preserve the Git
history; the contents are 100% rewritten for the new design.
"""
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from _1_800_operator.bridges.claude import REPLY_PREFIX_SLIP
from _1_800_operator.pipeline import chat_runner as cr_mod
from _1_800_operator.pipeline.chat_runner import ChatRunner


class FakeConnector:
    """Stand-in for AttachAdapter. Tracks both prefixed (`send_chat`) and
    raw (`send_chat_raw`) posts so we can verify operator-voice narration
    bypasses the slip prefix correctly."""

    def __init__(self):
        self.sent_prefixed = []
        self.sent_raw = []
        self.lock = threading.Lock()

    def send_chat(self, text):
        with self.lock:
            self.sent_prefixed.append(text)
        return f"prefixed-{len(self.sent_prefixed)}"

    def send_chat_raw(self, text):
        with self.lock:
            self.sent_raw.append(text)
        return f"raw-{len(self.sent_raw)}"


class FakeRecord:
    def __init__(self):
        self.appended = []

    def append(self, *, sender, text, kind):
        self.appended.append({"sender": sender, "text": text, "kind": kind})


def _make_runner():
    return ChatRunner(
        connector=FakeConnector(),
        llm=object(),
        meeting_record=FakeRecord(),
    )


def test_narrate_tool_use_throttles_rapid_chains(monkeypatch):
    """First tool_use posts narration; subsequent ones within
    TOOL_NARRATION_THROTTLE_SECONDS are suppressed."""
    runner = _make_runner()
    fake_now = [1000.0]
    monkeypatch.setattr(cr_mod.time, "monotonic", lambda: fake_now[0])

    runner._narrate_tool_use("Read", {"file_path": "/tmp/foo.txt"})
    runner._narrate_tool_use("Bash", {"command": "ls -la"})
    runner._narrate_tool_use("Grep", {"pattern": "BLOCKS"})

    raw = runner._connector.sent_raw
    assert len(raw) == 1, f"throttle should suppress chained narrations; got {raw}"
    assert raw[0].startswith(REPLY_PREFIX_SLIP)
    assert "running Read" in raw[0]
    assert "/tmp/foo.txt" in raw[0]

    # Advance past the throttle window — next tool fires.
    fake_now[0] += cr_mod.TOOL_NARRATION_THROTTLE_SECONDS + 0.1
    runner._narrate_tool_use("Edit", {"file_path": "/tmp/bar.txt"})
    assert len(runner._connector.sent_raw) == 2
    assert "running Edit" in runner._connector.sent_raw[1]


def test_narrate_tool_use_skips_internal_tools(monkeypatch):
    """ToolSearch is internal scaffolding; meeting participants don't
    need to see it."""
    runner = _make_runner()
    fake_now = [1000.0]
    monkeypatch.setattr(cr_mod.time, "monotonic", lambda: fake_now[0])

    runner._narrate_tool_use("ToolSearch", {"query": "Read"})
    assert runner._connector.sent_raw == []
    assert runner._last_tool_narration_ts == 0.0  # throttle stamp untouched


def test_narrate_denial_dedupes_per_tool_use_id():
    """Same tool_use_id only fires denial narration once per turn."""
    runner = _make_runner()
    runner._narrate_denial("toolu_abc")
    runner._narrate_denial("toolu_abc")
    runner._narrate_denial("toolu_abc")
    assert len(runner._connector.sent_raw) == 1
    assert "permission denied" in runner._connector.sent_raw[0]
    assert "--yolo" in runner._connector.sent_raw[0]

    runner._narrate_denial("toolu_xyz")  # different id → fires
    assert len(runner._connector.sent_raw) == 2

    # Simulate turn boundary: clearing the dedup set re-arms.
    runner._denied_tool_ids_in_turn.clear()
    runner._narrate_denial("toolu_abc")
    assert len(runner._connector.sent_raw) == 3


def test_narrate_connection_dropped_posts_switchboard_voice():
    """Operator narrates connection drops in switchboard voice, posted
    via send_chat_raw so the slip bot prefix isn't double-prepended."""
    runner = _make_runner()
    runner._narrate_connection("dropped")
    assert len(runner._connector.sent_raw) == 1
    assert runner._connector.sent_raw[0].startswith(REPLY_PREFIX_SLIP)
    assert "connection dropped" in runner._connector.sent_raw[0]


def test_narrate_connection_reconnecting_suppressed():
    """`reconnecting` is implied by `dropped`'s ellipsis; suppress to
    avoid double-posting the same state."""
    runner = _make_runner()
    runner._narrate_connection("reconnecting")
    assert runner._connector.sent_raw == []


def test_narrate_connection_failed_posts_retry_hint():
    """When the EOF retry also fails, operator surfaces a retry hint."""
    runner = _make_runner()
    runner._narrate_connection("failed")
    assert len(runner._connector.sent_raw) == 1
    assert "couldn't reach Claude" in runner._connector.sent_raw[0]
    assert "@mentioning again" in runner._connector.sent_raw[0]


def test_narrate_failure_skipped_during_shutdown():
    """No chat post when stop_event is set — the chat panel is detaching
    and posting would race the shutdown."""
    runner = _make_runner()
    runner._stop_event.set()
    runner._narrate_failure("something broke")
    assert runner._connector.sent_raw == []
    assert runner._connector.sent_prefixed == []


def test_narrate_failure_posts_operator_voice_directly():
    """No LLM call, no operator-authored prompt — direct switchboard-
    voice post."""
    runner = _make_runner()
    runner._narrate_failure("hit an unexpected snag — try @mentioning again")
    assert len(runner._connector.sent_raw) == 1
    assert runner._connector.sent_raw[0].startswith(REPLY_PREFIX_SLIP)
    assert "snag" in runner._connector.sent_raw[0]
    # Must not have gone through the prefixed path.
    assert runner._connector.sent_prefixed == []


def test_summarize_tool_input_picks_informative_arg():
    """Single-arg summarizer used by _narrate_tool_use."""
    runner = _make_runner()
    assert "/tmp/foo.txt" in runner._summarize_tool_input({"file_path": "/tmp/foo.txt"})
    assert "ls -la /tmp" in runner._summarize_tool_input({"command": "ls -la /tmp"})
    assert "BLOCKS LAUNCH" in runner._summarize_tool_input(
        {"pattern": "BLOCKS LAUNCH", "path": "docs/"}
    )
    # Unknown keys → fallback to arg-key list
    summary = runner._summarize_tool_input({"weird_key": "weird_val"})
    assert "weird_key" in summary
    # Empty input → empty summary
    assert runner._summarize_tool_input({}) == ""


def main():
    import os
    tests = [
        test_narrate_tool_use_throttles_rapid_chains,
        test_narrate_tool_use_skips_internal_tools,
        test_narrate_denial_dedupes_per_tool_use_id,
        test_narrate_connection_dropped_posts_switchboard_voice,
        test_narrate_connection_reconnecting_suppressed,
        test_narrate_connection_failed_posts_retry_hint,
        test_narrate_failure_skipped_during_shutdown,
        test_narrate_failure_posts_operator_voice_directly,
        test_summarize_tool_input_picks_informative_arg,
    ]
    # Lightweight monkeypatch shim so we don't need pytest. Tests that
    # take `monkeypatch` get a fresh shim per run; teardown restores
    # original attributes on exit.
    class _Patch:
        def __init__(self):
            self._undo = []
        def setattr(self, target, attr, value):
            original = getattr(target, attr)
            self._undo.append((target, attr, original))
            setattr(target, attr, value)
        def undo(self):
            for target, attr, original in reversed(self._undo):
                setattr(target, attr, original)

    failed = 0
    for t in tests:
        patch = _Patch()
        try:
            if "monkeypatch" in t.__code__.co_varnames:
                t(patch)
            else:
                t()
            print(f"  ✓ {t.__name__}")
        except Exception as e:
            print(f"  ✗ {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
        finally:
            patch.undo()

    if failed:
        print(f"\n{failed} test(s) failed")
        sys.exit(1)
    else:
        print("\nAll operator-narration tests passed.")


if __name__ == "__main__":
    main()
