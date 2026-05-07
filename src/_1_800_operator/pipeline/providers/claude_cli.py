"""
Claude Code CLI LLM provider.

Wraps a long-lived `claude -p --input-format stream-json --output-format
stream-json` subprocess as an LLMProvider. One subprocess per meeting:
spawned lazily on the first complete() call, fed each turn's new user
message over stdin, terminated via stop() at meeting end.

Architecturally different from the OpenAI / Anthropic providers:
inner-claude owns its own tool-use loop, system prompt stack, and
context. We do not pass `tools`, `model`, or `max_tokens` — claude
handles those internally. `system` is consumed once at spawn time as
--append-system-prompt and ignored on subsequent calls (the system
prompt is set for the lifetime of the subprocess).

The subprocess runs under the user's Claude Max subscription
(apiKeySource: "none"); we explicitly clear ANTHROPIC_API_KEY from the
spawn env and assert apiKeySource at startup so an env-leak can never
silently bill the user's API account.

Spike data backing this design: debug/permission_mcp_spike/probes 4–7,
report at debug/permission_mcp_spike/SPIKE_PER_TURN_VS_PER_MEETING.md.
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from queue import Queue, Empty

from _1_800_operator.pipeline.providers.base import (
    LLMProvider,
    ProviderResponse,
    flush_paragraphs,
)

log = logging.getLogger(__name__)


# How long we'll wait for the subprocess's first system-init event before
# concluding that something is wrong with auth or the binary itself.
SPAWN_INIT_TIMEOUT_SECONDS = 30
# How long a single turn (user message in -> result event out) is allowed
# to take before we treat the subprocess as hung. Generous because inner
# claude may chain many tool calls before producing a final reply. Hard
# ceiling — backstop only.
TURN_TIMEOUT_SECONDS = 600
# Inter-event silence threshold for the wedge watchdog. If the subprocess
# emits no event of any kind for this long AND no tool is currently in
# flight, we conclude the subprocess is wedged and abort the turn (caller
# narrates the failure, user gets a recoverable message instead of waiting
# the full TURN_TIMEOUT_SECONDS ceiling). Tools legitimately produce no
# top-level events while running (a 5-minute Bash test suite is normal),
# so the gate is silence + not-in-flight, never silence alone.
HEARTBEAT_SILENCE_SECONDS = 60


# Framework-level UX guarantee for chat-mediated tool confirmation.
#
# The PreToolUse bridge (chat_runner + permission_chat_handler) blocks
# every non-auto-approved tool until the user replies in chat. Pre-14.19.8
# the bridge posted a sterile templated card ("Run? linear__list_issues
# …  OK?") to elicit the yes/no. 14.19.8 drops the card entirely and
# uses the model's pre-tool narration AS the question — the inner-claude
# model authors a one-sentence question in its own voice, the bridge
# silently waits for the user's next chat message as the answer.
#
# Two reliability-shaping requirements drove this rule's wording:
#
#   1. INTERROGATIVE FORM. A declarative narration ("I'll pull the open
#      Linear issues now.") leaves the user unsure whether action is
#      required from them. The rule below requires the model to phrase
#      every pre-tool message as a question ending in "?", so the user
#      knows a yes/no is expected.
#
#   2. PER-TOOL NARRATION. In multi-tool turns (e.g. ToolSearch to
#      discover a Linear MCP tool, then the actual list_issues call) the
#      model's default behavior is to narrate the conceptual intent ONCE
#      and run subsequent tools silently. Without an explicit per-tool
#      requirement, the second tool's permission gate would block with
#      no question on screen. The rule explicitly calls out "each
#      subsequent tool in a multi-step turn" to head that off.
#
# Inner-claude flushes text-delta events to stdout before invoking the
# PreToolUse hook (probe 10, session 196), so ordering is reliable: the
# question lands in chat first, then the bridge unblocks on the user's
# reply.
#
# Lives in code per feedback_capability_in_code_over_prompt — load-bearing
# UX guarantees don't belong in user-editable system_prompt where a
# hand-edit can silently drop them. The disposition (must ask, must be
# a question, must precede the tool) is encoded here; the exact wording
# stays the model's per feedback_llm_steering_via_tool_results.
_PRE_TOOL_VOICE_RULE = (
    "MEETING-CHAT TOOL UX RULE — load-bearing, do not skip.\n"
    "\n"
    "Every assistant turn that calls a tool must put a text content "
    "block first and the tool_use content block right after, in the "
    "SAME turn. Tools fall into two tiers:\n"
    "\n"
    "READ tools — pure information lookups with no side effects. "
    "Includes any MCP tool whose verb is get_/list_/search_/find_/"
    "read_; plus the built-ins Read, Grep, Glob, LS, WebSearch, and "
    "ToolSearch. For these, the text block is a one-sentence "
    "DECLARATIVE narration of what you're checking. Do NOT end this "
    "line with '?'. The framework auto-approves reads silently; your "
    "narration is the only signal the user sees. Exception: do not "
    "narrate ToolSearch — it's internal tool-schema loading the user "
    "doesn't need to see; just invoke it without preceding text on "
    "that one tool.\n"
    "\n"
    "WRITE / ACTION tools — anything that changes state, sends, "
    "creates, updates, deletes, runs commands, posts, or has external "
    "side effects. Includes Write, Edit, Bash, Task, WebFetch, and any "
    "MCP tool whose verb is create_/update_/delete_/save_/send_/post_/"
    "run_. For these, the text block is a one-sentence QUESTION ending "
    "in '?'. ALWAYS a question — even when the user's prior message "
    "was an explicit command ('create that issue', 'write the file'). "
    "Why: the framework's permission gate blocks the write until the "
    "user replies in meeting chat, and a declarative narration leaves "
    "the user with nothing visible to reply to (the operation hangs "
    "until they realize). Phrase as a question regardless of how "
    "explicit the user's instruction was. The framework gates the call "
    "at a permission checkpoint AFTER your invocation: invoke the "
    "tool_use normally — do NOT withhold it waiting for a reply; the "
    "framework handles the pause.\n"
    "\n"
    "For high-blast-radius operations include the literal critical "
    "detail verbatim in the question — exact command, path, recipient, "
    "or summary — so the user can audit before approving.\n"
    "\n"
    "CHAINED TURNS — most common failure mode, read carefully. A "
    "narration on an earlier turn does NOT carry forward. If you call "
    "five tools across five agentic turns to satisfy one user request, "
    "that is five separate text blocks, one immediately preceding each "
    "tool_use. The user sees each turn independently; they cannot tell "
    "what the second, third, fourth, fifth tool calls are for from a "
    "narration that scrolled past several seconds ago. Do not collapse "
    "narration to once-per-task; do not let a long investigation run "
    "silent between an opening line and a closing summary. If you have "
    "nothing new to say, you have nothing new to do — re-think before "
    "calling another tool unnarrated.\n"
    "\n"
    "VOICE: phrase narrations and questions in whatever voice your "
    "system_prompt established. Match the persona the user set up — "
    "do NOT default to specific wording like 'Want me to...' or "
    "'Pulling...'. Those are shape examples, not required phrasings. "
    "If your system_prompt says speak like a pirate, narrate and ask "
    "like a pirate.\n"
    "\n"
    "If a write is denied, you'll get a tool_result describing the "
    "user's response — react conversationally and do not retry without "
    "explicit approval. In multi-tool turns ask one question per write "
    "tool (one question, one approval, one tool); reads chain freely "
    "but each tool_use still gets its own preceding text block — never "
    "one narration covering several."
)


# YOLO-mode override appended after _PRE_TOOL_VOICE_RULE when the user
# passed `--yolo` (sets OPERATOR_YOLO=1, which adds
# `--dangerously-skip-permissions` to the inner-claude spawn). With the
# permission gate bypassed at the CLI level, asking a question for
# writes leaves the user thinking they're being prompted when they're
# not — the bot proceeds either way. This override flattens the WRITE
# tier into the READ tier: narrate everything declaratively, no
# question form, since there's no chat-side approval contract to honor.
# The audit-detail rule still applies (include the literal critical
# detail in narrations of high-blast-radius operations), so the user
# can still see and react if they spot something wrong before the
# operation completes.
_PRE_TOOL_VOICE_RULE_YOLO_OVERRIDE = (
    "YOLO MODE OVERRIDE — applies on top of the rule above:\n"
    "\n"
    "The framework's permission gate is BYPASSED for this session. All "
    "tool calls run immediately without chat-side approval. As a result, "
    "the WRITE tier's question requirement does NOT apply: narrate writes "
    "in declarative voice too, just like reads. There is no permission "
    "gate for the user's reply to release, so a question form would "
    "mislead them into thinking action is required from them when it "
    "isn't.\n"
    "\n"
    "Continue to include the literal critical detail (exact command, "
    "path, recipient, summary) in narrations of high-blast-radius "
    "operations, so the user can spot a problem before the operation "
    "completes — even though they no longer have a gate to reject at.\n"
    "\n"
    "The CHAINED TURNS rule from above stands in full under YOLO. With "
    "no permission gate to enforce per-turn ceremony, the temptation to "
    "skip narration on the second, third, fourth tool call is stronger "
    "— resist it. Narration is the only signal the user has that work "
    "is still happening; without it a multi-tool run looks like the bot "
    "froze between the opening line and the final summary."
)


class ClaudeCLINotFoundError(RuntimeError):
    """Raised when the `claude` CLI is missing from PATH."""


class ClaudeCLISubscriptionRequiredError(RuntimeError):
    """Raised when the spawned subprocess reports anything other than apiKeySource=none.

    Track A is explicitly subscription-only — billing through the user's
    Claude Max plan, not the API. If something leaks an ANTHROPIC_API_KEY
    into the environment we want to fail loud at startup, not silently
    rack up API charges.
    """


class ClaudeCLIProtocolError(RuntimeError):
    """Subprocess exited or misbehaved unexpectedly. Wraps the surfacing diagnostic."""


def _reader_thread(stream, q):
    """Pump claude's stdout into a queue, one parsed JSON event per item."""
    try:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                q.put(("event", json.loads(line)))
            except json.JSONDecodeError:
                q.put(("raw", line))
    finally:
        q.put(("eof", None))


