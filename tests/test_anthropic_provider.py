"""
Unit tests for AnthropicProvider.

Exercises the translation between the app's neutral conversation shape and
Anthropic's Messages API format — both the request translation (neutral
history + system + OpenAI-style tool schemas → Anthropic's kwargs) and the
response translation (Anthropic content blocks → ProviderResponse).

Uses a mocked anthropic.Anthropic client — no network calls.

Run:
    source venv/bin/activate
    python tests/test_anthropic_provider.py
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from types import SimpleNamespace
from unittest.mock import MagicMock

import anthropic

from _1_800_operator.pipeline.providers import (
    AnthropicProvider,
    ContextOverflowError,
    ProviderResponse,
    ToolCall,
)


# ---------------------------------------------------------------------------
# Helpers to build fake Anthropic SDK response objects
# ---------------------------------------------------------------------------

def _text_block(text):
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(id, name, input):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


def _anthropic_response(content, stop_reason="end_turn"):
    return SimpleNamespace(content=content, stop_reason=stop_reason)


def _make_provider():
    client = MagicMock()
    return AnthropicProvider(client), client


# ---------------------------------------------------------------------------
# Request translation
# ---------------------------------------------------------------------------

def test_system_prompt_passed_separately():
    provider, client = _make_provider()
    client.messages.create.return_value = _anthropic_response([_text_block("ok")])

    provider.complete(
        system="You are Operator.",
        messages=[{"role": "user", "content": "hi"}],
        model="claude-sonnet-4-5",
        max_tokens=60,
    )

    kwargs = client.messages.create.call_args.kwargs
    # System is wrapped as a cache_control content block so it becomes
    # part of the cached prefix alongside the tool schemas.
    assert kwargs["system"] == [{
        "type": "text",
        "text": "You are Operator.",
        "cache_control": {"type": "ephemeral"},
    }]
    assert kwargs["messages"] == [{"role": "user", "content": "hi"}]
    assert kwargs["model"] == "claude-sonnet-4-5"
    assert kwargs["max_tokens"] == 60
    # System must NOT be in the messages list
    assert all(m["role"] != "system" for m in kwargs["messages"])
    print("PASS  test_system_prompt_passed_separately")


def test_empty_system_omitted():
    provider, client = _make_provider()
    client.messages.create.return_value = _anthropic_response([_text_block("ok")])

    provider.complete(
        system="",
        messages=[{"role": "user", "content": "hi"}],
        model="claude-sonnet-4-5",
        max_tokens=60,
    )

    kwargs = client.messages.create.call_args.kwargs
    assert "system" not in kwargs, "empty system should not be sent"
    print("PASS  test_empty_system_omitted")


def test_assistant_tool_call_becomes_content_blocks():
    """Neutral assistant message with tool_calls → Anthropic content-block list."""
    provider, client = _make_provider()
    client.messages.create.return_value = _anthropic_response([_text_block("ok")])

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
        model="claude-sonnet-4-5",
        max_tokens=60,
    )

    sent = client.messages.create.call_args.kwargs["messages"]
    assert len(sent) == 3

    # User message pass-through
    assert sent[0] == {"role": "user", "content": "list issues"}

    # Assistant message: content becomes list of blocks, one tool_use block
    asst = sent[1]
    assert asst["role"] == "assistant"
    assert isinstance(asst["content"], list)
    assert len(asst["content"]) == 1
    block = asst["content"][0]
    assert block["type"] == "tool_use"
    assert block["id"] == "call_1"
    assert block["name"] == "linear__list_issues"
    assert block["input"] == {"team": "moj"}

    # Tool result becomes user turn with tool_result content block
    tr = sent[2]
    assert tr["role"] == "user"
    assert tr["content"][0]["type"] == "tool_result"
    assert tr["content"][0]["tool_use_id"] == "call_1"
    assert tr["content"][0]["content"] == "[...]"
    print("PASS  test_assistant_tool_call_becomes_content_blocks")


def test_assistant_with_text_and_tool_call():
    """Assistant message with both text and a tool_call emits both blocks."""
    provider, client = _make_provider()
    client.messages.create.return_value = _anthropic_response([_text_block("ok")])

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
        model="claude-sonnet-4-5",
        max_tokens=60,
    )
    asst = client.messages.create.call_args.kwargs["messages"][1]
    assert len(asst["content"]) == 2
    assert asst["content"][0] == {"type": "text", "text": "Looking that up."}
    assert asst["content"][1]["type"] == "tool_use"
    print("PASS  test_assistant_with_text_and_tool_call")


def test_openai_tool_schema_translated():
    """OpenAI-function-calling tool schemas → Anthropic input_schema format."""
    provider, client = _make_provider()
    client.messages.create.return_value = _anthropic_response([_text_block("ok")])

    provider.complete(
        system="sys",
        messages=[{"role": "user", "content": "hi"}],
        model="claude-sonnet-4-5",
        max_tokens=60,
        tools=[{
            "type": "function",
            "function": {
                "name": "linear__list_issues",
                "description": "List issues",
                "parameters": {"type": "object", "properties": {"team": {"type": "string"}}},
            },
        }],
    )

    sent_tools = client.messages.create.call_args.kwargs["tools"]
    assert len(sent_tools) == 1
    assert sent_tools[0]["name"] == "linear__list_issues"
    assert sent_tools[0]["description"] == "List issues"
    assert sent_tools[0]["input_schema"] == {
        "type": "object",
        "properties": {"team": {"type": "string"}},
    }
    # OpenAI's function wrapper should be gone
    assert "function" not in sent_tools[0]
    assert "parameters" not in sent_tools[0]
    # Last tool carries cache_control so the tool-schema prefix is cached
    assert sent_tools[-1]["cache_control"] == {"type": "ephemeral"}
    print("PASS  test_openai_tool_schema_translated")


# ---------------------------------------------------------------------------
# Response translation
# ---------------------------------------------------------------------------

def test_plain_text_response():
    provider, client = _make_provider()
    client.messages.create.return_value = _anthropic_response(
        [_text_block("Hello there.")], stop_reason="end_turn",
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
    client.messages.create.return_value = _anthropic_response(
        [_tool_use_block("c1", "linear__list_issues", {"team": "moj"})],
        stop_reason="tool_use",
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
    assert tc.args == {"team": "moj"}
    assert result.stop_reason == "tool_use"
    print("PASS  test_tool_use_response")


def test_mixed_text_and_tool_use_response():
    """Claude sometimes emits preamble text + a tool_use block in one reply."""
    provider, client = _make_provider()
    client.messages.create.return_value = _anthropic_response(
        [
            _text_block("Sure, let me check."),
            _tool_use_block("c1", "t", {"x": 1}),
        ],
        stop_reason="tool_use",
    )

    result = provider.complete(
        system="sys", messages=[{"role": "user", "content": "hi"}],
        model="m", max_tokens=60,
    )
    assert result.text == "Sure, let me check."
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].id == "c1"
    assert result.stop_reason == "tool_use"
    print("PASS  test_mixed_text_and_tool_use_response")


def test_max_tokens_stop_reason_mapped_to_length():
    provider, client = _make_provider()
    client.messages.create.return_value = _anthropic_response(
        [_text_block("truncated")], stop_reason="max_tokens",
    )
    result = provider.complete(
        system="sys", messages=[{"role": "user", "content": "hi"}],
        model="m", max_tokens=60,
    )
    assert result.stop_reason == "length"
    print("PASS  test_max_tokens_stop_reason_mapped_to_length")


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------

def test_context_overflow_translated():
    """Anthropic BadRequestError with 'prompt is too long' → ContextOverflowError."""
    provider, client = _make_provider()

    err = anthropic.BadRequestError.__new__(anthropic.BadRequestError)
    err.message = "prompt is too long: 210000 tokens > 200000 maximum"
    # str(err) needs to work — set .args too
    err.args = (err.message,)
    client.messages.create.side_effect = err

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
    """A BadRequestError that isn't about context length must propagate as-is."""
    provider, client = _make_provider()

    err = anthropic.BadRequestError.__new__(anthropic.BadRequestError)
    err.message = "invalid model"
    err.args = (err.message,)
    client.messages.create.side_effect = err

    raised_wrong = False
    try:
        provider.complete(
            system="sys", messages=[{"role": "user", "content": "hi"}],
            model="m", max_tokens=60,
        )
    except ContextOverflowError:
        raised_wrong = True
    except anthropic.BadRequestError:
        pass  # expected
    assert not raised_wrong, "non-overflow error should not be translated"
    print("PASS  test_other_bad_request_still_raises")


