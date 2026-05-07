"""
Permission handler that round-trips PreToolUse decisions through meeting chat.

Plugged into ClaudeCLIProvider via set_permission_handler(). Invoked from
the provider's pump thread on every PreToolUse event. Tools matching an
entry in `auto_approve` (passed at construction) are approved silently;
tools matching `always_ask` — and anything on neither list — block until
the user replies in chat (yes/ok/sure => allow, anything else => deny
with the user's text as the reason). always_ask is checked first so an
explicit deny pattern beats a broad allow pattern.

Per Phase 14.19.8 the natural-language prompt is authored by the
inner-claude model in its pre-tool narration, NOT by this handler — we
just block on chat for the next user message and treat that as the
decision.

Entries are fnmatch glob patterns. Literal tool names (`Read`, `Bash`)
match exactly; entries containing `*`, `?`, or `[` match by glob —
`mcp__sentry__get_*` covers every read tool from the Sentry MCP server.

Threading: this runs on the provider's pump thread. The handler reads
chat directly from connector.read_chat() while waiting for a reply and
claims consumed messages by adding their IDs to runner._seen_ids — so
the main polling loop doesn't re-feed the user's "ok" to the LLM.
"""
import fnmatch
import logging
import threading
import time

from _1_800_operator import config
from _1_800_operator.pipeline.confirmation import is_yes as _is_yes

log = logging.getLogger(__name__)


_GLOB_CHARS = ("*", "?", "[")


def _matches_any(tool_name, patterns):
    """Return True if tool_name matches any entry in patterns.

    Bare names (no glob characters) match exactly — same shape as the
    pre-pattern set-membership check. Entries with `*`, `?`, or `[` are
    fnmatch globs. Empty / None patterns is a no-op (False).
    """
    if not patterns:
        return False
    for pat in patterns:
        if not pat:
            continue
        if any(c in pat for c in _GLOB_CHARS):
            if fnmatch.fnmatchcase(tool_name, pat):
                return True
        elif tool_name == pat:
            return True
    return False


# Hard upper bound on how long a single permission request can wait for a
# user reply. Set generous — meetings can pause, the user can be talking,
# read chat slowly. After this we auto-deny so the subprocess isn't stuck.
REPLY_TIMEOUT_SECONDS = 600
POLL_INTERVAL = 0.5

# How recently the user must have said "yes" (as the turn's input) for
# the bridge to treat that affirmation as the approval for an
# immediately-following tool gate. Tight enough that an unrelated "yes"
# from earlier in the meeting can't drift into auto-approving a tool
# the user never intended; loose enough to span the model's typical
# 1–10s reply-to-tool latency on chained MCP calls.
RECENT_YES_WINDOW_SECONDS = 30


