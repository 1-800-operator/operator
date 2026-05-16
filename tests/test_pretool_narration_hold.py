"""
Tests for the pre-tool narration hold-and-drop behavior in ChatRunner.

Background: when claude is about to call a tool that needs a permreq,
the narration text it emits BEFORE the tool call (e.g. "marking it done
now.") is queued for chat send via the off-thread send queue. The bug
this guards against is that narration shipping AFTER the room said NO —
"marking it done now" appearing right after the user replied "no" —
which contradicts the verdict.

What this exercises:
  - While a permreq is active, `_drain_pending_sends` is a no-op: queued
    items are held, not flushed.
  - On allow resolution, the queue clears via the next drain (held items
    ship in order).
  - On deny resolution, the queue is purged: held items are dropped and
    never reach chat.
  - Same purge fires on the safety-timeout path (hook self-denied).
  - Pre-allowed tools (no permreq) keep the historical immediate-drain
    behavior — held-mode only applies when a permreq is in flight.
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _1_800_operator.pipeline.chat_runner import ChatRunner


class StubConnector:
    def __init__(self):
        self.sent: list[str] = []
        self.chat_messages: list[dict] = []
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


class FakeClassifier:
    def __init__(self, verdict=True):
        self.verdict = verdict
        self.calls = []

    def classify(self, reply, question, chat_context=None):
        self.calls.append((reply, question, chat_context))
        return self.verdict


def make_runner(classifier=None):
    return ChatRunner(
        StubConnector(), StubLLM(),
        meeting_record=None,
        permission_classifier=classifier,
    )


def make_req(*, request_id="req-1", tool_name="Bash", tool_input=None, tmp_dir=None):
    if tmp_dir is None:
        tmp_dir = Path(tempfile.mkdtemp(prefix="permreq_hold_test_"))
    return {
        "request_id": request_id,
        "ts": time.time(),
        "tool_name": tool_name,
        "tool_input": tool_input or {"command": "echo hi"},
        "answer_path": tmp_dir / "permreq_answers" / f"{request_id}.json",
    }


def test_drain_is_noop_while_permreq_active():
    """Items queued while a permreq is active stay in the queue — drain
    refuses to flush them."""
    runner = make_runner(classifier=FakeClassifier(verdict=True))
    runner._on_permission_request(make_req())
    # Question is already in sent (it goes via direct _send, not queue).
    question_count = len(runner._connector.sent)
    # Now simulate claude's PTY pump enqueueing pre-tool narration.
    runner._send_queue.put(("marking it done now.", "chat"))
    runner._send_queue.put(("about to write the file", "chat"))
    runner._drain_pending_sends()
    # Still active — nothing new should have been sent.
    assert len(runner._connector.sent) == question_count, runner._connector.sent
    assert runner._send_queue.qsize() == 2
    print("  drain is a no-op while permreq is active: OK")


def test_allow_flushes_held_narration_on_next_drain():
    """When the permreq resolves with allow, the next drain sends the
    held narration in queue order."""
    runner = make_runner(classifier=FakeClassifier(verdict=True))
    req = make_req()
    runner._on_permission_request(req)
    runner._send_queue.put(("marking it done now.", "chat"))
    # Simulate the chat reply "yes" arriving.
    runner._connector.chat_messages = [
        {"id": "msg-1", "sender": "alice", "text": "yes go ahead"},
    ]
    runner._check_permreq_chat_for_answer()
    # Allow → answer file written, active cleared, queue intact.
    assert json.loads(req["answer_path"].read_text()) == {"behavior": "allow"}
    assert runner._permreq_active is None
    assert runner._send_queue.qsize() == 1
    # Next drain sends the held narration.
    runner._drain_pending_sends()
    assert runner._connector.sent[-1] == "marking it done now."
    print("  allow path: next drain ships the held narration: OK")


def test_deny_purges_held_narration():
    """When the permreq resolves with deny, the held narration is
    dropped — the contradicting line never reaches chat."""
    runner = make_runner(classifier=FakeClassifier(verdict=False))
    req = make_req()
    runner._on_permission_request(req)
    runner._send_queue.put(("marking it done now.", "chat"))
    runner._send_queue.put(("about to write the file", "chat"))
    pre_send_count = len(runner._connector.sent)
    runner._connector.chat_messages = [
        {"id": "msg-1", "sender": "alice", "text": "no, leave it"},
    ]
    runner._check_permreq_chat_for_answer()
    # Deny → answer file holds deny + user's words.
    ans = json.loads(req["answer_path"].read_text())
    assert ans["behavior"] == "deny"
    assert "no, leave it" in ans["message"]
    # Crucially: the held narration is gone, not flushed.
    assert runner._send_queue.qsize() == 0
    assert len(runner._connector.sent) == pre_send_count
    # And a subsequent drain has nothing to send either.
    runner._drain_pending_sends()
    assert len(runner._connector.sent) == pre_send_count
    print("  deny path: held narration is purged, never sent: OK")


def test_post_deny_narration_still_flushes():
    """After a deny, NEW narration enqueued by claude in response to
    the deny tool_result flushes normally on the next drain — the purge
    is one-shot at resolve time, not an ongoing block."""
    runner = make_runner(classifier=FakeClassifier(verdict=False))
    runner._on_permission_request(make_req())
    runner._send_queue.put(("marking it done now.", "chat"))  # gets purged
    runner._connector.chat_messages = [
        {"id": "msg-1", "sender": "alice", "text": "no"},
    ]
    runner._check_permreq_chat_for_answer()
    # Now claude's post-result narration comes through.
    runner._send_queue.put(("got it, leaving as is.", "chat"))
    runner._drain_pending_sends()
    # The post-result line landed; the pre-tool line did not.
    sent_texts = runner._connector.sent
    assert "got it, leaving as is." in sent_texts
    assert "marking it done now." not in sent_texts
    print("  post-deny narration flushes normally: OK")


def test_safety_timeout_also_purges():
    """If the hook self-denied past its 120s ceiling, the safety
    timeout path also purges the held narration — same posture as a
    user-driven deny."""
    runner = make_runner(classifier=FakeClassifier(verdict=False))
    req = make_req()
    runner._on_permission_request(req)
    runner._send_queue.put(("marking it done now.", "chat"))
    # Force the timeout: walk the active-since clock back.
    runner._permreq_active["_active_since_mono"] = (
        time.monotonic() - runner._permreq_safety_timeout_s - 1.0
    )
    pre_send_count = len(runner._connector.sent)
    runner._check_permreq_chat_for_answer()
    assert runner._permreq_active is None
    # Held narration is purged on timeout, exactly like on a normal deny.
    assert runner._send_queue.qsize() == 0
    assert len(runner._connector.sent) == pre_send_count
    print("  safety-timeout path purges held narration: OK")


def test_no_permreq_drain_works_normally():
    """Sanity: when no permreq is active, drain flushes immediately —
    the hold-and-drop logic only applies during a permreq."""
    runner = make_runner()
    runner._send_queue.put(("just narrating", "chat"))
    runner._send_queue.put(("more narration", "chat"))
    runner._drain_pending_sends()
    assert runner._connector.sent == ["just narrating", "more narration"]
    print("  no permreq → drain flushes immediately (status quo): OK")


if __name__ == "__main__":
    print("Pre-tool narration hold-and-drop tests:")
    test_drain_is_noop_while_permreq_active()
    test_allow_flushes_held_narration_on_next_drain()
    test_deny_purges_held_narration()
    test_post_deny_narration_still_flushes()
    test_safety_timeout_also_purges()
    test_no_permreq_drain_works_normally()
    print("\nAll 6 pre-tool narration hold-and-drop tests passed.")
