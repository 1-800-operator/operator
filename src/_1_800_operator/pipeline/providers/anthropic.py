"""
Anthropic (Claude) LLM provider.

Wraps an anthropic.Anthropic client. Translates the app's neutral
conversation shape (see pipeline/providers/base.py) to and from
Anthropic's Messages API format, and maps Anthropic's "prompt is too
long" BadRequestError into the provider-agnostic ContextOverflowError.
"""
import logging
import threading
import time
from datetime import datetime, timezone

import anthropic

log = logging.getLogger(__name__)

from _1_800_operator import config
from _1_800_operator.pipeline.providers.base import (
    LLMProvider,
    ContextOverflowError,
    ToolCall,
    ProviderResponse,
    flush_paragraphs,
)


# Rate-limit retry policy. The common 429 case is "your tier's per-minute
# input-token bucket hasn't replenished yet" — a sliding window. The headers
# tell us when the bucket resets, so we sleep exactly that long and retry.
# Capped at MAX_SLEEP_SECONDS so we never block the meeting on a permanently
# undersized tier — after that, we surface the error to the user.
RATE_LIMIT_MAX_RETRIES = 2
RATE_LIMIT_MAX_SLEEP_SECONDS = 60
RATE_LIMIT_FALLBACK_SLEEP_SECONDS = 30


def _compute_retry_sleep(err) -> float:
    """Read Anthropic's rate-limit headers and compute how long to sleep before retrying.

    Falls back to RATE_LIMIT_FALLBACK_SLEEP_SECONDS if the headers are absent
    or unparseable. Never sleeps more than RATE_LIMIT_MAX_SLEEP_SECONDS so a
    misconfigured retry header can't stall the meeting indefinitely.
    """
    headers = getattr(getattr(err, "response", None), "headers", {}) or {}
    retry_after = headers.get("retry-after")
    if retry_after:
        try:
            return min(max(float(retry_after), 1.0), RATE_LIMIT_MAX_SLEEP_SECONDS)
        except (TypeError, ValueError):
            pass
    reset = headers.get("anthropic-ratelimit-input-tokens-reset")
    if reset:
        try:
            reset_dt = datetime.fromisoformat(reset.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            delta = (reset_dt - now).total_seconds() + 1.0  # +1s safety margin
            return max(1.0, min(delta, RATE_LIMIT_MAX_SLEEP_SECONDS))
        except (TypeError, ValueError):
            pass
    return RATE_LIMIT_FALLBACK_SLEEP_SECONDS


def _log_rate_limit(err, *, attempt: int, retrying: bool, sleep_s: float = 0.0):
    """One canonical log line for 429s — diagnostic detail + what we're doing about it."""
    headers = getattr(getattr(err, "response", None), "headers", {}) or {}
    itpm = headers.get("anthropic-ratelimit-input-tokens-limit", "?")
    reset = headers.get("anthropic-ratelimit-input-tokens-reset", "?")
    if retrying:
        log.warning(
            "Anthropic rate limit hit (HTTP 429, attempt %d/%d). Tier caps input tokens "
            "at %s/min; resets at %s. Sleeping %.0fs and retrying.",
            attempt, RATE_LIMIT_MAX_RETRIES + 1, itpm, reset, sleep_s,
        )
    else:
        log.error(
            "Anthropic rate limit hit (HTTP 429, exhausted %d retries). Tier caps input "
            "tokens at %s/min; resets at %s. Raise your tier at console.anthropic.com "
            "→ Plans & Billing, or reduce the MCP tool count in config.yaml (each tool "
            "schema costs input tokens on every call).",
            RATE_LIMIT_MAX_RETRIES, itpm, reset,
        )


def _neutral_to_anthropic_messages(messages):
    """Translate neutral history into Anthropic's messages list.

    System prompt is passed separately and is NOT part of this list.
    """
    out = []
    for m in messages:
        role = m["role"]
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            tool_calls = m.get("tool_calls") or []
            if tool_calls:
                blocks = []
                text = m.get("content")
                if text:
                    blocks.append({"type": "text", "text": text})
                for tc in tool_calls:
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.args,
                    })
                out.append({"role": "assistant", "content": blocks})
            else:
                out.append({"role": "assistant", "content": m.get("content") or ""})
        elif role == "tool_result":
            out.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": m["tool_call_id"],
                    "content": m["content"],
                }],
            })
        else:
            raise ValueError(f"unknown neutral message role: {role!r}")
    return out


