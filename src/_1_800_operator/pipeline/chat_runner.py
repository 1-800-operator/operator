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

    def _wire_permissions(self):
        """Plug a chat-routed PreToolUse handler into the claude_cli provider.

        Every tool call inside the inner CLI subprocess fires a PreToolUse
        hook that round-trips through meeting chat: operator posts a
        confirmation prompt, the user replies "ok"/"no", the handler
        returns allow/deny to the CLI. Without this, the CLI's default
        prompt would block on a tty that doesn't exist (we spawn under
        PIPE) and every tool call would silently deny.

        v1 ships no per-bot allow/ask lists — every tool prompts.
        Bypass the whole flow by passing `--yolo` (sets
        `--dangerously-skip-permissions` at spawn).
        """
        from _1_800_operator.pipeline.providers.claude_cli import ClaudeCLIProvider
        from _1_800_operator.pipeline.permission_chat_handler import PermissionChatHandler
        provider = getattr(self._llm, "_provider", None)
        if not isinstance(provider, ClaudeCLIProvider):
            return
        handler = PermissionChatHandler(
            connector=self._connector,
            runner=self,
            auto_approve=[],
            always_ask=[],
        )
        provider.set_permission_handler(handler)
        log.info("ChatRunner: permission handler wired (ask-on-every-tool)")

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
                self._connector.leave()
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
                self._connector.leave()
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

    def _loop(self):
        """Main polling loop."""
        # Seed participant count immediately so the intro gate doesn't
        # wait on the first read_chat + count cycle (~2s on slow joins).
        # Best-effort: any failure falls through to the regular polling
        # path on the first iteration.
        last_participant_check = 0
        participant_count = 0
        saw_others = False
        try:
            participant_count = self._connector.get_participant_count()
            last_participant_check = time.time()
            if participant_count > 1:
                saw_others = True
                log.info(f"ChatRunner: seed participant_count={participant_count} (saw_others=True)")
        except Exception as e:
            log.warning(f"ChatRunner: seed get_participant_count failed: {e}")
        alone_since = None
        while not self._stop_event.is_set():
            # Detect unexpected browser session death (crash, page loss, etc.)
            if not self._connector.is_connected():
                log.warning("ChatRunner: connector disconnected unexpectedly — exiting loop")
                ui.err("Meeting connection lost — chat loop stopped.")
                break

            # Post the self-intro the first iteration after generation completes
            # AND at least one human has been seen in the meeting — so the intro
            # lands in front of someone, not into an empty room before they
            # reach Meet's pre-join screen (the meet.new path: bot joins
            # instantly, user takes 5–15s to get through pre-join + open chat,
            # and Meet only shows messages received after you've opened chat).
            # Drain anything buffered during the gap once it does post.
            # "Hold for <bot>..." line — first chat post, gated on saw_others
            # for the same reason the intro is: Meet only renders messages
            # received after a participant opens chat, so posting before a
            # human is in the room means nobody ever sees it. Stamp the post
            # time so the intro gate below can enforce the connecting-beat
            # floor. We also kick off intro generation here (rather than at
            # run() start) so we can scrape participant names/count first
            # and bake them into the LLM prompt.
            if not self._quiet_mode and self._hold_posted_at is None and saw_others:
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

            # Enforce a minimum gap after the "Hold for <bot>..." line so the
            # "connecting you now" beat registers even if intro generation
            # finishes faster than humans can read. Floor only — if the LLM
            # is slow we never wait *longer* than it does.
            hold_elapsed_ok = (
                self._hold_posted_at is not None
                and (time.time() - self._hold_posted_at) >= config.HOLD_DURATION_SECONDS
            )
            if not self._intro_posted and self._intro_ready.is_set() and saw_others and hold_elapsed_ok:
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

            # Periodically refresh participant count
            now = time.time()
            if now - last_participant_check >= PARTICIPANT_CHECK_INTERVAL:
                last_participant_check = now
                try:
                    new_count = self._connector.get_participant_count()
                    if self._stop_event.is_set():
                        break
                    if new_count != participant_count:
                        log.info(f"ChatRunner: participant count changed {participant_count} → {new_count}")
                    participant_count = new_count
                except Exception as e:
                    log.warning(f"ChatRunner: get_participant_count failed: {e}")

                if participant_count > 1:
                    saw_others = True
                    alone_since = None
                elif saw_others and participant_count == 1:
                    if alone_since is None:
                        alone_since = now
                        log.info("ChatRunner: alone in meeting — grace timer started")
                    elif now - alone_since >= config.ALONE_EXIT_GRACE_SECONDS:
                        log.info(
                            f"ChatRunner: alone for {int(now - alone_since)}s — auto-leaving"
                        )
                        ui.ok("Everyone left — dropping from the meeting.")
                        self._connector.leave()
                        return

            # Quiet mode (slip) requires the trigger phrase regardless of
            # participant count — claude must be explicitly addressed.
            # Dial/deploy keep the 1-on-1 bypass since claude is a
            # separate participant and ambient chat IS for it.
            one_on_one = (not self._quiet_mode) and (participant_count <= ONE_ON_ONE_THRESHOLD)

            # Track which own-message texts matched this batch so we can
            # discard AFTER the full batch — Meet creates multiple DOM
            # elements per message (different IDs, same text), so we must
            # keep the text in the set until all duplicates are filtered.
            own_matched = set()

            for msg in messages:
                msg_id = msg.get("id", "")
                text = msg.get("text", "").strip()
                sender = msg.get("sender", "").strip()

                # Skip already-processed messages
                if msg_id and msg_id in self._seen_ids:
                    continue
                if msg_id:
                    self._seen_ids.add(msg_id)

                # Skip empty messages
                if not text:
                    continue

                # Skip our own messages. Primary path is the ID-based dedup
                # above (msg_id added to _seen_ids by `_send`); these two
                # checks are fallbacks for adapters that can't return an ID,
                # or when the post-send DOM read-back timed out. Text match
                # compares stripped strings since Meet's DOM strips trailing
                # whitespace on render — exact-equality comparison broke
                # session-164's stuck-LLM watchdog (`...hang tight.\n\n` sent
                # vs `...hang tight.` read back) and triggered a self-reply
                # cascade.
                if sender and sender.lower() == config.AGENT_NAME.lower():
                    log.debug(f"ChatRunner: skipping own message (sender={sender!r})")
                    continue
                text_stripped = text.strip()
                if not sender and text_stripped in self._own_messages:
                    log.debug(f"ChatRunner: skipping own message (text match)")
                    own_matched.add(text_stripped)
                    continue

                log.info(f"ChatRunner: new message sender={sender!r} id={msg_id!r} text={text!r} one_on_one={one_on_one}")

                # Pre-intro user messages: buffer in memory and persist at
                # drain time, AFTER the intro has been written to the record.
                # If we persisted now, the JSONL would order user→assistant→…
                # which leaves _tail_messages ending in role='assistant' on
                # Q1 — claude_cli rejects that. Holding the record.append
                # until drain forces the on-disk order to match the
                # processing order: assistant(intro) before user(Q1).
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

            self._stop_event.wait(POLL_INTERVAL)

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
            self._narrate_failure(
                context=(
                    f"the meeting bot tried to answer the user's question but "
                    f"the LLM call failed with: {type(e).__name__}: {e}."
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
            self._narrate_failure(
                context=(
                    f"the previous turn produced a result the meeting bot "
                    f"doesn't know how to render in chat. The raw payload was: "
                    f"{result!r}."
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
        Permission prompts pass `kind="confirmation"` so they're audited but
        invisible to the model — prevents the model from mimicking the
        harness's own confirmation wording back at the user.

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
