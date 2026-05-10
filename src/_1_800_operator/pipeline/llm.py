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

    def _tail_messages(self) -> list[dict]:
        """Build neutral-shape messages from the meeting record tail.

        Chat-only. Captions are accessible to inner-claude on demand via
        the bundled transcript MCP server, so they don't go in the prompt.
        Chat carries a UI continuity expectation captions don't — users
        assume the bot saw earlier chat messages, but won't assume it heard
        ambient room talk unless they reference it explicitly.

        Served from MeetingRecord's in-memory chat deque, not the JSONL —
        so an hour-long meeting with thousands of caption lines on disk
        doesn't pay any per-turn read/parse cost.
        """
        entries = self._record.tail_chat(config.HISTORY_MESSAGES)
        agent = (config.AGENT_NAME or "").lower()
        messages: list[dict] = []
        for e in entries:
            sender = (e.get("sender") or "").strip()
            text = e.get("text", "")
            if sender.lower() == agent:
                messages.append({"role": "assistant", "content": text})
                continue
            first = sender.split()[0] if sender else ""
            content = f"{first}: {text}" if first else text
            messages.append({"role": "user", "content": content})
        return messages

    def _build_messages(self, extra_user_msg: str | None = None) -> list[dict]:
        """tail (chat) + optional trailing user turn."""
        messages = self._tail_messages()
        if extra_user_msg is not None:
            messages.append({"role": "user", "content": extra_user_msg})
        return messages

    def ask(self, message, record=True, on_paragraph=None, retry_rate_limits=True):
        """Send a message to the LLM and return the reply.

        ChatRunner is expected to have appended this message to the meeting
        record already, so it appears once in the tail. If `record` is False,
        the record was NOT pre-populated and we pass `message` as an extra
        trailing user turn without persisting it.

        Returns a plain string normally. If `on_paragraph` is provided, the
        provider streams the reply and invokes the callback with each
        completed paragraph as it arrives — the return value is then a dict
        `{"type": "text", "content": ..., "streamed": True}` so the caller
        knows the content has already been posted paragraph-by-paragraph and
        should not re-send it as one blob.
        """
        if record:
            messages = self._build_messages()
        else:
            messages = self._build_messages(extra_user_msg=message)

        log.info(
            f"LLM ask max_tokens={self._max_tokens} "
            f"messages={len(messages)} prompt_chars={len(message)} "
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
            return {"type": "text", "content": reply, "streamed": True}
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

