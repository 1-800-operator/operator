"""
Claude Code CLI LLM provider — naked per-@mention `claude -p` shellouts.

Each turn (each meeting-chat @mention) spawns a fresh `claude -p
--output-format stream-json --include-partial-messages` subprocess, drains
its stream until the `result` event, and exits. There is no long-lived
subprocess; there is no system prompt injected from operator; there is no
per-spawn `--mcp-config` tempfile. The spawn shape is identical to what a
user typing `claude --resume <id>` themselves would produce — by design.

This is the load-bearing constraint for V1 (Phase 14.22, S210/S211): the
spawn signature is what Anthropic uses to detect harness-on-subscription
patterns, so operator must contribute zero flags or metadata that wouldn't
exist if the user ran `claude` directly. Whatever steering we want lives
client-side in the user's own Claude Code session (CLAUDE.md, plugin
SKILL.md content) or in MCP tool descriptions read via normal tool
discovery. See memory `project_anthropic_detection_vector.md`.

Session continuity:
  - First turn: spawn with no `--resume` (or with `--resume <pre-populated-id>`
    when the plugin passed `--resume-session ${CLAUDE_SESSION_ID}` to the
    operator CLI). Capture `session_id` from the `system_init` event.
  - Subsequent turns: spawn with `--resume <captured-id>`. Claude rehydrates
    its on-disk session store; prompt-cache hits via `cache_read_input_tokens`
    keep the per-turn cost reasonable.
  - Mid-stream EOF: retry once with `--resume`. Genuine wedges (subprocess
    silent forever) are caller-cancelled via `/operator hangup` — no
    operator-imposed turn timeout, no heartbeat watchdog (both were
    harness-shaped and got stripped in S211).

Subscription auth: ANTHROPIC_API_KEY is stripped from the spawn env on
every shellout, and `apiKeySource == "none"` is asserted on the first
spawn's `system_init` event. We then trust subsequent spawns under the
same `_session_id` to inherit the same credential — the assertion fires
only at provider startup, not on every shellout, since re-asserting on
each spawn would be belt-and-suspenders without changing the failure
mode.
"""
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from queue import Queue, Empty

from _1_800_operator.pipeline.providers.base import (
    LLMProvider,
    ProviderResponse,
    flush_paragraphs,
)

log = logging.getLogger(__name__)


def _format_stage_breakdown(t_run_start, t_spawn_done, t_init, t_first_token, warm=False):
    """Format the per-stage TTFT breakdown for the TIMING log line.

    Splits the pre-first-token time into three observable stages so a
    high `ttft` can be attributed to the right cost:

      spawn_ms   — subprocess.Popen → return (fork+exec, dyld, etc.)
      cli_init_ms — t_spawn_done → system_init event from claude. For
                    cold turns this is the full Node boot + MCP attach +
                    --resume JSONL parse + stdin envelope read. For warm
                    turns it's just (envelope-write → init), because
                    the boot/attach/parse happened in parallel with idle
                    during pre_warm. A small cli_init_ms on warm=1 is
                    the visible win.
      api_ttft_ms — system_init → first content_block_delta (API call).

    The `warm=1|0` flag at the end makes warm-vs-cold turns greppable.
    Any stage with no captured stamp shows as `n/a`. Returns a single
    space-separated fragment ready to drop into the TIMING line.
    """
    def _f(v):
        return f"{v}ms" if v is not None else "n/a"
    spawn_ms = int((t_spawn_done - t_run_start) * 1000) if t_spawn_done else None
    cli_init_ms = int((t_init - t_spawn_done) * 1000) if (t_init and t_spawn_done) else None
    api_ttft_ms = int((t_first_token - t_init) * 1000) if (t_first_token and t_init) else None
    return (
        f"spawn={_f(spawn_ms)} "
        f"cli_init={_f(cli_init_ms)} "
        f"api_ttft={_f(api_ttft_ms)} "
        f"warm={1 if warm else 0}"
    )


