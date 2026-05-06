"""
ChatRunner — polling loop that reads meeting chat and responds via LLM.

Usage:
    runner = ChatRunner(connector, llm)
    runner.run(meeting_url)   # blocks until stop() is called
"""
import logging
import re
import threading
import time

from _1_800_operator import config
from _1_800_operator.pipeline import ui
from _1_800_operator.pipeline.meeting_record import MeetingRecord, slug_from_url

log = logging.getLogger(__name__)

POLL_INTERVAL = 0.5  # seconds between read_chat() calls
PARTICIPANT_CHECK_INTERVAL = 3  # seconds between participant count checks
ONE_ON_ONE_THRESHOLD = 2  # participant count at or below = 1-on-1 mode (skip trigger phrase)

# Min wall-clock spacing between streamed paragraph posts. Two reasons:
# (a) Meet's chat panel rate-limits rapid sends and may swallow back-to-back
# messages, (b) staggered posts give the user's eye a chance to register
# each paragraph as a distinct message rather than a burst.
STREAM_PARAGRAPH_MIN_INTERVAL = 0.25


class ChatRunner:
    """Polls meeting chat and responds to messages."""

    def __init__(
        self,
        connector,
        llm,
        meeting_record: MeetingRecord | None = None,
        quiet_mode: bool = False,
    ):
        self._connector = connector
        self._llm = llm
        self._record = meeting_record
        # Slip mode: claude is "speak when spoken to." No intro, no
        # "Hold for Claude..." filler, no 1-on-1 trigger bypass —
        # claude only responds when explicitly addressed via
        # config.TRIGGER_PHRASE. Dial/deploy leave this False so the
        # original speak-up behavior holds.
        self._quiet_mode = quiet_mode
        self._stop_event = threading.Event()
        # Track messages we've sent so we can ignore our own echoes
        self._own_messages: set[str] = set()
        # Track message IDs we've already processed
        self._seen_ids: set[str] = set()
        # Per-turn heartbeat. Set in _handle_message, drained in
        # _dispatch_result on the terminal text branches. None means
        # "no turn in flight" — the heartbeat closer is a no-op so
        # intro sends don't get a phantom "Replied" line.
        self._turn_count = 0
        self._turn_start_ts: float | None = None
        # Self-intro on join. Background thread generates the text; main loop
        # posts it (so send_chat stays single-threaded). User-message
        # processing is deferred until the intro lands; messages that arrive
        # during the gap are persisted to the record as normal and buffered
        # for in-order replay once the intro posts.
        self._intro_ready = threading.Event()
        self._intro_text = ""
        # Quiet mode forces _intro_posted=True so the polling loop never
        # generates / sends an intro and never buffers pre-intro user
        # messages.
        self._intro_posted = self._quiet_mode
        self._hold_posted_at: float | None = None
        self._pre_intro_buffer: list[dict] = []
        # Bookkeeping for any future progress narrator: _last_send_time
        # is updated by _send so the gap since the last user-facing
        # message can be measured.
        self._last_send_time = 0.0
        # Serializes _send across threads. Playwright's sync API is
        # single-threaded by contract; the streaming-paragraph callback
        # (provider pump thread) and the main poll loop both call _send,
        # so concurrent connector.send_chat would race. The lock also
        # keeps _own_messages add + send_chat + record append atomic,
        # which prevents a partial-state observer from the read loop
        # seeing one without the other.
        self._send_lock = threading.Lock()
        # Most-recent-user-message bookkeeping for the recent-yes
        # auto-approval path in PermissionChatHandler. When the user has
        # JUST said "yes" (the message that triggered the current turn)
        # and the model immediately invokes a tool, the bridge would
        # otherwise sit waiting for a redundant second "yes". The handler
        # consults `latest_user_message()` and, if the most recent user
        # turn is unambiguously affirmative AND was not already consumed
        # by a prior gate (tracked in `_approval_msg_ids_used`), auto-
        # allows. Updated in the polling loop on every observed user
        # message; never cleared mid-meeting (stale entries fall out of
        # the recency window the handler enforces).
        self._latest_user_msg: tuple[str, str, float] | None = None
        self._approval_msg_ids_used: set[str] = set()
        # Loop-state. Promoted to self.* (vs. _loop locals) so the
        # _advance_intro_state / _check_participant_state / _process_messages
        # helpers can read+mutate without 4-tuple parameter passing. Lifetime
        # is one meeting (one ChatRunner instance per meeting).
        self._participant_count: int = 0
        self._saw_others: bool = False
        self._alone_since: float | None = None
        self._last_participant_check: float = 0.0

    def _wire_permissions(self):
        """Plug a chat-routed PreToolUse handler into the claude_cli provider.

        Every tool call inside the inner CLI subprocess fires a PreToolUse
        hook that round-trips through meeting chat. Reads auto-approve
        silently (the model narrates what it's looking up; the framework
        runs the tool); writes gate at a chat round-trip (the model asks
        a question, the user replies yes/no). Bypass the whole flow by
        passing `--yolo` (sets `--dangerously-skip-permissions` at spawn).

        Auto-approved patterns are deliberately conservative — anything
        not on this list falls through to a chat round-trip, which is the
        safe default. The patterns target the standard MCP naming
        convention `mcp__<server>__<verb>_<object>` plus the canonical
        claude-code built-in reads. If an MCP author names a destructive
        tool with a read-shaped verb (`mcp__x__list_and_burn`) we'd
        false-positive auto-approve it — accept that risk in exchange
        for friction-free read flows. The prompt rule
        (claude_cli._PRE_TOOL_VOICE_RULE) keeps the model's tier
        classification aligned with these patterns.
        """
        import os
        from _1_800_operator.pipeline.providers.claude_cli import ClaudeCLIProvider
        from _1_800_operator.pipeline.permission_chat_handler import PermissionChatHandler
        provider = getattr(self._llm, "_provider", None)
        if not isinstance(provider, ClaudeCLIProvider):
            return
        if os.environ.get("OPERATOR_YOLO") == "1":
            # Belt and suspenders alongside --dangerously-skip-permissions:
            # auto-approve every tool at the bridge so the handler never
            # gates anything. Without this, the bridge still calls the
            # handler under YOLO (the CLI flag overrides deny decisions
            # but doesn't suppress the hook itself), and the handler's
            # default behavior on unknown tools is to round-trip via chat.
            auto_approve = ["*"]
            log.info("ChatRunner: permission handler wired (YOLO — all tools auto-approve)")
        else:
            auto_approve = [
                # Claude's internal tool-schema discovery — meta, not
                # user-visible action.
                "ToolSearch",
                # Built-in claude-code reads.
                "Read", "Grep", "Glob", "LS", "WebSearch",
                # MCP read patterns (mcp__<server>__<verb>_<object>).
                "*__get_*",
                "*__list_*",
                "*__search_*",
                "*__find_*",
                "*__read_*",
                # MCP servers commonly expose bare-verb tools too —
                # `mcp__github__get_me`, `mcp__sentry__whoami`. The
                # bare-verb ones we want to auto-approve are typed out
                # explicitly to avoid over-broad globs.
                "*__whoami",
            ]
            log.info("ChatRunner: permission handler wired (reads auto-approve, writes gate)")
        handler = PermissionChatHandler(
            connector=self._connector,
            runner=self,
            auto_approve=auto_approve,
            always_ask=[],
        )
        provider.set_permission_handler(handler)

    def run(self, meeting_url):
        """Join the meeting and start the chat polling loop."""
        log.info(f"ChatRunner: joining {meeting_url}")
        self._wire_permissions()
        # Open a meeting record for this URL if one wasn't provided.
        if self._record is None:
            slug = slug_from_url(meeting_url)
            self._record = MeetingRecord(
                slug=slug,
                meta={"meet_url": meeting_url},
            )
            self._llm.set_record(self._record)
        # Intro generation is deferred to when `saw_others` flips in the
        # main loop, so we can scrape participant names/count first and
        # bake them into the prompt (lets the model address people by name
        # and pick singular vs plural greeting). Trades the ~3–5s parallel-
        # generation win for participant-aware framing; the HOLD_DURATION
        # beat usually overlaps the LLM call so the user-visible delay is
        # small. See _generate_intro for the kickoff.
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
                if "session_expired" in reason:
                    log.error("Re-export session: python scripts/auth_export.py")
                    ui.err("Not authenticated — run: python scripts/auth_export.py")
                elif "already_running" in reason:
                    ui.warn("Another Operator session is already running. Use --force to stop it and start a new one.")
                else:
                    ui.err(f"Join failed: {reason}")
                self._safe_leave()
                return
            if join_status.session_recovered:
                log.warning("ChatRunner: session recovered via cookie injection — "
                            "consider re-running scripts/auth_export.py")

        log.info("ChatRunner: joined")
        # Listening line — show trigger phrase + room state up front so
        # the user knows how to address the bot and whether 1-on-1 mode
        # is active. Best-effort participant count; falls back to a
        # generic message if the connector hasn't reported yet.
        try:
            seed_count = self._connector.get_participant_count()
        except Exception:
            seed_count = 0
        trigger = config.TRIGGER_PHRASE
        if self._quiet_mode:
            ui.ok(f"Listening for @{trigger} — quiet mode (won't speak unless addressed).")
        elif seed_count and seed_count <= ONE_ON_ONE_THRESHOLD:
            ui.ok(f"Joined as @{trigger} · solo (1-on-1 mode)")
        elif seed_count:
            ui.ok(f"Joined as @{trigger} · {seed_count} participants")
        else:
            ui.ok(f"Joined as @{trigger} — listening for chat.")
        log.info("ChatRunner: starting chat loop")
        self._loop()

    def _generate_intro(self, participant_names=None, participant_count=0):
        """Background-thread LLM call for the self-intro.

        Stores the result in _intro_text and signals via _intro_ready. The
        main loop is responsible for sending it (so send_chat is never
        called off-thread). On generation failure, _intro_text stays empty
        and the main loop will skip the post.

        Participant info (names/count) is scraped once at trigger time and
        passed in so the LLM can address people by name and pick the right
        singular/plural framing.
        """
        try:
            self._intro_text = self._llm.intro(
                participant_names=participant_names,
                participant_count=participant_count,
            )
        except Exception as e:
            log.error(f"ChatRunner: intro generation failed — skipping: {e}")
            self._intro_text = ""
        self._intro_ready.set()

    def stop(self):
        """Signal the polling loop to exit."""
        self._stop_event.set()

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
        """Main polling loop. Thin orchestrator — see _advance_intro_state,
        _check_participant_state, and _process_messages for the per-iteration
        work."""
        self._seed_loop_state()
        while not self._stop_event.is_set():
            # Detect unexpected browser session death (crash, page loss, etc.)
            if not self._connector.is_connected():
                log.warning("ChatRunner: connector disconnected unexpectedly — exiting loop")
                ui.err("Meeting connection lost — chat loop stopped.")
                break

            self._advance_intro_state()

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

            # Quiet mode (slip) requires the trigger phrase regardless of
            # participant count — claude must be explicitly addressed.
            # Dial/deploy keep the 1-on-1 bypass since claude is a
            # separate participant and ambient chat IS for it.
            one_on_one = (not self._quiet_mode) and (self._participant_count <= ONE_ON_ONE_THRESHOLD)
            self._process_messages(messages, one_on_one)

            self._stop_event.wait(POLL_INTERVAL)

    def _seed_loop_state(self):
        """Seed participant count immediately so the intro gate doesn't wait
        on the first read_chat + count cycle (~2s on slow joins). Best-effort:
        any failure falls through to the regular polling path on the first
        iteration."""
        try:
            self._participant_count = self._connector.get_participant_count()
            self._last_participant_check = time.time()
            if self._participant_count > 1:
                self._saw_others = True
                log.info(f"ChatRunner: seed participant_count={self._participant_count} (saw_others=True)")
        except Exception as e:
            log.warning(f"ChatRunner: seed get_participant_count failed: {e}")

    def _advance_intro_state(self):
        """Drive the intro state machine: hold-message post, intro generation
        kickoff, intro post, pre-intro buffer drain.

        Posts "Hold for <bot>..." once at least one human has been seen — Meet
        only renders messages received after a participant opens chat, so
        posting before a human is in the room means nobody ever sees it. Then
        kicks off intro generation in a background thread (so we can scrape
        participant names/count first and bake them into the LLM prompt).
        Once intro generation completes AND the connecting-beat floor has
        elapsed AND a human is present, posts the intro and drains any
        messages that arrived during the gap (writing them to the record at
        drain time so on-disk order matches processing order: assistant(intro)
        before user(Q1) — claude_cli rejects a tail ending in assistant).
        """
        if not self._quiet_mode and self._hold_posted_at is None and self._saw_others:
            self._send(f"Hold for {config.AGENT_NAME}...")
            self._hold_posted_at = time.time()
            try:
                names = self._connector.get_participant_names()
            except Exception as e:
                log.warning(f"ChatRunner: get_participant_names failed: {e}")
                names = []
            try:
                count = self._connector.get_participant_count()
            except Exception:
                count = 0
            threading.Thread(
                target=self._generate_intro,
                kwargs={"participant_names": names, "participant_count": count},
                daemon=True,
            ).start()

        # Floor only — if the LLM is slow we never wait *longer* than it does.
        hold_elapsed_ok = (
            self._hold_posted_at is not None
            and (time.time() - self._hold_posted_at) >= config.HOLD_DURATION_SECONDS
        )
        if not self._intro_posted and self._intro_ready.is_set() and self._saw_others and hold_elapsed_ok:
            if self._intro_text:
                self._send(self._intro_text)
            self._intro_posted = True
            if self._pre_intro_buffer:
                log.info(f"ChatRunner: draining {len(self._pre_intro_buffer)} pre-intro msg(s)")
                buffered = self._pre_intro_buffer
                self._pre_intro_buffer = []
                for buf in buffered:
                    if self._record is not None:
                        self._record.append(
                            sender=buf["sender"], text=buf["text"], kind=buf["kind"],
                        )
                    self._dispatch_user_message(buf["text"], buf["one_on_one"])

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
        elif self._saw_others and self._participant_count == 1:
            if self._alone_since is None:
                self._alone_since = now
                log.info("ChatRunner: alone in meeting — grace timer started")
            elif now - self._alone_since >= config.ALONE_EXIT_GRACE_SECONDS:
                log.info(
                    f"ChatRunner: alone for {int(now - self._alone_since)}s — auto-leaving"
                )
                ui.ok("Everyone left — dropping from the meeting.")
                self._safe_leave()
                return True
        return False

    def _process_messages(self, messages, one_on_one: bool):
        """Filter out own/seen/empty messages, persist new ones to the record
        (or buffer pre-intro), and dispatch them to the LLM router."""
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

            log.info(f"ChatRunner: new message sender={sender!r} id={msg_id!r} text={text!r} one_on_one={one_on_one}")
            # Stash for the recent-yes auto-approval path. The handler reads
            # this when its bridge fires; the time.monotonic() stamp is
            # what enforces the recency window.
            self._latest_user_msg = (msg_id or text, text, time.monotonic())

            # Pre-intro user messages: buffer in memory and persist at drain
            # time, AFTER the intro has been written to the record. If we
            # persisted now, the JSONL would order user→assistant→… which
            # leaves _tail_messages ending in role='assistant' on Q1 —
            # claude_cli rejects that. Holding the record.append until drain
            # forces the on-disk order to match the processing order:
            # assistant(intro) before user(Q1).
            if not self._intro_posted:
                self._pre_intro_buffer.append({
                    "text": text, "one_on_one": one_on_one,
                    "sender": sender, "kind": "chat",
                })
                continue

            if self._record is not None:
                self._record.append(sender=sender, text=text, kind="chat")

            self._dispatch_user_message(text, one_on_one)

        self._own_messages -= own_matched

    def _dispatch_user_message(self, text: str, one_on_one: bool):
        """Trigger-check a chat message and route it to the LLM if addressed.

        Called both from the live polling loop and from the post-intro buffer
        drain. Pure routing — message persistence and seen-id tracking happen
        upstream, before this is invoked.
        """
        trigger = config.TRIGGER_PHRASE.lower()
        has_trigger = trigger in text.lower()
        if has_trigger or one_on_one:
            if has_trigger:
                prompt = re.sub(
                    re.escape(config.TRIGGER_PHRASE) + r'[,:]?\s*',
                    '', text, count=1, flags=re.IGNORECASE,
                ).strip()
            else:
                prompt = text
            if prompt:
                self._handle_message(prompt)
        else:
            log.debug("ChatRunner: stored as context (no trigger phrase)")

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
        try:
            result = self._llm.ask(
                text, on_paragraph=self._streaming_callback(),
            )
        except Exception as e:
            log.error(f"ChatRunner: LLM call failed: {e}")
            # Pass only the exception class name to the narration prompt —
            # never the str(e) body, which can carry SDK response payloads,
            # tokens-bearing URLs, or upstream secrets. Full detail still
            # lands in /tmp/operator.log via the log.error above.
            self._narrate_failure(
                context=(
                    f"the meeting bot tried to answer the user's question but "
                    f"the LLM call failed with: {type(e).__name__}."
                ),
                fallback="Sorry — I couldn't reach my brain just now. Try again in a moment?",
            )
            return
        self._dispatch_result(result)

    def _dispatch_result(self, result):
        """Route an LLM result (text or context_overflow).

        claude_cli owns its own tool loop, so the only result shapes that
        reach here are text (streamed or non-streamed) and context_overflow.
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
        elif kind == "context_overflow":
            self._send("Our conversation got too long — I've cleared the history. What would you like to do next?")
            self._emit_turn_done()
        else:
            log.error(f"_dispatch_result: unknown result shape {result!r}")
            # Don't pass the raw payload into the narration prompt — same
            # reason as the LLM-failure path above. Full repr is in the log.
            self._narrate_failure(
                context=(
                    "the previous turn produced a result the meeting bot "
                    "doesn't know how to render in chat (unknown shape)."
                ),
                fallback="Sorry — something went wrong on my end. Try again?",
            )

    def _narrate_failure(self, *, context: str, fallback: str):
        """Hand a failure context to the LLM via a small no-tools call asking
        for a plain-text reply the user can act on. One-shot: the call uses
        record=False (not persisted to the meeting record) and
        retry_rate_limits=False (don't make the user wait through a second
        retry window after the original call already exhausted its retries).
        On any narration failure (call raises, returns empty), posts the
        hardcoded `fallback`. Always emits turn_done(failed=True).
        """
        prompt = (
            f"Internal note (do not echo verbatim): {context} "
            f"Send a short plain-text reply telling the user what happened "
            f"and what to try next (retry, wait, ping the operator host, etc.). "
            f"Do not call any tool."
        )
        try:
            narrated = self._llm.ask(
                prompt, record=False, retry_rate_limits=False,
            )
        except Exception as e:
            log.error(f"_narrate_failure call failed: {e}")
            narrated = None
        if isinstance(narrated, str) and narrated.strip():
            self._send(narrated)
        else:
            self._send(fallback)
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

    def _send(self, text, kind: str = "chat"):
        """Send a chat message, append it to the meeting record, and track it as our own.

        `kind` is persisted to the record but filtered by `pipeline/llm.py` when
        building the LLM prompt (only `chat` and `caption` are replayed).

        Own-message dedup: primary path is by message ID — when the connector
        returns the new `data-message-id` it captured post-send, we add it to
        `_seen_ids` so the read path's later observation gets short-circuited
        at the ID check. The text-match path (`_own_messages`) is the fallback
        for adapters that can't return an ID (linux) or when the ID read-back
        times out; we store text stripped so DOM normalization (trailing
        newlines etc.) doesn't break the comparison.
        """
        text_normalized = text.strip()
        with self._send_lock:
            self._own_messages.add(text_normalized)
            try:
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
