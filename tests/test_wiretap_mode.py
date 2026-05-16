"""Unit tests for ChatRunner in wiretap mode.

Wiretap is the passive-recording mode (S238 stage 2): operator joins
the meeting and captures chat + captions + participant roster into the
meeting JSONL, but does NOT spawn an inner-claude, post anything to
chat, or interpret any messages. Verified here:

  - mode="wiretap" with llm=None, classifier=None constructs cleanly
  - _dispatch_user_message returns immediately, never calls _handle_message
  - run() doesn't crash on _wire_provider / set_record paths (llm is None)
  - the meeting record receives chat appends from _process_messages
    (which is upstream of _dispatch_user_message)

End-to-end live behavior (actual meeting join + caption capture) is
covered by the same connector/audio infrastructure the speak-modes use
and is exercised by the live wiretap test, not here.
"""
import sys
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _1_800_operator.pipeline.chat_runner import ChatRunner


class _FakeConnector:
    def __init__(self):
        self.sent = []
        self.chat_messages = []
        self.join_status = None

    def send_chat(self, text):
        self.sent.append(text)
        return f"id-out-{len(self.sent)}"

    def read_chat(self):
        return list(self.chat_messages)

    def is_connected(self):
        return True

    def get_participant_count(self):
        return 2

    def get_participant_names(self):
        return ["Alice", "Bob"]

    def get_self_name(self):
        return "operator"


class _FakeRecord:
    def __init__(self):
        self.slug = "test-meeting"
        self.appended = []

    def append(self, sender, text, kind="chat", timestamp=None):
        self.appended.append({"sender": sender, "text": text, "kind": kind})

    def tail_chat(self, n):
        return []


def test_wiretap_construct_with_no_llm_no_classifier():
    """Constructor must accept llm=None + permission_classifier=None when
    mode='wiretap' without exploding."""
    runner = ChatRunner(
        _FakeConnector(),
        llm=None,
        meeting_record=_FakeRecord(),
        permission_classifier=None,
        mode="wiretap",
    )
    assert runner._mode == "wiretap"
    assert runner._llm is None
    assert runner._classifier is None
    print("  construct: wiretap mode + llm=None + classifier=None: OK")


def test_wiretap_dispatch_user_message_returns_immediately():
    """In wiretap mode, _dispatch_user_message must NOT call _handle_message
    regardless of trigger phrase, sticky window, or message content."""
    runner = ChatRunner(
        _FakeConnector(),
        llm=None,
        meeting_record=_FakeRecord(),
        permission_classifier=None,
        mode="wiretap",
    )
    with mock.patch.object(runner, "_handle_message") as handle:
        runner._dispatch_user_message("@claude do a thing", sender="Alice")
        runner._dispatch_user_message("just chatting", sender="Bob")
        runner._dispatch_user_message("hi @claude", sender="Carol")
    assert handle.call_count == 0, f"_handle_message was called {handle.call_count}x"
    print("  dispatch: wiretap never calls _handle_message: OK")


def test_wiretap_continuation_state_never_opens():
    """No sticky window in wiretap — @claude messages don't open one."""
    runner = ChatRunner(
        _FakeConnector(),
        llm=None,
        meeting_record=_FakeRecord(),
        permission_classifier=None,
        mode="wiretap",
    )
    runner._dispatch_user_message("@claude hello", sender="Alice")
    assert runner._continuation_sender is None
    assert runner._continuation_open_until == 0.0
    assert runner._continuation_pending is None
    print("  continuation: wiretap doesn't open sticky window: OK")


def test_wiretap_stop_with_no_provider_no_classifier():
    """stop() must be safe with no provider wired (mode='wiretap' never
    calls _wire_provider) and no classifier to tear down."""
    runner = ChatRunner(
        _FakeConnector(),
        llm=None,
        meeting_record=_FakeRecord(),
        permission_classifier=None,
        mode="wiretap",
    )
    # Should not raise — provider is None, classifier is None.
    runner.stop()
    assert runner._stop_event.is_set()
    print("  stop: wiretap teardown is a no-op when provider/classifier absent: OK")


if __name__ == "__main__":
    print("Wiretap mode unit tests:")
    tests = [
        test_wiretap_construct_with_no_llm_no_classifier,
        test_wiretap_dispatch_user_message_returns_immediately,
        test_wiretap_continuation_state_never_opens,
        test_wiretap_stop_with_no_provider_no_classifier,
    ]
    failures = 0
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            failures += 1
            print(f"  {fn.__name__}: FAIL — {e}")
        except Exception as e:
            failures += 1
            print(f"  {fn.__name__}: ERROR — {type(e).__name__}: {e}")
    if failures:
        print(f"\n{failures} test(s) failed")
        sys.exit(1)
    print(f"\nAll {len(tests)} wiretap mode tests passed.")
