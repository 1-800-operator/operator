"""
Unit tests for PermissionChatHandler.

Exercises the round-trip in isolation with a fake connector and a stub
runner-shaped object. No real LLM, no real claude subprocess — just the
chat-routing logic.

Run:
    source venv/bin/activate
    OPERATOR_BOT=claude python tests/test_permission_chat_handler.py
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _1_800_operator.pipeline.permission_chat_handler import (
    PermissionChatHandler,
    _is_yes,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class FakeConnector:
    """Minimal connector stand-in: scriptable read_chat queue, send_chat capture."""

    def __init__(self):
        self._inbox = []          # messages read_chat will return next
        self.outbox = []          # messages send_chat received
        self._lock = threading.Lock()

    def read_chat(self):
        with self._lock:
            out = list(self._inbox)
            self._inbox.clear()
        return out

    def send_chat(self, text):
        self.outbox.append(text)
        return f"sent-{len(self.outbox)}"

    # Simulate a user typing into the meet chat
    def push_user_message(self, text, sender="Alice", msg_id=None):
        with self._lock:
            self._inbox.append({
                "id": msg_id or f"u{int(time.time()*1000)}-{len(self._inbox)}",
                "text": text,
                "sender": sender,
            })


class FakeRunner:
    """Stand-in for ChatRunner — exposes the slots PermissionChatHandler reads."""

    def __init__(self, connector):
        self._connector = connector
        self._seen_ids: set[str] = set()
        self._own_messages: set[str] = set()
        # Mirrors chat_runner's bookkeeping for the recent-yes auto-approval
        # path. Tests may set _latest_user_msg directly to simulate a turn
        # whose triggering user message was an affirmation.
        self._latest_user_msg: tuple[str, str, float] | None = None
        self._approval_msg_ids_used: set[str] = set()

    def _send(self, text, kind="chat"):
        msg_id = self._connector.send_chat(text)
        # Mirror chat_runner._send: track our own outgoing text so the
        # handler's poll loop doesn't treat it as an inbound user reply.
        self._own_messages.add(text)
        return msg_id


# ---------------------------------------------------------------------------
# Helper-fn tests — pure, no threading
# ---------------------------------------------------------------------------

def test_is_yes_variants():
    yes_inputs = ["yes", "ok", "Sure", "approve", "yep", "yeah", "OK!", "Go ahead", "do it", "y"]
    no_inputs = ["no", "stop", "nope", "use a different path", "what?"]
    for t in yes_inputs:
        assert _is_yes(t), f"_is_yes({t!r}) should be True"
    for t in no_inputs:
        assert not _is_yes(t), f"_is_yes({t!r}) should be False"
    print("  _is_yes variants OK")


def test_is_yes_negation_gate():
    """Affirmative token paired with a negation must NOT approve.

    Mirrors the negation gate in chat_runner._handle_confirmation so the
    track-A permission handler and the track-B confirmation flow agree
    on the same yes/no contract. Without the gate, "ok no don't do that"
    would auto-approve a tool call because "ok" matches.
    """
    negated = [
        "ok no don't do that",
        "yes don't",
        "go ahead no actually wait",
        "sure, but do not run it",
        "approve? nah, cancel that",
        "do it… no actually stop",
    ]
    for t in negated:
        assert not _is_yes(t), f"_is_yes({t!r}) should be False (negation gate)"
    print("  _is_yes negation gate OK")


# ---------------------------------------------------------------------------
# Round-trip tests — handler runs on a worker thread, simulated user replies
# arrive via FakeConnector.push_user_message.
# ---------------------------------------------------------------------------

def _run_handler_on_thread(handler, tool_name, tool_input):
    """Spawn a thread that calls handler(tool_name, tool_input) and stash the result."""
    result_box = {}

    def run():
        result_box["decision"] = handler(tool_name, tool_input)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, result_box


def test_auto_approve_returns_immediately_no_chat():
    """Tools in auto_approve return allow without posting to chat."""
    conn = FakeConnector()
    runner = FakeRunner(conn)
    handler = PermissionChatHandler(
        connector=conn, runner=runner,
        auto_approve=["Read", "Grep"], always_ask=["Write", "Bash"],
    )
    decision = handler("Read", {"file_path": "/tmp/foo"})
    assert decision["permissionDecision"] == "allow"
    assert "auto-approved" in decision["permissionDecisionReason"].lower()
    assert conn.outbox == [], "auto-approve must not post to chat"
    print("  auto_approve returns silent allow OK")


def test_chat_round_trip_yes_returns_allow():
    """A 'yes' reply returns allow.

    14.19.8 — handler no longer posts a templated card; the natural-language
    question is authored by the inner-claude model and lands in chat via
    the provider's pre-tool narration (outside this handler's scope). The
    handler just waits for the user's next chat message.
    """
    conn = FakeConnector()
    runner = FakeRunner(conn)
    handler = PermissionChatHandler(
        connector=conn, runner=runner,
        auto_approve=[], always_ask=["Write"],
    )
    t, box = _run_handler_on_thread(handler, "Write", {"file_path": "/tmp/foo", "content": "hello"})
    # Give the handler a moment to enter its await-reply loop, then push
    # the user's affirmative.
    time.sleep(0.2)
    assert conn.outbox == [], "handler should not post anything; model authors the question"
    conn.push_user_message("yes please")
    t.join(timeout=5)
    assert not t.is_alive(), "handler thread did not return"
    assert box["decision"]["permissionDecision"] == "allow"
    assert "yes please" in box["decision"]["permissionDecisionReason"]
    print("  chat round-trip yes -> allow OK")


def test_chat_round_trip_other_returns_deny_with_text():
    """A non-yes reply returns deny with the user's text as the reason."""
    conn = FakeConnector()
    runner = FakeRunner(conn)
    handler = PermissionChatHandler(
        connector=conn, runner=runner,
        auto_approve=[], always_ask=["Bash"],
    )
    t, box = _run_handler_on_thread(handler, "Bash", {"command": "rm -rf /"})
    time.sleep(0.2)
    conn.push_user_message("absolutely not, use rm -i instead")
    t.join(timeout=5)
    assert not t.is_alive()
    decision = box["decision"]
    assert decision["permissionDecision"] == "deny"
    assert "rm -i" in decision["permissionDecisionReason"]
    print("  chat round-trip non-yes -> deny with reason OK")


