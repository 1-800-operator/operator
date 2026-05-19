"""
Tests for the sticky conversation window in ChatRunner (dial mode).

Stage 3 redesign (S238):
  - @claude opens the window; any participant can follow up while it's
    open (not sender-scoped — anyone can reply or follow up).
  - The window stays open for CONTINUATION_WINDOW_SECONDS, OR indefinitely
    while claude's last chat post contained a `?`.
  - Any incoming non-self message clears the `?`-driven indefinite flag.
  - Strict mode has NO window — every prompt requires @claude.
  - Yolo mode has NO trigger gating — every message dispatches.

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


def make_runner(mode="dial"):
    runner = ChatRunner(StubConnector(), StubLLM(), meeting_record=None, mode=mode)
    runner._dispatched = []

    def fake_handle(text, sender="", *, t_dom=0, t_drained=0):
        runner._dispatched.append({"text": text, "sender": sender})
    runner._handle_message = fake_handle  # type: ignore[method-assign]
    return runner


# ---- dial mode -------------------------------------------------------------

def test_dial_trigger_dispatches_and_opens_window():
    runner = make_runner()
    runner._dispatch_user_message(f"{config.TRIGGER_PHRASE} hi there", sender="Alice")
    assert runner._dispatched == [{"text": "hi there", "sender": "Alice"}]
    assert runner._continuation_open_until > time.time()
    print("  dial: trigger dispatches + opens window: OK")


def test_dial_followup_same_sender_buffered():
    runner = make_runner()
    runner._dispatch_user_message(f"{config.TRIGGER_PHRASE} hi", sender="Alice")
    runner._dispatched.clear()
    runner._dispatch_user_message("thanks", sender="Alice")
    assert runner._dispatched == []
    assert runner._continuation_pending is not None
    assert runner._continuation_pending["text"] == "thanks"
    print("  dial: same-sender follow-up buffered, not dispatched immediately: OK")


def test_dial_followup_dispatches_after_debounce():
    runner = make_runner()
    runner._dispatch_user_message(f"{config.TRIGGER_PHRASE} hi", sender="Alice")
    runner._dispatched.clear()
    runner._dispatch_user_message("thanks", sender="Alice")
    runner._continuation_pending["ts"] = time.time() - cr_mod.CONTINUATION_DEBOUNCE_SECONDS - 0.5
    runner._flush_continuation_if_ready()
    assert runner._dispatched == [{"text": "thanks", "sender": "Alice"}]
    assert runner._continuation_pending is None
    print("  dial: follow-up dispatches after debounce: OK")


def test_dial_rapid_followups_collapse_to_latest():
    runner = make_runner()
    runner._dispatch_user_message(f"{config.TRIGGER_PHRASE} go", sender="Alice")
    runner._dispatched.clear()
    runner._dispatch_user_message("wait", sender="Alice")
    runner._dispatch_user_message("actually no, do Y instead", sender="Alice")
    runner._continuation_pending["ts"] = time.time() - cr_mod.CONTINUATION_DEBOUNCE_SECONDS - 0.5
    runner._flush_continuation_if_ready()
    assert len(runner._dispatched) == 1
    assert runner._dispatched[0]["text"] == "actually no, do Y instead", runner._dispatched
    print("  dial: rapid follow-ups collapse to the latest: OK")


def test_dial_window_is_not_sender_scoped():
    """S238: window is no longer sender-scoped. Bob CAN follow up in
    Alice's window without @claude."""
    runner = make_runner()
    runner._dispatch_user_message(f"{config.TRIGGER_PHRASE} hi", sender="Alice")
    runner._dispatched.clear()
    runner._dispatch_user_message("relevant aside from Bob", sender="Bob")
    # Buffered as continuation (not dispatched immediately — debounce).
    assert runner._continuation_pending is not None
    assert runner._continuation_pending["sender"] == "Bob"
    runner._continuation_pending["ts"] = time.time() - cr_mod.CONTINUATION_DEBOUNCE_SECONDS - 0.5
    runner._flush_continuation_if_ready()
    assert runner._dispatched == [{"text": "relevant aside from Bob", "sender": "Bob"}]
    print("  dial: window not sender-scoped — anyone can follow up: OK")


def test_dial_window_expires_after_window_seconds():
    runner = make_runner()
    runner._dispatch_user_message(f"{config.TRIGGER_PHRASE} hi", sender="Alice")
    runner._dispatched.clear()
    runner._continuation_open_until = time.time() - 1.0
    runner._dispatch_user_message("late follow-up", sender="Alice")
    assert runner._dispatched == []
    assert runner._continuation_pending is None
    print("  dial: time-based window expires after CONTINUATION_WINDOW_SECONDS: OK")


