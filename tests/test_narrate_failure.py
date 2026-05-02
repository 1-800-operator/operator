"""
Unit tests for ChatRunner._narrate_failure (session 178, T1.4 + T1.5 + T1.12).

Covers the unified failure-narration helper and its four call sites:
  - dispatcher else-arm (unknown result shape)
  - main loop on LLM exception (every user message)
  - _handle_load_skill on follow-up LLM exception
  - tool-result summary on LLM exception (with raw result inlined)

The helper hands a failure context to the LLM via a small no-tools call
asking for a plain-text reply, with retry_rate_limits=False so the user
doesn't wait through a second 429 retry window. One-shot — the call uses
record=False, tools=None, and the bare-string return is posted inline
(never re-enters the dispatcher). On any narration failure, falls
through to a hardcoded message. Always emits turn_done(failed=True).

Run:
    source venv/bin/activate
    python tests/test_narrate_failure.py
"""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
os.environ.setdefault("OPERATOR_BOT", "pm")

from unittest.mock import MagicMock


def make_runner():
    """Build a ChatRunner with mocks. Reset the heartbeat counter so each
    test can assert turn_done was emitted with failed=True."""
    from _1_800_operator.pipeline.chat_runner import ChatRunner

    connector = MagicMock()
    llm = MagicMock()
    mcp = MagicMock()
    mcp.get_openai_tools.return_value = []
    mcp.tool_timeout_for.return_value = None
    mcp.record_tool_result.return_value = False

    runner = ChatRunner(connector, llm, mcp)
    sent = []
    runner._send = lambda text: sent.append(text)

    turn_done_calls = []
    runner._emit_turn_done = lambda **kw: turn_done_calls.append(kw)

    return runner, sent, llm, mcp, turn_done_calls


# ---------------------------------------------------------------------------
# _narrate_failure direct tests
# ---------------------------------------------------------------------------

def test_narrate_failure_calls_llm_with_safe_kwargs():
    """Helper must call ask() with record=False, tools=None,
    retry_rate_limits=False — guarantees one-shot, no recursion, and
    no second retry window on 429s."""
    runner, sent, llm, mcp, turn_done = make_runner()
    llm.ask.return_value = "Sorry, hit a snag."

    runner._narrate_failure(context="something broke.", fallback="generic fallback")

    llm.ask.assert_called_once()
    kwargs = llm.ask.call_args.kwargs
    assert kwargs.get("record") is False, f"record must be False, got {kwargs.get('record')!r}"
    assert kwargs.get("tools") is None, f"tools must be None, got {kwargs.get('tools')!r}"
    assert kwargs.get("retry_rate_limits") is False, \
        f"retry_rate_limits must be False, got {kwargs.get('retry_rate_limits')!r}"
    print("PASS  test_narrate_failure_calls_llm_with_safe_kwargs")


def test_narrate_failure_posts_narrated_text():
    """Happy path: LLM returns a non-empty string, that string is posted to chat."""
    runner, sent, llm, mcp, turn_done = make_runner()
    llm.ask.return_value = "Anthropic is rate-limiting me — try again in 30s."

    runner._narrate_failure(
        context="the LLM call failed with: RateLimitError.",
        fallback="generic fallback",
    )

    assert sent == ["Anthropic is rate-limiting me — try again in 30s."], \
        f"Expected narrated text, got: {sent}"
    print(f"PASS  test_narrate_failure_posts_narrated_text  sent={sent}")


def test_narrate_failure_uses_fallback_on_raise():
    """When the narration call itself raises, the hardcoded fallback is posted."""
    runner, sent, llm, mcp, turn_done = make_runner()
    llm.ask.side_effect = RuntimeError("provider died")

    runner._narrate_failure(
        context="something broke.",
        fallback="Hardcoded fallback message.",
    )

    assert sent == ["Hardcoded fallback message."], \
        f"Expected hardcoded fallback, got: {sent}"
    print(f"PASS  test_narrate_failure_uses_fallback_on_raise  sent={sent}")


def test_narrate_failure_uses_fallback_on_empty():
    """When the narration call returns an empty/whitespace string, fallback is posted."""
    runner, sent, llm, mcp, turn_done = make_runner()
    llm.ask.return_value = "   "

    runner._narrate_failure(context="x", fallback="Hardcoded fallback message.")

    assert sent == ["Hardcoded fallback message."], \
        f"Expected hardcoded fallback, got: {sent}"
    print("PASS  test_narrate_failure_uses_fallback_on_empty")