def test_handler_skips_own_echoes_in_reply_poll():
    """Bot's own pre-tool question must not be misread as the user's reply.

    The model's pre-tool narration is sent via the provider's streaming
    path (chat_runner._send), which records it in runner._own_messages so
    chat_runner's main loop doesn't re-feed it to the LLM. The handler's
    await loop must apply the same dedup — if Meet round-trips our own
    message back with no `sender` field, the handler must skip it.
    """
    conn = FakeConnector()
    runner = FakeRunner(conn)
    handler = PermissionChatHandler(
        connector=conn, runner=runner,
        auto_approve=[], always_ask=["Write"],
    )
    # Simulate the bot already having posted its question via the streaming
    # path: the text lands in runner._own_messages.
    own_question = "Want me to write that file?"
    runner._own_messages.add(own_question)

    t, box = _run_handler_on_thread(handler, "Write", {"file_path": "/tmp/foo"})
    time.sleep(0.2)
    # Echo the bot's question back with no sender — handler must skip it.
    conn.push_user_message(own_question, sender="")
    time.sleep(0.5)
    assert t.is_alive(), "handler must NOT have decided based on its own echo"
    # Now the real user reply
    conn.push_user_message("ok")
    t.join(timeout=5)
    assert not t.is_alive()
    assert box["decision"]["permissionDecision"] == "allow"
    print("  handler skips own-echoes OK")


def test_recent_yes_auto_approves_without_chat_round_trip():
    """When the user just said 'yes' as the turn input, the next gate auto-allows.

    Reproduces the live-test failure mode: model asks "Want me to pull
    Linear issues?", user replies "yes", that "yes" becomes the next
    turn's input, model invokes the tool — handler must not sit waiting
    for a redundant second "yes".
    """
    conn = FakeConnector()
    runner = FakeRunner(conn)
    # Simulate chat_runner having just observed an affirmative user msg.
    runner._latest_user_msg = ("msg-id-yes", "yes", time.monotonic() - 2.0)
    handler = PermissionChatHandler(
        connector=conn, runner=runner,
        auto_approve=[], always_ask=["mcp__linear__list_issues"],
    )
    decision = handler("mcp__linear__list_issues", {"assignee": "me"})
    assert decision["permissionDecision"] == "allow"
    assert "yes" in decision["permissionDecisionReason"].lower()
    assert "msg-id-yes" in runner._approval_msg_ids_used, (
        "handler must mark the consumed yes so chained gates don't re-use it"
    )
    assert conn.outbox == [], "auto-allow path must not post anything"
    print("  recent-yes auto-approve OK")


