#!/usr/bin/env python3
"""Operator-voice failure-narration tests for ChatRunner.

History: this file once covered a side-channel `claude -p` heartbeat
(stripped in S211, Phase 14.22.3) and then a set of operator-side tool/
denial/connection narration callbacks (stripped in S228 when the 14.22
PTY pivot moved narration into Claude's own voice via the provider's
first-paste briefing — see ClaudeCLIProvider._BRIEFING).

What remains is operator's own failure channel: `_narrate_failure`
posts directly in operator voice when operator *itself* fails (a result
shape it can't render, a crashed subprocess) — no LLM call, no
operator-authored prompt. That's distinct from Claude's self-narration
and still operator's job, so it keeps its tests here.

The file name is kept (`test_heartbeat.py`) to preserve Git history.
"""
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from _1_800_operator.bridges.claude import REPLY_PREFIX_OPERATOR
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
    assert runner._connector.sent_raw[0].startswith(REPLY_PREFIX_OPERATOR)
    assert "snag" in runner._connector.sent_raw[0]
    # Must not have gone through the prefixed path.
    assert runner._connector.sent_prefixed == []


def main():
    tests = [
        test_narrate_failure_skipped_during_shutdown,
        test_narrate_failure_posts_operator_voice_directly,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
        except Exception as e:
            print(f"  ✗ {t.__name__}: {type(e).__name__}: {e}")
            failed += 1

    if failed:
        print(f"\n{failed} test(s) failed")
        sys.exit(1)
    else:
        print("\nAll operator failure-narration tests passed.")


if __name__ == "__main__":
    main()
