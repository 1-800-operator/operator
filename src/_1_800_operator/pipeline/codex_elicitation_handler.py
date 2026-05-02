"""
Elicitation handler that round-trips Codex command-approval requests through chat.

Plugged into MCPClient via set_elicitation_handler("codex", handler) once the
Codex MCP server is running. Invoked from MCPClient's event-loop pump (via an
executor thread) on every inbound `elicitation/create` request from the codex
server.

Codex emits these whenever its agent loop wants to run a shell command that
isn't covered by codex's internal safe-command allowlist (writes, network
calls, anything classified as `parsed_cmd.type=unknown`). Read-class commands
(cat, grep, ls, find, ...) are allowed by codex internally and never reach
this handler — see debug/codex_spike/PHASE_0_FINDINGS.md for the taxonomy
probe results.

Architecturally simpler than PermissionChatHandler: no fnmatch/glob lists,
no per-tool vocabulary. Codex commands are typed `unknown` from operator's
perspective; the only user-facing knob is the `approval-policy` per call
(set on the codex MCP server config). What this handler does:

  1. Format the elicitation as a chat-friendly prompt — `codex_command` argv
     + `codex_cwd`, voice-aware (plain summary vs technical full argv).
  2. Block on chat reply via the same `runner._await_reply`-shape polling
     used by PermissionChatHandler.
  3. Map the reply to a decision:
       - is_yes_always(reply)  →  {"decision": {"approved_execpolicy_amendment":
                                   {"proposed_execpolicy_amendment": <argv>}}}
                                  Codex remembers this exact argv for the
                                  rest of the thread; identical follow-up
                                  commands skip elicitation entirely.
       - is_yes(reply)         →  {"decision": "approved"}
                                  Single-shot — next identical call still
                                  round-trips to chat.
       - anything else / null  →  {"decision": "abort"}

Threading: MCPClient's `_dispatch_elicitation` runs this on the asyncio
executor pool. Reads chat directly from connector.read_chat() while waiting
and claims consumed messages by adding their IDs to runner._seen_ids — so
the main polling loop doesn't re-feed the user's "ok" to the LLM. Same
contract as PermissionChatHandler.
"""
import logging
import threading
import time

from _1_800_operator import config
from _1_800_operator.pipeline.confirmation import is_yes, is_yes_always

log = logging.getLogger(__name__)


# Hard upper bound on how long a single elicitation can wait for a user
# reply. Generous — meetings can pause, the user can be talking, the user
# can be reading. Past this we abort so the codex agent loop isn't stuck.
REPLY_TIMEOUT_SECONDS = 600
POLL_INTERVAL = 0.5

# Cap on the verbatim command rendered into chat. Most shell commands are
# short; pathological cases (huge piped expressions, embedded base64 blobs)
# get head…tail truncation so chat doesn't break.
COMMAND_RENDER_MAX = 400
COMMAND_RENDER_HEAD = 180
COMMAND_RENDER_TAIL = 180


def _render_command(argv):
    """Render the codex_command argv as a single shell-style line.

    Codex sends `["/bin/zsh", "-lc", "<the actual command>"]` for nearly all
    cases. We strip the zsh wrapper and show just the user-meaningful inner
    command in plain voice; technical voice keeps the full argv.
    """
    if not isinstance(argv, list) or not argv:
        return repr(argv)
    if (
        len(argv) >= 3
        and argv[0].endswith("zsh")
        and argv[1] in ("-lc", "-c")
    ):
        body = argv[2]
    else:
        body = " ".join(argv)
    if len(body) > COMMAND_RENDER_MAX:
        head = body[:COMMAND_RENDER_HEAD]
        tail = body[-COMMAND_RENDER_TAIL:]
        body = f"{head}…{tail}"
    return body


def _format_confirmation(params):
    """Render the codex elicitation as a neutral approval challenge.

    Same shape regardless of voice — operator emits a sterile prompt; the
    bot's persona (set via system_prompt) is responsible for the
    conversational preamble in chat before this prompt arrives.

    Voice modes pick detail level:
      plain     — single command line + cwd, zsh wrapper stripped.
      technical — full argv joined, plus the full cwd path.
    """
    cwd = params.get("codex_cwd", "?")
    argv = params.get("codex_command") or []
    voice = getattr(config, "VOICE", "plain")
    if voice == "technical":
        cmd = " ".join(repr(a) for a in argv)
        return f"Run codex command?\n  argv: {cmd}\n  cwd:  {cwd}\nOK?"
    body = _render_command(argv)
    return f"Run? `{body}` in `{cwd}`\nOK?"


class CodexElicitationChatHandler:
    """Callable that resolves codex `elicitation/create` decisions via chat.

    Construct once per meeting and pass to
    MCPClient.set_elicitation_handler("codex", handler). Every elicitation
    blocks on a chat round-trip. No auto-approve list — codex's internal
    safe-command allowlist already filters read-class operations before they
    reach this handler.

    The `runner` reference is needed for two things only:
      - runner._send: serialized chat send that records the message in
        _own_messages so we don't re-read our own confirmation prompt.
      - runner._seen_ids / runner._own_messages: claim consumed user replies
        so the main loop doesn't feed them to the LLM.
    """

    def __init__(self, connector, runner):
        self._connector = connector
        self._runner = runner
        # Serialize concurrent requests. Codex's session is single-threaded
        # per turn, but defense-in-depth against future parallel-tool paths.
        self._lock = threading.Lock()

    def __call__(self, server_name, params):
        # Only "exec-approval" is observed today (codex's command-approval
        # surface). Future kinds — "apply-patch-approval" etc. — would
        # land here too; we treat them generically through the same
        # chat round-trip until proven otherwise. Log the kind so a
        # surprising one shows up in /tmp/operator.log.
        kind = params.get("codex_elicitation", "?")
        log.info(f"CodexElicitationChatHandler: kind={kind!r} server={server_name!r}")
        with self._lock:
            return self._round_trip(params)

    def _round_trip(self, params):
        prompt = _format_confirmation(params)
        try:
            self._runner._send(prompt, kind="confirmation")
        except Exception as e:
            log.error(f"CodexElicitationChatHandler: failed to post confirmation: {e}")
            return {"decision": "abort"}

        reply = self._await_reply(REPLY_TIMEOUT_SECONDS)
        if reply is None:
            log.warning(
                f"CodexElicitationChatHandler: no reply within "
                f"{REPLY_TIMEOUT_SECONDS}s — aborting"
            )
            return {"decision": "abort"}

        if is_yes_always(reply):
            argv = params.get("codex_command") or []
            log.info(
                f"CodexElicitationChatHandler: approved-with-amendment "
                f"(reply={reply!r})"
            )
            return {
                "decision": {
                    "approved_execpolicy_amendment": {
                        "proposed_execpolicy_amendment": argv,
                    }
                }
            }
        if is_yes(reply):
            log.info(f"CodexElicitationChatHandler: approved (reply={reply!r})")
            return {"decision": "approved"}
        log.info(f"CodexElicitationChatHandler: aborted (reply={reply!r})")
        return {"decision": "abort"}

    def _await_reply(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                messages = self._connector.read_chat()
            except Exception as e:
                log.warning(f"CodexElicitationChatHandler: read_chat failed: {e}")
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
                if sender and sender.lower() == config.AGENT_NAME.lower():
                    continue
                if not sender and text in self._runner._own_messages:
                    continue
                if msg_id:
                    self._runner._seen_ids.add(msg_id)
                return text
            time.sleep(POLL_INTERVAL)
        return None
