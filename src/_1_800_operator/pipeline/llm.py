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
import re
from _1_800_operator import config
from _1_800_operator.pipeline.meeting_record import MeetingRecord

log = logging.getLogger(__name__)


def _sanitize_speaker(speaker: str) -> str:
    # Drop attribute-breaking chars from the attacker-controlled display name.
    # Used by intro() before participant names hit the prompt; Meet display
    # names are user-controlled and could otherwise carry prompt-injection
    # payloads.
    return re.sub(r'[<>"\'&]', "", speaker)[:64]

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

        Also forwards the record's JSONL path to providers that expose
        `set_meeting_record_path` — used by claude_cli to register a
        bundled transcript MCP server pointing at the live file.
        """
        self._record = record
        setter = getattr(self._provider, "set_meeting_record_path", None)
        if callable(setter) and getattr(record, "path", None) is not None:
            try:
                setter(record.path)
            except Exception as e:
                log.warning(f"LLM: provider rejected meeting record path: {e}")

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
        """Fire a 1-token request to establish the TCP/TLS connection pool."""
        try:
            self._provider.warmup("")
            log.info("LLM warmup complete")
        except Exception as e:
            log.warning(f"LLM warmup failed (non-fatal): {e}")

    def intro(
        self,
        *,
        participant_names: list[str] | None = None,
        participant_count: int = 0,
    ) -> str:
        """Generate a self-introduction for the chat panel on join.

        Sent with no message history — the bot is greeting the room, not
        reacting to it. Relies on the system prompt already carrying skills,
        MCP hints, and MCP status (injected during startup) so the model has
        full visibility into what it can actually do this session.

        `participant_names` and `participant_count` are best-effort signals
        from the connector. When names are available, the bot can address
        people directly; when only count is available, the bot at least
        knows whether it's a 1-on-1 or a group.
        """
        # Filter out the bot's own tile from any scraped names. Match
        # case-insensitively because Meet sometimes uppercases the display
        # name in the participants panel. Sanitize the remaining names
        # before they hit the prompt — Meet display names are attacker-
        # controlled (anyone can set their display name to a prompt-
        # injection payload), and intro generation runs on the same
        # system prompt as regular turns. Drop attribute-breaking chars
        # and cap length, same as _sanitize_speaker does for captions.
        own_name_lower = config.AGENT_NAME.lower()
        others: list[str] = []
        for n in (participant_names or []):
            stripped = (n or "").strip()
            if not stripped or stripped.lower() == own_name_lower:
                continue
            sanitized = _sanitize_speaker(stripped)
            if sanitized:
                others.append(sanitized)
        if others:
            if len(others) == 1:
                room_ctx = f"You are joining a 1-on-1 with {others[0]}. "
            elif len(others) <= 4:
                room_ctx = f"You are joining a meeting with: {', '.join(others)}. "
            else:
                sample = ", ".join(others[:3])
                room_ctx = (
                    f"You are joining a meeting with {len(others)} people, "
                    f"including {sample}. "
                )
        elif participant_count > 1:
            room_ctx = (
                f"You are joining a meeting with {participant_count - 1} "
                f"other participants. "
            )
        elif participant_count == 1:
            room_ctx = "You are joining a 1-on-1 with one other person. "
        else:
            room_ctx = ""
        prompt = (
            f"{room_ctx}"
            f"Introduce yourself in chat. Your name is "
            f"\"{config.AGENT_NAME}\" — use that exact name; do not invent "
            f"a different one.\n"
            "Constraints:\n"
            "- Keep it tight: two mid-size sentences, or up to three short "
            "ones. Aim for ~30 words total; never exceed 45.\n"
            "- After the greeting, cover two things only: who you are "
            "(one line), and 1–2 brief use cases framed as 'I can …'. "
            "No third use case, no elaboration.\n"
            "- Focus on outcomes, not mechanisms. Never name specific tools, "
            "MCP servers, or skill names.\n"
            "- No offers to help, no questions back. Lead with substance "
            "after the greeting.\n"
            "- Plain text. No markdown, no bullet block, no headings."
        )
        response = self._provider.complete(
            system="",
            messages=[{"role": "user", "content": prompt}],
            model="",
            max_tokens=self._max_tokens,
        )
        text = (response.text or "").strip()
        log.info(f"LLM intro generated ({len(text)} chars): \"{text[:80]}\"")
        return text