class PermissionChatHandler:
    """Callable that resolves PreToolUse decisions via meeting chat round-trip.

    Construct once per meeting and set on ClaudeCLIProvider via
    set_permission_handler(). Auto-approves tools in `auto_approve`,
    asks the user in chat for everything else.

    The `runner` reference is needed for two things only:
      - runner._seen_ids / runner._own_messages: claim consumed user
        replies so the main loop doesn't feed them to the LLM.
      - runner._latest_user_msg / runner._approval_msg_ids_used:
        recent-yes auto-approval (Phase 14.19.8).
    """

    def __init__(self, connector, runner, auto_approve, always_ask):
        self._connector = connector
        self._runner = runner
        # Preserve list ordering so a wizard / config author can layer
        # narrower rules on top of broader globs deterministically.
        self._auto_approve = list(auto_approve or [])
        self._always_ask = list(always_ask or [])
        # Serialize concurrent requests. Tool calls are sequential per
        # turn, but a misbehaving sub-agent or future parallel-tool-use
        # path could fire two — lock makes round-trips strictly ordered.
        self._lock = threading.Lock()

    def __call__(self, tool_name, tool_input):
        # always_ask wins over auto_approve so users can pin a specific
        # deny (e.g. mcp__sentry__analyze_issue_with_seer) on top of a
        # broad allow (mcp__sentry__*).
        if _matches_any(tool_name, self._always_ask):
            with self._lock:
                return self._round_trip(tool_name, tool_input)
        if _matches_any(tool_name, self._auto_approve):
            log.info(f"PermissionChatHandler: auto-approve {tool_name!r}")
            return {
                "permissionDecision": "allow",
                "permissionDecisionReason": "auto-approved by config (auto_approve list)",
            }
        with self._lock:
            return self._round_trip(tool_name, tool_input)

    def _round_trip(self, tool_name, tool_input):
        # Phase 14.19.8 — the natural-language question is authored by the
        # inner-claude model in its pre-tool narration (steered by
        # claude_cli._PRE_TOOL_VOICE_RULE) and lands in chat via the
        # provider's streaming on_paragraph path BEFORE this handler is
        # invoked. We do not post a templated card; we just block here
        # waiting for the user's next chat message and treat that as the
        # approval/deny.
        #
        # Recent-yes auto-approval. When the user said "yes" (or
        # equivalent) seconds ago AS THE TURN'S INPUT — i.e. their
        # approval was already consumed as the user_text the LLM is
        # currently responding to — there's no fresh chat message coming
        # for this gate; the user already approved and has moved on. We'd
        # otherwise sit forever waiting for a redundant second "yes". So
        # before await_reply, peek at the most recent user message: if
        # it's unambiguously affirmative, within the recency window, and
        # not yet consumed by a prior gate, we auto-allow and mark the
        # message id consumed so chained tool calls within the same turn
        # fall through to the normal await path (the user has to
        # re-approve each subsequent tool — one yes, one tool).
        latest = getattr(self._runner, "_latest_user_msg", None)
        if latest is not None:
            msg_id, text, observed_at = latest
            consumed = getattr(self._runner, "_approval_msg_ids_used", set())
            age_s = time.monotonic() - observed_at
            if (
                msg_id not in consumed
                and age_s <= RECENT_YES_WINDOW_SECONDS
                and _is_yes(text)
            ):
                consumed.add(msg_id)
                log.info(
                    f"PermissionChatHandler: auto-allow {tool_name!r} "
                    f"from recent user yes (age={age_s:.1f}s, text={text!r})"
                )
                return {
                    "permissionDecision": "allow",
                    "permissionDecisionReason": (
                        f"user said yes {age_s:.0f}s ago: {text!r}"
                    ),
                }

        log.info(f"PermissionChatHandler: awaiting user reply for {tool_name!r}")
        reply = self._await_reply(REPLY_TIMEOUT_SECONDS)
        if reply is None:
            log.warning(
                f"PermissionChatHandler: no reply for {tool_name!r} within {REPLY_TIMEOUT_SECONDS}s — denying"
            )
            return {
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"no chat reply within {REPLY_TIMEOUT_SECONDS}s; defaulting to deny"
                ),
            }
        if _is_yes(reply):
            return {
                "permissionDecision": "allow",
                "permissionDecisionReason": f"user approved in chat: {reply!r}",
            }
        return {
            "permissionDecision": "deny",
            "permissionDecisionReason": f"user replied (treated as deny): {reply!r}",
        }

    def _await_reply(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                messages = self._connector.read_chat()
            except Exception as e:
                log.warning(f"PermissionChatHandler: read_chat failed: {e}")
                time.sleep(POLL_INTERVAL)
                continue
            for msg in messages:
                msg_id = msg.get("id", "")
                text = (msg.get("text") or "").strip()
                sender = (msg.get("sender") or "").strip()
                if not text:
                    continue
                if msg_id and msg_id in self._runner._seen_ids:
                    continue
                # Skip our own echoes (matches chat_runner._loop logic)
                if sender and sender.lower() == config.AGENT_NAME.lower():
                    continue
                if not sender and text in self._runner._own_messages:
                    continue
                # New user reply — claim it so the main loop doesn't
                # re-feed it to the LLM as a normal message.
                if msg_id:
                    self._runner._seen_ids.add(msg_id)
                log.info(f"PermissionChatHandler: reply received: {text!r}")
                return text
            time.sleep(POLL_INTERVAL)
        return None