class ClaudeCLIProvider(LLMProvider):
    """Long-lived `claude -p` subprocess as an LLMProvider.

    Construction is cheap; the subprocess is spawned lazily on the first
    complete() call so callers can build the provider during config load
    without paying spawn cost until a meeting actually starts.
    """

    def __init__(self, *, append_system_prompt=None, cwd=None, permission_handler=None):
        """
        Args:
          append_system_prompt: text passed via --append-system-prompt at spawn.
            None or empty leaves the default Claude Code system prompt alone.
          cwd: working directory for the subprocess. Defaults to $HOME for
            stable, predictable resolution of relative paths. The app-level
            builder (build_provider) overrides this with the user's
            invocation cwd so "this codebase" resolves naturally — same
            model as the bare `claude` CLI.
          permission_handler: optional callable
              (tool_name: str, tool_input: dict) -> dict
            Called from a pump thread on every PreToolUse event. Must return
            a dict with at minimum `permissionDecision` ("allow"|"deny"|"ask")
            and optionally `permissionDecisionReason` (str). When None, the
            PreToolUse hook is not registered and inner-claude follows its
            default permission flow (subject to the user's
            ~/.claude/settings.json rules).
        """
        self._append_system_prompt = append_system_prompt or None
        self._cwd = cwd or os.path.expanduser("~")
        self._permission_handler = permission_handler
        # Optional progress narrator: callable (tool_name, tool_input) ->
        # None, fired on every tool_use content block as the model emits
        # them. None disables narration.
        self._progress_callback = None

        self._proc = None
        self._out_q = None
        self._reader = None
        # Bounded so an hour-long meeting with chatty subprocess stderr can't grow
        # this without limit; only ever read as a 20-line tail on error paths.
        self._stderr_buf: deque[str] = deque(maxlen=500)
        # Tracks whether we've validated apiKeySource for the live subprocess.
        # claude in stream-json input mode only emits system-init after the
        # first user envelope arrives — not at startup — so we cannot perform
        # the assertion in _spawn(). Instead we observe the init event during
        # the first _send_and_collect() and flip this flag.
        self._init_validated = False
        # Captured from the system-init event of the first successful spawn.
        # On crash, _restart_after_death spawns a new subprocess with
        # `--resume <session_id>` so the new process rehydrates claude's
        # full local session state (messages + tool use + tool results)
        # rather than rebuilding from a synthesized text-only opener.
        self._session_id: str | None = None
        # Permission-bridge state (populated by _spawn when permission_handler
        # is set). Tempdir holds settings.json + named pipes; pump thread
        # listens on req pipe and dispatches to the handler.
        self._perm_tempdir = None
        self._perm_req_pipe = None
        self._perm_resp_pipe = None
        self._perm_pump_thread = None
        self._perm_stop = threading.Event()
        # Standalone tempdir created by _maybe_write_mcp_config when no
        # permission bridge is in play (no permission handler set). Its
        # lifecycle is independent of _perm_tempdir; cleaned up alongside
        # the bridge in _teardown_permission_bridge.
        self._mcp_only_tempdir = None
        # Meeting record path. When set (captions enabled + meeting URL
        # known), _spawn registers a bundled transcript MCP server via
        # --mcp-config so inner-claude can fetch spoken-caption history
        # on demand. None disables that server entirely.
        self._meeting_record_path: str | None = None

    def set_meeting_record_path(self, path):
        """Set the meeting JSONL path so the bundled transcript MCP can read it.

        Called by LLMClient.set_record once the meeting record is wired.
        Takes effect on the next subprocess spawn — already-spawned
        subprocesses keep whatever config they were launched with, which
        is fine because mid-meeting record changes don't happen.

        Also appends a runtime backstop to `_append_system_prompt` so the
        transcript tool hints survive even if a user nukes the agent's
        system_prompt. The MCP tools' own descriptions are the primary
        signal; this is defense in depth.
        """
        self._meeting_record_path = str(path) if path else None
        if self._meeting_record_path:
            backstop = (
                "\n\nThree transcript tools are available this meeting: "
                "`search_captions(query, speaker?, start_minutes_ago?, "
                "end_minutes_ago?, context_lines?)` for keyword lookups, "
                "`list_captions(start_minutes_ago?, end_minutes_ago?, "
                "last_n?, speaker?)` for chronological browse, and "
                "`list_speakers()` to see who's spoken. Call them when a "
                "chat message asks about something said aloud — spoken "
                "audio is not in your prompt context."
            )
            existing = self._append_system_prompt or ""
            if "search_captions" not in existing:
                self._append_system_prompt = existing + backstop

    # --- lifecycle -----------------------------------------------------

    def _spawn(self):
        """Ensure a live subprocess. Returns True if a fresh one was started.

        Idempotent: returns False when the existing subprocess is still
        running. If the previous subprocess died (poll returns a code) or
        was never started, this spawns a new one. When `self._session_id`
        is set, the spawn uses `--resume <id>` so claude rehydrates the
        prior session's full message history (incl. tool use + tool
        results) from its on-disk session store.

        The system-init event is emitted lazily by claude after the first
        user envelope is sent, so the apiKeySource assertion (and the
        first session_id capture) happen during the first
        _send_and_collect() call instead.
        """
        if self._proc is not None and self._proc.poll() is None:
            return False
        # Process is None (never started) or has exited. Either way, the
        # old subprocess's permission bridge state and reader thread are
        # dead too — clean them up before spawning fresh.
        if self._proc is not None:
            self._terminate_subprocess()
            self._teardown_permission_bridge()
            self._init_validated = False

        claude = shutil.which("claude")
        if not claude:
            raise ClaudeCLINotFoundError(
                "`claude` CLI not found on PATH. Install it from "
                "https://docs.anthropic.com/en/docs/claude-code and ensure it is "
                "logged in (`claude auth status`)."
            )

        cmd = [
            claude, "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            # Session persistence is required for `--resume` (claude's own
            # help text: "sessions will not be saved to disk and cannot be
            # resumed"). Sessions land under ~/.claude/projects/<...>/<id>.jsonl.
            # Always include partial messages. Non-streaming complete() simply
            # ignores `stream_event` events (the final `assistant` event still
            # arrives at end-of-turn). complete_streaming() consumes the
            # `content_block_delta` text_delta payloads to feed paragraphs to
            # on_paragraph as they arrive.
            "--include-partial-messages",
        ]
        # Phase 14.19.2 — `--yolo` flag on dial/deploy/slip sets
        # OPERATOR_YOLO=1 in env before _run_bot runs. Append the
        # claude-CLI permission-bypass flag at spawn time. The permission
        # bridge below still gets set up (PreToolUse hook fires) but
        # `--dangerously-skip-permissions` overrides at the CLI level so
        # claude never pauses regardless of hook output. 14.19.8 rewrites
        # the bridge to be conditionally skipped under yolo.
        if os.environ.get("OPERATOR_YOLO") == "1":
            cmd.append("--dangerously-skip-permissions")
        if self._session_id is not None:
            # Re-spawn after a crash: rehydrate the prior session so the new
            # subprocess inherits full message history (including tool use
            # and tool results) rather than rebuilding from text-only
            # turn pairs. The new init event will echo the same session_id.
            cmd += ["--resume", self._session_id]
        # Compose the agent's voice (self._append_system_prompt) with the
        # framework's pre-tool voice rule. Single injection point so the
        # rule composes cleanly with: (a) the lazy `system` path in
        # complete()/complete_streaming() that sets _append_system_prompt
        # on first call when no construction-time prompt was passed, and
        # (b) the transcript-tool backstop appended by
        # set_meeting_record_path. By the time _spawn() runs, both have
        # already landed in self._append_system_prompt.
        #
        # When OPERATOR_YOLO=1 the permission gate is bypassed at the
        # CLI level (--dangerously-skip-permissions, appended above);
        # the YOLO override tacks on after the main rule to flatten the
        # WRITE tier into the READ tier so the model doesn't ask
        # questions the user has nothing to reply to.
        rule_parts = [_PRE_TOOL_VOICE_RULE]
        if os.environ.get("OPERATOR_YOLO") == "1":
            rule_parts.append(_PRE_TOOL_VOICE_RULE_YOLO_OVERRIDE)
        composed = "\n\n".join(
            p for p in [self._append_system_prompt, *rule_parts] if p
        )
        if composed:
            cmd += ["--append-system-prompt", composed]

        # If a permission_handler was provided, set up the named-pipe IPC
        # rendezvous + write a per-invocation settings.json that registers
        # our PreToolUse hook. Without a handler we skip this entirely so
        # inner-claude follows its default permission flow.
        #
        # YOLO note: contrary to a stale comment from 14.19.2, live
        # testing in session 196 confirmed `--dangerously-skip-permissions`
        # does NOT skip PreToolUse hooks — they still fire and the
        # handler is invoked. The flag appears to override deny decisions
        # but doesn't suppress the hook itself. We rely on this behavior:
        # under YOLO, chat_runner._wire_permissions wires an auto_approve
        # list that matches '*' so the handler always returns allow,
        # belt-and-suspenders alongside the CLI flag.
        if self._permission_handler is not None:
            self._setup_permission_bridge()
            cmd += ["--settings", str(self._perm_tempdir / "settings.json")]
            # `default` permission mode lets PreToolUse hooks be the source
            # of truth (rather than auto-accept or auto-bypass).
            cmd += ["--permission-mode", "default"]

        # Register the bundled transcript MCP server via --mcp-config so the
        # model can fetch spoken-caption history on demand. We piggyback on
        # the permission-bridge tempdir when present; otherwise create a
        # one-off tempdir scoped to the spawn so the JSON survives until
        # claude reads it.
        mcp_config_path = self._maybe_write_mcp_config()
        if mcp_config_path is not None:
            cmd += ["--mcp-config", str(mcp_config_path)]

        # Force subscription auth: clear ANTHROPIC_API_KEY so claude falls
        # through to the OAuth-stored Max credential. We additionally
        # assert apiKeySource == "none" on the system-init event below.
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

        log.info(f"ClaudeCLI spawning subprocess: cwd={self._cwd}")
        try:
            self._proc = subprocess.Popen(
                cmd,
                cwd=self._cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
            )
        except OSError as exc:
            # Bridge tempdir + named pipes were created in
            # _setup_permission_bridge above; if Popen fails, tear them down
            # before raising so we don't accumulate orphan /tmp dirs across
            # repeated spawn failures.
            self._teardown_permission_bridge()
            raise ClaudeCLIProtocolError(f"failed to launch claude CLI: {exc}") from exc

        self._out_q = Queue()
        self._reader = threading.Thread(
            target=_reader_thread, args=(self._proc.stdout, self._out_q), daemon=True,
        )
        self._reader.start()
        # Drain stderr in the background so a chatty subprocess doesn't deadlock.
        threading.Thread(
            target=lambda: self._stderr_buf.extend(self._proc.stderr), daemon=True,
        ).start()
        return True

    def _maybe_write_mcp_config(self):
        """Write the per-spawn MCP config registering the transcript server.

        Returns the path to the JSON file, or None when no meeting record
        path is set (captions disabled, or no meeting yet). Lives in the
        permission-bridge tempdir if one exists, else a fresh one.
        """
        if not self._meeting_record_path:
            return None
        if self._perm_tempdir is not None:
            target_dir = self._perm_tempdir
        else:
            # Track the standalone tempdir so _teardown_permission_bridge
            # can rm it — it has no permission-bridge state to piggyback on.
            target_dir = Path(tempfile.mkdtemp(prefix="operator-claude-mcp-"))
            self._mcp_only_tempdir = target_dir
        config_path = target_dir / "mcp.json"
        config_path.write_text(json.dumps({
            "mcpServers": {
                "transcript": {
                    "command": sys.executable,
                    "args": ["-m", "_1_800_operator.mcp_servers.transcript_server"],
                    "env": {
                        "OPERATOR_MEETING_RECORD_PATH": self._meeting_record_path,
                    },
                }
            }
        }, indent=2))
        log.info(
            f"ClaudeCLI registering transcript MCP server "
            f"(record={self._meeting_record_path})"
        )
        return config_path

    def _setup_permission_bridge(self):
        """Create the tempdir, named pipes, and settings.json for the IPC bridge.

        Spawns the pump thread that listens on the request pipe.
        """
        tmp = Path(tempfile.mkdtemp(prefix="operator-claude-perm-"))
        req = tmp / "request.pipe"
        resp = tmp / "response.pipe"
        os.mkfifo(req, 0o600)
        os.mkfifo(resp, 0o600)
        self._perm_tempdir = tmp
        self._perm_req_pipe = req
        self._perm_resp_pipe = resp

        # Bridge command claude will invoke on every PreToolUse event. We
        # pass the bridge's file path directly rather than `-m
        # operator.pipeline.permission_bridge` so it runs as a standalone
        # script — no dependency on PYTHONPATH or `pip install -e .` in the
        # spawned shell. The bridge module imports only stdlib (verified at
        # write-time), so this is sound.
        import shlex
        from _1_800_operator.pipeline import permission_bridge as _bridge_mod
        bridge_path = Path(_bridge_mod.__file__).resolve()
        bridge_cmd = (
            f"{shlex.quote(sys.executable)} {shlex.quote(str(bridge_path))} "
            f"{shlex.quote(str(req))} {shlex.quote(str(resp))}"
        )

        settings = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "*",
                        "hooks": [
                            {
                                "type": "command",
                                "command": bridge_cmd,
                                "timeout": 600,  # generous; parent governs UX-level timeout
                            }
                        ],
                    }
                ]
            }
        }
        (tmp / "settings.json").write_text(json.dumps(settings, indent=2))
        log.info(f"ClaudeCLI permission bridge: tempdir={tmp}")

        self._perm_stop.clear()
        self._perm_pump_thread = threading.Thread(
            target=self._permission_pump,
            args=(req, resp, self._permission_handler),
            daemon=True,
        )
        self._perm_pump_thread.start()

    def _permission_pump(self, req_pipe, resp_pipe, handler):
        """Read one JSON request per bridge invocation, write back the decision.

        Bridge writes one payload then closes its end (EOF). We re-open the
        pipe each iteration. Stops cleanly when self._perm_stop is set: the
        sentinel write in _teardown_permission_bridge() unblocks the open().
        """
        log.info("ClaudeCLI permission pump started")
        while not self._perm_stop.is_set():
            try:
                with open(req_pipe, "r") as fr:
                    line = fr.read()
            except Exception as e:
                if self._perm_stop.is_set():
                    break
                log.warning(f"ClaudeCLI permission pump req-read failed: {e}")
                continue
            if self._perm_stop.is_set():
                break
            if not line.strip():
                # Spurious wakeup or empty payload — ignore.
                continue
            try:
                request = json.loads(line.strip())
            except json.JSONDecodeError as e:
                log.warning(f"ClaudeCLI permission pump got non-JSON: {e}; payload={line!r}")
                continue
            tool_name = request.get("tool_name", "")
            tool_input = request.get("tool_input", {})
            try:
                decision = handler(tool_name, tool_input)
            except Exception as e:
                log.exception(f"ClaudeCLI permission handler raised on {tool_name!r}")
                decision = {
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"handler error: {e}",
                }
            try:
                with open(resp_pipe, "w") as fw:
                    fw.write(json.dumps(decision) + "\n")
            except Exception as e:
                log.warning(f"ClaudeCLI permission pump resp-write failed: {e}")
                continue
        log.info("ClaudeCLI permission pump exited")

    def _teardown_permission_bridge(self):
        """Stop the pump thread and remove the tempdir + pipes.

        Also cleans up any standalone mcp-only tempdir created by
        _maybe_write_mcp_config when no permission bridge is in play —
        that path is independent of the bridge's own state and must run
        even when _perm_tempdir was never created.
        """
        # mcp-only tempdir cleanup runs first so it's not skipped by the
        # bridge-not-set early return below. Created by
        # _maybe_write_mcp_config when permission_handler is None but
        # captions/transcript MCP are wired.
        if self._mcp_only_tempdir is not None:
            if self._mcp_only_tempdir.exists():
                shutil.rmtree(self._mcp_only_tempdir, ignore_errors=True)
            self._mcp_only_tempdir = None

        if self._perm_tempdir is None:
            return
        self._perm_stop.set()
        # Unblock the pump's open() by writing a sentinel to the request
        # pipe. open(..., "w") would block until a reader exists, but the
        # pump itself is the reader and is currently blocked in its own
        # open(..., "r") — so writing satisfies both sides.
        try:
            if self._perm_req_pipe and self._perm_req_pipe.exists():
                # Use os.open + O_NONBLOCK so we don't deadlock if the
                # pump already exited.
                fd = os.open(str(self._perm_req_pipe), os.O_WRONLY | os.O_NONBLOCK)
                try:
                    os.write(fd, b"\n")
                finally:
                    os.close(fd)
        except OSError:
            pass  # pump already gone; that's fine
        if self._perm_pump_thread is not None:
            self._perm_pump_thread.join(timeout=5)
            self._perm_pump_thread = None
        if self._perm_tempdir.exists():
            shutil.rmtree(self._perm_tempdir, ignore_errors=True)
        self._perm_tempdir = None
        self._perm_req_pipe = None
        self._perm_resp_pipe = None

    # --- restart / resume ---------------------------------------------

    def _restart_after_death(self):
        """Tear down the dead subprocess and spawn a fresh one in its place.

        If a session_id was captured from the dead subprocess's init event,
        the fresh spawn uses `--resume <session_id>` so claude rehydrates
        the prior message history (incl. tool use + tool results) from its
        local session store. If no session_id was ever captured (subprocess
        died before its first init event), spawn fresh — caller's retry
        will replay the new user_text as a turn-1 against a clean session.

        Permission-bridge state is also rebuilt — pipes/settings.json are
        per-subprocess. Reset _init_validated so the new subprocess gets
        its own apiKeySource check.
        """
        log.warning(
            "ClaudeCLI: subprocess died mid-meeting, restarting "
            f"(session={self._session_id or 'none — fresh spawn'})"
        )
        self._terminate_subprocess()
        self._teardown_permission_bridge()
        self._init_validated = False
        self._spawn()

    def _validate_init_event(self, payload):
        """Check the apiKeySource on a system-init event. Raise if not subscription.

        Called from _send_and_collect() the first time it sees a system-init.
        """
        source = payload.get("apiKeySource")
        if source != "none":
            self._terminate_subprocess()
            raise ClaudeCLISubscriptionRequiredError(
                f"claude reported apiKeySource={source!r}; track A requires "
                "subscription auth (apiKeySource='none'). Refusing to "
                "proceed — an API key may have leaked into the environment."
            )
        self._init_validated = True
        session_id = payload.get("session_id")
        if session_id:
            # Persist for `--resume` on subprocess restart. On a resumed
            # spawn, claude echoes the same id back in the new init event,
            # so re-assigning is idempotent.
            self._session_id = session_id
        log.info(
            "ClaudeCLI subprocess ready: apiKeySource=none, "
            f"session={session_id or '?'}"
        )

    def _terminate_subprocess(self):
        if self._proc is None:
            return
        try:
            if self._proc.stdin and not self._proc.stdin.closed:
                self._proc.stdin.close()
        except Exception:
            pass
        if self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        self._proc = None
        self._out_q = None
        self._reader = None

    def set_permission_handler(self, handler):
        """Late-bind the permission handler.

        Construction-time wiring is awkward when the handler needs the
        meeting connector (only available after ChatRunner sets up). This
        setter lets the handler be plugged in just before the first
        complete() call. Must be called before _spawn() — once the
        subprocess is alive the bridge wiring is fixed for its lifetime.
        """
        if self._proc is not None:
            raise RuntimeError(
                "set_permission_handler must be called before the subprocess spawns; "
                "the bridge is wired in _spawn() and not reconfigurable mid-meeting."
            )
        self._permission_handler = handler

    def set_progress_callback(self, callback):
        """Late-bind the progress narrator.

        Called once per tool_use block during streaming, on the pump
        thread. Signature: (tool_name: str, tool_input: dict) -> None.
        Exceptions raised by the callback are swallowed and logged so a
        misbehaving narrator can't kill the turn.
        """
        self._progress_callback = callback

    def stop(self):
        """Cleanly shut down the subprocess + permission bridge. Idempotent.

        Called at meeting end. If the meeting bot is still talking, this
        cuts off the response — caller is responsible for sequencing.
        """
        log.info("ClaudeCLI stop() called")
        self._terminate_subprocess()
        self._teardown_permission_bridge()

    # --- event-loop helpers (used by _send_and_collect{,_streaming}) --

    def _check_heartbeat(self, last_event_ts: float, tool_in_flight: bool):
        """Raise ClaudeCLIProtocolError if the subprocess looks wedged.

        Wedge = no event for HEARTBEAT_SILENCE_SECONDS AND no tool in flight.
        Tools legitimately produce silent stretches (e.g. a 5-min Bash run);
        the gate is silence + not-in-flight, never silence alone.
        """
        if tool_in_flight:
            return
        if time.monotonic() - last_event_ts <= HEARTBEAT_SILENCE_SECONDS:
            return
        raise ClaudeCLIProtocolError(
            f"claude subprocess silent for {HEARTBEAT_SILENCE_SECONDS}s "
            "with no tool in flight — treating as wedged"
        )

    def _dispatch_assistant_blocks(self, payload, *, accumulate_text):
        """Walk an assistant event's content blocks, returning per-block effects.

        Returns a tuple (text_chunks, saw_tool_use) where:
          - text_chunks: list of text-block strings (empty when streaming —
            stream_event handles partial deltas; assistant text is just
            terminal echo). Caller passes accumulate_text=True for the
            non-streaming path to opt into reconstruction from blocks.
          - saw_tool_use: True iff at least one tool_use block was seen.
            Caller uses this to flip tool_in_flight and (in streaming) to
            dispatch the progress_callback.

        The progress_callback dispatch happens here so both event loops
        narrate inner tool use uniformly.
        """
        text_chunks: list[str] = []
        saw_tool_use = False
        for block in (payload.get("message") or {}).get("content") or []:
            btype = block.get("type")
            if btype == "text" and accumulate_text:
                text_chunks.append(block.get("text", ""))
            elif btype == "tool_use":
                saw_tool_use = True
                if self._progress_callback:
                    try:
                        self._progress_callback(
                            block.get("name", ""),
                            block.get("input") or {},
                        )
                    except Exception as exc:
                        log.warning(
                            f"ClaudeCLI: progress_callback raised on tool_use "
                            f"{block.get('name', '')!r}: {exc}"
                        )
        return text_chunks, saw_tool_use

    @staticmethod
    def _user_event_carries_tool_result(payload) -> bool:
        """True if a `user` event carries any tool_result block.

        Tool results arrive on the stream as user events (claude's protocol).
        Both event loops use this to flip tool_in_flight back to False.
        """
        for block in (payload.get("message") or {}).get("content") or []:
            if block.get("type") == "tool_result":
                return True
        return False

    # --- LLMProvider interface ----------------------------------------

    def complete(self, system, messages, model, max_tokens, tools=None, retry_rate_limits=True):
        """Send the latest user message and return the assistant's reply.

        Args ignored: model, max_tokens, tools — inner-claude handles these.
        Args partially ignored: system — used as --append-system-prompt at
        first spawn; subsequent calls' system arg is dropped (the prompt
        is fixed for the subprocess's lifetime).

        The caller's `messages` list is treated as the live conversation
        with the user. Inner-claude already has the prior turns in its own
        context, so we send only the LAST entry — which must be the new
        user turn.
        """
        if not messages:
            raise ValueError("complete() requires at least one message")
        last = messages[-1]
        if last.get("role") != "user":
            raise ValueError(
                f"claude_cli expects the last message to be a user turn; got role={last.get('role')!r}"
            )
        new_user_text = last.get("content") or ""

        # First-call lazy spawn. If `system` differs from what was used at
        # spawn we can't honor the change — log a warning and continue.
        first_call = self._proc is None
        if first_call and system and not self._append_system_prompt:
            self._append_system_prompt = system
        self._spawn()
        if not first_call and system and system != self._append_system_prompt:
            log.warning(
                "ClaudeCLI: system prompt changed after spawn — ignoring. "
                "The subprocess's system prompt was fixed at first call."
            )

        # The subprocess (whether fresh first-spawn or post-crash respawn
        # via --resume) already holds the full prior message history in
        # claude's own session store, so we send only the new user text.
        try:
            response = self._send_and_collect(new_user_text)
        except ClaudeCLIProtocolError as exc:
            # Subprocess died *during* the turn (write or read). Restart
            # via _restart_after_death (which uses --resume when a
            # session_id has been captured) and replay just the new user
            # text against the rehydrated session.
            log.warning(f"ClaudeCLI: turn aborted ({exc}); attempting one restart")
            self._restart_after_death()
            response = self._send_and_collect(new_user_text)

        return response

    def _send_and_collect(self, user_text):
        """Write one stream-json envelope, drain events until we see `result`."""
        envelope = {
            "type": "user",
            "message": {"role": "user", "content": user_text},
        }
        t_start = time.monotonic()
        try:
            self._proc.stdin.write(json.dumps(envelope) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise ClaudeCLIProtocolError(
                f"claude subprocess stdin closed unexpectedly: {exc}"
            ) from exc

        deadline = time.monotonic() + TURN_TIMEOUT_SECONDS
        text_parts = []
        result_evt = None
        last_event_ts = time.monotonic()
        tool_in_flight = False
        while time.monotonic() < deadline:
            try:
                kind, payload = self._out_q.get(timeout=0.5)
            except Empty:
                self._check_heartbeat(last_event_ts, tool_in_flight)
                continue
            last_event_ts = time.monotonic()
            if kind == "eof":
                stderr_tail = "".join(list(self._stderr_buf)[-20:]) if self._stderr_buf else "(empty)"
                raise ClaudeCLIProtocolError(
                    f"claude subprocess exited mid-turn. stderr tail:\n{stderr_tail}"
                )
            if kind != "event":
                continue
            etype = payload.get("type")
            if etype == "system" and payload.get("subtype") == "init":
                if not self._init_validated:
                    self._validate_init_event(payload)
            elif etype == "assistant":
                chunks, saw_tool_use = self._dispatch_assistant_blocks(
                    payload, accumulate_text=True,
                )
                text_parts.extend(chunks)
                if saw_tool_use:
                    tool_in_flight = True
                elif chunks:
                    # Top-level text — model is emitting reply content,
                    # no tool currently running.
                    tool_in_flight = False
            elif etype == "user":
                if self._user_event_carries_tool_result(payload):
                    tool_in_flight = False
            elif etype == "result":
                result_evt = payload
                break
        else:
            raise ClaudeCLIProtocolError(
                f"claude subprocess did not emit result within {TURN_TIMEOUT_SECONDS}s"
            )

        if result_evt is None:
            raise ClaudeCLIProtocolError("event loop exited without a result event")

        elapsed = time.monotonic() - t_start
        subtype = result_evt.get("subtype")
        log.info(
            f"TIMING claude_cli_turn={elapsed:.1f}s "
            f"result.subtype={subtype} text_chars={sum(len(p) for p in text_parts)}"
        )

        if subtype == "error_during_execution":
            raise ClaudeCLIProtocolError(
                f"claude reported error_during_execution: {result_evt.get('error', '')}"
            )

        return ProviderResponse(
            text="".join(text_parts) or None,
            tool_calls=[],          # claude handles its own tool use; we never see calls
            stop_reason="end",      # the result event marks turn end
        )

    def complete_streaming(
        self, system, messages, model, max_tokens, tools=None, on_paragraph=None,
        retry_rate_limits=True,
    ):
        """Same contract as complete(), but flushes paragraphs as they arrive.

        on_paragraph(text: str) is called for each completed paragraph (split
        on \\n\\n, decoration-only fragments dropped) plus the trailing
        partial at end-of-stream. Returns a ProviderResponse with the full
        accumulated text in `text` so the caller can record it.

        If on_paragraph is None this falls back to non-streaming behavior.
        """
        if on_paragraph is None:
            return self.complete(system, messages, model, max_tokens, tools=tools)

        if not messages:
            raise ValueError("complete_streaming() requires at least one message")
        last = messages[-1]
        if last.get("role") != "user":
            raise ValueError(
                f"claude_cli expects the last message to be a user turn; got role={last.get('role')!r}"
            )
        new_user_text = last.get("content") or ""

        first_call = self._proc is None
        if first_call and system and not self._append_system_prompt:
            self._append_system_prompt = system
        self._spawn()
        if not first_call and system and system != self._append_system_prompt:
            log.warning(
                "ClaudeCLI: system prompt changed after spawn — ignoring. "
                "The subprocess's system prompt was fixed at first call."
            )

        try:
            response = self._send_and_collect_streaming(new_user_text, on_paragraph)
        except ClaudeCLIProtocolError as exc:
            # Mid-stream death. Note that any paragraphs already flushed
            # to on_paragraph will have been seen by the user; the retry
            # may emit overlapping content. Caller is responsible for
            # tolerating that (typically: meeting chat surfaces the new
            # reply as a fresh message and the user reads it as a redo).
            # _restart_after_death uses --resume when a session_id has
            # been captured so the rehydrated subprocess inherits prior
            # context; we replay just the new user text.
            log.warning(
                f"ClaudeCLI: streaming turn aborted ({exc}); attempting one restart"
            )
            self._restart_after_death()
            response = self._send_and_collect_streaming(new_user_text, on_paragraph)

        return response

    def _send_and_collect_streaming(self, user_text, on_paragraph):
        """Variant of _send_and_collect that consumes content_block_delta events.

        Top-level assistant text arrives as `stream_event` events of shape:
            {"type": "stream_event",
             "event": {"type": "content_block_delta",
                       "index": N,
                       "delta": {"type": "text_delta", "text": "..."}},
             "parent_tool_use_id": null | <id>}

        We only flush text where `parent_tool_use_id` is null — sub-agent
        deltas (parent_tool_use_id non-null) are not the bot's outgoing
        reply. The terminal `assistant` event is used to verify that what
        we accumulated matches the canonical full text.
        """
        envelope = {
            "type": "user",
            "message": {"role": "user", "content": user_text},
        }
        t_start = time.monotonic()
        t_first_token = None
        t_first_flush = None
        try:
            self._proc.stdin.write(json.dumps(envelope) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise ClaudeCLIProtocolError(
                f"claude subprocess stdin closed unexpectedly: {exc}"
            ) from exc

        deadline = time.monotonic() + TURN_TIMEOUT_SECONDS
        buffer = ""
        full_text_parts = []
        result_evt = None
        last_event_ts = time.monotonic()
        tool_in_flight = False
        while time.monotonic() < deadline:
            try:
                kind, payload = self._out_q.get(timeout=0.5)
            except Empty:
                self._check_heartbeat(last_event_ts, tool_in_flight)
                continue
            last_event_ts = time.monotonic()
            if kind == "eof":
                stderr_tail = "".join(list(self._stderr_buf)[-20:]) if self._stderr_buf else "(empty)"
                raise ClaudeCLIProtocolError(
                    f"claude subprocess exited mid-turn. stderr tail:\n{stderr_tail}"
                )
            if kind != "event":
                continue
            etype = payload.get("type")
            if etype == "system" and payload.get("subtype") == "init":
                if not self._init_validated:
                    self._validate_init_event(payload)
                continue
            if etype == "stream_event":
                # Skip sub-agent deltas — they're inner Task-tool output, not
                # the bot's reply. (parent_tool_use_id is set when this delta
                # belongs to a sub-agent's response.)
                if payload.get("parent_tool_use_id"):
                    continue
                inner = payload.get("event") or {}
                if inner.get("type") != "content_block_delta":
                    continue
                delta = inner.get("delta") or {}
                if delta.get("type") != "text_delta":
                    continue
                text = delta.get("text") or ""
                if not text:
                    continue
                # Top-level text delta — model is producing reply content,
                # not running a tool. Re-arm the watchdog.
                tool_in_flight = False
                if t_first_token is None:
                    t_first_token = time.monotonic()
                full_text_parts.append(text)
                buffer += text
                if "\n\n" in buffer:
                    if t_first_flush is None:
                        t_first_flush = time.monotonic()
                    buffer = flush_paragraphs(buffer, on_paragraph)
                continue
            if etype == "assistant":
                # Sub-agent assistants arrive here with parent_tool_use_id set;
                # they're inner Task-tool output, not the bot's reply.
                if payload.get("parent_tool_use_id"):
                    continue
                # Streaming reconstructs text from stream_event deltas — we
                # don't accumulate from assistant blocks here, just dispatch
                # tool_use to progress_callback and flip tool_in_flight.
                _, saw_tool_use = self._dispatch_assistant_blocks(
                    payload, accumulate_text=False,
                )
                if saw_tool_use:
                    tool_in_flight = True
                # An `assistant` event finalizes one sub-message of the turn.
                # When the model calls a tool, the next text comes in a NEW
                # assistant message (indices reset to 0). Without flushing
                # here, "Hey Jojo — writing that now." and "Done — ..." get
                # concatenated as one paragraph and posted smooshed.
                if buffer.strip():
                    if t_first_flush is None:
                        t_first_flush = time.monotonic()
                    flush_paragraphs(buffer, on_paragraph, force_final=True)
                    full_text_parts.append("\n\n")
                    buffer = ""
                continue
            if etype == "user":
                if self._user_event_carries_tool_result(payload):
                    tool_in_flight = False
                continue
            if etype == "result":
                result_evt = payload
                break
        else:
            raise ClaudeCLIProtocolError(
                f"claude subprocess did not emit result within {TURN_TIMEOUT_SECONDS}s"
            )

        if buffer.strip():
            flush_paragraphs(buffer, on_paragraph, force_final=True)

        if result_evt is None:
            raise ClaudeCLIProtocolError("event loop exited without a result event")

        elapsed = time.monotonic() - t_start
        ttft = (t_first_token - t_start) if t_first_token else None
        first_flush = (t_first_flush - t_start) if t_first_flush else None
        ttft_str = f"{ttft:.1f}s" if ttft is not None else "n/a"
        flush_str = f"{first_flush:.1f}s" if first_flush is not None else "n/a"
        log.info(
            f"TIMING claude_cli_turn={elapsed:.1f}s ttft={ttft_str} first_flush={flush_str} streamed=1 "
            f"result.subtype={result_evt.get('subtype')}"
        )

        if result_evt.get("subtype") == "error_during_execution":
            raise ClaudeCLIProtocolError(
                f"claude reported error_during_execution: {result_evt.get('error', '')}"
            )

        # `full_text_parts` is the single source of truth for what reached
        # the user via on_paragraph (and via _send → meeting record). No
        # canonical reconstruction from terminal assistant events — those
        # are only walked to dispatch tool_use blocks to progress_callback.
        final_text = "".join(full_text_parts).strip()

        return ProviderResponse(
            text=final_text or None,
            tool_calls=[],
            stop_reason="end",
        )

    def warmup(self, model):
        """Spawn the subprocess so the first real turn doesn't pay init cost.

        `model` is unused — claude picks the model itself.
        """
        self._spawn()
