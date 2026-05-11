"""
ChatRunner — polling loop that reads meeting chat and responds via LLM.

Usage:
    runner = ChatRunner(connector, llm)
    runner.run(meeting_url)   # blocks until stop() is called
"""
import logging
import queue
import re
import threading
import time

from _1_800_operator import config
from _1_800_operator.bridges.claude import REPLY_PREFIX_OPERATOR
from _1_800_operator.pipeline import ui
from _1_800_operator.pipeline.meeting_record import MeetingRecord, slug_from_url

log = logging.getLogger(__name__)

POLL_INTERVAL = 0.5  # seconds between read_chat() calls
PARTICIPANT_CHECK_INTERVAL = 3  # seconds between participant count checks

# Min wall-clock spacing between streamed paragraph posts. Two reasons:
# (a) Meet's chat panel rate-limits rapid sends and may swallow back-to-back
# messages, (b) staggered posts give the user's eye a chance to register
# each paragraph as a distinct message rather than a burst.
STREAM_PARAGRAPH_MIN_INTERVAL = 0.25

# Throttle for operator-voice tool-use narration (`[☎️ Operator] running
# <tool>: <args>` posts). progress_callback fires per tool_use block as
# the model emits them; without throttling, fast tool chains would spam
# chat (`[☎️ Operator] running ls`, `cat`, `grep`, `ls`, `cat`...). After
# posting one narration, suppress further posts until this many seconds
# have passed; on the next tool_use after cooldown, post the latest tool.
# 20s lands in the user-expressed 20-30s comfort window — long enough that
# rapid chains collapse into one line, short enough that genuinely long
# tool runs (90s test suite) get periodic "still working" updates.
TOOL_NARRATION_THROTTLE_SECONDS = 20.0