def _openai_tools_to_anthropic(tools):
    """Translate OpenAI-function-calling tool schemas to Anthropic's format.

    Marks the last tool with cache_control so Anthropic caches the entire
    tool-schema block (plus the system prompt that precedes it). Tool
    schemas are static across requests, so this turns a ~20k-token
    per-call payload into ~2k-token cache reads after the first call.
    """
    out = []
    for t in tools:
        fn = t["function"]
        out.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    if out:
        out[-1] = {**out[-1], "cache_control": {"type": "ephemeral"}}
    return out


def _anthropic_response_to_neutral(response):
    """Translate an Anthropic Messages API response into a ProviderResponse."""
    text_parts = []
    tool_calls = []
    for block in response.content:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_parts.append(block.text)
        elif btype == "tool_use":
            tool_calls.append(ToolCall(
                id=block.id,
                name=block.name,
                args=dict(block.input) if block.input else {},
            ))
    text = "".join(text_parts) if text_parts else None

    raw_stop = getattr(response, "stop_reason", None)
    if raw_stop == "tool_use":
        stop_reason = "tool_use"
    elif raw_stop == "end_turn" or raw_stop == "stop_sequence":
        stop_reason = "end"
    elif raw_stop == "max_tokens":
        stop_reason = "length"
    else:
        stop_reason = "other"

    return ProviderResponse(text=text, tool_calls=tool_calls, stop_reason=stop_reason)


def _is_context_overflow(err):
    msg = str(err).lower()
    return "prompt is too long" in msg or "context" in msg and "too long" in msg