def test_recent_yes_consumed_so_chained_gate_falls_through():
    """A second tool call in the same turn does NOT inherit the prior yes.

    Once the first gate consumes the yes, subsequent gates must fall
    through to await_reply — one yes, one tool.
    """
    conn = FakeConnector()
    runner = FakeRunner(conn)
    runner._latest_user_msg = ("msg-id-yes-2", "ok", time.monotonic() - 1.0)
    handler = PermissionChatHandler(
        connector=conn, runner=runner,
        auto_approve=[], always_ask=["Write"],
    )
    # First call: auto-approve.
    decision1 = handler("Write", {"file_path": "/tmp/a"})
    assert decision1["permissionDecision"] == "allow"
    # Second call same turn: must wait for fresh chat reply, not re-use.
    t, box = _run_handler_on_thread(handler, "Write", {"file_path": "/tmp/b"})
    time.sleep(0.2)
    assert t.is_alive(), "handler must wait — the prior yes is already consumed"
    conn.push_user_message("yes again")
    t.join(timeout=5)
    assert box["decision"]["permissionDecision"] == "allow"
    print("  chained gate falls through OK")


def test_recent_yes_outside_window_does_not_auto_approve():
    """A 'yes' from too long ago must not drift into a later gate."""
    conn = FakeConnector()
    runner = FakeRunner(conn)
    runner._latest_user_msg = ("old-yes", "yes", time.monotonic() - 120.0)
    handler = PermissionChatHandler(
        connector=conn, runner=runner,
        auto_approve=[], always_ask=["Write"],
    )
    t, box = _run_handler_on_thread(handler, "Write", {"file_path": "/tmp/foo"})
    time.sleep(0.2)
    assert t.is_alive(), "handler must wait — the recent-yes window has expired"
    conn.push_user_message("ok")
    t.join(timeout=5)
    assert box["decision"]["permissionDecision"] == "allow"
    print("  recency window enforcement OK")


def test_recent_non_yes_does_not_auto_approve():
    """A recent non-affirmative message must not auto-approve."""
    conn = FakeConnector()
    runner = FakeRunner(conn)
    runner._latest_user_msg = ("recent-question", "what does that do?", time.monotonic() - 2.0)
    handler = PermissionChatHandler(
        connector=conn, runner=runner,
        auto_approve=[], always_ask=["Write"],
    )
    t, box = _run_handler_on_thread(handler, "Write", {"file_path": "/tmp/foo"})
    time.sleep(0.2)
    assert t.is_alive(), "non-yes recent message must not auto-approve"
    conn.push_user_message("yes")
    t.join(timeout=5)
    assert box["decision"]["permissionDecision"] == "allow"
    print("  non-yes recent message correctly does not auto-approve OK")


def test_handler_claims_seen_ids_so_main_loop_skips():
    """A consumed user reply's id is added to runner._seen_ids."""
    conn = FakeConnector()
    runner = FakeRunner(conn)
    handler = PermissionChatHandler(
        connector=conn, runner=runner,
        auto_approve=[], always_ask=["Write"],
    )
    t, box = _run_handler_on_thread(handler, "Write", {"file_path": "/tmp/foo"})
    time.sleep(0.2)
    conn.push_user_message("ok", msg_id="user-reply-42")
    t.join(timeout=5)
    assert not t.is_alive()
    assert "user-reply-42" in runner._seen_ids, (
        "handler should have claimed the consumed reply's id so the main loop skips it"
    )
    print("  handler claims seen_ids OK")


def main():
    print("test_is_yes_variants")
    test_is_yes_variants()
    print("test_is_yes_negation_gate")
    test_is_yes_negation_gate()
    print("test_auto_approve_returns_immediately_no_chat")
    test_auto_approve_returns_immediately_no_chat()
    print("test_chat_round_trip_yes_returns_allow")
    test_chat_round_trip_yes_returns_allow()
    print("test_chat_round_trip_other_returns_deny_with_text")
    test_chat_round_trip_other_returns_deny_with_text()
    print("test_handler_skips_own_echoes_in_reply_poll")
    test_handler_skips_own_echoes_in_reply_poll()
    print("test_recent_yes_auto_approves_without_chat_round_trip")
    test_recent_yes_auto_approves_without_chat_round_trip()
    print("test_recent_yes_consumed_so_chained_gate_falls_through")
    test_recent_yes_consumed_so_chained_gate_falls_through()
    print("test_recent_yes_outside_window_does_not_auto_approve")
    test_recent_yes_outside_window_does_not_auto_approve()
    print("test_recent_non_yes_does_not_auto_approve")
    test_recent_non_yes_does_not_auto_approve()
    print("test_handler_claims_seen_ids_so_main_loop_skips")
    test_handler_claims_seen_ids_so_main_loop_skips()
    print("\nAll permission_chat_handler tests passed.")


if __name__ == "__main__":
    main()