class ChatRunner:
    """Polls meeting chat and responds to messages."""

    def __init__(
        self,
        connector,
        llm,
        meeting_record: MeetingRecord | None = None,
    ):
        self._connector = connector
        self._llm = llm
        self._record = meeting_record
        self._stop_event = threading.Event()
        # Track messages we've sent so we can ignore our own echoes
        self._own_messages: set[str] = set()
        # Track message IDs we've already processed
        self._seen_ids: set[str] = set()
        # Per-turn heartbeat. Set in _handle_message, drained in
        # _dispatch_result on the terminal text branches. None means
        # "no turn in flight" so the heartbeat closer is a no-op.
        self._turn_count = 0
        self._turn_start_ts: float | None = None
        # Bookkeeping for the streaming paragraph callback's pacer.
        # `_last_send_time` is updated by `_send` after a successful
        # post; the streaming on_paragraph closure reads it to enforce
        # STREAM_PARAGRAPH_MIN_INTERVAL between back-to-back
        # paragraph posts.
        self._last_send_time = 0.0
        # Serializes _send across threads. Playwright's sync API is
        # single-threaded by contract; the streaming-paragraph callback
        # (provider pump thread) and the main poll loop both call _send,
        # so concurrent connector.send_chat would race. The lock also
        # keeps _own_messages add + send_chat + record append atomic,
        # which prevents a partial-state observer from the read loop
        # seeing one without the other.
        self._send_lock = threading.Lock()
        # Loop-state. Promoted to self.* (vs. _loop locals) so the
        # _check_participant_state / _process_messages helpers can read+mutate
        # without parameter passing. Lifetime is one meeting (one ChatRunner
        # instance per meeting).
        self._participant_count: int = 0
        self._saw_others: bool = False
        self._alone_since: float | None = None
        self._last_participant_check: float = 0.0
        # Operator-voice tool-narration throttle. Stamps the monotonic
        # time of the last `[☎️ Operator] running <tool>` post so the
        # progress_callback can suppress further narrations for
        # TOOL_NARRATION_THROTTLE_SECONDS after each one. Reset to 0.0
        # at turn start so each new @mention's first tool_use fires
        # narration without delay.
        self._last_tool_narration_ts: float = 0.0
        # Per-turn dedup for `[☎️ Operator] permission denied for X`
        # hints. Operator names `--yolo` once per @mention even if
        # multiple tools are denied — repeating the same recovery
        # suggestion on every chained denial is noise. Cleared at turn
        # start in _handle_message.
        self._denied_tool_ids_in_turn: set[str] = set()
        # Cached LLM provider. Set by _wire_provider when the provider
        # is a ClaudeCLIProvider; remains None otherwise. stop() calls
        # provider.stop() so a SIGINT shutdown doesn't race a mid-turn
        # restart.
        self._provider = None
        # Thread-routing for outbound chat sends. Playwright's sync API
        # is single-threaded by contract — only the thread that opened
        # the Page may call its methods. The polling loop owns the
        # Page, so any _send call from another thread (the per-turn
        # heartbeat daemon, primarily) gets enqueued here and drained
        # on the polling thread (between turns) and on the provider's
        # out-queue tick (during turns — the polling thread is blocked
        # inside _send_and_collect_streaming, but it cycles through
        # out_q.get every 0.5s and we drain on each cycle).
        self._main_thread = threading.current_thread()
        self._send_queue: queue.Queue[tuple[str, str]] = queue.Queue()

    def _wire_provider(self):
        """Cache the ClaudeCLIProvider and wire its observability callbacks.

        Operator does not impose its own permission layer — Claude Code's
        native rules apply (governed by `--dangerously-skip-permissions`
        when --yolo is set, otherwise by the user's
        `~/.claude/settings.json`). This method wires the four
        operator-side observability callbacks the provider exposes:

          - progress_callback: posts `[☎️ Operator] running <tool>: <args>`
            to chat in operator's switchboard voice, throttled to one
            post per TOOL_NARRATION_THROTTLE_SECONDS so fast tool chains
            don't spam.
          - tick_callback: drains off-thread queued sends on the polling
            thread during in-turn out-queue iteration (the polling
            thread is parked inside `complete_streaming` for the duration
            of a turn; without this drain, operator-voice sends queued
            from the provider's pump thread would wait until the turn
            finished and post in a burst at end-of-stream).
          - denial_callback: posts `[☎️ Operator] permission denied for
            X — re-run with --yolo to skip per-tool approval` once per
            tool_use_id per turn when Claude Code blocks a tool call.
          - connection_callback: posts switchboard-voice status when
            inner-claude crashes mid-stream (`[☎️ Operator] connection
            dropped — reconnecting…` → retry → `connection restored` or
            `couldn't reach Claude`).

        Caching `self._provider` lets stop() also stop the provider so
        SIGINT doesn't race a mid-turn EOF retry.
        """
        from _1_800_operator.pipeline.providers.claude_cli import ClaudeCLIProvider
        provider = getattr(self._llm, "_provider", None)
        if not isinstance(provider, ClaudeCLIProvider):
            return
        provider.set_progress_callback(self._narrate_tool_use)
        provider.set_tick_callback(self._drain_pending_sends)
        provider.set_denial_callback(self._narrate_denial)
        provider.set_connection_callback(self._narrate_connection)
        self._provider = provider
        log.info("ChatRunner: provider wired (no permission gate; Claude Code defaults apply)")

    def _narrate_tool_use(self, tool_name: str, tool_input: dict):
        """Progress callback hooked into ClaudeCLIProvider — posts a
        `[☎️ Operator] running <tool>: <args>` line to meeting chat,
        throttled to one post per TOOL_NARRATION_THROTTLE_SECONDS.

        Runs on the provider's reader thread, NOT the main Playwright-
        owning thread, so `_send` enqueues onto `_send_queue` and the
        provider's tick callback drains it on the polling thread.

        Filter: skip ToolSearch (internal tool-schema loader the user
        doesn't need to see) — same filter rule the old
        `_PRE_TOOL_VOICE_RULE` carried before it was stripped.
        """
        if not tool_name:
            return
        if tool_name == "ToolSearch":
            return
        now = time.monotonic()
        if now - self._last_tool_narration_ts < TOOL_NARRATION_THROTTLE_SECONDS:
            return
        try:
            summary = self._summarize_tool_input(tool_input or {})
            line = f"running {tool_name}"
            if summary:
                line = f"{line}: {summary}"
            self._send(REPLY_PREFIX_OPERATOR + line, kind="operator_status", raw=True)
            self._last_tool_narration_ts = now
        except Exception as e:
            log.warning(f"ChatRunner: _narrate_tool_use failed: {e}")

    def _narrate_denial(self, tool_use_id: str):
        """Denial callback — posts an operator-voice hint about `--yolo`
        when Claude Code blocks a tool call. Once per tool_use_id per
        turn so chained denials don't spam the same recovery suggestion.
        """
        if tool_use_id in self._denied_tool_ids_in_turn:
            return
        self._denied_tool_ids_in_turn.add(tool_use_id)
        try:
            self._send(
                REPLY_PREFIX_OPERATOR
                + "permission denied — re-run with --yolo to skip per-tool approval",
                kind="operator_status",
                raw=True,
            )
        except Exception as e:
            log.warning(f"ChatRunner: _narrate_denial failed: {e}")

    def _narrate_connection(self, event: str):
        """Connection callback — posts switchboard-voice status when the
        inner-claude shellout crashes mid-stream and we're retrying with
        `--resume`. event ∈ {"dropped", "reconnecting", "failed"}.
        """
        if event == "dropped":
            text = "connection dropped — reconnecting…"
        elif event == "reconnecting":
            # We posted "dropped" already; "reconnecting…" is implied by
            # the ellipsis. Suppress to avoid double-posting the same
            # state. Kept on the callback contract for symmetry in case
            # the provider's retry path ever fires the two events
            # independently.
            return
        elif event == "failed":
            text = "couldn't reach Claude — try @mentioning again in a moment"
        else:
            log.warning(f"ChatRunner: unknown connection event {event!r}")
            return
        try:
            self._send(REPLY_PREFIX_OPERATOR + text, kind="operator_status", raw=True)
        except Exception as e:
            log.warning(f"ChatRunner: _narrate_connection failed: {e}")

    @staticmethod
    def _summarize_tool_input(tool_input: dict) -> str:
        """Format tool args for a one-line operator-voice narration.

        Picks the most informative single argument: `file_path` for
        Read/Edit/Write, `command` for Bash, `pattern` for Grep, etc.
        Falls back to a `(arg-key-list)` sentinel when no preferred key
        matches. Caps individual arg length at 100 chars so a giant
        Bash command doesn't blow the chat line length.
        """
        if not tool_input:
            return ""
        preferred_keys = (
            "file_path", "command", "pattern", "query",
            "url", "prompt", "path",
        )
        for key in preferred_keys:
            if key in tool_input:
                val = tool_input[key]
                if isinstance(val, str):
                    return val if len(val) < 100 else val[:100] + "…"
                return str(val)[:100]
        return "(" + ", ".join(tool_input.keys())[:80] + ")"

    def run(self, meeting_url):
        """Join the meeting and start the chat polling loop."""
        log.info(f"ChatRunner: joining {meeting_url}")
        self._wire_provider()
        # Open a meeting record for this URL if one wasn't provided.
        if self._record is None:
            slug = slug_from_url(meeting_url)
            self._record = MeetingRecord(
                slug=slug,
                meta={"meet_url": meeting_url},
            )
            self._llm.set_record(self._record)
        # Skip join if connector was already started (e.g. for parallel MCP init)
        if not self._connector.join_status:
            self._connector.join(meeting_url)

        # Wait for browser to actually join
        join_status = self._connector.join_status
        if join_status:
            join_timeout = config.LOBBY_WAIT_SECONDS + 60
            if not join_status.ready.wait(timeout=join_timeout):
                log.error(f"ChatRunner: join timed out ({join_timeout}s)")
                self._safe_leave()
                return
            if not join_status.success:
                reason = join_status.failure_reason or "unknown"
                log.error(f"ChatRunner: join failed: {reason}")
                ui.err(f"Join failed: {reason}")
                self._safe_leave()
                return

        log.info("ChatRunner: joined")
        trigger = config.TRIGGER_PHRASE
        ui.ok(f"Listening for {trigger} — claude only replies when addressed.")
        log.info("ChatRunner: starting chat loop")
        self._loop()

    def stop(self):
        """Signal the polling loop to exit and tear down the LLM provider.

        Calling provider.stop() before the safety net SIGKILLs the
        subprocess closes the race where the provider's mid-turn
        restart path would otherwise spawn a fresh claude subprocess
        right as operator is shutting down.
        """
        self._stop_event.set()
        if self._provider is not None:
            try:
                self._provider.stop()
            except Exception as e:
                log.warning(f"ChatRunner: provider.stop raised: {e}")

    def _safe_leave(self):
        """Wrap connector.leave() — if it raises (e.g. Playwright already
        torn down), don't compound a primary error with a stack trace from
        the cleanup attempt. Used in error paths (join timeout, join failure,
        auto-leave) where leave() is best-effort cleanup, not the main work."""
        try:
            self._connector.leave()
        except Exception as e:
            log.warning(f"ChatRunner: connector.leave raised during cleanup: {e}")

    def _loop(self):
        """Main polling loop. Thin orchestrator — see
        _check_participant_state and _process_messages for the per-iteration
        work."""
        self._seed_loop_state()
        while not self._stop_event.is_set():
            # Detect unexpected browser session death (crash, page loss, etc.)
            if not self._connector.is_connected():
                log.warning("ChatRunner: connector disconnected unexpectedly — exiting loop")
                ui.err("Meeting connection lost — chat loop stopped.")
                break

            try:
                messages = self._connector.read_chat()
            except Exception as e:
                log.warning(f"ChatRunner: read_chat failed: {e}")
                messages = []

            # Bail out before doing any more work if shutdown fired while we
            # were blocked in read_chat — prevents a stray final iteration
            # (and its participant-count log) after SIGINT.
            if self._stop_event.is_set():
                break

            if self._check_participant_state():
                # Auto-leave fired — connector.leave() already called.
                return
            if self._stop_event.is_set():
                break

            # Slip mode is "speak when spoken to" — claude only responds
            # to messages containing the trigger phrase, regardless of
            # participant count.
            self._process_messages(messages)

            # Flush any sends queued by off-thread callers since the
            # last iteration. Between-turn coverage; in-turn drain
            # happens via the provider's tick callback (set in
            # _wire_provider) since this loop is blocked inside the
            # LLM call once a turn is in flight.
            self._drain_pending_sends()

            self._stop_event.wait(POLL_INTERVAL)

    def _seed_loop_state(self):
        """Seed participant count immediately so the auto-leave alone-since
        timer doesn't wait on the first read_chat + count cycle (~2s on
        slow joins). Best-effort: any failure falls through to the regular
        polling path on the first iteration."""
        try:
            self._participant_count = self._connector.get_participant_count()
            self._last_participant_check = time.time()
            if self._participant_count > 1:
                self._saw_others = True
                log.info(f"ChatRunner: seed participant_count={self._participant_count} (saw_others=True)")
        except Exception as e:
            log.warning(f"ChatRunner: seed get_participant_count failed: {e}")

    def _check_participant_state(self) -> bool:
        """Refresh participant count on a PARTICIPANT_CHECK_INTERVAL cadence
        and run the alone-since auto-leave timer. Returns True iff auto-leave
        fired (and the connector was already told to leave) — caller exits
        the loop."""
        now = time.time()
        if now - self._last_participant_check < PARTICIPANT_CHECK_INTERVAL:
            return False

        self._last_participant_check = now
        try:
            new_count = self._connector.get_participant_count()
            if self._stop_event.is_set():
                return False
            if new_count != self._participant_count:
                log.info(f"ChatRunner: participant count changed {self._participant_count} → {new_count}")
            self._participant_count = new_count
        except Exception as e:
            log.warning(f"ChatRunner: get_participant_count failed: {e}")

        if self._participant_count > 1:
            self._saw_others = True
            self._alone_since = None
        elif self._participant_count == 0 or (
            self._saw_others and self._participant_count == 1
        ):
            # Two cases share the auto-leave grace timer:
            #   - count == 1 after _saw_others=True: the original "everyone
            #     left, just me here" path.
            #   - count == 0 (regardless of _saw_others): the bot is no
            #     longer on the participant list at all — Meet booted the
            #     tab back to its landing page (lobby idle timeout, host
            #     declined admission and the request expired, network drop
            #     re-routed the tab, etc.). Without this branch the chat
            #     loop polls forever after a bounce because _saw_others
            #     stayed False (bot was alone in the lobby the whole time)
            #     and the original branch only fires when count==1. The
            #     grace window absorbs transient 0s (Meet briefly drops
            #     the participant panel during reconnect/state changes).
            if self._alone_since is None:
                self._alone_since = now
                if self._participant_count == 0:
                    log.info(
                        "ChatRunner: participant count is 0 — booted from meeting? "
                        "grace timer started"
                    )
                else:
                    log.info("ChatRunner: alone in meeting — grace timer started")
            elif now - self._alone_since >= config.ALONE_EXIT_GRACE_SECONDS:
                elapsed = int(now - self._alone_since)
                if self._participant_count == 0:
                    log.info(
                        f"ChatRunner: participant count 0 for {elapsed}s — "
                        "auto-leaving (likely booted from meeting)"
                    )
                    ui.warn("Lost the meeting — dropping out.")
                else:
                    log.info(
                        f"ChatRunner: alone for {elapsed}s — auto-leaving"
                    )
                    ui.ok("Everyone left — dropping from the meeting.")
                self._safe_leave()
                return True
        else:
            # count == 1 and _saw_others == False (slip-mode lobby wait,
            # 1-on-1 mode pre-arrival). Not a leave condition.
            self._alone_since = None
        return False

    def _process_messages(self, messages):
        """Filter out own/seen/empty messages, persist new ones to the record,
        and dispatch them to the LLM router."""
        # Track which own-message texts matched this batch so we can discard
        # AFTER the full batch — Meet creates multiple DOM elements per
        # message (different IDs, same text), so we must keep the text in
        # the set until all duplicates are filtered.
        own_matched = set()

        for msg in messages:
            msg_id = msg.get("id", "")
            text = msg.get("text", "").strip()
            sender = msg.get("sender", "").strip()

            if msg_id and msg_id in self._seen_ids:
                continue
            if msg_id:
                self._seen_ids.add(msg_id)

            if not text:
                continue

            # Skip our own messages. Primary path is the ID-based dedup
            # above (msg_id added to _seen_ids by `_send`); these two checks
            # are fallbacks for adapters that can't return an ID, or when
            # the post-send DOM read-back timed out. Text match compares
            # stripped strings since Meet's DOM strips trailing whitespace
            # on render — exact-equality comparison broke session-164's
            # stuck-LLM watchdog (`...hang tight.\n\n` sent vs `...hang
            # tight.` read back) and triggered a self-reply cascade.
            if sender and sender.lower() == config.AGENT_NAME.lower():
                log.debug(f"ChatRunner: skipping own message (sender={sender!r})")
                continue
            if not sender and text in self._own_messages:
                log.debug(f"ChatRunner: skipping own message (text match)")
                own_matched.add(text)
                continue

            log.info(f"ChatRunner: new message sender={sender!r} id={msg_id!r} text={text!r}")

            if self._record is not None:
                self._record.append(sender=sender, text=text, kind="chat")

            self._dispatch_user_message(text)

        self._own_messages -= own_matched

    def _dispatch_user_message(self, text: str):
        """Trigger-check a chat message and route it to the LLM if addressed.

        Pure routing — message persistence and seen-id tracking happen
        upstream, before this is invoked.
        """
        trigger = config.TRIGGER_PHRASE.lower()
        if trigger not in text.lower():
            log.debug("ChatRunner: stored as context (no trigger phrase)")
            return
        prompt = re.sub(
            re.escape(config.TRIGGER_PHRASE) + r'[,:]?\s*',
            '', text, count=1, flags=re.IGNORECASE,
        ).strip()
        if prompt:
            self._handle_message(prompt)

    def _emit_turn_done(self, *, failed: bool = False):
        """Close out the per-turn stdout heartbeat.

        Drains _turn_start_ts so the next call is a no-op (idempotent).
        Reports elapsed wall time only — claude_cli runs its own tool
        loop internally, so operator never sees individual tool calls.
        """
        if self._turn_start_ts is None:
            return
        elapsed = time.time() - self._turn_start_ts
        self._turn_start_ts = None
        if failed:
            ui.err(f"Turn {self._turn_count} failed — {elapsed:.1f}s")
        else:
            ui.ok(f"Replied — {elapsed:.1f}s")

    def _handle_message(self, text):
        """Process a single chat message via LLM."""
        self._turn_count += 1
        self._turn_start_ts = time.time()
        # Reset per-turn observability state so the new @mention's first
        # tool_use fires narration without delay (throttle stamp) and
        # any prior turn's denial dedup doesn't leak.
        self._last_tool_narration_ts = 0.0
        self._denied_tool_ids_in_turn.clear()
        try:
            result = self._llm.ask(
                text, on_paragraph=self._streaming_callback(),
            )
        except Exception as e:
            log.error(f"ChatRunner: LLM call failed: {e}")
            # The provider's connection_callback already posted a
            # `[☎️ Operator] couldn't reach Claude…` line for EOF /
            # protocol errors before raising. Other exception types
            # (binary missing, subscription mis-configured, etc.) skip
            # that path; map them to a switchboard-voice line keyed on
            # the exception class name. Never echo str(e) into chat —
            # it can carry response payloads, token-bearing URLs, or
            # upstream secrets. Full detail still lands in
            # /tmp/operator.log via the log.error above.
            exc_name = type(e).__name__
            if "NotFound" in exc_name:
                msg = "claude CLI not found — install from claude.ai/code"
            elif "Subscription" in exc_name:
                msg = "claude subscription not detected — run `claude auth status`"
            elif "Protocol" in exc_name:
                # connection_callback already posted "couldn't reach
                # Claude" before raising; don't double-post.
                self._emit_turn_done(failed=True)
                return
            else:
                msg = f"hit an unexpected snag ({exc_name}) — try @mentioning again"
            self._narrate_failure(msg)
            return
        self._dispatch_result(result)

    def _dispatch_result(self, result):
        """Route an LLM result.

        claude_cli owns its own tool loop, so the only result shapes that
        reach here are text (streamed or non-streamed). Anything else is
        a bug — operator narrates the failure in switchboard voice.
        """
        if isinstance(result, str):
            self._send(result)
            self._emit_turn_done()
            return
        kind = result.get("type")
        if kind == "text":
            # Streaming path already posted each paragraph via on_paragraph.
            if not result.get("streamed"):
                self._send(result["content"])
            self._emit_turn_done()
        else:
            log.error(f"_dispatch_result: unknown result shape {result!r}")
            self._narrate_failure(
                "something came back I couldn't render — try @mentioning again",
            )

    def _narrate_failure(self, message: str):
        """Post an operator-voice failure line and close the turn.

        Switchboard-voice direct post — no model in the loop, no
        operator-authored prompts fed into `claude -p`. Pre-14.22.3 this
        method spawned a one-shot LLM call to author a model-voice
        failure narration; that pattern was harness-shaped (operator-
        authored prompt → claude -p subprocess) and got stripped in
        S211 along with the heartbeat side-channel. The replacement is
        a direct chat post in operator's voice — visible, transparent,
        and free of any spawn-signature impact.

        Skipped during shutdown: the only "failures" reaching us post-
        stop are subprocess-killed-by-safety-net and other shutdown
        artifacts — narrating them would post into a chat panel that's
        already detaching.
        """
        if self._stop_event.is_set():
            log.info("ChatRunner: skipping failure narration — shutdown in progress")
            self._emit_turn_done(failed=True)
            return
        try:
            self._send(REPLY_PREFIX_OPERATOR + message, kind="operator_status", raw=True)
        except Exception as e:
            log.warning(f"ChatRunner: _narrate_failure post failed: {e}")
        self._emit_turn_done(failed=True)

    def _streaming_callback(self):
        """Build an on_paragraph closure for the current LLM call.

        Each invocation posts the paragraph via _send() (so it lands in chat
        AND the meeting record). Enforces STREAM_PARAGRAPH_MIN_INTERVAL between
        posts so Meet's chat panel doesn't swallow back-to-back messages and
        so the user perceives each paragraph as a distinct chat bubble.
        """
        last = [0.0]
        def on_paragraph(text: str):
            elapsed = time.monotonic() - last[0]
            if elapsed < STREAM_PARAGRAPH_MIN_INTERVAL:
                time.sleep(STREAM_PARAGRAPH_MIN_INTERVAL - elapsed)
            self._send(text)
            last[0] = time.monotonic()
        return on_paragraph

    def _send(self, text, kind: str = "chat", raw: bool = False):
        """Send a chat message, append it to the meeting record, and track it as our own.

        `kind` is persisted to the record but filtered by `pipeline/llm.py` when
        building the LLM prompt (only `chat` and `caption` are replayed;
        `operator_status` is recorded for the local JSONL but not replayed
        back into the model's prompt context).

        `raw=True` posts via the connector's `send_chat_raw` (no slip
        reply-prefix prepended), used for operator-voice status lines that
        already carry `[☎️ Operator] `. Without raw=True the connector
        prepends the slip bot prefix `[🤖 Claude] ` so meeting participants
        can tell Claude's replies apart from the user's own messages.

        Own-message dedup: primary path is by message ID — when the connector
        returns the new `data-message-id` it captured post-send, we add it to
        `_seen_ids` so the read path's later observation gets short-circuited
        at the ID check. The text-match path (`_own_messages`) is the fallback
        for adapters that can't return an ID (linux) or when the ID read-back
        times out; we store text stripped so DOM normalization (trailing
        newlines etc.) doesn't break the comparison.

        Off-thread callers (provider's reader thread for operator-voice
        narration) get their send enqueued instead of executed inline —
        Playwright's sync API rejects calls from any thread other than the
        one that opened the Page, and silent failure inside the connector
        would otherwise look like a successful post in the log. The polling
        loop and the provider's out-queue tick drain the queue on the main
        thread.
        """
        if threading.current_thread() is not self._main_thread:
            self._send_queue.put((text, kind, raw))
            return
        text_normalized = text.strip()
        with self._send_lock:
            self._own_messages.add(text_normalized)
            try:
                if raw and hasattr(self._connector, "send_chat_raw"):
                    msg_id = self._connector.send_chat_raw(text)
                else:
                    msg_id = self._connector.send_chat(text)
            except Exception as e:
                log.error(f"ChatRunner: send_chat failed: {e}")
                self._own_messages.discard(text_normalized)
                return
            # Record only after successful send — otherwise the LLM's next
            # turn replays a phantom assistant message the user never
            # received.
            if self._record is not None:
                self._record.append(sender=config.AGENT_NAME, text=text, kind=kind)
            if msg_id:
                self._seen_ids.add(msg_id)
            self._last_send_time = time.time()

    def _drain_pending_sends(self):
        """Flush any queued off-thread sends on the main thread.

        Called from two places, both on the main (Playwright-owning)
        thread: the polling loop between iterations (covers between-turn
        sends) and the provider's out-queue tick (covers during-turn
        sends, when the polling thread is blocked inside the LLM call).
        Bounded per call so a flood doesn't starve the caller.
        """
        drained = 0
        while drained < 16:
            try:
                text, kind, raw = self._send_queue.get_nowait()
            except queue.Empty:
                return
            self._send(text, kind=kind, raw=raw)
            drained += 1