def test_dial_question_keeps_window_open_indefinitely():
    """If claude's last chat post had `?`, the window stays open past
    the time-based ceiling."""
    runner = make_runner()
    runner._dispatch_user_message(f"{config.TRIGGER_PHRASE} hi", sender="Alice")
    runner._dispatched.clear()
    # Simulate claude having posted a ?-question that overwrites the flag.
    runner._last_reply_had_question = True
    # Force the time-based window closed.
    runner._continuation_open_until = time.time() - 100.0
    # Follow-up arrives — window is open via the ? flag.
    runner._dispatch_user_message("blue", sender="Alice")
    assert runner._continuation_pending is not None, "? flag should keep window open"
    print("  dial: `?` in claude's reply keeps window open past time ceiling: OK")


def test_dial_incoming_reply_clears_question_flag():
    """First non-self incoming message clears the `?`-driven indefinite
    window. The flag is cleared in _process_messages — exercise that path."""
    runner = make_runner()
    runner._last_reply_had_question = True
    # _process_messages is what clears the flag; simulate one message
    # arriving through that path.
    runner._process_messages([{"id": "m1", "sender": "Alice", "text": "hi"}])
    assert runner._last_reply_had_question is False
    print("  dial: incoming non-self message clears the ?-driven flag: OK")


# ---- strict mode -----------------------------------------------------------

def test_strict_trigger_dispatches():
    runner = make_runner(mode="strict")
    runner._dispatch_user_message(f"{config.TRIGGER_PHRASE} hi there", sender="Alice")
    assert runner._dispatched == [{"text": "hi there", "sender": "Alice"}]
    print("  strict: @claude prompt dispatches: OK")


def test_strict_followup_without_trigger_dropped():
    runner = make_runner(mode="strict")
    runner._dispatch_user_message(f"{config.TRIGGER_PHRASE} hi", sender="Alice")
    runner._dispatched.clear()
    runner._dispatch_user_message("thanks", sender="Alice")
    assert runner._dispatched == []
    assert runner._continuation_pending is None
    print("  strict: same-sender follow-up without @claude is dropped: OK")


def test_strict_no_continuation_state_mutated():
    runner = make_runner(mode="strict")
    runner._dispatch_user_message(f"{config.TRIGGER_PHRASE} hi", sender="Alice")
    # Strict mode doesn't open the continuation window at all.
    assert runner._continuation_open_until == 0.0, runner._continuation_open_until
    print("  strict: trigger does NOT open continuation window: OK")


# ---- yolo mode -------------------------------------------------------------

def test_yolo_every_message_dispatches():
    runner = make_runner(mode="yolo")
    runner._dispatch_user_message("no trigger here", sender="Alice")
    runner._dispatch_user_message("nor here", sender="Bob")
    runner._dispatch_user_message(f"{config.TRIGGER_PHRASE} this one has it", sender="Carol")
    assert len(runner._dispatched) == 3
    assert [d["sender"] for d in runner._dispatched] == ["Alice", "Bob", "Carol"]
    # Yolo doesn't strip the trigger — it passes the raw text.
    assert config.TRIGGER_PHRASE in runner._dispatched[2]["text"]
    print("  yolo: every message dispatches, no trigger stripping: OK")


def test_yolo_no_window_state_mutated():
    runner = make_runner(mode="yolo")
    runner._dispatch_user_message("anything", sender="Alice")
    assert runner._continuation_open_until == 0.0
    assert runner._continuation_pending is None
    print("  yolo: no continuation window state mutated: OK")


# ---- mode validation -------------------------------------------------------

def test_invalid_mode_raises():
    try:
        ChatRunner(StubConnector(), StubLLM(), meeting_record=None, mode="nonsense")
    except ValueError as e:
        assert "nonsense" in str(e)
        print("  invalid mode raises ValueError: OK")
        return
    raise AssertionError("invalid mode should have raised")


if __name__ == "__main__":
    print("Sticky conversation-window + mode tests:")
    tests = [
        test_dial_trigger_dispatches_and_opens_window,
        test_dial_followup_same_sender_buffered,
        test_dial_followup_dispatches_after_debounce,
        test_dial_rapid_followups_collapse_to_latest,
        test_dial_window_is_not_sender_scoped,
        test_dial_window_expires_after_window_seconds,
        test_dial_question_keeps_window_open_indefinitely,
        test_dial_incoming_reply_clears_question_flag,
        test_strict_trigger_dispatches,
        test_strict_followup_without_trigger_dropped,
        test_strict_no_continuation_state_mutated,
        test_yolo_every_message_dispatches,
        test_yolo_no_window_state_mutated,
        test_invalid_mode_raises,
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
    print(f"\nAll {len(tests)} continuation-window + mode tests passed.")
