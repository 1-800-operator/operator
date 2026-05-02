"""
Tests for CodexMCPProvider — threadId tracking, dead-thread fallback,
and the basic complete() round-trip.

Drives a fake MCPClient stub (no real codex subprocess) so we can
exercise the provider's thread-state and error-handling logic in isolation.

Usage:
    python tests/test_codex_mcp_provider.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("OPERATOR_BOT", "pm")

from _1_800_operator.pipeline.providers.codex_mcp import (
    CodexMCPProvider,
    _looks_like_dead_thread,
    _last_user_text,
)


class FakeMCPClient:
    """Minimal stub of MCPClient with execute_tool + last_structured_content."""

    def __init__(self):
        self.calls = []  # list of (tool_name, args)
        self.responses = []  # list of (text, structured_dict) OR Exception
        self._next_structured = {}

    def queue_response(self, text, structured):
        self.responses.append((text, structured))

    def queue_error(self, exc):
        self.responses.append(exc)

    def execute_tool(self, tool_name, args):
        self.calls.append((tool_name, dict(args)))
        if not self.responses:
            raise AssertionError("No responses queued for execute_tool call")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            self._next_structured = {}
            raise nxt
        text, structured = nxt
        self._next_structured = structured or {}
        return text

    def last_structured_content(self, server_name):
        return dict(self._next_structured)


def _check(label, cond, detail=""):
    mark = "✓" if cond else "✗"
    print(f"  {mark} {label}{(' — ' + detail) if detail else ''}")
    if not cond:
        raise AssertionError(label)


def test_first_call_uses_codex_codex():
    print("\n1. First call invokes `codex__codex` and stores threadId")
    p = CodexMCPProvider(append_developer_instructions="be brief", cwd="/tmp")
    fake = FakeMCPClient()
    p.set_mcp_client(fake)

    fake.queue_response("hello world", {"threadId": "thread-abc", "content": "hello world"})
    resp = p.complete("be brief", [{"role": "user", "content": "hi"}])

    _check("called codex__codex", fake.calls[0][0] == "codex__codex")
    args = fake.calls[0][1]
    _check("prompt is the last user message", args["prompt"] == "hi")
    _check("approval-policy default", args["approval-policy"] == "on-request")
    _check("sandbox default", args["sandbox"] == "read-only")
    _check("cwd passed through", args["cwd"] == "/tmp")
    _check("developer-instructions on first call",
           args["developer-instructions"] == "be brief")
    _check("response text correct", resp.text == "hello world")
    _check("threadId stored", p._thread_id == "thread-abc")


def test_second_call_uses_codex_reply():
    print("\n2. Second call invokes `codex__codex-reply` with stored threadId")
    p = CodexMCPProvider(cwd="/tmp")
    fake = FakeMCPClient()
    p.set_mcp_client(fake)

    fake.queue_response("first reply", {"threadId": "thread-xyz", "content": "first reply"})
    fake.queue_response("second reply", {"threadId": "thread-xyz", "content": "second reply"})

    p.complete("", [{"role": "user", "content": "msg-1"}])
    resp2 = p.complete("", [
        {"role": "user", "content": "msg-1"},
        {"role": "assistant", "content": "first reply"},
        {"role": "user", "content": "msg-2"},
    ])

    _check("call 1 used codex__codex", fake.calls[0][0] == "codex__codex")
    _check("call 2 used codex__codex-reply",
           fake.calls[1][0] == "codex__codex-reply")
    _check("call 2 sent stored threadId",
           fake.calls[1][1]["threadId"] == "thread-xyz")
    _check("call 2 sent latest user msg as prompt",
           fake.calls[1][1]["prompt"] == "msg-2")
    _check("call 2 did NOT resend developer-instructions",
           "developer-instructions" not in fake.calls[1][1])
    _check("response text correct", resp2.text == "second reply")


def test_dead_thread_falls_back_to_fresh_codex():
    print("\n3. Dead-thread error → clear threadId, retry with `codex__codex`")
    from _1_800_operator.pipeline.mcp_client import MCPToolError

    p = CodexMCPProvider()
    fake = FakeMCPClient()
    p.set_mcp_client(fake)

    fake.queue_response("first", {"threadId": "thread-1", "content": "first"})
    fake.queue_error(MCPToolError("Tool error: thread not found for id thread-1"))
    fake.queue_response("recovered", {"threadId": "thread-2", "content": "recovered"})

    p.complete("", [{"role": "user", "content": "a"}])
    resp = p.complete("", [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "first"},
        {"role": "user", "content": "b"},
    ])

    _check("attempted codex-reply first",
           fake.calls[1][0] == "codex__codex-reply")
    _check("retried with fresh codex", fake.calls[2][0] == "codex__codex")
    _check("retried prompt is the user msg",
           fake.calls[2][1]["prompt"] == "b")
    _check("recovered response surfaces", resp.text == "recovered")
    _check("threadId now points at the new thread",
           p._thread_id == "thread-2")


def test_complete_before_wire_raises():
    print("\n4. complete() before set_mcp_client() raises with a clear message")
    p = CodexMCPProvider()
    try:
        p.complete("", [{"role": "user", "content": "x"}])
    except RuntimeError as e:
        msg = str(e)
        _check("error mentions set_mcp_client", "set_mcp_client" in msg)
        _check("error mentions chat_runner wire-up",
               "chat_runner" in msg or "_wire_codex_elicitation" in msg)
        return
    raise AssertionError("expected RuntimeError")


def test_dead_thread_hint_detection():
    print("\n5. Dead-thread substring detection covers the known shapes")
    _check("'thread not found' detected",
           _looks_like_dead_thread("Tool error: thread not found"))
    _check("'session not connected' detected",
           _looks_like_dead_thread("ERROR: session not connected"))
    _check("'Unknown threadId' detected (case-insensitive)",
           _looks_like_dead_thread("Tool error: Unknown threadId xyz"))
    _check("unrelated error not flagged",
           not _looks_like_dead_thread("rate limit exceeded; retry later"))


def test_last_user_text_picks_latest_user():
    print("\n6. _last_user_text picks the most recent user message")
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "latest"},
    ]
    _check("returns latest user", _last_user_text(msgs) == "latest")
    _check("empty list → empty string", _last_user_text([]) == "")
    _check("no user role → empty string",
           _last_user_text([{"role": "assistant", "content": "x"}]) == "")


def test_developer_instructions_skipped_when_empty():
    print("\n7. No developer-instructions arg when system prompt is empty")
    p = CodexMCPProvider(append_developer_instructions=None)
    fake = FakeMCPClient()
    p.set_mcp_client(fake)
    fake.queue_response("ok", {"threadId": "t", "content": "ok"})
    p.complete("", [{"role": "user", "content": "hi"}])
    _check("no developer-instructions key",
           "developer-instructions" not in fake.calls[0][1])


def test_custom_approval_and_sandbox():
    print("\n8. Custom approval_policy + sandbox flow into the call")
    p = CodexMCPProvider(approval_policy="untrusted", sandbox="workspace-write")
    fake = FakeMCPClient()
    p.set_mcp_client(fake)
    fake.queue_response("ok", {"threadId": "t", "content": "ok"})
    p.complete("", [{"role": "user", "content": "hi"}])
    _check("approval-policy passed through",
           fake.calls[0][1]["approval-policy"] == "untrusted")
    _check("sandbox passed through",
           fake.calls[0][1]["sandbox"] == "workspace-write")


def main():
    print("=" * 50)
    print("CodexMCPProvider")
    print("=" * 50)
    failed = 0
    for fn in [
        test_first_call_uses_codex_codex,
        test_second_call_uses_codex_reply,
        test_dead_thread_falls_back_to_fresh_codex,
        test_complete_before_wire_raises,
        test_dead_thread_hint_detection,
        test_last_user_text_picks_latest_user,
        test_developer_instructions_skipped_when_empty,
        test_custom_approval_and_sandbox,
    ]:
        try:
            fn()
        except AssertionError as e:
            print(f"  FAILED: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {e}")
            failed += 1
    print("\n" + "=" * 50)
    if failed == 0:
        print("All tests passed!")
        return 0
    print(f"{failed} test(s) failed")
    return 1


if __name__ == "__main__":
    sys.exit(main())
