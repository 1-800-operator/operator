"""
Unit tests for the 11.3a tool-loop scratchpad.

Previous behavior (pre-11.3a) kept tool_call/tool_result messages in history
and collapsed them after the summary via `_collapse_tool_exchange`. The new
design keeps them in an in-memory `_scratch` that clears automatically when
the tool loop closes with a final text reply — no collapse needed, and the
meeting record never sees those protocol messages.

Run:
    source venv/bin/activate
    python tests/test_913_tool_history_collapse.py
"""
import sys
import os
os.environ.setdefault("OPERATOR_BOT", "pm")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from unittest.mock import MagicMock


def make_llm():
    from _1_800_operator.pipeline.llm import LLMClient
    provider = MagicMock()
    return LLMClient(provider)


def make_text_message(text):
    from _1_800_operator.pipeline.providers import ProviderResponse
    return ProviderResponse(text=text, tool_calls=[], stop_reason="end")


def make_tool_call_message(tool_id, name):
    from _1_800_operator.pipeline.providers import ProviderResponse, ToolCall
    return ProviderResponse(
        text=None,
        tool_calls=[ToolCall(id=tool_id, name=name, args={})],
        stop_reason="tool_use",
    )


def test_single_tool_call_clears_scratch():
    """After send_tool_result returns a text summary, _scratch is empty."""
    from _1_800_operator.pipeline.providers import ToolCall
    llm = make_llm()
    llm._scratch = [
        {"role": "assistant", "content": None, "tool_calls": [
            ToolCall(id="call_1", name="linear__create_issue", args={}),
        ]},
    ]
    llm._provider.complete.return_value = make_text_message("Done — ticket LIN-42 created.")

    result = llm.send_tool_result("call_1", "linear__create_issue", "x" * 10000)

    assert result == "Done — ticket LIN-42 created."
    assert llm._scratch == [], f"scratch should be empty, got {llm._scratch!r}"
    print("PASS  test_single_tool_call_clears_scratch")


def test_chained_tool_calls_accumulate_then_clear():
    """Mid-chain send_tool_result keeps scratch; final text clears it."""
    from _1_800_operator.pipeline.providers import ToolCall
    llm = make_llm()
    llm._scratch = [
        {"role": "assistant", "content": None, "tool_calls": [
            ToolCall(id="call_A", name="linear__list_issues", args={}),
        ]},
    ]

    # First tool_result → model requests tool B
    llm._provider.complete.return_value = make_tool_call_message("call_B", "linear__create_issue")
    result = llm.send_tool_result("call_A", "linear__list_issues", "x" * 5000, tools=[{"function": {"name": "t"}}])
    assert result["type"] == "tool_call"
    assert len(llm._scratch) == 3  # [asst A, tool_result A, asst B]

    # Second tool_result → final text summary
    llm._provider.complete.return_value = make_text_message("Listed and created LIN-43.")
    result = llm.send_tool_result("call_B", "linear__create_issue", "y" * 5000, tools=[{"function": {"name": "t"}}])
    assert result == {"type": "text", "content": "Listed and created LIN-43."}
    assert llm._scratch == [], f"scratch should be empty after final text, got {llm._scratch!r}"
    print("PASS  test_chained_tool_calls_accumulate_then_clear")


def test_new_ask_clears_stale_scratch():
    """Starting a new user turn drops any leftover tool-loop scratch."""
    from _1_800_operator.pipeline.providers import ToolCall
    llm = make_llm()
    llm._scratch = [
        {"role": "assistant", "content": None, "tool_calls": [
            ToolCall(id="stale", name="some__tool", args={}),
        ]},
    ]
    llm._provider.complete.return_value = make_text_message("fresh reply")

    result = llm.ask("new question")

    assert result == "fresh reply"
    assert llm._scratch == [], "scratch should be reset at start of ask()"
    print("PASS  test_new_ask_clears_stale_scratch")


if __name__ == "__main__":
    tests = [
        test_single_tool_call_clears_scratch,
        test_chained_tool_calls_accumulate_then_clear,
        test_new_ask_clears_stale_scratch,
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
