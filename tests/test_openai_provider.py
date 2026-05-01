"""
Unit tests for OpenAIProvider.

Mirrors tests/test_anthropic_provider.py. Exercises the translation between
the app's neutral conversation shape and OpenAI's Chat Completions format —
both the request translation (neutral history + system + OpenAI-style tool
schemas → OpenAI kwargs) and the response translation (OpenAI choice/message
→ ProviderResponse).

Uses a mocked openai.OpenAI client — no network calls.

Run:
    source venv/bin/activate
    python tests/test_openai_provider.py
"""
import json
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from types import SimpleNamespace
from unittest.mock import MagicMock

import openai

from _1_800_operator.pipeline.providers import (
    OpenAIProvider,
    ContextOverflowError,
    ProviderResponse,
    ToolCall,
)


# ---------------------------------------------------------------------------
# Helpers to build fake OpenAI SDK response objects
# ---------------------------------------------------------------------------

def _tool_call_obj(id, name, arguments_json):
    return SimpleNamespace(
        id=id,
        function=SimpleNamespace(name=name, arguments=arguments_json),
    )


def _openai_response(content=None, tool_calls=None, finish_reason="stop"):
    message = SimpleNamespace(content=content, tool_calls=tool_calls or None)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=None)


def _make_provider():
    client = MagicMock()
    return OpenAIProvider(client), client


# ---------------------------------------------------------------------------
# Request translation
# ---------------------------------------------------------------------------

def test_system_prompt_prepended_as_system_message():
    provider, client = _make_provider()
    client.chat.completions.create.return_value = _openai_response(content="ok")

    provider.complete(
        system="You are Operator.",
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-4o",
        max_tokens=60,
    )

    kwargs = client.chat.completions.create.call_args.kwargs
    sent = kwargs["messages"]
    assert sent[0] == {"role": "system", "content": "You are Operator."}
    assert sent[1] == {"role": "user", "content": "hi"}
    assert kwargs["model"] == "gpt-4o"
    assert kwargs["max_tokens"] == 60
    print("PASS  test_system_prompt_prepended_as_system_message")


def test_empty_system_omitted():
    provider, client = _make_provider()
    client.chat.completions.create.return_value = _openai_response(content="ok")

    provider.complete(
        system="",
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-4o",
        max_tokens=60,
    )

    sent = client.chat.completions.create.call_args.kwargs["messages"]
    assert all(m["role"] != "system" for m in sent), "empty system should not be sent"
    assert sent[0] == {"role": "user", "content": "hi"}
    print("PASS  test_empty_system_omitted")


def test_assistant_tool_call_becomes_openai_tool_calls():
    """Neutral assistant message with tool_calls → OpenAI tool_calls with JSON arguments."""
    provider, client = _make_provider()
    client.chat.completions.create.return_value = _openai_response(content="ok")

    provider.complete(
        system="sys",
        messages=[
            {"role": "user", "content": "list issues"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [ToolCall(
                    id="call_1", name="linear__list_issues", args={"team": "moj"},
                )],
            },
            {"role": "tool_result", "tool_call_id": "call_1", "content": "[...]"},
        ],
        model="gpt-4o",
        max_tokens=60,
    )

    sent = client.chat.completions.create.call_args.kwargs["messages"]
    # [system, user, assistant(tool_calls), tool]
    assert len(sent) == 4
    assert sent[0]["role"] == "system"
    assert sent[1] == {"role": "user", "content": "list issues"}

    asst = sent[2]
    assert asst["role"] == "assistant"
    assert asst["content"] is None
    assert len(asst["tool_calls"]) == 1
    tc = asst["tool_calls"][0]
    assert tc["id"] == "call_1"
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "linear__list_issues"
    # args are serialized to a JSON string for OpenAI
    assert json.loads(tc["function"]["arguments"]) == {"team": "moj"}

    tr = sent[3]
    assert tr["role"] == "tool"
    assert tr["tool_call_id"] == "call_1"
    assert tr["content"] == "[...]"
    print("PASS  test_assistant_tool_call_becomes_openai_tool_calls")


def test_assistant_with_text_and_tool_call():
    """Assistant message with both text and a tool_call keeps content alongside tool_calls."""
    provider, client = _make_provider()
    client.chat.completions.create.return_value = _openai_response(content="ok")

    provider.complete(
        system="sys",
        messages=[
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "Looking that up.",
                "tool_calls": [ToolCall(id="c1", name="t", args={})],
            },
        ],
        model="gpt-4o",
        max_tokens=60,
    )
    asst = client.chat.completions.create.call_args.kwargs["messages"][2]
    assert asst["role"] == "assistant"
    assert asst["content"] == "Looking that up."
    assert len(asst["tool_calls"]) == 1
    assert asst["tool_calls"][0]["function"]["name"] == "t"
    print("PASS  test_assistant_with_text_and_tool_call")


def test_tools_passed_through_with_parallel_disabled():
    """OpenAI-shaped tool schemas pass through; parallel_tool_calls forced off."""
    provider, client = _make_provider()
    client.chat.completions.create.return_value = _openai_response(content="ok")

    schema = {
        "type": "function",
        "function": {
            "name": "linear__list_issues",
            "description": "List issues",
            "parameters": {"type": "object", "properties": {"team": {"type": "string"}}},
        },
    }
    provider.complete(
        system="sys",
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-4o",
        max_tokens=60,
        tools=[schema],
    )

    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["tools"] == [schema]
    assert kwargs["parallel_tool_calls"] is False
    print("PASS  test_tools_passed_through_with_parallel_disabled")


