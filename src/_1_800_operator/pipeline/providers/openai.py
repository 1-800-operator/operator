"""
OpenAI LLM provider.

Wraps an openai.OpenAI client. Translates the app's neutral conversation
shape (see pipeline/providers/base.py) to and from OpenAI's chat
completion format, and maps OpenAI's context_length_exceeded error into
the provider-agnostic ContextOverflowError.
"""
import json
import logging
import time

import openai

log = logging.getLogger(__name__)

from _1_800_operator.pipeline.providers.base import (
    LLMProvider,
    ContextOverflowError,
    ToolCall,
    ProviderResponse,
    flush_paragraphs,
)


def _neutral_to_openai_messages(system, messages):
    """Translate (system, neutral messages) into OpenAI's messages list."""
    out = []
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        role = m["role"]
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            tool_calls = m.get("tool_calls") or []
            if tool_calls:
                out.append({
                    "role": "assistant",
                    "content": m.get("content"),
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.args),
                            },
                        }
                        for tc in tool_calls
                    ],
                })
            else:
                out.append({"role": "assistant", "content": m.get("content")})
        elif role == "tool_result":
            out.append({
                "role": "tool",
                "tool_call_id": m["tool_call_id"],
                "content": m["content"],
            })
        else:
            raise ValueError(f"unknown neutral message role: {role!r}")
    return out


class OpenAIProvider(LLMProvider):
    def __init__(self, client):
        self._client = client

    def complete(self, system, messages, model, max_tokens, tools=None):
        kwargs = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": _neutral_to_openai_messages(system, messages),
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["parallel_tool_calls"] = False
        t_start = time.monotonic()
        try:
            response = self._client.chat.completions.create(**kwargs)
        except openai.BadRequestError as e:
            if getattr(e, "code", None) == "context_length_exceeded":
                raise ContextOverflowError() from e
            raise

        elapsed = time.monotonic() - t_start
        usage = getattr(response, "usage", None)
        if usage is not None:
            inp = getattr(usage, "prompt_tokens", 0) or 0
            out_tok = getattr(usage, "completion_tokens", 0) or 0
            # OpenAI surfaces cached prefix tokens via prompt_tokens_details.cached_tokens.
            details = getattr(usage, "prompt_tokens_details", None)
            cache_read = getattr(details, "cached_tokens", 0) if details else 0
            cache_pct = (cache_read * 100 // inp) if inp else 0
            log.info(
                f"TIMING llm_call={elapsed:.1f}s "
                f"TOKENS in={inp} cache_read={cache_read} out={out_tok} cache_hit={cache_pct}%"
            )

        choice = response.choices[0]
        message = choice.message

        tool_calls = []
        if message.tool_calls:
            for tc in message.tool_calls:
                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    args=json.loads(tc.function.arguments),
                ))

        if tool_calls:
            stop_reason = "tool_use"
        elif choice.finish_reason == "length":
            stop_reason = "length"
        elif choice.finish_reason == "stop":
            stop_reason = "end"
        else:
            stop_reason = "other"

        return ProviderResponse(
            text=message.content,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
        )

    def complete_streaming(
        self, system, messages, model, max_tokens, tools=None, on_paragraph=None,
    ):
        if on_paragraph is None:
            return self.complete(system, messages, model, max_tokens, tools=tools)

        kwargs = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": _neutral_to_openai_messages(system, messages),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["parallel_tool_calls"] = False

        buffer = ""
        full_text_parts = []
        # Tool-call deltas arrive incrementally indexed by `index`; we
        # accumulate id/name/arguments per index and parse args at end.
        tool_state: dict[int, dict] = {}
        finish_reason = None
        usage = None
        t_start = time.monotonic()

        try:
            response = self._client.chat.completions.create(**kwargs)
            for chunk in response:
                # Final chunk in stream_options=include_usage may have empty choices.
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    usage = chunk_usage
                if not getattr(chunk, "choices", None):
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                if getattr(delta, "content", None):
                    full_text_parts.append(delta.content)
                    buffer += delta.content
                    if "\n\n" in buffer:
                        buffer = flush_paragraphs(buffer, on_paragraph)
                if getattr(delta, "tool_calls", None):
                    for tc_delta in delta.tool_calls:
                        idx = getattr(tc_delta, "index", 0)
                        state = tool_state.setdefault(idx, {"id": "", "name": "", "args": ""})
                        if getattr(tc_delta, "id", None):
                            state["id"] = tc_delta.id
                        fn = getattr(tc_delta, "function", None)
                        if fn is not None:
                            if getattr(fn, "name", None):
                                state["name"] = fn.name
                            if getattr(fn, "arguments", None):
                                state["args"] += fn.arguments
                if getattr(choice, "finish_reason", None):
                    finish_reason = choice.finish_reason
        except openai.BadRequestError as e:
            if getattr(e, "code", None) == "context_length_exceeded":
                raise ContextOverflowError() from e
            raise

        if buffer.strip():
            flush_paragraphs(buffer, on_paragraph, force_final=True)

        elapsed = time.monotonic() - t_start
        if usage is not None:
            inp = getattr(usage, "prompt_tokens", 0) or 0
            out_tok = getattr(usage, "completion_tokens", 0) or 0
            details = getattr(usage, "prompt_tokens_details", None)
            cache_read = getattr(details, "cached_tokens", 0) if details else 0
            cache_pct = (cache_read * 100 // inp) if inp else 0
            log.info(
                f"TIMING llm_call={elapsed:.1f}s streamed=1 "
                f"TOKENS in={inp} cache_read={cache_read} out={out_tok} cache_hit={cache_pct}%"
            )

        text = "".join(full_text_parts) or None
        tool_calls = []
        for state in tool_state.values():
            try:
                args = json.loads(state["args"]) if state["args"] else {}
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(id=state["id"], name=state["name"], args=args))

        if tool_calls:
            stop_reason = "tool_use"
        elif finish_reason == "length":
            stop_reason = "length"
        elif finish_reason == "stop":
            stop_reason = "end"
        else:
            stop_reason = "other"

        return ProviderResponse(text=text, tool_calls=tool_calls, stop_reason=stop_reason)

    def complete_stream(self, system, messages, model, max_tokens):
        response = self._client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=_neutral_to_openai_messages(system, messages),
            stream=True,
        )
        for chunk in response:
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content

    def warmup(self, model):
        self._client.chat.completions.create(
            model=model,
            max_tokens=1,
            messages=[{"role": "user", "content": "hi"}],
        )
