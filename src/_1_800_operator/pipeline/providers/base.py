"""
LLM provider interface.

Providers translate between the app's neutral conversation shape and a
specific backend (OpenAI, Anthropic, etc.). All conversation state —
history trimming, tool-result validation, system-prompt assembly —
stays in LLMClient and is expressed in the neutral shape defined here.

Neutral history message shape (what LLMClient stores and passes in):
  {"role": "user", "content": str}
  {"role": "assistant", "content": str}                         # plain text reply
  {"role": "assistant", "content": str|None,
                        "tool_calls": [ToolCall, ...]}          # tool-call turn
  {"role": "tool_result", "tool_call_id": str, "content": str}  # result of a tool

The system prompt is passed as its own `system` argument to complete(),
not as a message with role="system".
"""
import re
from dataclasses import dataclass, field


# Two-or-more consecutive newlines = paragraph boundary. Tightened to
# require at least one is followed by a non-whitespace char on the next
# break, but the simple `\n{2,}` split is what models actually emit.
_PARAGRAPH_BOUNDARY_RE = re.compile(r"\n{2,}")
# Lines made entirely of separator glyphs (`---`, `***`, `===`, `___`,
# `~~~`, mixed) are visual decoration the model uses between paragraphs.
# We drop them so they don't post as their own chat message.
_DECORATION_RE = re.compile(r"^[\s\-=*_~]+$")


def flush_paragraphs(buffer: str, on_paragraph, *, force_final: bool = False) -> str:
    """Flush complete paragraphs from buffer; return the unflushed remainder.

    Used by streaming providers. Splits on `\\n{2,}`, drops empty and
    decoration-only fragments, calls on_paragraph(stripped_text) for the
    rest. If force_final is True, the trailing partial paragraph is
    flushed too (call once at end-of-stream).
    """
    parts = _PARAGRAPH_BOUNDARY_RE.split(buffer)
    if force_final:
        to_flush, remainder = parts, ""
    else:
        # Trailing partial may still be growing — keep buffered.
        to_flush, remainder = parts[:-1], parts[-1]
    for piece in to_flush:
        stripped = piece.strip()
        if not stripped or _DECORATION_RE.match(stripped):
            continue
        on_paragraph(stripped)
    return remainder


class ContextOverflowError(Exception):
    """Raised by a provider when the model reports the context window is exceeded.

    LLMClient catches this and surfaces {"type": "context_overflow"} to callers
    after clearing history.
    """


@dataclass
class ToolCall:
    """A single tool invocation requested by the model.

    args is the already-parsed argument object (dict), not a JSON string.
    Providers are responsible for parsing whatever their SDK returns.
    """
    id: str
    name: str
    args: dict


@dataclass
class ProviderResponse:
    """Neutral response returned by LLMProvider.complete().

    stop_reason values:
      "end"       — model finished a normal text reply
      "tool_use"  — model wants to call one or more tools
      "length"    — hit max_tokens
      "other"     — anything else (content filter, etc.)
    """
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end"


class LLMProvider:
    """Abstract LLM transport.

    Subclasses translate the neutral inputs/outputs defined in this module
    to and from a specific backend (OpenAI, Anthropic, etc.). Callers pass
    the system prompt separately from the neutral `messages` list and
    receive a ProviderResponse.
    """

    def complete(self, system, messages, model, max_tokens, tools=None):
        """Send a chat completion and return a ProviderResponse.

        Args:
          system: system prompt string (may be empty)
          messages: neutral history list (see module docstring for shape)
          model: backend-specific model id
          max_tokens: int
          tools: optional list of tool schemas in OpenAI-function-calling shape
                 (providers translate to their own schema format if needed)

        Raises ContextOverflowError if the model's context window is exceeded.
        """
        raise NotImplementedError

    def complete_stream(self, system, messages, model, max_tokens):
        """Stream a plain-text completion. Yields text chunks (str) as they arrive.

        Tool-call streaming is not part of this interface.
        """
        raise NotImplementedError

    def complete_streaming(
        self, system, messages, model, max_tokens, tools=None, on_paragraph=None,
    ):
        """Same contract as complete(), but flushes paragraphs as they arrive.

        on_paragraph(text: str) is invoked for each completed paragraph (split
        on `\\n{2,}` boundaries, decoration-only fragments dropped) — including
        the trailing partial at end-of-stream. Returns a ProviderResponse with
        the FULL accumulated text in `text` so the caller can record it; tool
        calls and stop_reason follow the same shape as complete().

        If on_paragraph is None, providers may fall back to non-streaming
        behaviour. Default implementation does exactly that.
        """
        return self.complete(system, messages, model, max_tokens, tools=tools)

    def warmup(self, model):
        """Fire a 1-token request to warm the TCP/TLS connection pool."""
        raise NotImplementedError