def test_no_tools_means_no_tools_kwarg():
    """If tools is None/empty, no tools kwarg is sent (and no parallel_tool_calls)."""
    provider, client = _make_provider()
    client.chat.completions.create.return_value = _openai_response(content="ok")

    provider.complete(
        system="sys",
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-4o",
        max_tokens=60,
    )
    kwargs = client.chat.completions.create.call_args.kwargs
    assert "tools" not in kwargs
    assert "parallel_tool_calls" not in kwargs
    print("PASS  test_no_tools_means_no_tools_kwarg")


# ---------------------------------------------------------------------------
# Response translation
# ---------------------------------------------------------------------------

def test_plain_text_response():
    provider, client = _make_provider()
    client.chat.completions.create.return_value = _openai_response(
        content="Hello there.", finish_reason="stop",
    )

    result = provider.complete(
        system="sys", messages=[{"role": "user", "content": "hi"}],
        model="m", max_tokens=60,
    )
    assert isinstance(result, ProviderResponse)
    assert result.text == "Hello there."
    assert result.tool_calls == []
    assert result.stop_reason == "end"
    print("PASS  test_plain_text_response")


def test_tool_use_response():
    provider, client = _make_provider()
    client.chat.completions.create.return_value = _openai_response(
        content=None,
        tool_calls=[_tool_call_obj("c1", "linear__list_issues", '{"team": "moj"}')],
        # OpenAI reports "tool_calls" as the finish_reason, but the provider
        # derives stop_reason from the presence of tool_calls either way.
        finish_reason="tool_calls",
    )

    result = provider.complete(
        system="sys", messages=[{"role": "user", "content": "hi"}],
        model="m", max_tokens=60,
    )
    assert result.text is None
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert isinstance(tc, ToolCall)
    assert tc.id == "c1"
    assert tc.name == "linear__list_issues"
    # args come back parsed from JSON, not a string
    assert tc.args == {"team": "moj"}
    assert result.stop_reason == "tool_use"
    print("PASS  test_tool_use_response")


def test_length_finish_reason_mapped_to_length():
    provider, client = _make_provider()
    client.chat.completions.create.return_value = _openai_response(
        content="truncated", finish_reason="length",
    )
    result = provider.complete(
        system="sys", messages=[{"role": "user", "content": "hi"}],
        model="m", max_tokens=60,
    )
    assert result.stop_reason == "length"
    print("PASS  test_length_finish_reason_mapped_to_length")


def test_other_finish_reason_mapped_to_other():
    """Finish reasons outside stop/length/tool_calls collapse to 'other'."""
    provider, client = _make_provider()
    client.chat.completions.create.return_value = _openai_response(
        content="filtered", finish_reason="content_filter",
    )
    result = provider.complete(
        system="sys", messages=[{"role": "user", "content": "hi"}],
        model="m", max_tokens=60,
    )
    assert result.stop_reason == "other"
    print("PASS  test_other_finish_reason_mapped_to_other")


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------

def test_context_overflow_translated():
    """OpenAI BadRequestError with code=context_length_exceeded → ContextOverflowError."""
    provider, client = _make_provider()

    err = openai.BadRequestError.__new__(openai.BadRequestError)
    err.message = "This model's maximum context length is 128000 tokens."
    err.code = "context_length_exceeded"
    err.args = (err.message,)
    client.chat.completions.create.side_effect = err

    raised = None
    try:
        provider.complete(
            system="sys", messages=[{"role": "user", "content": "hi"}],
            model="m", max_tokens=60,
        )
    except ContextOverflowError as e:
        raised = e
    assert raised is not None, "expected ContextOverflowError"
    print("PASS  test_context_overflow_translated")


def test_other_bad_request_still_raises():
    """A BadRequestError whose code isn't context_length_exceeded must propagate as-is."""
    provider, client = _make_provider()

    err = openai.BadRequestError.__new__(openai.BadRequestError)
    err.message = "invalid model"
    err.code = "model_not_found"
    err.args = (err.message,)
    client.chat.completions.create.side_effect = err

    raised_wrong = False
    try:
        provider.complete(
            system="sys", messages=[{"role": "user", "content": "hi"}],
            model="m", max_tokens=60,
        )
    except ContextOverflowError:
        raised_wrong = True
    except openai.BadRequestError:
        pass  # expected
    assert not raised_wrong, "non-overflow error should not be translated"
    print("PASS  test_other_bad_request_still_raises")


# ---------------------------------------------------------------------------
# Run all
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_system_prompt_prepended_as_system_message,
        test_empty_system_omitted,
        test_assistant_tool_call_becomes_openai_tool_calls,
        test_assistant_with_text_and_tool_call,
        test_tools_passed_through_with_parallel_disabled,
        test_no_tools_means_no_tools_kwarg,
        test_plain_text_response,
        test_tool_use_response,
        test_length_finish_reason_mapped_to_length,
        test_other_finish_reason_mapped_to_other,
        test_context_overflow_translated,
        test_other_bad_request_still_raises,
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