class AnthropicProvider(LLMProvider):
    def __init__(self, client):
        self._client = client

    def complete(self, system, messages, model, max_tokens, tools=None):
        kwargs = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": _neutral_to_anthropic_messages(messages),
        }
        if system:
            # Wrap system as a content block with cache_control so it
            # becomes part of the cached prefix (system + tools).
            kwargs["system"] = [{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }]
        if tools:
            kwargs["tools"] = _openai_tools_to_anthropic(tools)
            # Our pipeline feeds back one tool_result at a time; parallel
            # tool_use blocks would leave later ids orphaned and trip a 400.
            # Mirror OpenAI's parallel_tool_calls=False. Revisit post-MVP.
            kwargs["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
        t_start = time.monotonic()
        for attempt in range(1, RATE_LIMIT_MAX_RETRIES + 2):
            try:
                response = self._client.messages.create(**kwargs)
                break
            except anthropic.BadRequestError as e:
                if _is_context_overflow(e):
                    raise ContextOverflowError() from e
                raise
            except anthropic.RateLimitError as e:
                if attempt <= RATE_LIMIT_MAX_RETRIES:
                    sleep_s = _compute_retry_sleep(e)
                    _log_rate_limit(e, attempt=attempt, retrying=True, sleep_s=sleep_s)
                    time.sleep(sleep_s)
                    continue
                _log_rate_limit(e, attempt=attempt, retrying=False)
                raise

        elapsed = time.monotonic() - t_start
        usage = getattr(response, "usage", None)
        if usage is not None:
            cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
            cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
            inp = getattr(usage, "input_tokens", 0) or 0
            out_tok = getattr(usage, "output_tokens", 0) or 0
            total_in = inp + cache_read + cache_create
            cache_pct = (cache_read * 100 // total_in) if total_in else 0
            log.info(
                f"TIMING llm_call={elapsed:.1f}s "
                f"TOKENS in={inp} cache_read={cache_read} cache_create={cache_create} "
                f"out={out_tok} cache_hit={cache_pct}%"
            )

        return _anthropic_response_to_neutral(response)

    def complete_streaming(
        self, system, messages, model, max_tokens, tools=None, on_paragraph=None,
    ):
        if on_paragraph is None:
            return self.complete(system, messages, model, max_tokens, tools=tools)

        kwargs = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": _neutral_to_anthropic_messages(messages),
        }
        if system:
            kwargs["system"] = [{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }]
        if tools:
            kwargs["tools"] = _openai_tools_to_anthropic(tools)
            kwargs["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}

        t_start = time.monotonic()
        t_first_token = None
        t_first_flush = None

        # Stuck-LLM watchdog: a one-shot informational ping if no token has
        # arrived by LLM_STUCK_THRESHOLD_SECONDS. Healthy streaming calls
        # produce their first token within a few seconds; this only fires
        # when Anthropic is genuinely sitting on the request. Posts via
        # `on_paragraph` so it appears in chat just like a streamed paragraph.
        watchdog_fired = [False]

        def _stuck_watchdog():
            if t_first_token is None:
                watchdog_fired[0] = True
                try:
                    # Trailing newlines get stripped by Meet's DOM on read-back,
                    # which broke own-message text-match detection — the watchdog
                    # post got reprocessed as a user message and triggered a
                    # cascade. Plain text avoids the mismatch.
                    on_paragraph(
                        "Anthropic is taking longer than usual to respond — hang tight."
                    )
                except Exception as e:  # never let watchdog errors bring down the call
                    log.warning(f"stuck-LLM watchdog post failed: {e}")

        watchdog = threading.Timer(
            config.LLM_STUCK_THRESHOLD_SECONDS, _stuck_watchdog,
        )
        watchdog.daemon = True
        watchdog.start()

        # 429s in the streaming path occur at request initiation (input-token
        # check happens before any text is produced), so retrying here cannot
        # cause partial paragraphs to post twice — buffer is reset per attempt.
        try:
            for attempt in range(1, RATE_LIMIT_MAX_RETRIES + 2):
                buffer = ""
                try:
                    with self._client.messages.stream(**kwargs) as stream:
                        for text in stream.text_stream:
                            if not text:
                                continue
                            if t_first_token is None:
                                t_first_token = time.monotonic()
                            buffer += text
                            if "\n\n" in buffer:
                                if t_first_flush is None:
                                    t_first_flush = time.monotonic()
                                buffer = flush_paragraphs(buffer, on_paragraph)
                        final = stream.get_final_message()
                    break
                except anthropic.BadRequestError as e:
                    if _is_context_overflow(e):
                        raise ContextOverflowError() from e
                    raise
                except anthropic.RateLimitError as e:
                    if attempt <= RATE_LIMIT_MAX_RETRIES:
                        sleep_s = _compute_retry_sleep(e)
                        _log_rate_limit(e, attempt=attempt, retrying=True, sleep_s=sleep_s)
                        time.sleep(sleep_s)
                        continue
                    _log_rate_limit(e, attempt=attempt, retrying=False)
                    raise
        finally:
            # Cancel even on exception paths so a failed call doesn't leave
            # the watchdog ticking and fire a stale "still working" message.
            watchdog.cancel()

        if buffer.strip():
            flush_paragraphs(buffer, on_paragraph, force_final=True)

        elapsed = time.monotonic() - t_start
        ttft = (t_first_token - t_start) if t_first_token else None
        first_flush = (t_first_flush - t_start) if t_first_flush else None
        usage = getattr(final, "usage", None)
        if usage is not None:
            cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
            cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
            inp = getattr(usage, "input_tokens", 0) or 0
            out_tok = getattr(usage, "output_tokens", 0) or 0
            total_in = inp + cache_read + cache_create
            cache_pct = (cache_read * 100 // total_in) if total_in else 0
            ttft_str = f"{ttft:.1f}s" if ttft is not None else "n/a"
            flush_str = f"{first_flush:.1f}s" if first_flush is not None else "n/a"
            log.info(
                f"TIMING llm_call={elapsed:.1f}s ttft={ttft_str} first_flush={flush_str} streamed=1 "
                f"TOKENS in={inp} cache_read={cache_read} cache_create={cache_create} "
                f"out={out_tok} cache_hit={cache_pct}%"
            )

        return _anthropic_response_to_neutral(final)

    def complete_stream(self, system, messages, model, max_tokens):
        kwargs = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": _neutral_to_anthropic_messages(messages),
        }
        if system:
            kwargs["system"] = system
        with self._client.messages.stream(**kwargs) as stream:
            for text in stream.text_stream:
                if text:
                    yield text

    def warmup(self, model):
        self._client.messages.create(
            model=model,
            max_tokens=1,
            messages=[{"role": "user", "content": "hi"}],
        )