def test_narrate_failure_uses_fallback_on_non_string():
    """Defensive: if the narration call returns something other than a
    string (None, dict, etc.), still fall through to fallback."""
    runner, sent, llm, mcp, turn_done = make_runner()
    llm.ask.return_value = None

    runner._narrate_failure(context="x", fallback="Hardcoded fallback message.")

    assert sent == ["Hardcoded fallback message."], \
        f"Expected hardcoded fallback, got: {sent}"
    print("PASS  test_narrate_failure_uses_fallback_on_non_string")


def test_narrate_failure_always_emits_turn_done_failed():
    """Whether the narration succeeds or falls back, _emit_turn_done(failed=True)
    must fire so the per-turn watchdog closes."""
    # Success path
    runner, sent, llm, mcp, turn_done = make_runner()
    llm.ask.return_value = "narrated"
    runner._narrate_failure(context="x", fallback="y")
    assert turn_done == [{"failed": True}], f"Expected turn_done(failed=True), got {turn_done}"

    # Failure path
    runner, sent, llm, mcp, turn_done = make_runner()
    llm.ask.side_effect = RuntimeError("down")
    runner._narrate_failure(context="x", fallback="y")
    assert turn_done == [{"failed": True}], f"Expected turn_done(failed=True), got {turn_done}"
    print("PASS  test_narrate_failure_always_emits_turn_done_failed")


# ---------------------------------------------------------------------------
# Call-site integration tests
# ---------------------------------------------------------------------------

def test_dispatcher_else_arm_routes_unknown_shape():
    """An unknown result shape passed to _dispatch_result triggers
    _narrate_failure with the payload repr in the context."""
    runner, sent, llm, mcp, turn_done = make_runner()
    llm.ask.return_value = "Sorry, something unusual happened."

    unknown = {"type": "future_capability", "data": "xyz"}
    runner._dispatch_result(unknown)

    llm.ask.assert_called_once()
    prompt = llm.ask.call_args.args[0]
    assert "future_capability" in prompt, \
        f"Expected unknown payload repr in narration prompt, got: {prompt[:200]}"
    assert sent == ["Sorry, something unusual happened."]
    assert turn_done == [{"failed": True}]
    print("PASS  test_dispatcher_else_arm_routes_unknown_shape")


def test_dispatcher_known_shapes_skip_narration():
    """Known shapes (text, context_overflow) must not invoke _narrate_failure
    — only the fall-through path does."""
    # text shape
    runner, sent, llm, mcp, turn_done = make_runner()
    runner._dispatch_result({"type": "text", "content": "hello"})
    assert llm.ask.call_count == 0, "text shape should not invoke ask()"
    assert sent == ["hello"]

    # context_overflow shape
    runner, sent, llm, mcp, turn_done = make_runner()
    runner._dispatch_result({"type": "context_overflow"})
    assert llm.ask.call_count == 0, "context_overflow shape should not invoke ask()"
    assert any("conversation got too long" in s for s in sent), \
        f"Expected overflow message, got: {sent}"
    print("PASS  test_dispatcher_known_shapes_skip_narration")


def test_handle_load_skill_exception_routes_through_narrate():
    """When the skill follow-up LLM call raises, the user gets a narrated
    explanation that names the skill they invoked."""
    runner, sent, llm, mcp, turn_done = make_runner()
    # First call (the skill follow-up) raises; second call (the narrate) succeeds.
    llm.send_tool_result.side_effect = RuntimeError("rate limited")
    llm.ask.return_value = "The /foo skill couldn't run right now — Anthropic is rate-limiting. Try in 30s."

    # Plant a fake skill so _handle_load_skill resolves it locally before the LLM call.
    fake_skill = MagicMock()
    fake_skill.name = "foo"
    fake_skill.body = "skill body content"
    runner._skills = {"foo": fake_skill}

    tc = {"id": "c1", "name": "load_skill", "arguments": {"name": "foo"}}
    runner._handle_load_skill(tc)

    llm.ask.assert_called_once()
    prompt = llm.ask.call_args.args[0]
    assert "/foo" in prompt, f"Expected skill name in narration prompt, got: {prompt[:200]}"
    assert "rate limited" in prompt, \
        f"Expected exception message in narration prompt, got: {prompt[:200]}"
    assert sent == [
        "The /foo skill couldn't run right now — Anthropic is rate-limiting. Try in 30s."
    ]
    assert turn_done == [{"failed": True}]
    print("PASS  test_handle_load_skill_exception_routes_through_narrate")


