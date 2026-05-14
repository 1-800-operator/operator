"""
LLM integration for Operator.

Wraps a provider-agnostic chat interface. Conversation history lives in a
MeetingRecord (JSONL on disk + in-memory chat deque) — `ask()` replays the
chat tail on each call.

The provider (claude_cli) owns its own tool loop internally — Operator
never sees tool_use/tool_result events at this layer; they are consumed
inside the subprocess. We just send the user turn in and stream final
text out.
"""
import logging
from _1_800_operator import config
from _1_800_operator.pipeline.meeting_record import MeetingRecord

log = logging.getLogger(__name__)


class LLMClient:
    """Sends prompts to an LLM provider and builds context from a MeetingRecord.

    Typical use:
        record = MeetingRecord(slug="pgy-qauk-frn")
        client = LLMClient(provider, record=record)
        reply = client.ask("What's the plan?")

    If `record` is None, an in-memory MeetingRecord is created automatically.
    """

    def __init__(self, provider, record: MeetingRecord | None = None):
        self._provider = provider
        self._record = record if record is not None else MeetingRecord(slug=None)
        self._max_tokens = config.MAX_TOKENS

    def set_record(self, record: MeetingRecord):
        """Attach (or replace) the MeetingRecord backing this client.

        The transcript MCP discovers the live meeting JSONL via the
        `~/.operator/.current_meeting` marker file written by operator
        at meeting-join (and removed at leave). No per-spawn forwarding
        from operator into the inner-claude subprocess — that was the
        old `--mcp-config` tempfile path, stripped in 14.22.3 because
        it carried harness identity at the spawn layer.
        """
        self._record = record

    def ask(self, message, on_paragraph=None, retry_rate_limits=True):
        """Send a message to the LLM and return the reply.

        Inner-claude carries prior conversation context through `--resume`
        against its on-disk session store, so operator only forwards the
        new user turn. The provider's `messages` arg stays a list for
        provider-neutrality — future providers that want history can build
        it themselves from the meeting record.

        Returns a plain string normally. If `on_paragraph` is provided, the
        provider streams the reply and invokes the callback with each
        completed paragraph as it arrives — the return value is then a dict
        `{"type": "text", "content": ..., "streamed": True}` so the caller
        knows the content has already been posted paragraph-by-paragraph and
        should not re-send it as one blob.
        """
        messages = [{"role": "user", "content": message}]

        log.info(
            f"LLM ask max_tokens={self._max_tokens} "
            f"prompt_chars={len(message)} "
            f"streaming={bool(on_paragraph)}"
        )
        log.debug(f"LLM message: {message}")

        try:
            if on_paragraph is not None:
                response = self._provider.complete_streaming(
                    system="",
                    messages=messages,
                    model="",
                    max_tokens=self._max_tokens,
                    on_paragraph=on_paragraph,
                    retry_rate_limits=retry_rate_limits,
                )
            else:
                response = self._provider.complete(
                    system="",
                    messages=messages,
                    model="",
                    max_tokens=self._max_tokens,
                    retry_rate_limits=retry_rate_limits,
                )
        except Exception as e:
            log.error(f"LLM API call failed: {e}", exc_info=True)
            raise

        reply = response.text
        log.info(f"LLM reply=\"{(reply or '')[:80]}\"")
        if on_paragraph is not None:
            return {
                "type": "text",
                "content": reply,
                "streamed": True,
                "notices": list(response.notices),
            }
        return reply

    def warmup(self):
        """No-op for the per-@mention provider shape.

        Pre-14.22.3 this fired a 1-token request to spawn the long-lived
        claude subprocess so the first real turn didn't pay init cost.
        With per-@mention shellouts there's no persistent subprocess to
        warm — every turn pays its own spawn cost. Kept on the LLMClient
        surface for caller compatibility.
        """
        return None