def _format_cache_stats(result_evt):
    """Format prompt-cache usage from the result event's `usage` block.

    claude reports per-turn token usage with cache_read_input_tokens and
    cache_creation_input_tokens, which directly indicate whether the
    prompt cache hit on this turn. A high cache_read with low input
    means the cache is doing its job; a high input with low cache_read
    means we're re-paying for the prefix every turn (likely because
    the prefix isn't stable across spawns).
    """
    if not result_evt:
        return "cache=n/a"
    usage = result_evt.get("usage") or {}
    if not usage:
        return "cache=n/a"
    inp = usage.get("input_tokens", 0)
    creation = usage.get("cache_creation_input_tokens", 0)
    read = usage.get("cache_read_input_tokens", 0)
    return f"cache_input={inp} cache_creation={creation} cache_read={read}"


class ClaudeCLINotFoundError(RuntimeError):
    """Raised when the `claude` CLI is missing from PATH."""


class ClaudeCLISubscriptionRequiredError(RuntimeError):
    """Raised when the spawned subprocess reports anything other than apiKeySource=none.

    V1 is explicitly subscription-only — billing through the user's
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
    """Per-@mention `claude -p` shellouts as an LLMProvider.

    Construction is cheap; nothing happens until the first complete()
    call. Each call spawns a fresh subprocess, runs it to the `result`
    event, and exits. State carried across calls is just the captured
    `session_id` (used to pass `--resume <id>` on subsequent spawns).
    """

    def __init__(self, *, cwd=None, resume_session_id=None):
        """
        Args:
          cwd: working directory for each spawn. Defaults to $HOME for
            stable resolution of relative paths. The app-level builder
            (build_provider) overrides this with the user's invocation
            cwd so "this codebase" resolves naturally — same model as
            the bare `claude` CLI.
          resume_session_id: optional. Pre-populates `_session_id` so
            the very first spawn passes `--resume <id>`. The plugin
            slash command always passes this (substituted from
            `${CLAUDE_SESSION_ID}` at execution time); terminal-direct
            invocation omits it and a fresh session is born on first
            @mention.
        """
        self._cwd = cwd or os.path.expanduser("~")
        # Captured from the `system_init` event of the first spawn.
        # Subsequent spawns pass `--resume <id>` so claude rehydrates
        # the prior session's full message history (incl. tool use +
        # tool results) from its on-disk session store. Pre-populated
        # via the constructor when the plugin slash command bridges a
        # caller's existing Claude Code session into the meeting.
        self._session_id: str | None = resume_session_id
        # Tracks whether we've validated apiKeySource at least once.
        # Per-shellout assertion is unnecessary — the first spawn's
        # check is sufficient since the spawn env is built identically
        # each time (ANTHROPIC_API_KEY stripped, no other auth
        # breadcrumbs). Re-asserting on every spawn would just waste
        # work without changing the failure mode.
        self._init_validated = False
        # Optional progress narrator: callable (tool_name, tool_input)
        # -> None, fired on every tool_use content block as the model
        # emits them. ChatRunner uses this to post `[☎️ Operator]`-
        # voice tool narration (20s-throttled) into meeting chat.
        self._progress_callback = None
        # Optional in-turn tick: callable () -> None, fired on every
        # iteration of the out-queue read loop. ChatRunner uses this
        # to drain its off-thread send queue (operator-voice
        # narrations queued from the provider's pump thread) onto the
        # main Playwright-owning thread while the polling thread is
        # parked inside complete_streaming().
        self._tick_callback = None
        # Optional permission-denial narrator: callable (tool_name) ->
        # None, fired when a tool_result block carries a permission-
        # denied signature. ChatRunner uses this to post a one-time
        # `[☎️ Operator] permission denied for X — re-run with --yolo`
        # hint. Throttled to once per turn at the ChatRunner layer.
        self._denial_callback = None
        # Optional connection-status narrator: callable (event: str) ->
        # None, fired on EOF mid-stream + on the retry. ChatRunner uses
        # this to post `[☎️ Operator] connection dropped — reconnecting…`
        # to chat in operator's switchboard voice. event is one of
        # {"dropped", "reconnecting", "failed"}.
        self._connection_callback = None
        # Set by stop() during shutdown. Suppresses the EOF retry path
        # so a SIGINT-triggered subprocess kill doesn't race in a
        # fresh spawn after the rest of operator has torn down.
        self._stopping = False
        # Eager pre-spawn slot. pre_warm() spawns a `claude -p --resume <id>`
        # subprocess and consumes its system_init event (validating auth +
        # capturing session_id) ahead of any user turn. The next turn
        # claims this slot in _run_one_turn and skips the ~2.6s
        # cli_init cost. Two callers fire pre_warm: (a) ChatRunner.run()
        # after a successful meeting join (covers turn 1) and (b) the
        # tail of complete_streaming on success (covers turns 2+).
        # `_warm_in_progress` is a dedup latch so overlapping callers
        # don't spawn twice. The slow spawn happens OUTSIDE the lock so
        # a turn arriving mid-prewarm doesn't serialize on it.
        self._warm_proc = None
        self._warm_out_q: Queue | None = None
        self._warm_stderr_buf: deque[str] | None = None
        self._warm_lock = threading.Lock()
        self._warm_in_progress = False

    # --- callback wiring (set by ChatRunner._wire_provider) -----------

    def set_progress_callback(self, callback):
        """Late-bind the tool-use narrator.

        Called once per tool_use content block during streaming, on the
        provider's reader thread. Signature: (tool_name: str,
        tool_input: dict) -> None. Exceptions are swallowed so a
        misbehaving narrator can't kill the turn.
        """
        self._progress_callback = callback

    def set_tick_callback(self, callback):
        """Late-bind a per-iteration tick fired during in-turn out-queue
        polling. Signature: () -> None. Called on the same thread that
        invoked complete()/complete_streaming() (the polling thread on
        the live runner) on every loop iteration of the streaming and
        non-streaming event readers — both event arrivals and the 0.5s
        timeout cycle. ChatRunner uses this to drain its off-thread
        send queue while the polling thread is parked here. Exceptions
        are swallowed so a misbehaving callback can't kill the turn."""
        self._tick_callback = callback

    def set_denial_callback(self, callback):
        """Late-bind the permission-denial narrator.

        Called when a tool_result block carries a permission-denied
        signature. Signature: (tool_name: str) -> None. ChatRunner
        debounces to once per turn at its layer.
        """
        self._denial_callback = callback

    def set_connection_callback(self, callback):
        """Late-bind the switchboard-voice connection narrator.

        Called on mid-stream EOF + on the retry path. Signature:
        (event: str) -> None where event ∈ {"dropped", "reconnecting",
        "failed"}. ChatRunner posts the operator-voice status line.
        """
        self._connection_callback = callback

    # --- lifecycle ----------------------------------------------------

    def stop(self):
        """Mark the provider as shutting down.

        Per-@mention spawns are owned by their complete_streaming()
        call frame and tear down via its finally block. Pre-warmed
        subprocesses are owned by the provider, so this method
        terminates any warm slot inhabitant. Setting `_stopping` makes
        the EOF retry path bail rather than spawning a zombie
        replacement after a SIGINT-triggered kill. Idempotent.
        """
        if self._stopping:
            return
        log.info("ClaudeCLI stop() called")
        self._stopping = True
        # Terminate the warm subprocess if any. Hold the lock so a
        # concurrent _run_one_turn doesn't claim a doomed subprocess
        # mid-shutdown.
        with self._warm_lock:
            if self._warm_proc is not None:
                self._terminate(self._warm_proc)
                self._warm_proc = None
                self._warm_out_q = None
                self._warm_stderr_buf = None

    def pre_warm(self):
        """Spawn a `claude -p --resume <id>` subprocess ahead of demand
        and park it idle waiting for stdin.

        Empirical finding (S220): claude in stream-json mode emits zero
        events until it reads its first stdin input — so we cannot wait
        for `system_init` here. The subprocess *does* perform its Node
        boot + MCP attach + --resume JSONL parse during the parked
        window, but silently. When the next turn writes its envelope to
        stdin, `system_init` arrives almost instantly because the
        startup work was already done.

        We slot the subprocess as-is and let the in-turn drain consume
        init events normally (validating apiKeySource and capturing
        session_id there, same as a cold spawn).

        Callers:
          - ChatRunner.run() after a successful meeting join (turn 1).
          - complete_streaming tail on success (turns 2+).

        Idempotent: returns if the slot already holds a live subprocess
        or another pre_warm is mid-spawn. Safe to call from any thread.
        Best-effort: failures log a warning and leave the slot empty.
        """
        if self._stopping:
            return
        with self._warm_lock:
            if self._warm_proc is not None and self._warm_proc.poll() is None:
                return  # already warm and alive
            if self._warm_in_progress:
                return  # another pre_warm is mid-spawn
            self._warm_in_progress = True

        proc = None
        try:
            proc, out_q, stderr_buf = self._spawn_one()
        except Exception as exc:
            log.warning(f"ClaudeCLI: pre_warm spawn failed: {exc}")
            if proc is not None:
                self._terminate(proc)
            with self._warm_lock:
                self._warm_in_progress = False
            return

        # Slot the subprocess. The startup work (Node + MCP + --resume)
        # continues in the background; the next turn will write its
        # envelope and see system_init promptly.
        with self._warm_lock:
            if self._stopping:
                self._terminate(proc)
            else:
                self._warm_proc = proc
                self._warm_out_q = out_q
                self._warm_stderr_buf = stderr_buf
                log.info(f"ClaudeCLI: warm subprocess slotted (pid={proc.pid})")
            self._warm_in_progress = False

    # --- spawn helpers ------------------------------------------------

    def _build_cmd(self):
        """Assemble the per-shellout command vector.

        Naked: only flags that look like what a user typing `claude`
        themselves would produce. No `--append-system-prompt`, no
        `--mcp-config`, no harness identity at the spawn layer.
        """
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
            "--include-partial-messages",
        ]
        # `--yolo` on the operator CLI sets OPERATOR_YOLO=1 in env. We
        # forward it as `--dangerously-skip-permissions`, which is a
        # user-equivalent flag (any user can pass it directly to
        # claude). With no yolo flag we pass nothing extra and Claude
        # Code applies its native permission rules from the user's
        # ~/.claude/settings.json. Operator does not impose its own
        # permission layer.
        if os.environ.get("OPERATOR_YOLO") == "1":
            cmd.append("--dangerously-skip-permissions")
        if self._session_id is not None:
            # Subsequent @mention (or first @mention with a session id
            # bridged in via --resume-session): rehydrate the prior
            # session so the model inherits full message history. The
            # init event will echo this same session_id back.
            cmd += ["--resume", self._session_id]
        return cmd

    def _spawn_one(self):
        """Launch one per-@mention `claude -p` subprocess.

        Returns (proc, out_q, stderr_buf). Caller is responsible for
        terminating proc in a finally block. On any error tearing up
        the subprocess raises ClaudeCLIProtocolError with a useful
        diagnostic.
        """
        cmd = self._build_cmd()
        # Strip ANTHROPIC_API_KEY from the spawn env so claude falls
        # through to the OAuth-stored Max credential. The first spawn
        # additionally asserts apiKeySource == "none" on its init
        # event below.
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

        log.info(
            f"ClaudeCLI spawning per-@mention subprocess: cwd={self._cwd} "
            f"resume={'<pre-populated>' if self._session_id else 'none'}"
        )
        try:
            proc = subprocess.Popen(
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
            raise ClaudeCLIProtocolError(f"failed to launch claude CLI: {exc}") from exc

        out_q: Queue = Queue()
        threading.Thread(
            target=_reader_thread, args=(proc.stdout, out_q), daemon=True,
        ).start()

        stderr_buf: deque[str] = deque(maxlen=500)
        threading.Thread(
            target=lambda: stderr_buf.extend(proc.stderr), daemon=True,
        ).start()

        return proc, out_q, stderr_buf

    def _terminate(self, proc):
        """Best-effort tear-down for a spawned subprocess. Idempotent."""
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:
            pass
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass

    def _validate_init_event(self, payload):
        """Check apiKeySource on a system-init event. Raise if not subscription.

        Captures the session_id on every init event so subsequent
        spawns can `--resume`. The first init also flips
        `_init_validated` so we only assert once per provider lifetime.
        """
        source = payload.get("apiKeySource")
        if source != "none":
            raise ClaudeCLISubscriptionRequiredError(
                f"claude reported apiKeySource={source!r}; v1 requires "
                "subscription auth (apiKeySource='none'). Refusing to "
                "proceed — an API key may have leaked into the environment."
            )
        self._init_validated = True
        session_id = payload.get("session_id")
        if session_id:
            # On a resumed spawn claude echoes the same id; on a
            # fresh first spawn it issues a new one. Either way,
            # capturing here gives subsequent shellouts a stable
            # `--resume <id>` target.
            self._session_id = session_id
        log.info(
            f"ClaudeCLI spawn ready: apiKeySource=none, session={session_id or '?'}"
        )

    def _fire_tick(self):
        cb = self._tick_callback
        if cb is None:
            return
        try:
            cb()
        except Exception as e:
            log.warning(f"ClaudeCLI: tick callback raised: {e}")

    def _fire_progress(self, name, tool_input):
        cb = self._progress_callback
        if cb is None:
            return
        try:
            cb(name, tool_input)
        except Exception as e:
            log.warning(f"ClaudeCLI: progress_callback raised on {name!r}: {e}")

    def _fire_denial(self, name):
        cb = self._denial_callback
        if cb is None:
            return
        try:
            cb(name)
        except Exception as e:
            log.warning(f"ClaudeCLI: denial_callback raised on {name!r}: {e}")

    def _fire_connection(self, event):
        cb = self._connection_callback
        if cb is None:
            return
        try:
            cb(event)
        except Exception as e:
            log.warning(f"ClaudeCLI: connection_callback raised on {event!r}: {e}")

    # --- event-loop helpers -------------------------------------------

    def _dispatch_assistant_blocks(self, payload, *, accumulate_text):
        """Walk an assistant event's content blocks. Returns (text_chunks, saw_tool_use).

        text_chunks is empty when streaming (stream_event handles partial
        deltas; assistant events are terminal echo). The non-streaming path
        passes accumulate_text=True to opt into reconstruction from blocks.
        Fires progress_callback per tool_use block.
        """
        text_chunks: list[str] = []
        saw_tool_use = False
        for block in (payload.get("message") or {}).get("content") or []:
            btype = block.get("type")
            if btype == "text" and accumulate_text:
                text_chunks.append(block.get("text", ""))
            elif btype == "tool_use":
                saw_tool_use = True
                self._fire_progress(
                    block.get("name", ""), block.get("input") or {},
                )
        return text_chunks, saw_tool_use

    @staticmethod
    def _user_event_carries_tool_result(payload) -> bool:
        """True if a `user` event carries any tool_result block."""
        for block in (payload.get("message") or {}).get("content") or []:
            if block.get("type") == "tool_result":
                return True
        return False

    def _check_user_event_for_denials(self, payload):
        """Inspect a `user` event's tool_result blocks for permission denials.

        Fires denial_callback once per denied tool_use_id. The signature
        match is intentionally permissive — Claude Code formats permission
        errors as plain text inside the tool_result content, varying by
        tool. We match on substrings that consistently appear ("permission
        denied", "not allowed", "not granted", "blocked by permissions").
        """
        for block in (payload.get("message") or {}).get("content") or []:
            if block.get("type") != "tool_result":
                continue
            content = block.get("content")
            if isinstance(content, list):
                # Some Claude Code tool_result payloads ship an array of
                # text blocks; flatten for matching.
                content = " ".join(
                    str(c.get("text", c)) if isinstance(c, dict) else str(c)
                    for c in content
                )
            text = (content or "").lower() if isinstance(content, str) else ""
            if not text:
                continue
            denial_signals = (
                "permission denied",
                "not allowed",
                "not granted",
                "not permitted",
                "blocked by permissions",
                "requires approval",
            )
            if any(sig in text for sig in denial_signals):
                # tool name isn't on the tool_result block itself —
                # it's on the corresponding tool_use we already saw.
                # Pass the tool_use_id so ChatRunner can correlate if
                # it kept that mapping; falls back to "<tool>" when
                # ChatRunner doesn't track ids.
                tool_id = block.get("tool_use_id") or "<tool>"
                self._fire_denial(tool_id)

    # --- LLMProvider interface ----------------------------------------

    def complete(self, system, messages, model, max_tokens, tools=None, retry_rate_limits=True):
        """Spawn one shellout, send the latest user message, return the reply.

        Args ignored: system, model, max_tokens, tools — inner-claude
        owns its own system prompt + tool loop natively. The neutral
        `messages` list arrives with the meeting chat tail; we send only
        the last entry (the new user turn) since claude has the prior
        history in its on-disk session store via `--resume`.
        """
        if self._stopping:
            raise ClaudeCLIProtocolError("provider is stopping")
        if not messages:
            raise ValueError("complete() requires at least one message")
        last = messages[-1]
        if last.get("role") != "user":
            raise ValueError(
                f"claude_cli expects the last message to be a user turn; got role={last.get('role')!r}"
            )
        new_user_text = last.get("content") or ""

        try:
            return self._run_one_turn(new_user_text, on_paragraph=None)
        except ClaudeCLIProtocolError as exc:
            if self._stopping:
                log.info("ClaudeCLI: turn aborted during shutdown — propagating")
                raise
            log.warning(f"ClaudeCLI: turn aborted ({exc}); attempting one retry with --resume")
            self._fire_connection("dropped")
            self._fire_connection("reconnecting")
            try:
                return self._run_one_turn(new_user_text, on_paragraph=None)
            except ClaudeCLIProtocolError as exc2:
                self._fire_connection("failed")
                raise exc2

    def complete_streaming(
        self, system, messages, model, max_tokens, tools=None, on_paragraph=None,
        retry_rate_limits=True,
    ):
        """Same contract as complete(), but flushes paragraphs as they arrive.

        on_paragraph(text: str) is called for each completed paragraph
        (split on \\n\\n, decoration-only fragments dropped) plus the
        trailing partial at end-of-stream. Returns a ProviderResponse
        with the full accumulated text in `text`.
        """
        if self._stopping:
            raise ClaudeCLIProtocolError("provider is stopping")
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

        try:
            result = self._run_one_turn(new_user_text, on_paragraph=on_paragraph)
            self._schedule_pre_warm()
            return result
        except ClaudeCLIProtocolError as exc:
            if self._stopping:
                log.info("ClaudeCLI: streaming turn aborted during shutdown — propagating")
                raise
            log.warning(
                f"ClaudeCLI: streaming turn aborted ({exc}); attempting one retry with --resume. "
                "Note: any paragraphs already flushed to on_paragraph are visible to the user; "
                "the retry may emit overlapping content."
            )
            self._fire_connection("dropped")
            self._fire_connection("reconnecting")
            try:
                result = self._run_one_turn(new_user_text, on_paragraph=on_paragraph)
                self._schedule_pre_warm()
                return result
            except ClaudeCLIProtocolError as exc2:
                self._fire_connection("failed")
                raise exc2

    def _schedule_pre_warm(self):
        """Fire pre_warm on a daemon thread so the caller doesn't block
        for the ~2.6s claude CLI startup. The next turn will claim the
        warm slot if it arrives after pre_warm finishes; otherwise it
        cold-spawns and pre_warm slots a fresh one for the turn after.
        """
        if self._stopping:
            return
        threading.Thread(target=self.pre_warm, daemon=True).start()

    def warmup(self, model):
        """No-op for the per-@mention shape.

        With the long-lived subprocess gone, there's nothing to pre-
        spawn. Kept on the LLMProvider ABC for compatibility — the ABC
        contract is "fire a 1-token request to warm the connection
        pool", but per-@mention shellouts have no persistent
        connection to warm.
        """
        return None

    # --- the actual turn ----------------------------------------------

    def _run_one_turn(self, user_text, on_paragraph):
        """Spawn one shellout, write the user turn, drain to result, exit.

        Used by both complete() and complete_streaming(). When
        on_paragraph is None, runs in non-streaming mode (assistant text
        accumulated from terminal events). When provided, runs in
        streaming mode (text from stream_event content_block_delta
        payloads, with per-paragraph flush).

        Claims the warm-slot subprocess (slotted by `pre_warm`) if one
        is alive; otherwise spawns fresh. The warm subprocess has done
        its Node boot + MCP attach + --resume JSONL parse during the
        parked window but emits no events until it reads stdin — so
        the drain methods see `system_init` in-turn either way. The
        win materializes as a much smaller cli_init measurement on
        warm turns because the work happened in parallel with idle.

        Captures wall-time bookends (t_run_start, t_spawn_done, warm)
        so the drain methods can split ttft into per-stage costs in
        their TIMING log lines (spawn / cli_init / api_ttft). The
        `warm=1|0` flag groups warm and cold turns for comparison.
        """
        t_run_start = time.monotonic()
        proc = None
        out_q = None
        stderr_buf = None
        was_warm = False
        with self._warm_lock:
            if self._warm_proc is not None and self._warm_proc.poll() is None:
                proc = self._warm_proc
                out_q = self._warm_out_q
                stderr_buf = self._warm_stderr_buf
                self._warm_proc = None
                self._warm_out_q = None
                self._warm_stderr_buf = None
                was_warm = True
            elif self._warm_proc is not None:
                # Slot held a dead subprocess (idle timeout, crash, etc.).
                # Clean it up and fall through to cold spawn.
                self._terminate(self._warm_proc)
                self._warm_proc = None
                self._warm_out_q = None
                self._warm_stderr_buf = None

        if not was_warm:
            proc, out_q, stderr_buf = self._spawn_one()
        t_spawn_done = time.monotonic()
        try:
            envelope = {
                "type": "user",
                "message": {"role": "user", "content": user_text},
            }
            try:
                proc.stdin.write(json.dumps(envelope) + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                stderr_tail = "".join(list(stderr_buf)[-20:]) if stderr_buf else "(empty)"
                raise ClaudeCLIProtocolError(
                    f"claude subprocess stdin closed unexpectedly: {exc}\nstderr tail:\n{stderr_tail}"
                ) from exc

            timing = {
                "t_run_start": t_run_start,
                "t_spawn_done": t_spawn_done,
                "warm": was_warm,
            }
            if on_paragraph is None:
                return self._drain_non_streaming(out_q, stderr_buf, timing)
            return self._drain_streaming(out_q, stderr_buf, on_paragraph, timing)
        finally:
            self._terminate(proc)

    def _drain_non_streaming(self, out_q, stderr_buf, timing):
        """Drain the stream until the `result` event. Return ProviderResponse.

        No turn timeout: trust the user to /operator hangup if a tool
        chain runs long. Heartbeat watchdog removed in S211 (was
        harness-shaped — operator-imposed silence threshold that killed
        the subprocess from outside).
        """
        t_run_start = timing["t_run_start"]
        t_spawn_done = timing["t_spawn_done"]
        t_init: float | None = None
        text_parts: list[str] = []
        result_evt = None

        while True:
            self._fire_tick()
            try:
                kind, payload = out_q.get(timeout=0.5)
            except Empty:
                continue
            if kind == "eof":
                stderr_tail = "".join(list(stderr_buf)[-20:]) if stderr_buf else "(empty)"
                raise ClaudeCLIProtocolError(
                    f"claude subprocess exited mid-turn. stderr tail:\n{stderr_tail}"
                )
            if kind != "event":
                continue
            etype = payload.get("type")
            if etype == "system" and payload.get("subtype") == "init":
                if t_init is None:
                    t_init = time.monotonic()
                self._validate_init_event(payload)
            elif etype == "assistant":
                chunks, _ = self._dispatch_assistant_blocks(payload, accumulate_text=True)
                text_parts.extend(chunks)
            elif etype == "user":
                if self._user_event_carries_tool_result(payload):
                    self._check_user_event_for_denials(payload)
            elif etype == "result":
                result_evt = payload
                break

        elapsed = time.monotonic() - t_run_start
        subtype = result_evt.get("subtype") if result_evt else None
        log.info(
            f"TIMING claude_cli_turn={elapsed:.1f}s "
            f"{_format_stage_breakdown(t_run_start, t_spawn_done, t_init, None, warm=timing.get('warm', False))} "
            f"{_format_cache_stats(result_evt)} "
            f"result.subtype={subtype} text_chars={sum(len(p) for p in text_parts)}"
        )

        if subtype == "error_during_execution":
            raise ClaudeCLIProtocolError(
                f"claude reported error_during_execution: {result_evt.get('error', '')}"
            )

        return ProviderResponse(
            text="".join(text_parts) or None,
            tool_calls=[],
            stop_reason="end",
        )

    def _drain_streaming(self, out_q, stderr_buf, on_paragraph, timing):
        """Drain the stream, flushing paragraphs as they arrive.

        Top-level assistant text arrives as `stream_event` events of shape:
            {"type": "stream_event",
             "event": {"type": "content_block_delta",
                       "index": N,
                       "delta": {"type": "text_delta", "text": "..."}},
             "parent_tool_use_id": null | <id>}

        We only flush text where `parent_tool_use_id` is null — sub-
        agent deltas (parent_tool_use_id non-null) are not the bot's
        outgoing reply.
        """
        t_run_start = timing["t_run_start"]
        t_spawn_done = timing["t_spawn_done"]
        t_init: float | None = None
        t_first_token: float | None = None
        t_first_flush: float | None = None
        buffer = ""
        full_text_parts: list[str] = []
        result_evt = None

        while True:
            self._fire_tick()
            try:
                kind, payload = out_q.get(timeout=0.5)
            except Empty:
                continue
            if kind == "eof":
                stderr_tail = "".join(list(stderr_buf)[-20:]) if stderr_buf else "(empty)"
                raise ClaudeCLIProtocolError(
                    f"claude subprocess exited mid-turn. stderr tail:\n{stderr_tail}"
                )
            if kind != "event":
                continue
            etype = payload.get("type")
            if etype == "system" and payload.get("subtype") == "init":
                if t_init is None:
                    t_init = time.monotonic()
                self._validate_init_event(payload)
                continue
            if etype == "stream_event":
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
                # Sub-agent assistants arrive here with parent_tool_use_id
                # set; they're inner Task-tool output, not the bot's
                # reply. Skip.
                if payload.get("parent_tool_use_id"):
                    continue
                # Streaming reconstructs text from stream_event deltas
                # — we don't accumulate from assistant blocks here, just
                # dispatch tool_use to progress_callback.
                self._dispatch_assistant_blocks(payload, accumulate_text=False)
                # An `assistant` event finalizes one sub-message of the
                # turn. When the model calls a tool, the next text comes
                # in a NEW assistant message (indices reset to 0).
                # Without flushing here, "Hey — writing that now." and
                # "Done — ..." get concatenated as one paragraph.
                if buffer.strip():
                    if t_first_flush is None:
                        t_first_flush = time.monotonic()
                    flush_paragraphs(buffer, on_paragraph, force_final=True)
                    full_text_parts.append("\n\n")
                    buffer = ""
                continue
            if etype == "user":
                if self._user_event_carries_tool_result(payload):
                    self._check_user_event_for_denials(payload)
                continue
            if etype == "result":
                result_evt = payload
                break

        if buffer.strip():
            flush_paragraphs(buffer, on_paragraph, force_final=True)

        elapsed = time.monotonic() - t_run_start
        ttft = (t_first_token - t_run_start) if t_first_token else None
        first_flush = (t_first_flush - t_run_start) if t_first_flush else None
        ttft_str = f"{ttft:.1f}s" if ttft is not None else "n/a"
        flush_str = f"{first_flush:.1f}s" if first_flush is not None else "n/a"
        log.info(
            f"TIMING claude_cli_turn={elapsed:.1f}s ttft={ttft_str} first_flush={flush_str} streamed=1 "
            f"{_format_stage_breakdown(t_run_start, t_spawn_done, t_init, t_first_token, warm=timing.get('warm', False))} "
            f"{_format_cache_stats(result_evt)} "
            f"result.subtype={result_evt.get('subtype') if result_evt else 'n/a'}"
        )

        if result_evt is not None and result_evt.get("subtype") == "error_during_execution":
            raise ClaudeCLIProtocolError(
                f"claude reported error_during_execution: {result_evt.get('error', '')}"
            )

        final_text = "".join(full_text_parts).strip()

        return ProviderResponse(
            text=final_text or None,
            tool_calls=[],
            stop_reason="end",
        )