def test_handle_load_skill_falls_back_when_narration_also_fails():
    """When BOTH the skill follow-up and the narration call raise,
    the hardcoded fallback hits chat — no silent fail."""
    runner, sent, llm, mcp, turn_done = make_runner()
    llm.send_tool_result.side_effect = RuntimeError("primary failure")
    llm.ask.side_effect = RuntimeError("fallback also down")

    fake_skill = MagicMock()
    fake_skill.name = "foo"
    fake_skill.body = "skill body content"
    runner._skills = {"foo": fake_skill}

    tc = {"id": "c1", "name": "load_skill", "arguments": {"name": "foo"}}
    runner._handle_load_skill(tc)

    assert len(sent) == 1, f"Expected exactly one fallback message, got: {sent}"
    assert "/foo" in sent[0], f"Expected skill name in fallback, got: {sent[0]!r}"
    assert turn_done == [{"failed": True}]
    print(f"PASS  test_handle_load_skill_falls_back_when_narration_also_fails  sent={sent}")


def test_tool_summary_exception_includes_raw_result_in_context():
    """When the tool succeeded but the summary LLM call raises, the
    narration prompt includes (a truncated form of) the raw tool result
    so the LLM can pass it through to the user."""
    runner, sent, llm, mcp, turn_done = make_runner()

    raw = "The Linear issue is OP-123: 'Fix login bug', priority high, owner sarah@."
    mcp.execute_tool.return_value = raw
    llm.send_tool_result.side_effect = RuntimeError("summary failed")
    llm.ask.return_value = "I got the issue but couldn't summarize: OP-123 fix login bug, sarah."

    runner._pending_tool_call = {
        "id": "c1", "name": "linear__get_issue", "arguments": {"id": "OP-123"},
    }
    runner._handle_confirmation("yes")

    llm.ask.assert_called_once()
    prompt = llm.ask.call_args.args[0]
    assert "OP-123" in prompt, \
        f"Expected raw tool result in narration prompt, got: {prompt[:300]}"
    assert "linear__get_issue" in prompt, \
        f"Expected tool name in narration prompt, got: {prompt[:300]}"
    assert any("OP-123" in s for s in sent), f"Expected narrated reply in chat, got: {sent}"
    assert turn_done == [{"failed": True}]
    print(f"PASS  test_tool_summary_exception_includes_raw_result_in_context  sent={sent}")


def test_tool_summary_exception_truncates_huge_raw_result():
    """Raw tool results longer than 600 chars are truncated before being
    inlined into the narration prompt — keeps prompt cost bounded."""
    runner, sent, llm, mcp, turn_done = make_runner()

    huge = "X" * 5000
    mcp.execute_tool.return_value = huge
    llm.send_tool_result.side_effect = RuntimeError("summary failed")
    llm.ask.return_value = "Got the result but couldn't summarize."

    runner._pending_tool_call = {
        "id": "c1", "name": "big__tool", "arguments": {},
    }
    runner._handle_confirmation("yes")

    llm.ask.assert_called_once()
    prompt = llm.ask.call_args.args[0]
    # The full 5000-char payload must NOT be in the prompt.
    assert huge not in prompt, "Untruncated huge payload was inlined into narration prompt"
    # But the truncation marker should be.
    assert "..." in prompt, f"Expected truncation marker '...', got: {prompt[-300:]}"
    print("PASS  test_tool_summary_exception_truncates_huge_raw_result")


# ---------------------------------------------------------------------------
# Run all
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_narrate_failure_calls_llm_with_safe_kwargs,
        test_narrate_failure_posts_narrated_text,
        test_narrate_failure_uses_fallback_on_raise,
        test_narrate_failure_uses_fallback_on_empty,
        test_narrate_failure_uses_fallback_on_non_string,
        test_narrate_failure_always_emits_turn_done_failed,
        test_dispatcher_else_arm_routes_unknown_shape,
        test_dispatcher_known_shapes_skip_narration,
        test_handle_load_skill_exception_routes_through_narrate,
        test_handle_load_skill_falls_back_when_narration_also_fails,
        test_tool_summary_exception_includes_raw_result_in_context,
        test_tool_summary_exception_truncates_huge_raw_result,
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
