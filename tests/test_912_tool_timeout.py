"""
Unit tests for step 9.12 — tool execution + per-MCP timeout.

Session 157: timeouts were moved from chat_runner into the MCP layer so each
server can carry its own deadline (see config.DEFAULT_TOOL_TIMEOUTS + the
per-server `tool_timeout_seconds` override). When the MCP raises (including
timeout as an MCPToolError), the normal error-handler path feeds an
informative signpost to the LLM.

Session 164: verb heartbeats were removed. claude-code now surfaces real
inner tool_use events via stream-json tail; other tools run silently.

Covers:
  1. Fast tool: result delivered, no extraneous chat noise
  2. Slow tool: still no heartbeat noise — silence is correct
  3. MCP-reported timeout: MCPToolError propagates to the LLM with "timed out"
  4. MCP-reported timeout + LLM follow-up fails: fallback message to chat

Run:
    source venv/bin/activate
    python tests/test_912_tool_timeout.py
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
os.environ.setdefault("OPERATOR_BOT", "pm")

import time
from unittest.mock import MagicMock
from _1_800_operator import config
from _1_800_operator.pipeline.mcp_client import MCPToolError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_runner():
    """Build a ChatRunner with a mock MCP. Caller sets execute_tool behavior."""
    from _1_800_operator.pipeline.chat_runner import ChatRunner

    connector = MagicMock()
    llm = MagicMock()
    mcp = MagicMock()
    mcp.get_openai_tools.return_value = []
    mcp.tool_timeout_for.return_value = None
    mcp.record_tool_result.return_value = False  # server not tripped
    llm.send_tool_result.return_value = {"type": "text", "content": "Done."}

    runner = ChatRunner(connector, llm, mcp)
    sent = []
    runner._send = lambda text: sent.append(text)

    return runner, sent, llm, mcp


def _slow_tool_returning(delay, value="tool result"):
    def _fn(name, args):
        time.sleep(delay)
        return value
    return _fn


def _slow_tool_raising(delay, exc):
    def _fn(name, args):
        time.sleep(delay)
        raise exc
    return _fn


# ---------------------------------------------------------------------------
# Test 1: fast tool — silent execution
# ---------------------------------------------------------------------------

def test_fast_tool_silent_execution():
    """Tool finishes quickly — no placeholder pings, just the LLM summary."""
    runner, sent, llm, mcp = make_runner()
    mcp.execute_tool.side_effect = _slow_tool_returning(0.1)
    runner._pending_tool_call = {"id": "c1", "name": "fast__tool", "arguments": {}}

    runner._handle_confirmation("yes")

    placeholder_pings = [m for m in sent if m.endswith("...")]
    assert len(placeholder_pings) == 0, f"Expected no placeholder pings, got: {sent}"
    llm.send_tool_result.assert_called_once()
    assert "Done." in sent, f"Expected 'Done.' in sent, got: {sent}"
    print(f"PASS  test_fast_tool_silent_execution  sent={sent}")


# ---------------------------------------------------------------------------
# Test 2: slow tool — still silent (heartbeat verbs removed in session 164)
# ---------------------------------------------------------------------------

def test_slow_tool_silent_execution():
    """Slow non-claude-code tool runs silently. Real progress for claude-code
    flows through stream-json tail (see commit d0ff5a7); other MCPs are
    short-lived enough that placeholder verbs added more noise than signal."""
    runner, sent, llm, mcp = make_runner()
    mcp.execute_tool.side_effect = _slow_tool_returning(2.0)
    runner._pending_tool_call = {"id": "c2", "name": "slow__tool", "arguments": {}}

    runner._handle_confirmation("yes")

    placeholder_pings = [m for m in sent if m.endswith("...")]
    timeout_msgs = [m for m in sent if "timed out" in m.lower()]
    assert len(placeholder_pings) == 0, f"Expected no placeholder pings, got: {sent}"
    assert len(timeout_msgs) == 0, f"Unexpected timeout message: {sent}"
    llm.send_tool_result.assert_called_once()
    call_args = llm.send_tool_result.call_args[0]
    assert call_args[2] == "tool result", f"Expected tool result, got: {call_args[2]}"
    print(f"PASS  test_slow_tool_silent_execution  sent={sent}")


# ---------------------------------------------------------------------------
# Test 3: MCP-reported timeout flows through as a tool error
# ---------------------------------------------------------------------------

def test_mcp_timeout_is_signposted_to_llm():
    """When the MCP layer raises MCPToolError('timed out'), the LLM follow-up gets a 'timed out' signpost."""
    runner, sent, llm, mcp = make_runner()
    mcp.execute_tool.side_effect = _slow_tool_raising(
        0.2, MCPToolError("Tool 'hung__tool' timed out after 30s on MCP server 'hung'.")
    )
    runner._pending_tool_call = {"id": "c4", "name": "hung__tool", "arguments": {}}

    runner._handle_confirmation("yes")

    llm.send_tool_result.assert_called_once()
    result_content = llm.send_tool_result.call_args[0][2]
    assert "timed out" in result_content.lower(), \
        f"Expected 'timed out' in error signpost, got: {result_content}"
    print(f"PASS  test_mcp_timeout_is_signposted_to_llm  signpost={result_content[:80]!r}")


def test_mcp_timeout_fallback_when_llm_fails():
    """On MCP-reported timeout, if the LLM follow-up itself fails, fallback hits chat."""
    runner, sent, llm, mcp = make_runner()
    mcp.execute_tool.side_effect = _slow_tool_raising(
        0.2, MCPToolError("Tool 'hung__tool' timed out after 30s on MCP server 'hung'.")
    )
    llm.send_tool_result.side_effect = RuntimeError("LLM down")
    runner._pending_tool_call = {"id": "c5", "name": "hung__tool", "arguments": {}}

    runner._handle_confirmation("yes")

    failure_msgs = [m for m in sent if "failed" in m.lower() or "timed out" in m.lower()]
    assert len(failure_msgs) >= 1, f"Expected fallback failure message, got: {sent}"
    assert "Done." not in sent, f"Unexpected success message: {sent}"
    print(f"PASS  test_mcp_timeout_fallback_when_llm_fails  sent={sent}")


# ---------------------------------------------------------------------------
# Run all
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_fast_tool_silent_execution,
        test_slow_tool_silent_execution,
        test_mcp_timeout_is_signposted_to_llm,
        test_mcp_timeout_fallback_when_llm_fails,
    ]
    failures = []
    for t in tests:
        try:
            t()
        except Exception as e:
            import traceback
            print(f"FAIL  {t.__name__}: {e}")
            traceback.print_exc()
            failures.append(t.__name__)

    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
