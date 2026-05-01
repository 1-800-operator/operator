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
    _format_confirmation,
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


def test_format_confirmation_truncates_long_args():
    """Technical-voice path renders every parameter with head…tail truncation.

    `_format_confirmation` dispatches on `config.VOICE`: plain returns a
    one-line summary (no field-name literals), technical returns the verbose
    parameter dump used here.
    """
    from _1_800_operator import config
    saved_voice = config.VOICE
    config.VOICE = "technical"
    try:
        out = _format_confirmation("Write", {
            "file_path": "/tmp/foo.txt",
            "content": "x" * 5000,
        })
    finally:
        config.VOICE = saved_voice
    assert "Write" in out
    assert "file_path" in out
    assert "content" in out
    assert "…" in out, "long content should be truncated with an ellipsis"
    assert len(out) < 1000, f"confirmation prompt too long ({len(out)} chars)"
    print("  format_confirmation truncates long args OK")


def test_format_confirmation_no_args():
    """Technical-voice path emits "(no arguments)" for empty arg dicts."""
    from _1_800_operator import config
    saved_voice = config.VOICE
    config.VOICE = "technical"
    try:
        out = _format_confirmation("Read", {})
    finally:
        config.VOICE = saved_voice
    assert "(no arguments)" in out
    print("  format_confirmation handles no-arg tools OK")


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
    """A confirmation with a 'yes' reply returns allow."""
    conn = FakeConnector()
    runner = FakeRunner(conn)
    handler = PermissionChatHandler(
        connector=conn, runner=runner,
        auto_approve=[], always_ask=["Write"],
    )
    t, box = _run_handler_on_thread(handler, "Write", {"file_path": "/tmp/foo", "content": "hello"})
    # Wait for the handler to post the prompt
    deadline = time.monotonic() + 3
    while not conn.outbox and time.monotonic() < deadline:
        time.sleep(0.05)
    assert conn.outbox, "handler should have posted a confirmation prompt"
    assert "Write" in conn.outbox[0]
    # User replies yes
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
    deadline = time.monotonic() + 3
    while not conn.outbox and time.monotonic() < deadline:
        time.sleep(0.05)
    conn.push_user_message("absolutely not, use rm -i instead")
    t.join(timeout=5)
    assert not t.is_alive()
    decision = box["decision"]
    assert decision["permissionDecision"] == "deny"
    assert "rm -i" in decision["permissionDecisionReason"]
    print("  chat round-trip non-yes -> deny with reason OK")


def test_handler_skips_own_echoes_in_reply_poll():
    """Our own confirmation prompt must not be misread as the user's reply."""
    conn = FakeConnector()
    runner = FakeRunner(conn)
    handler = PermissionChatHandler(
        connector=conn, runner=runner,
        auto_approve=[], always_ask=["Write"],
    )
    t, box = _run_handler_on_thread(handler, "Write", {"file_path": "/tmp/foo"})
    # The runner._send adds the prompt to own_messages. Push that same text
    # back through read_chat (Meet often round-trips our own messages with
    # no `sender` field) — handler must skip it and keep waiting.
    deadline = time.monotonic() + 3
    while not conn.outbox and time.monotonic() < deadline:
        time.sleep(0.05)
    own_text = conn.outbox[0]
    # Echo with no sender (the case chat_runner's text-fallback dedup catches)
    conn.push_user_message(own_text, sender="")
    time.sleep(0.5)
    assert t.is_alive(), "handler must NOT have decided based on its own echo"
    # Now the real user reply
    conn.push_user_message("ok")
    t.join(timeout=5)
    assert not t.is_alive()
    assert box["decision"]["permissionDecision"] == "allow"
    print("  handler skips own-echoes OK")


def test_handler_claims_seen_ids_so_main_loop_skips():
    """A consumed user reply's id is added to runner._seen_ids."""
    conn = FakeConnector()
    runner = FakeRunner(conn)
    handler = PermissionChatHandler(
        connector=conn, runner=runner,
        auto_approve=[], always_ask=["Write"],
    )
    t, box = _run_handler_on_thread(handler, "Write", {"file_path": "/tmp/foo"})
    deadline = time.monotonic() + 3
    while not conn.outbox and time.monotonic() < deadline:
        time.sleep(0.05)
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
    print("test_format_confirmation_truncates_long_args")
    test_format_confirmation_truncates_long_args()
    print("test_format_confirmation_no_args")
    test_format_confirmation_no_args()
    print("test_auto_approve_returns_immediately_no_chat")
    test_auto_approve_returns_immediately_no_chat()
    print("test_chat_round_trip_yes_returns_allow")
    test_chat_round_trip_yes_returns_allow()
    print("test_chat_round_trip_other_returns_deny_with_text")
    test_chat_round_trip_other_returns_deny_with_text()
    print("test_handler_skips_own_echoes_in_reply_poll")
    test_handler_skips_own_echoes_in_reply_poll()
    print("test_handler_claims_seen_ids_so_main_loop_skips")
    test_handler_claims_seen_ids_so_main_loop_skips()
    print("\nAll permission_chat_handler tests passed.")


if __name__ == "__main__":
    main()
