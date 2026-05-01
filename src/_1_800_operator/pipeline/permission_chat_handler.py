"""
Permission handler that round-trips PreToolUse decisions through meeting chat.

Plugged into ClaudeCLIProvider via set_permission_handler(). Invoked from
the provider's pump thread on every PreToolUse event. Tools matching an
entry in config.PERMISSIONS_AUTO_APPROVE are approved silently; tools
matching config.PERMISSIONS_ALWAYS_ASK — and anything on neither list —
post a confirmation prompt to chat and block until the user replies
(yes/ok/sure => allow, anything else => deny with the user's text as the
reason). always_ask is checked first so an explicit deny pattern beats a
broad allow pattern.

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
import re
import threading
import time

from _1_800_operator import config

log = logging.getLogger(__name__)


_GLOB_CHARS = ("*", "?", "[")


def _disabled_mcp_for_cli_tool(tool_name: str) -> str | None:
    """For Claude CLI's `mcp__<server>__<tool>` naming, return the operator
    overlay key of the disabled MCP if `tool_name`'s server prefix matches
    one, else None.

    Two name conventions to bridge:
      - JSON-keyed servers (sentry, test-mcp, MyMCP) keep their JSON key as
        operator's overlay key. The CLI's tool prefix uses underscores in
        place of any non-alphanumeric, so `test-mcp` → tool prefix
        `test_mcp` while overlay key stays `test-mcp`. Direct lookup catches
        keys that survive intact (sentry, MyMCP); fallback lookup with
        `_`→`-` + lowercase catches the hyphen/underscore swap.
      - claude.ai-hosted servers come from `claude mcp list` display names
        (e.g., "claude.ai Gmail") which operator slugifies to
        `claude-ai-gmail`. The CLI's tool prefix is `claude_ai_Gmail`. The
        `_`→`-` + lowercase normalization handles this.

    Direct match runs first to avoid collapsing case-sensitive distinct
    JSON keys (`MyMCP` vs `mymcp`).
    """
    if not tool_name.startswith("mcp__"):
        return None
    parts = tool_name.split("__", 2)
    if len(parts) < 3:
        return None
    server_raw = parts[1]
    disabled = getattr(config, "DISABLED_MCP_SERVERS", None) or {}
    if server_raw in disabled:
        return server_raw
    server_norm = server_raw.lower().replace("_", "-")
    if server_norm in disabled:
        return server_norm
    return None


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

# Maximum length of a single tool argument value rendered into the chat
# confirmation prompt. Long values are head…tail-truncated so a 50KB Write
# `content` argument doesn't blow up the chat panel.
ARG_RENDER_MAX = 200
ARG_RENDER_HEAD = 90
ARG_RENDER_TAIL = 90


_AFFIRM_PATTERNS = [
    re.compile(r"\b(yes|ok|okay|sure|approve|approved|confirmed|yep|yeah|y)\b", re.I),
]


def _is_yes(text):
    """Best-effort yes detection, modeled on chat_runner._handle_confirmation.

    Mirrors the negation gate added to _handle_confirmation: a reply that
    pairs an affirmative token with a negation ("ok no don't do that",
    "yes don't") must NOT auto-approve. Same semantics on both confirmation
    surfaces (track-B chat_runner + track-A permission handler) keeps the
    user-facing contract consistent regardless of which path they're on.
    """
    lower = text.lower().strip()
    has_negative = (
        re.search(r"\b(no|nope|nah|stop|cancel)\b", lower) is not None
        or "don't" in lower
        or "dont" in lower
        or "do not" in lower
    )
    if has_negative:
        return False
    if "go ahead" in lower or "do it" in lower:
        return True
    return any(p.search(lower) for p in _AFFIRM_PATTERNS)


def _human_size(n):
    """Compact byte-size: '845 B', '12.3 KB', '4.2 MB'."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


# Field names whose values are *imperative* — they describe what's about
# to happen and the user needs to see them verbatim to make a sensible
# yes/no decision. Same principle as Bash command staying verbatim.
# Length-only collapsing rules don't apply to these.
_IMPERATIVE_FIELD_NAMES = {
    "url", "path", "file_path", "command",
    "query", "pattern", "search",
    "notebook_path",
}
_IMPERATIVE_MAX_LEN = 1000  # cap so a pathological case can't blow up chat


