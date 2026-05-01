"""
Unit tests for step 9.11 — chat message size management.

Tests the three mechanical behaviors that don't require a live meeting:
  1. Tool result size guard (oversized result → archive placeholder)
  2. context_length_exceeded error → {"type": "context_overflow"} + history cleared
  3. ChatRunner routes context_overflow to the right user message

Run:
    source venv/bin/activate
    python tests/test_911_size_management.py
"""
import sys
import os
os.environ.setdefault("OPERATOR_BOT", "pm")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import json
from unittest.mock import MagicMock, patch
import openai

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_llm():
    from _1_800_operator.pipeline.llm import LLMClient
    provider = MagicMock()
    return LLMClient(provider)


def make_text_message(text):
    """Return a ProviderResponse yielding a plain text reply."""
    from _1_800_operator.pipeline.providers import ProviderResponse
    return ProviderResponse(text=text, tool_calls=[], stop_reason="end")


def make_bad_request_error(code):
    """Return an openai.BadRequestError with a given code (used for non-overflow path)."""
    err = openai.BadRequestError.__new__(openai.BadRequestError)
    err.code = code
    err.message = f"error code: {code}"
    return err


# ---------------------------------------------------------------------------
# Test 1: tool result size guard
# ---------------------------------------------------------------------------

def test_tool_result_size_guard():
    from _1_800_operator import config
    from _1_800_operator.pipeline.providers import ToolCall
    llm = make_llm()

    # Seed an in-flight tool call in the scratchpad.
    llm._scratch = [{
        "role": "assistant",
        "content": None,
        "tool_calls": [ToolCall(id="call_abc", name="dummy_tool", args={})],
    }]

    oversized = "x" * (config.TOOL_RESULT_MAX_CHARS + 1)
    llm._provider.complete.return_value = make_text_message("summary")

    llm.send_tool_result("call_abc", "dummy_tool", oversized)

    call_args = llm._provider.complete.call_args
    messages = call_args.kwargs["messages"]
    tool_msg = next(m for m in messages if m.get("role") == "tool_result")
    assert "archived" in tool_msg["content"], f"Expected archive placeholder, got: {tool_msg['content'][:100]}"
    assert oversized not in tool_msg["content"], "Raw oversized content leaked into API call"
    print("PASS  test_tool_result_size_guard")


# ---------------------------------------------------------------------------
# Test 2a: context_length_exceeded in ask() → context_overflow + history cleared
# ---------------------------------------------------------------------------

def test_ask_context_overflow():
    llm = make_llm()
    llm._record.append("Alice", "old message")
    llm._record.append("Operator", "old reply")
    before = llm._max_messages

    from _1_800_operator.pipeline.providers import ContextOverflowError
    llm._provider.complete.side_effect = ContextOverflowError()

    result = llm.ask("new message", tools=[{"type": "function", "function": {"name": "t", "parameters": {}}}])

    assert result == {"type": "context_overflow"}, f"Expected context_overflow, got: {result}"
    assert llm._max_messages < before, f"Expected replay window to shrink, stayed at {llm._max_messages}"
    print("PASS  test_ask_context_overflow")


# ---------------------------------------------------------------------------
# Test 2b: context_length_exceeded in send_tool_result() → context_overflow + history cleared
# ---------------------------------------------------------------------------

def test_send_tool_result_context_overflow():
    from _1_800_operator.pipeline.providers import ContextOverflowError, ToolCall
    llm = make_llm()
    llm._scratch = [{
        "role": "assistant", "content": None,
        "tool_calls": [ToolCall(id="call_xyz", name="some_tool", args={})],
    }]
    before = llm._max_messages
    llm._provider.complete.side_effect = ContextOverflowError()

    result = llm.send_tool_result("call_xyz", "some_tool", "result content")

    assert result == {"type": "context_overflow"}, f"Expected context_overflow, got: {result}"
    assert llm._scratch == [], f"Expected scratch cleared, got: {llm._scratch}"
    assert llm._max_messages < before, f"Expected replay window to shrink, stayed at {llm._max_messages}"
    print("PASS  test_send_tool_result_context_overflow")


# ---------------------------------------------------------------------------
# Test 2c: non-context BadRequestError still raises
# ---------------------------------------------------------------------------

def test_other_bad_request_still_raises():
    llm = make_llm()
    err = make_bad_request_error("invalid_api_key")
    llm._provider.complete.side_effect = err

    try:
        llm.ask("something", tools=[{"type": "function", "function": {"name": "t", "parameters": {}}}])
        assert False, "Should have raised"
    except openai.BadRequestError:
        pass
    print("PASS  test_other_bad_request_still_raises")


# ---------------------------------------------------------------------------
# Test 3: ChatRunner routes context_overflow to user-facing message
# ---------------------------------------------------------------------------

def test_chatrunner_routes_context_overflow():
    from _1_800_operator.pipeline.chat_runner import ChatRunner

    connector = MagicMock()
    llm = MagicMock()
    mcp = MagicMock()
    mcp.get_openai_tools.return_value = []

    runner = ChatRunner(connector, llm, mcp)
    sent = []
    runner._send = lambda text: sent.append(text)

    # Simulate ask() returning context_overflow
    llm.ask.return_value = {"type": "context_overflow"}
    runner._handle_message("do something")

    assert len(sent) == 1, f"Expected 1 message sent, got {len(sent)}"
    assert "too long" in sent[0].lower() or "cleared" in sent[0].lower(), \
        f"Unexpected overflow message: {sent[0]}"
    print("PASS  test_chatrunner_routes_context_overflow")


def test_chatrunner_routes_context_overflow_from_tool_result():
    from _1_800_operator.pipeline.chat_runner import ChatRunner

    connector = MagicMock()
    llm = MagicMock()
    mcp = MagicMock()
    mcp.get_openai_tools.return_value = []
    mcp.execute_tool.return_value = "some result"
    mcp.tool_timeout_for.return_value = None

    runner = ChatRunner(connector, llm, mcp)
    sent = []
    runner._send = lambda text: sent.append(text)

    # Simulate a confirmed tool call whose result triggers overflow
    runner._pending_tool_call = {"id": "c1", "name": "some__tool", "arguments": {}}
    llm.send_tool_result.return_value = {"type": "context_overflow"}

    runner._handle_confirmation("yes")

    overflow_msgs = [m for m in sent if "too long" in m.lower() or "cleared" in m.lower()]
    assert len(overflow_msgs) == 1, f"Expected overflow message, sent: {sent}"
    print("PASS  test_chatrunner_routes_context_overflow_from_tool_result")


# ---------------------------------------------------------------------------
# Run all
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_tool_result_size_guard,
        test_ask_context_overflow,
        test_send_tool_result_context_overflow,
        test_other_bad_request_still_raises,
        test_chatrunner_routes_context_overflow,
        test_chatrunner_routes_context_overflow_from_tool_result,
    ]
    failures = []
    for t in tests:
        try:
            t()
        except Exception as e:
            print(f"FAIL  {t.__name__}: {e}")
            failures.append(t.__name__)

    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
