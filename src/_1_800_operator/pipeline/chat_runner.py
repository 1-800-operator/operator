"""
ChatRunner — polling loop that reads meeting chat and responds via LLM.

Usage:
    runner = ChatRunner(connector, llm)
    runner.run(meeting_url)   # blocks until stop() is called
"""
import collections
import logging
import re
import subprocess
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

# Heartbeat — meeting-chat reassurance during long silent stretches in a
# turn. When inner-claude is grinding through tool calls without emitting
# user-facing text for HEARTBEAT_SILENCE_SECONDS, ChatRunner spawns a
# one-shot side-channel `claude -p` invocation that authors a single
# status sentence (model voice, given the user's question + recent tool
# names as context). That sentence lands in chat with kind="heartbeat"
# so it's filtered out of the LLM's prompt tail (won't pollute the inner
# bot's context). Tunable knobs:
#   - HEARTBEAT_SILENCE_SECONDS: silence threshold before a heartbeat
#     fires. 30s splits the user's expressed 20-30s preference at the
#     top — gives chained reads room to land their own narrations
#     before we step in. Mid-task interruptions aren't free (token cost
#     + ~5-8s spawn+gen latency) so we err on the looser side.
#   - HEARTBEAT_TICK_SECONDS: how often the heartbeat thread wakes to
#     check the silence clock. 2s is fine-grained enough to fire close
#     to the threshold without burning CPU.
#   - HEARTBEAT_CALL_TIMEOUT_SECONDS: side-channel `claude -p` ceiling.
#     20s covers cold-start spawn + a one-sentence reply; if it slips
#     past, the heartbeat is best-effort and we drop it silently.
#   - HEARTBEAT_RECENT_TOOL_LIMIT: how many recent tool_use names we
#     show the side-channel model. 5 is enough for "what are you
#     doing" without padding the prompt.
HEARTBEAT_SILENCE_SECONDS = 30
HEARTBEAT_TICK_SECONDS = 2.0
HEARTBEAT_CALL_TIMEOUT_SECONDS = 20
HEARTBEAT_RECENT_TOOL_LIMIT = 5


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
        # Heartbeat: tracks recent tool_use events from the inner claude
        # subprocess so the side-channel "what are you doing" call has
        # something concrete to summarize. Populated by the progress
        # callback wired in _wire_progress_tracker. Cleared at turn
        # start in _handle_message. Bounded so the deque doesn't grow
        # without limit on tool-heavy turns; tail is what we read.
        self._recent_tool_uses: collections.deque = collections.deque(
            maxlen=HEARTBEAT_RECENT_TOOL_LIMIT * 2
        )
        # Per-turn user message — fed to the heartbeat side-channel call
        # as context so the model can author a status line tied to what
        # the user actually asked. None outside a turn (heartbeat thread
        # only runs during _handle_message).
        self._heartbeat_user_msg: str | None = None
        # Stamp of the last heartbeat post (monotonic). Re-arms the
        # silence clock so back-to-back heartbeats don't fire faster
        # than the threshold. Reset to 0.0 at turn start.
        self._last_heartbeat_post_ts: float = 0.0

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
        # Wire the tool-use tracker for heartbeats. The provider already
        # fires progress_callback per tool_use block during streaming;
        # we just need to record name+input into the bounded deque so
        # the heartbeat side-channel call has context.
        provider.set_progress_callback(self._record_tool_use)

    def _record_tool_use(self, tool_name: str, tool_input: dict):
        """Progress callback hooked into ClaudeCLIProvider — appends each
        tool_use to the recent-tools deque so the heartbeat thread has
        context to summarize. Runs on the provider's pump thread; the
        deque's append is atomic in CPython, no extra lock needed."""
        try:
            self._recent_tool_uses.append({
                "name": tool_name or "<unknown>",
                "input": tool_input or {},
            })
        except Exception as e:
            log.warning(f"ChatRunner: _record_tool_use failed: {e}")

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
        # Heartbeat setup: clear per-turn state so previous turn's tool
        # history and stale post stamp don't leak into this turn's
        # silence detection. Spawn the heartbeat thread; it'll wake every
        # HEARTBEAT_TICK_SECONDS to check the silence clock and fire a
        # side-channel call when the threshold trips. Stop event is set
        # in finally so the thread exits when the LLM call returns
        # (success or failure).
        self._recent_tool_uses.clear()
        self._heartbeat_user_msg = text
        self._last_heartbeat_post_ts = 0.0
        hb_stop = threading.Event()
        hb_thread = threading.Thread(
            target=self._heartbeat_loop, args=(hb_stop,), daemon=True
        )
        hb_thread.start()
        try:
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
        finally:
            hb_stop.set()
            self._heartbeat_user_msg = None

    def _dispatch_result(self, result):
        """Route an LLM result.

        claude_cli owns its own tool loop, so the only result shapes that
        reach here are text (streamed or non-streamed). Anything else is
        a bug — narrate-failure path catches it.
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

    def _heartbeat_loop(self, stop_event: threading.Event):
        """Daemon thread spawned per turn. Watches the gap between the
        last user-facing chat post and now; when it crosses
        HEARTBEAT_SILENCE_SECONDS, fires a side-channel `claude -p` call
        to author a one-sentence status update and posts it to chat.

        Why a side-channel call instead of a hardcoded heartbeat string:
        the meeting context has different optics than 1:1 with claude-
        code (other participants may see these messages), so a model-
        authored sentence reads better than templated framework voice.
        Cost is one extra `claude -p` invocation per heartbeat (~5-8s
        cold-start latency, small token footprint). The cost is bounded
        by the threshold — at most one heartbeat per HEARTBEAT_SILENCE_
        SECONDS of continuous silence.

        Race handling: while the side-channel call is in flight (~5-8s),
        the inner-claude may finally emit text and bump _last_send_time.
        Re-check the silence clock after the call returns; if it's been
        bumped, drop the heartbeat result rather than double-posting.
        """
        while not stop_event.wait(timeout=HEARTBEAT_TICK_SECONDS):
            now = time.monotonic()
            # Silence clock is measured from the most recent of:
            #   - last user-facing chat post (model paragraph or earlier
            #     heartbeat — both bump _last_send_time via _send)
            #   - turn start (so the first heartbeat fires
            #     HEARTBEAT_SILENCE_SECONDS after the user's question
            #     lands, not relative to a pre-turn timestamp)
            #   - last heartbeat post (re-arms the clock so we don't
            #     fire a second heartbeat 0s after the first one if the
            #     model is still silent)
            anchor = max(
                self._last_send_time,
                self._turn_start_ts or 0.0,
                self._last_heartbeat_post_ts,
            )
            # _last_send_time is wall-clock (time.time) per its existing
            # contract; we compare to time.time() not monotonic. The
            # threshold is wide enough that wall-clock vs monotonic
            # drift doesn't matter at our cadence.
            elapsed = time.time() - anchor
            if elapsed < HEARTBEAT_SILENCE_SECONDS:
                continue
            user_msg = self._heartbeat_user_msg
            if not user_msg:
                # Turn ended between the wake and the read — the finally
                # in _handle_message clears _heartbeat_user_msg before
                # setting stop_event. Bail.
                continue
            recent_tools = list(self._recent_tool_uses)
            log.info(
                f"ChatRunner: heartbeat threshold tripped "
                f"(silent {int(elapsed)}s, {len(recent_tools)} recent tools)"
            )
            text = self._request_heartbeat_text(user_msg, recent_tools)
            if not text:
                # Side-channel call failed or timed out. Don't re-arm
                # immediately — let the next tick check again. The
                # silence clock is still ticking from the same anchor,
                # so we won't spin. If the call keeps failing across
                # multiple ticks, that's just heartbeats being best-
                # effort; the inner turn is unaffected.
                continue
            # Race re-check: did the inner claude emit text while we
            # were waiting on the side-channel call? If so, skip.
            anchor_post_call = max(
                self._last_send_time,
                self._turn_start_ts or 0.0,
                self._last_heartbeat_post_ts,
            )
            if time.time() - anchor_post_call < HEARTBEAT_SILENCE_SECONDS:
                log.info(
                    "ChatRunner: heartbeat skipped — real reply landed "
                    "during side-channel call"
                )
                continue
            try:
                self._send(text, kind="heartbeat")
                # _send updates _last_send_time, but we also stamp the
                # heartbeat-specific post time so the silence-clock
                # anchor distinguishes "model spoke" from "we filled
                # silence." Both re-arm the threshold equally; the
                # separate stamp is purely diagnostic.
                self._last_heartbeat_post_ts = time.time()
                log.info(f"ChatRunner: heartbeat posted: {text!r}")
            except Exception as e:
                log.warning(f"ChatRunner: heartbeat _send failed: {e}")

    def _request_heartbeat_text(
        self, user_message: str, recent_tools: list[dict]
    ) -> str | None:
        """One-shot side-channel call to author a status sentence.

        Spawns a fresh `claude -p` (no MCPs, no permission bridge, no
        tools — pure prompt → reply) with the user's question and
        recent tool names as context. Returns the model's one-sentence
        reply, or None on any failure (timeout, non-zero exit, empty
        output). Best-effort; failures are logged and swallowed so the
        in-flight inner turn is never disrupted.

        Why a fresh subprocess.run instead of reusing the inner-claude
        provider: the inner provider is mid-turn (handling the user's
        question via the meeting chat path) and can't process a second
        message concurrently — `claude -p` in stream-json mode is
        single-threaded per session. A separate one-shot is the
        simplest way to get a model-authored sentence without
        disturbing the in-flight turn or maintaining a second long-
        lived subprocess.
        """
        tools_summary = self._summarize_recent_tools(recent_tools)
        prompt = (
            "You're a meeting copilot named Claude embedded in a Google Meet "
            "chat. Other meeting participants may be watching. You are "
            "currently mid-task — the user just asked the question below "
            "and you've been working on it for ~30 seconds without posting "
            "a status update.\n\n"
            f"User's question:\n  {user_message}\n\n"
            f"Recent tool calls so far:\n{tools_summary}\n\n"
            "Write ONE short status sentence telling the user what you're "
            "currently doing or about to do — like an aside whispered to a "
            "teammate while you work. Constraints: no greeting, no preface, "
            "no emoji, no quoting the question back, no closing punctuation "
            "beyond a period or ellipsis. Match a warm, terse, useful "
            "meeting-copilot voice. Output ONLY the sentence — nothing else."
        )
        # Mirror claude_cli.py:437 — strip ANTHROPIC_API_KEY from the
        # subprocess env so claude falls through to the subscription/
        # OAuth credential (which has the user's Max plan capacity).
        # `~/.operator/.env` typically carries an ANTHROPIC_API_KEY left
        # over from earlier provider experiments; in S206 testing that
        # key had zero credit balance and the heartbeat call failed
        # silently with `Credit balance is too low` on STDOUT (not
        # stderr — easy to miss). The inner-claude spawn already does
        # this; the heartbeat must too.
        import os as _os
        env = {k: v for k, v in _os.environ.items() if k != "ANTHROPIC_API_KEY"}
        try:
            result = subprocess.run(
                ["claude", "-p", prompt, "--output-format", "text"],
                capture_output=True,
                text=True,
                timeout=HEARTBEAT_CALL_TIMEOUT_SECONDS,
                env=env,
                # Don't inherit a Chrome/Playwright pipe parent's
                # signal handling — heartbeat call must not be killed
                # by the meeting bot's own SIGINT handler. start_new_
                # session is already the default via the shim in
                # __main__.py:_detached_popen_init, but be explicit.
                start_new_session=True,
            )
        except subprocess.TimeoutExpired:
            log.warning(
                f"ChatRunner: heartbeat side-channel call timed out "
                f"after {HEARTBEAT_CALL_TIMEOUT_SECONDS}s"
            )
            return None
        except Exception as e:
            log.warning(f"ChatRunner: heartbeat side-channel call raised: {e}")
            return None
        if result.returncode != 0:
            # Include both stdout and stderr — claude writes some user-
            # facing errors to stdout (e.g. "Credit balance is too
            # low") and an empty stderr would obscure the cause.
            log.warning(
                f"ChatRunner: heartbeat side-channel exited "
                f"{result.returncode} stdout={result.stdout[:200]!r} "
                f"stderr={result.stderr[:200]!r}"
            )
            return None
        text = (result.stdout or "").strip()
        if not text:
            log.warning("ChatRunner: heartbeat side-channel returned empty")
            return None
        # Trim hard ceilings: if the model exceeded "one sentence" and
        # gave us a paragraph, take the first sentence-ish chunk so the
        # heartbeat stays terse. Permissive split — we're not parsing
        # English perfectly, just clipping.
        text = text.split("\n", 1)[0].strip()
        if len(text) > 280:
            text = text[:280].rsplit(" ", 1)[0] + "…"
        return text

    def _summarize_recent_tools(self, recent_tools: list[dict]) -> str:
        """Format the tool deque for the heartbeat prompt. Returns a
        bullet list with name + a short args summary, capped to the
        most recent HEARTBEAT_RECENT_TOOL_LIMIT entries. Empty list →
        a `(none yet)` sentinel so the prompt parses naturally."""
        if not recent_tools:
            return "  (none yet)"
        tail = recent_tools[-HEARTBEAT_RECENT_TOOL_LIMIT:]
        lines = []
        for entry in tail:
            name = entry.get("name") or "<unknown>"
            args = entry.get("input") or {}
            # Pick the most informative single arg — usually file_path,
            # path, command, pattern, or query. Anything else: just
            # show the arg keys so the model has SOMETHING to say
            # without us dumping the whole input dict (which can be
            # large for Edits, Writes, Bash with long commands).
            # "What" args before "where" args. Rationale: for Read/
            # Edit/Write the most informative arg IS file_path (no
            # separate "what" — the file path is what the tool
            # operates on). But for Grep/Glob the pattern is more
            # informative than path; for Bash the command beats any
            # path arg. Order resolves both: Grep gets "pattern"
            # before "path"; Read still gets "file_path"; Bash gets
            # "command". `path` lives last as the generic fallback.
            preferred_keys = (
                "file_path", "command", "pattern", "query",
                "url", "prompt", "path",
            )
            summary = ""
            for key in preferred_keys:
                if key in args:
                    val = args[key]
                    if isinstance(val, str):
                        # Cap individual arg length so a giant Bash
                        # command doesn't blow the prompt budget.
                        summary = val if len(val) < 100 else val[:100] + "…"
                    else:
                        summary = str(val)[:100]
                    break
            if not summary and args:
                summary = "(" + ", ".join(args.keys())[:80] + ")"
            lines.append(f"  - {name}{(': ' + summary) if summary else ''}")
        return "\n".join(lines)

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