def _show_imperative(value):
    """Render an imperative field value verbatim, with a generous cap.

    URLs / paths / commands almost never legitimately exceed 1KB; if
    they do, head…tail truncate so chat doesn't break, but never
    collapse to a size hint (the safety check needs the literal value).
    """
    s = value if isinstance(value, str) else repr(value)
    if len(s) > _IMPERATIVE_MAX_LEN:
        head = s[: _IMPERATIVE_MAX_LEN // 2 - 1]
        tail = s[-(_IMPERATIVE_MAX_LEN // 2 - 1):]
        s = f"{head}…{tail}"
    return s


def _format_terse(tool_name, args):
    """One-line summary that hides bulk content but keeps imperative fields.

    Bash commands are NEVER summarized — the user's safety check depends
    on seeing the literal command. Other tools collapse content/blob
    fields into size hints.
    """
    if tool_name == "Bash":
        cmd = args.get("command", "")
        if len(cmd) > 300:
            cmd = cmd[:290] + "…"
        return f"Bash: {cmd}"
    # Read-only / discovery tools (auto-approved by default — these
    # surface mostly via the progress narrator). Keep names short and
    # lead with what the user cares about: which file/pattern.
    if tool_name == "Read":
        return f"Read {args.get('file_path', '?')}"
    if tool_name == "Grep":
        pat = args.get("pattern", "?")
        path = args.get("path", "")
        return f"Grep {pat!r}" + (f" in {path}" if path else "")
    if tool_name == "Glob":
        return f"Glob {args.get('pattern', '?')}"
    if tool_name == "LS":
        return f"LS {args.get('path', '?')}"
    if tool_name == "WebSearch":
        return f"WebSearch {args.get('query', '?')}"
    if tool_name == "Write":
        path = args.get("file_path", "?")
        size = _human_size(len(args.get("content") or ""))
        return f"Write {path} ({size})"
    if tool_name == "Edit":
        path = args.get("file_path", "?")
        return f"Edit {path}"
    if tool_name == "MultiEdit":
        path = args.get("file_path", "?")
        n = len(args.get("edits") or [])
        return f"MultiEdit {path} ({n} hunks)"
    if tool_name == "NotebookEdit":
        path = args.get("notebook_path", "?")
        return f"NotebookEdit {path}"
    if tool_name == "WebFetch":
        url = args.get("url", "?")
        prompt = (args.get("prompt") or "").strip()
        if len(prompt) > 80:
            prompt = prompt[:77] + "…"
        return f"WebFetch {url} — {prompt}" if prompt else f"WebFetch {url}"
    if tool_name == "Task":
        desc = args.get("description") or args.get("prompt") or ""
        if len(desc) > 120:
            desc = desc[:117] + "…"
        return f"Task: {desc}" if desc else "Task (no description)"
    # Unknown tool — compact fallback. Imperative fields (url/path/command/
    # …) are shown verbatim regardless of length: they describe *what* the
    # tool will do, and hiding them defeats the safety check. Bulky payload
    # fields collapse to size hints. Anything short renders verbatim.
    parts = []
    for k, v in args.items():
        if k in _IMPERATIVE_FIELD_NAMES:
            parts.append(f"{k}={_show_imperative(v)}")
            continue
        r = v if isinstance(v, str) else repr(v)
        if len(r) > 80:
            parts.append(f"{k}=({_human_size(len(r))})")
        else:
            parts.append(f"{k}={r}")
    body = ", ".join(parts)
    return f"{tool_name}: {body}" if body else tool_name


def _format_verbose(tool_name, args):
    """Verbatim parameter dump with head…tail truncation for long values."""
    if not args:
        body = "  (no arguments)"
    else:
        lines = []
        for k, v in args.items():
            r = v if isinstance(v, str) else repr(v)
            if len(r) > ARG_RENDER_MAX:
                head = r[:ARG_RENDER_HEAD]
                tail = r[-ARG_RENDER_TAIL:]
                r = f"{head}…{tail}"
            lines.append(f"  • {k}: {r}")
        body = "\n".join(lines)
    return f"Run {tool_name}?\n{body}\nOK?"


def _format_confirmation(tool_name, tool_input):
    """Render the tool call as a neutral approval challenge.

    Same shape regardless of voice — operator emits a sterile
    machine-style prompt; the bot's persona (set via personality +
    ground_rules) is responsible for the conversational preamble in
    chat before this prompt arrives. That keeps customization (pirate
    voice, Spanish, etc.) cleanly in prompt territory and out of
    Python templating.

    The two voice modes only choose how much detail to show:
      plain     — one-line summary that hides bulk content (Write
                  body, MultiEdit edits) and keeps imperative fields
                  (Bash command, file paths, URLs) verbatim.
      technical — full parameter dump with head…tail truncation, for
                  power users who want byte-level safety review.
    """
    args = tool_input or {}
    voice = getattr(config, "VOICE", "plain")
    if voice == "technical":
        return _format_verbose(tool_name, args)
    return f"Run? {_format_terse(tool_name, args)}\nOK?"


class PermissionChatHandler:
    """Callable that resolves PreToolUse decisions via meeting chat round-trip.

    Construct once per meeting and set on ClaudeCLIProvider via
    set_permission_handler(). Auto-approves tools in `auto_approve`,
    asks the user in chat for everything else.

    The `runner` reference is needed for two things only:
      - runner._send: serialized chat send that records the message in
        _own_messages so we don't re-read our own confirmation prompt.
      - runner._seen_ids / runner._own_messages: claim consumed user
        replies so the main loop doesn't feed them to the LLM.
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
        # Disabled-MCP guard runs first: a server the user toggled off in
        # `operator edit claude` should never be callable, even if the
        # CLI loaded it (claude.ai-hosted MCPs aren't governed by
        # disabledMcpjsonServers, so the cheap settings.json route can't
        # hide them — defense-in-depth via this hook is the functional
        # disable for those). For JSON-keyed MCPs the settings.json knob
        # already kept them out of the CLI's tool surface, but matching
        # here too is harmless.
        disabled_server = _disabled_mcp_for_cli_tool(tool_name)
        if disabled_server is not None:
            log.info(
                f"PermissionChatHandler: deny {tool_name!r} — "
                f"server {disabled_server!r} is disabled in operator overlay"
            )
            return {
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"MCP server '{disabled_server}' is disabled in this bot's "
                    f"config — re-enable via `operator edit claude` to use it."
                ),
            }
        # always_ask wins over auto_approve so users can pin a specific
        # deny (e.g. mcp__sentry__analyze_issue_with_seer) on top of a
        # broad allow (mcp__sentry__*). Same precedent as the legacy
        # confirm_tools / read_tools split for track-B bots.
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
        prompt = _format_confirmation(tool_name, tool_input)
        log.info(f"PermissionChatHandler: asking user about {tool_name!r}")
        try:
            self._runner._send(prompt, kind="confirmation")
        except Exception as e:
            log.error(f"PermissionChatHandler: failed to post confirmation: {e}")
            return {
                "permissionDecision": "deny",
                "permissionDecisionReason": f"could not post confirmation to chat: {e}",
            }

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