# ---------------------------------------------------------------------------
# Streaming — mid-stream 429 must not double-post (T1.6, session 178)
# ---------------------------------------------------------------------------


class _FakeStream:
    """Context manager that yields a list of text chunks then raises (or
    returns a final message). Mirrors the slice of anthropic.MessageStream
    surface that complete_streaming uses: `with` + `text_stream` iter +
    `get_final_message`.
    """

    def __init__(self, chunks, raise_on_index=None, final=None):
        self._chunks = chunks
        self._raise_on_index = raise_on_index
        self._final = final or SimpleNamespace(
            content=[_text_block("".join(chunks))], stop_reason="end_turn", usage=None,
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    @property
    def text_stream(self):
        for i, chunk in enumerate(self._chunks):
            if self._raise_on_index is not None and i == self._raise_on_index:
                err = anthropic.RateLimitError.__new__(anthropic.RateLimitError)
                err.message = "rate limited"
                err.args = (err.message,)
                err.response = SimpleNamespace(headers={})
                raise err
            yield chunk

    def get_final_message(self):
        return self._final


def test_streaming_mid_stream_429_bails_with_apology_no_double_post():
    """When a 429 fires after a paragraph has already flushed via on_paragraph,
    we must NOT retry (the user would see the reply twice). Instead, post a
    one-line apology via on_paragraph and return a synthetic empty response."""
    provider, client = _make_provider()
    # First chunk forms a complete paragraph (\n\n triggers flush_paragraphs);
    # second iteration raises 429 mid-stream.
    chunks = ["First paragraph.\n\n", "Second paragraph never arrives."]
    client.messages.stream.return_value = _FakeStream(chunks, raise_on_index=1)

    flushed: list[str] = []

    result = provider.complete_streaming(
        system="sys",
        messages=[{"role": "user", "content": "hi"}],
        model="m",
        max_tokens=60,
        on_paragraph=lambda text: flushed.append(text),
    )

    # Stream was opened exactly once — no retry that would re-post paragraphs.
    assert client.messages.stream.call_count == 1, \
        f"expected 1 stream attempt, got {client.messages.stream.call_count}"

    # First paragraph reached the user, then the apology line.
    assert any("First paragraph" in t for t in flushed), \
        f"expected first paragraph in flushed output: {flushed}"
    assert any("rate limit" in t.lower() for t in flushed), \
        f"expected mid-stream apology in flushed output: {flushed}"

    # Synthetic empty response so caller doesn't fire its own narrate fallback.
    assert isinstance(result, ProviderResponse)
    assert result.text == ""
    assert result.tool_calls == []
    assert result.stop_reason == "end"
    print(f"PASS  test_streaming_mid_stream_429_bails_with_apology_no_double_post  flushed={flushed}")


def test_streaming_pre_flush_429_still_retries():
    """When a 429 fires before any paragraph has flushed, retry is still
    safe — the buffer is reset per attempt and nothing reached the user.
    Verifies the flushed_any guard doesn't over-trigger."""
    provider, client = _make_provider()

    # First call raises immediately (index 0, before any chunk arrives — no flush);
    # second call succeeds.
    raising_stream = _FakeStream(["x"], raise_on_index=0)
    success_stream = _FakeStream(
        ["Hello.\n\n", "World."],
        final=SimpleNamespace(
            content=[_text_block("Hello.\n\nWorld.")], stop_reason="end_turn", usage=None,
        ),
    )
    client.messages.stream.side_effect = [raising_stream, success_stream]

    flushed: list[str] = []

    # Patch sleep to skip the retry backoff in the test.
    import _1_800_operator.pipeline.providers.anthropic as anth_mod
    original_sleep = anth_mod.time.sleep
    anth_mod.time.sleep = lambda s: None
    try:
        result = provider.complete_streaming(
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
            model="m",
            max_tokens=60,
            on_paragraph=lambda text: flushed.append(text),
        )
    finally:
        anth_mod.time.sleep = original_sleep

    assert client.messages.stream.call_count == 2, "expected one retry"
    assert any("Hello" in t for t in flushed)
    assert not any("rate limit" in t.lower() for t in flushed), \
        f"unexpected mid-stream apology: {flushed}"
    assert isinstance(result, ProviderResponse)
    print("PASS  test_streaming_pre_flush_429_still_retries")


# ---------------------------------------------------------------------------
# Run all
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_system_prompt_passed_separately,
        test_empty_system_omitted,
        test_assistant_tool_call_becomes_content_blocks,
        test_assistant_with_text_and_tool_call,
        test_openai_tool_schema_translated,
        test_plain_text_response,
        test_tool_use_response,
        test_mixed_text_and_tool_use_response,
        test_max_tokens_stop_reason_mapped_to_length,
        test_context_overflow_translated,
        test_other_bad_request_still_raises,
        test_streaming_mid_stream_429_bails_with_apology_no_double_post,
        test_streaming_pre_flush_429_still_retries,
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
