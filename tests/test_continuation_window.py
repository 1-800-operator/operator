"""
Tests for the sticky conversation window in ChatRunner.

What this exercises:
  - @claude trigger opens the window for the sender
  - Same-sender follow-up without @claude is buffered (not dispatched)
  - Buffer dispatches after the debounce window elapses
  - Rapid-fire follow-ups collapse to the latest message
  - Different sender does NOT count as a continuation
  - Window expires after CONTINUATION_WINDOW_SECONDS

LLM dispatch is mocked: `_handle_message` is monkey-patched to record
its calls instead of actually invoking the provider.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _1_800_operator import config
from _1_800_operator.pipeline import chat_runner as cr_mod
from _1_800_operator.pipeline.chat_runner import ChatRunner


class StubConnector:
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


class StubLLM:
    def __init__(self):
        self._provider = None

    def set_record(self, r):
        pass


def make_runner():
    runner = ChatRunner(StubConnector(), StubLLM(), meeting_record=None)
    runner._dispatched = []

    def fake_handle(text, sender="", *, t_dom=0, t_drained=0):
        runner._dispatched.append({"text": text, "sender": sender})
    runner._handle_message = fake_handle  # type: ignore[method-assign]
    return runner


def test_trigger_dispatches_and_opens_window():
    runner = make_runner()
    runner._dispatch_user_message(f"{config.TRIGGER_PHRASE} hi there", sender="Alice")
    assert runner._dispatched == [{"text": "hi there", "sender": "Alice"}]
    assert runner._continuation_sender == "Alice"
    assert runner._continuation_open_until > time.time()
    print("  trigger dispatches and opens window: OK")


def test_followup_same_sender_buffered_not_dispatched():
    runner = make_runner()
    runner._dispatch_user_message(f"{config.TRIGGER_PHRASE} hi", sender="Alice")
    runner._dispatched.clear()
    runner._dispatch_user_message("thanks", sender="Alice")
    # Not dispatched yet — debounce in flight.
    assert runner._dispatched == []
    assert runner._continuation_pending is not None
    assert runner._continuation_pending["text"] == "thanks"
    print("  same-sender follow-up buffered, not dispatched immediately: OK")


def test_followup_dispatches_after_debounce():
    runner = make_runner()
    runner._dispatch_user_message(f"{config.TRIGGER_PHRASE} hi", sender="Alice")
    runner._dispatched.clear()
    runner._dispatch_user_message("thanks", sender="Alice")
    # Walk pending.ts back so the debounce check passes without sleeping.
    runner._continuation_pending["ts"] = time.time() - cr_mod.CONTINUATION_DEBOUNCE_SECONDS - 0.5
    runner._flush_continuation_if_ready()
    assert runner._dispatched == [{"text": "thanks", "sender": "Alice"}]
    assert runner._continuation_pending is None
    print("  follow-up dispatches after debounce window: OK")


def test_rapid_followups_collapse_to_latest():
    runner = make_runner()
    runner._dispatch_user_message(f"{config.TRIGGER_PHRASE} go", sender="Alice")
    runner._dispatched.clear()
    runner._dispatch_user_message("wait", sender="Alice")
    runner._dispatch_user_message("actually no, do Y instead", sender="Alice")
    # Walk back the pending so debounce trips.
    runner._continuation_pending["ts"] = time.time() - cr_mod.CONTINUATION_DEBOUNCE_SECONDS - 0.5
    runner._flush_continuation_if_ready()
    assert len(runner._dispatched) == 1
    assert runner._dispatched[0]["text"] == "actually no, do Y instead", runner._dispatched
    print("  rapid follow-ups collapse to the latest message: OK")


def test_different_sender_does_not_count_as_continuation():
    runner = make_runner()
    runner._dispatch_user_message(f"{config.TRIGGER_PHRASE} hi", sender="Alice")
    runner._dispatched.clear()
    runner._dispatch_user_message("relevant aside", sender="Bob")
    assert runner._dispatched == []
    assert runner._continuation_pending is None  # Bob is not in Alice's window
    print("  different sender does not enter Alice's window: OK")


def test_window_expires_after_window_seconds():
    runner = make_runner()
    runner._dispatch_user_message(f"{config.TRIGGER_PHRASE} hi", sender="Alice")
    runner._dispatched.clear()
    # Force the window closed.
    runner._continuation_open_until = time.time() - 1.0
    runner._dispatch_user_message("late follow-up", sender="Alice")
    assert runner._dispatched == []
    assert runner._continuation_pending is None
    print("  window expires after CONTINUATION_WINDOW_SECONDS: OK")


def test_anonymous_sender_not_tracked():
    runner = make_runner()
    # Empty sender — adapter couldn't extract a name. Trigger still fires
    # the message but the window can't be opened (no sender key to scope
    # against), so a follow-up without @claude won't be a continuation.
    runner._dispatch_user_message(f"{config.TRIGGER_PHRASE} hi", sender="")
    assert runner._dispatched == [{"text": "hi", "sender": ""}]
    assert runner._continuation_sender is None
    runner._dispatched.clear()
    runner._dispatch_user_message("follow-up", sender="")
    assert runner._dispatched == []
    print("  anonymous sender doesn't open continuation window: OK")


def test_followup_extends_window():
    runner = make_runner()
    runner._dispatch_user_message(f"{config.TRIGGER_PHRASE} go", sender="Alice")
    runner._dispatched.clear()
    first_until = runner._continuation_open_until
    # Run a follow-up through the buffer + flush path; it should reopen
    # the window (push the deadline forward).
    runner._dispatch_user_message("thanks", sender="Alice")
    runner._continuation_pending["ts"] = time.time() - cr_mod.CONTINUATION_DEBOUNCE_SECONDS - 0.5
    runner._flush_continuation_if_ready()
    assert runner._continuation_open_until >= first_until
    print("  forwarded continuation extends the window: OK")


if __name__ == "__main__":
    print("Sticky conversation-window tests:")
    test_trigger_dispatches_and_opens_window()
    test_followup_same_sender_buffered_not_dispatched()
    test_followup_dispatches_after_debounce()
    test_rapid_followups_collapse_to_latest()
    test_different_sender_does_not_count_as_continuation()
    test_window_expires_after_window_seconds()
    test_anonymous_sender_not_tracked()
    test_followup_extends_window()
    print("\nAll 8 continuation-window tests passed.")
