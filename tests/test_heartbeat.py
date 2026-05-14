#!/usr/bin/env python3
"""Failure-narration tests for ChatRunner.

History: this file once covered a side-channel `claude -p` heartbeat
(stripped in S211) and then operator-side tool/denial/connection
narration callbacks (stripped in S228 when meeting narration moved into
Claude's own voice via the provider's first-paste briefing).

What remains is `_narrate_failure`: when operator *itself* can't render
a result (an unknown result shape, a crashed subprocess), it still owes
the room a reply. It posts on the normal `[🤖 Claude] ` path — there is
no operator voice; from the meeting's point of view the bot just
stumbled and says so.

The file name is kept (`test_heartbeat.py`) to preserve Git history.
"""
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from _1_800_operator.pipeline.chat_runner import ChatRunner


class FakeConnector:
    """Stand-in for AttachAdapter. Tracks `send_chat` posts (everything
    goes through the prefixed path now — there is no raw send path)."""

    def __init__(self):
        self.sent = []
        self.lock = threading.Lock()

    def send_chat(self, text):
        with self.lock:
            self.sent.append(text)
        return f"msg-{len(self.sent)}"


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
    assert runner._connector.sent == []


def test_narrate_failure_posts_to_chat():
    """A genuine operator-side failure still owes the room a reply —
    posted on the normal send_chat path, no LLM call."""
    runner = _make_runner()
    runner._narrate_failure("hit an unexpected snag — try @mentioning again")
    assert len(runner._connector.sent) == 1
    assert "snag" in runner._connector.sent[0]


def main():
    tests = [
        test_narrate_failure_skipped_during_shutdown,
        test_narrate_failure_posts_to_chat,
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
        print("\nAll failure-narration tests passed.")


if __name__ == "__main__":
    main()
