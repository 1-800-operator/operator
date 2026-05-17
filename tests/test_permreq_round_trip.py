"""
Tests for the PermissionRequest round-trip in ChatRunner. Mocked: no
real claude subprocess, no real Playwright connector, no real
classifier subprocess (a FakeClassifier with a configurable verdict
stands in).

What this exercises:
  - ChatRunner._on_permission_request → posts the question, sets
    active, snapshots seen-ids
  - _check_permreq_chat_for_answer → takes the first non-self
    post-question chat reply and hands it to the classifier
  - allow path: classifier returns True → answer file holds
    {"behavior": "allow"}
  - deny path: classifier returns False → answer file holds
    {"behavior": "deny", "message": "<directive guidance + verbatim reply>"}
  - classifier crash / no classifier configured → fail-safe deny
  - serial queueing when multiple PermissionRequests arrive
  - safety timeout cleanup if the hook self-denied without us being
    notified
  - send-failure eager-deny so the hook isn't left polling
  - long-input truncation in the chat post
  - atomic write contract (no leftover .tmp file)

The classifier itself is verified separately in
tests/test_permission_classifier.py.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

# Make the repo importable for `python tests/test_permreq_round_trip.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _1_800_operator import config
from _1_800_operator.pipeline.chat_runner import ChatRunner


# ---- fakes -----------------------------------------------------------

class FakeConnector:
    def __init__(self):
        self.sent = []
        self.chat_messages = []
        self.join_status = None
        # Bot's local-tile display name. ChatRunner reads this lazily
        # via get_self_name() to filter out its own echo posts (S-audit
        # fix — replaces the previous AGENT_NAME hardcoded compare so
        # an attendee can't spoof the bot's identity by setting their
        # Meet display name to "Claude").
        self._self_name = config.AGENT_NAME

    def send_chat(self, text):
        self.sent.append(text)
        return f"id-out-{len(self.sent)}"

    def read_chat(self):
        return list(self.chat_messages)

    def is_connected(self):
        return True

    def get_participant_count(self):
        return 2

    def get_self_name(self):
        return self._self_name


class BrokenSendConnector(FakeConnector):
    def send_chat(self, text):
        raise RuntimeError("simulated send failure")


class FakeLLM:
    def __init__(self):
        self._provider = None

    def set_record(self, r):
        pass


class FakeClassifier:
    """Stands in for PermissionClassifier. `verdict` is the bool
    classify() returns; `raises` (if set) makes classify() raise
    instead — exercising the runtime-failure path."""

    def __init__(self, verdict=True, raises=None):
        self.verdict = verdict
        self.raises = raises
        self.calls = []

    def classify(self, reply, question, chat_context=None):
        self.calls.append((reply, question, chat_context))
        if self.raises is not None:
            raise self.raises
        return self.verdict


def make_runner(connector=None, classifier=None):
    if connector is None:
        connector = FakeConnector()
    return ChatRunner(
        connector, FakeLLM(),
        meeting_record=None,
        permission_classifier=classifier,
    )


def make_req(*, tool_name="Bash", tool_input=None, request_id="req-1",
             tmp_dir=None):
    if tmp_dir is None:
        tmp_dir = Path(tempfile.mkdtemp(prefix="permreq_test_"))
    return {
        "request_id": request_id,
        "ts": time.time(),
        "tool_name": tool_name,
        "tool_input": tool_input or {"command": "echo hi"},
        "answer_path": tmp_dir / "permreq_answers" / f"{request_id}.json",
    }, tmp_dir


# ---- tests -----------------------------------------------------------

def test_request_posts_question_and_sets_active():
    runner = make_runner(classifier=FakeClassifier(verdict=True))
    req, _ = make_req(tool_name="Bash", tool_input={"command": "npm install"})
    runner._on_permission_request(req)
    assert len(runner._connector.sent) == 1, runner._connector.sent
    msg = runner._connector.sent[0]
    assert "Bash" in msg
    assert "npm install" in msg
    # Open-ended ask: "— OK?" suffix, no prescriptive "yes/no" token list
    # (the classifier accepts any phrasing — "ok", "sure", "👍", "nah", etc.).
    assert "OK?" in msg, msg
    # No 'always' wording — that path is gone.
    assert "always" not in msg.lower(), msg
    assert runner._permreq_active is req
    print("  request: posts question (open-ended ask, no 'always' wording), sets active: OK")


def test_classifier_yes_writes_allow_answer_atomically():
    classifier = FakeClassifier(verdict=True)
    runner = make_runner(classifier=classifier)
    req, _ = make_req()
    runner._on_permission_request(req)
    runner._connector.chat_messages = [
        # Free-form approval — classifier interprets it.
        {"id": "msg-1", "sender": "alice", "text": "yeah sounds good"}
    ]
    runner._check_permreq_chat_for_answer()
    assert classifier.calls == [
        ("yeah sounds good", req["_question_text"], []),
    ], classifier.calls
    assert req["answer_path"].exists()
    ans = json.loads(req["answer_path"].read_text())
    assert ans == {"behavior": "allow"}, ans
    assert runner._permreq_active is None
    # Atomic write contract: no leftover .tmp file.
    leftover = [p for p in req["answer_path"].parent.iterdir()
                if p.suffix == ".tmp"]
    assert leftover == [], leftover
    print("  classifier YES: writes allow atomically, clears active: OK")


def test_classifier_no_writes_deny_with_verbatim_message():
    classifier = FakeClassifier(verdict=False)
    runner = make_runner(classifier=classifier)
    req, _ = make_req()
    runner._on_permission_request(req)
    runner._connector.chat_messages = [
        {"id": "msg-1", "sender": "alice", "text": "nah, skip it"}
    ]
    runner._check_permreq_chat_for_answer()
    assert req["answer_path"].exists()
    ans = json.loads(req["answer_path"].read_text())
    assert ans["behavior"] == "deny"
    # Deny message carries the verbatim user reply so claude can narrate
    # the refusal with the right context.
    assert "nah, skip it" in ans["message"], ans
    assert runner._permreq_active is None
    print("  classifier NO: writes deny with verbatim user reply in message: OK")


def test_classifier_crash_falls_back_to_deny():
    classifier = FakeClassifier(raises=RuntimeError("classifier exploded"))
    runner = make_runner(classifier=classifier)
    req, _ = make_req()
    runner._on_permission_request(req)
    runner._connector.chat_messages = [
        {"id": "msg-1", "sender": "alice", "text": "yes please"}
    ]
    runner._check_permreq_chat_for_answer()
    assert req["answer_path"].exists()
    ans = json.loads(req["answer_path"].read_text())
    # Classifier exception → deny is the safe default.
    assert ans["behavior"] == "deny"
    print("  classifier crash: deny fallback fires: OK")


def test_no_classifier_configured_denies_with_log():
    runner = make_runner(classifier=None)  # explicitly none
    req, _ = make_req()
    runner._on_permission_request(req)
    runner._connector.chat_messages = [
        {"id": "msg-1", "sender": "alice", "text": "yes"}
    ]
    runner._check_permreq_chat_for_answer()
    assert req["answer_path"].exists()
    ans = json.loads(req["answer_path"].read_text())
    assert ans["behavior"] == "deny"
    print("  no classifier: deny fallback fires: OK")


def test_chatter_classified_first_then_resolved():
    """The new design takes the FIRST non-self post-question reply
    (any participant) and hands it to the classifier. Off-topic chatter
    gets classified — overwhelmingly as deny — rather than being
    filtered out. This test verifies that flow."""
    classifier = FakeClassifier(verdict=False)  # chatter → no
    runner = make_runner(classifier=classifier)
    req, _ = make_req()
    runner._on_permission_request(req)
    runner._connector.chat_messages = [
        {"id": "msg-1", "sender": "alice", "text": "what would that even do?"}
    ]
    runner._check_permreq_chat_for_answer()
    assert classifier.calls == [
        ("what would that even do?", req["_question_text"], []),
    ]
    assert req["answer_path"].exists()
    ans = json.loads(req["answer_path"].read_text())
    assert ans["behavior"] == "deny"
    print("  chatter: handed to classifier, classifier denies: OK")


def test_safety_timeout_clears_active_without_writing_answer():
    runner = make_runner(classifier=FakeClassifier(verdict=True))
    runner._permreq_safety_timeout_s = 0.05
    req, _ = make_req()
    runner._on_permission_request(req)
    time.sleep(0.1)
    runner._check_permreq_chat_for_answer()
    assert runner._permreq_active is None
    # We deliberately don't write a stale answer — the hook self-denied
    # at its own ceiling, the file (if any) was its concern.
    assert not req["answer_path"].exists()
    print("  safety timeout: clears active without phantom write: OK")


def test_queueing_serializes_concurrent_requests():
    classifier = FakeClassifier(verdict=True)
    runner = make_runner(classifier=classifier)
    req1, _ = make_req(request_id="r1")
    req2, _ = make_req(request_id="r2")
    runner._on_permission_request(req1)
    runner._on_permission_request(req2)
    # First active, second queued, only one chat post.
    assert runner._permreq_active is req1
    assert len(runner._permreq_queue) == 1
    assert runner._permreq_queue[0] is req2
    assert len(runner._connector.sent) == 1
    # Resolve first → second becomes active automatically.
    runner._connector.chat_messages = [
        {"id": "msg-1", "sender": "alice", "text": "yes"}
    ]
    runner._check_permreq_chat_for_answer()
    assert req1["answer_path"].exists()
    assert runner._permreq_active is req2
    assert len(runner._connector.sent) == 2
    print("  queueing: serializes one question at a time: OK")


def test_skips_self_messages_via_sender_match():
    classifier = FakeClassifier(verdict=True)
    runner = make_runner(classifier=classifier)
    req, _ = make_req()
    runner._on_permission_request(req)
    # Bot's own outgoing posts come back through read_chat tagged with
    # the bot's sender name — must be filtered out so the classifier
    # never sees them.
    runner._connector.chat_messages = [
        {"id": "echo-1", "sender": config.AGENT_NAME, "text": "yes"}
    ]
    runner._check_permreq_chat_for_answer()
    assert classifier.calls == [], classifier.calls
    assert runner._permreq_active is req, "self-echo should not resolve permreq"
    assert not req["answer_path"].exists()
    print("  self-message filter: own echoes don't reach classifier: OK")


def test_pre_existing_chat_does_not_count_as_answer():
    classifier = FakeClassifier(verdict=True)
    runner = make_runner(classifier=classifier)
    # Simulate prior chat history visible at post-time.
    runner._seen_ids.add("pre-existing-1")
    req, _ = make_req()
    runner._on_permission_request(req)
    # Same id surfaces in read_chat — it was visible before the question,
    # so it cannot be the answer to it.
    runner._connector.chat_messages = [
        {"id": "pre-existing-1", "sender": "alice", "text": "yes"}
    ]
    runner._check_permreq_chat_for_answer()
    assert classifier.calls == []
    assert runner._permreq_active is req
    assert not req["answer_path"].exists()
    print("  pre-existing chat doesn't count as answer: OK")


def test_send_failure_resolves_with_deny():
    runner = make_runner(BrokenSendConnector(), classifier=FakeClassifier(verdict=True))
    req, _ = make_req()
    runner._on_permission_request(req)
    # Send raised → eager deny so the hook isn't left polling forever.
    assert runner._permreq_active is None
    assert req["answer_path"].exists()
    ans = json.loads(req["answer_path"].read_text())
    assert ans["behavior"] == "deny"
    assert "could not post" in ans["message"]
    print("  send failure: eager deny so hook moves on: OK")


def test_question_truncation_for_long_inputs():
    runner = make_runner(classifier=FakeClassifier(verdict=True))
    huge = "x" * 2000
    req, _ = make_req(tool_name="Bash", tool_input={"command": huge})
    runner._on_permission_request(req)
    msg = runner._connector.sent[0]
    assert "..." in msg
    assert len(msg) < 600
    print("  long input truncation: keeps chat post legible: OK")


# ---- main ------------------------------------------------------------

if __name__ == "__main__":
    print("PermissionRequest round-trip tests:")
    tests = [
        test_request_posts_question_and_sets_active,
        test_classifier_yes_writes_allow_answer_atomically,
        test_classifier_no_writes_deny_with_verbatim_message,
        test_classifier_crash_falls_back_to_deny,
        test_no_classifier_configured_denies_with_log,
        test_chatter_classified_first_then_resolved,
        test_safety_timeout_clears_active_without_writing_answer,
        test_queueing_serializes_concurrent_requests,
        test_skips_self_messages_via_sender_match,
        test_pre_existing_chat_does_not_count_as_answer,
        test_send_failure_resolves_with_deny,
        test_question_truncation_for_long_inputs,
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
    print(f"\nAll {len(tests)} permreq round-trip tests passed.")
