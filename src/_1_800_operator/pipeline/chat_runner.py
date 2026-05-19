"""
ChatRunner — polling loop that reads meeting chat and responds via LLM.

Usage:
    runner = ChatRunner(connector, llm)
    runner.run(meeting_url)   # blocks until stop() is called
"""
import json
import logging
import os
import queue
import re
import threading
import time
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape, quoteattr as _xml_quoteattr

from _1_800_operator import config
from _1_800_operator.pipeline import ui
from _1_800_operator.pipeline.meeting_record import MeetingRecord, slug_from_url

log = logging.getLogger(__name__)


# Cap each string field in the failure snapshot — bounds disk + keeps
# doctor's rendered output legible. PTY tail typically <2KB anyway.
_FAILURE_MESSAGE_MAX = 2000
_FAILURE_PTY_TAIL_MAX = 2000
_FAILURE_LOG_TAIL_LINES = 30


def _operator_log_tail(n_lines: int = _FAILURE_LOG_TAIL_LINES) -> str:
    """Return the last n_lines of /tmp/operator.log, or '' if unreadable.

    Best-effort: the log lives in /tmp and may not exist in tests or
    short-lived processes; we just return empty rather than raising.
    """
    try:
        with open("/tmp/operator.log", "rb") as f:
            try:
                f.seek(-200 * n_lines, os.SEEK_END)
            except OSError:
                f.seek(0)
            tail_bytes = f.read()
    except OSError:
        return ""
    text = tail_bytes.decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-n_lines:])


def _write_last_failure(record, provider, exc):
    """Snapshot the failure for doctor to read.

    Schema is deliberately flat + small. Doctor dumps it pretty-printed
    so the model (the actual consumer of `operator doctor`'s output) has
    structured signals to interpret in plain language — see the doctor
    SKILL.md. No classification at write time.

    Best-effort: a failure to write must not interfere with the
    in-meeting failure narration, so all errors are caught + logged.
    """
    payload = {
        "ts": time.time(),
        "meeting_url": (getattr(record, "meta", {}) or {}).get("meet_url", ""),
        "meeting_slug": getattr(record, "slug", "") or "",
        "exception_class": type(exc).__name__,
        "message": str(exc)[:_FAILURE_MESSAGE_MAX],
        "phase": "unknown",
        "pty_tail": "",
        "operator_log_tail": _operator_log_tail(),
    }
    # Provider may not expose the snapshot hook (non-claude_cli provider,
    # or stub in a test) — best-effort merge.
    try:
        ctx = provider.snapshot_failure_context()
    except (AttributeError, Exception) as e:  # noqa: BLE001
        log.debug(f"_write_last_failure: provider snapshot unavailable: {e}")
        ctx = {}
    if isinstance(ctx, dict):
        if "phase" in ctx:
            payload["phase"] = str(ctx["phase"])
        if "pty_tail" in ctx:
            payload["pty_tail"] = str(ctx["pty_tail"])[:_FAILURE_PTY_TAIL_MAX]
    path = Path(config.LAST_FAILURE_PATH)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        log.warning(f"_write_last_failure: could not write {path}: {e}")

# Seconds between read_chat() calls. Dropped from 0.5 → 0.1 after S220
# instrumentation showed a consistent ~500ms `poll_lag_ms` on every turn —
# the DOM MutationObserver fires the instant a participant hits send, but
# the adapter only drains the JS-side queue once per POLL_INTERVAL. At
# 0.1s the lag ceiling falls to 100ms (5× more CDP page.evaluate calls,
# but each empty drain is sub-ms on localhost CDP). Participant-count
# checks remain on their own 3s cadence (PARTICIPANT_CHECK_INTERVAL).
POLL_INTERVAL = 0.1
PARTICIPANT_CHECK_INTERVAL = 3  # seconds between participant count checks

# Min wall-clock spacing between streamed paragraph posts. Two reasons:
# (a) Meet's chat panel rate-limits rapid sends and may swallow back-to-back
# messages, (b) staggered posts give the user's eye a chance to register
# each paragraph as a distinct message rather than a burst.
STREAM_PARAGRAPH_MIN_INTERVAL = 0.25

# Sticky conversation window. Once someone @claude's, follow-up messages
# from ANY participant within CONTINUATION_WINDOW_SECONDS skip the trigger
# requirement — the bot is "in conversation" with the room. The window
# slides forward on every forwarded message (trigger or continuation).
# New messages are debounced by CONTINUATION_DEBOUNCE_SECONDS so a quick
# correction ("thanks — wait, no, do Y instead") collapses into a single
# forwarded prompt (the last one).
#
# SECURITY: the window is NOT sender-scoped. After any participant opens
# the window with @claude, any other participant in the meeting can
# follow up without the trigger and reach the bot. This is intentional
# (mirrors how a human assistant in the room would respond to anyone
# during an active back-and-forth), but it means inviting claude into a
# meeting also invites every participant to drive claude until the
# window closes. Dial-strict mode disables this — every prompt requires
# @claude.
CONTINUATION_WINDOW_SECONDS = 90.0
CONTINUATION_DEBOUNCE_SECONDS = 2.0


class ChatRunner:
    """Polls meeting chat and responds to messages."""

    def __init__(
        self,
        connector,
        llm,
        meeting_record: MeetingRecord | None = None,
        permission_classifier=None,
        mode: str = "dial",
    ):
        # `mode` controls the trigger-routing branch in `_dispatch_user_message`,
        # the continuation-window semantics, and the provider/llm wiring in
        # `run()`. Four modes:
        #   - "dial"   default. Guarded (PermreqClassifier active). First
        #              `@claude` opens a sticky conversation window; sender
        #              not scoped — anyone in the meeting can follow up.
        #              `?` in claude's last chat post keeps the window open
        #              indefinitely until someone replies; otherwise the
        #              window closes after CONTINUATION_WINDOW_SECONDS.
        #   - "strict" guarded. No continuation window. Every non-permreq
        #              message requires the trigger phrase.
        #   - "yolo"   not guarded (no permreq hook, no classifier sidecar).
        #              No trigger check. Every chat message in the meeting
        #              is forwarded to claude. High noise — user opts in.
        #   - "wiretap" no inner-claude. Chat is recorded but never
        #              dispatched. `llm` and `permission_classifier` must
        #              both be None.
        valid_modes = {"dial", "strict", "yolo", "wiretap"}
        if mode not in valid_modes:
            raise ValueError(f"ChatRunner mode must be one of {valid_modes}, got {mode!r}")
        self._mode = mode
        self._connector = connector
        self._llm = llm
        self._record = meeting_record
        # PermissionClassifier sidecar for yolo-off mode. Optional —
        # when None (yolo-on, the default), the permreq round-trip never
        # fires and this stays unused. When set, _check_permreq_chat_for_answer
        # hands each chat reply to it for YES/NO interpretation. The
        # classifier blocks ~2-3s per call; that's fine inside the
        # provider tick because the main inner-claude is paused waiting
        # on the permission decision and is not producing anything to
        # drain in the meantime. Passed in by __main__ so test code
        # can inject mocks.
        self._classifier = permission_classifier
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
        # Per-turn end-to-end latency trace. Populated at message receipt
        # (t_dom + t_drained from the adapter), turn dispatch (t_handle_start),
        # and first-paragraph DOM-visibility (t_first_visible, stamped inside
        # _send after connector.send_chat returns). Drained + logged as the
        # `TIMING turn_complete …` line in _emit_turn_done. Pair with the
        # provider's `TIMING claude_cli_turn …` line for the LLM-internal
        # ttft / first_flush slice.
        self._turn_timing: dict | None = None
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
        self._send_queue: queue.Queue[tuple[str, str, bool]] = queue.Queue()
        # "claude is unavailable" gets posted exactly once per meeting.
        # The provider's _run_turn does its own retry-once + latch; by
        # the time an exception reaches us here, recovery has already
        # been attempted. So: say it once, then go quiet — repeated
        # @mentions after the latch are logged but never re-narrated.
        self._claude_unavailable_announced = False

        # Cached connector self-name (the local Meet tile's display
        # name). Resolved lazily on the first message tick where the
        # browser is alive. SECURITY: own-message filtering compares
        # against THIS, not against a hardcoded "Claude" string — a
        # participant who spoofs their display name to "Claude" would
        # otherwise have every message they send silently dropped,
        # including any "no" reply to a permreq. The local tile's
        # display name comes from the dial Chrome profile's Google
        # session and isn't trivially spoofable by other attendees.
        self._self_name: str | None = None

        # PermissionRequest round-trip state (yolo-off mode). The
        # provider tails permreq_requests.jsonl during a turn and fires
        # _on_permission_request for each new line; we post a question
        # to chat, watch for a yes/no reply from any participant (the
        # documented H1 tradeoff), then atomically write the answer
        # file the operator-plugin hook is polling. All of this runs
        # on the polling thread (via the provider's tick callback),
        # so no locking is needed across these fields.
        #
        # _permreq_active is the request currently waiting on a chat
        # reply; _permreq_queue holds any additional requests that
        # arrived while one was already pending (claude can batch
        # multiple tool calls — we serialise the questions one at a
        # time). _permreq_seen_at_post snapshots seen message IDs at
        # post time so we can tell answer candidates from prior chat.
        # _permreq_safety_timeout_s is slightly past the hook's own
        # 120s ceiling — defensive cleanup if the hook self-denied
        # without ChatRunner being notified.
        self._permreq_queue: list[dict] = []
        self._permreq_active: dict | None = None
        self._permreq_seen_at_post: set[str] = set()
        self._permreq_safety_timeout_s: float = 125.0

        # Sticky conversation window state (dial mode only). See
        # CONTINUATION_WINDOW_SECONDS / CONTINUATION_DEBOUNCE_SECONDS at
        # the module top for the spec. _continuation_pending holds the
        # last buffered non-trigger follow-up (overwritten on each new
        # one — only the latest goes through, debounced);
        # _flush_continuation_if_ready drains it from the polling loop
        # once the debounce window elapses. _continuation_sender is kept
        # for logging only — the window is no longer sender-scoped.
        self._continuation_sender: str | None = None
        self._continuation_open_until: float = 0.0
        self._continuation_pending: dict | None = None
        # `?`-driven indefinite window: True iff claude's last chat post
        # contained a `?`. Set in `_send` when kind=="chat"; cleared in
        # `_process_messages` on any non-self incoming message. While
        # True, the dial-mode continuation window stays open regardless
        # of the time-based ceiling — "claude asked a question, wait
        # for the room to answer." Permreq questions count too; the
        # first reply (resolving the permreq OR just side chat) clears
        # the flag and the window closes naturally.
        self._last_reply_had_question: bool = False

        # Cumulative attended-participants set. We add anyone we ever
        # saw in the participant panel; we never remove. Used by the
        # transcript MCP's list_participants tool so claude can answer
        # "who was in this meeting?" correctly even if someone joined,
        # spoke, and left before the question — the currently-present
        # list alone would drop them. Persisted to disk each tick on
        # the same cadence as the alone-detection participant check.
        self._attended_participants: set[str] = set()
        self._last_self_name: str = ""

    def _wire_provider(self):
        """Cache the ClaudeCLIProvider and wire its callbacks.

        Two callbacks are registered:

          - tick_callback (`_on_provider_tick`): runs on every reply-tail
            poll iteration during a turn. Drains off-thread queued sends
            (the polling thread is parked inside `complete_streaming`
            for the duration of a turn, so without this drain, sends
            queued from another thread would wait until the turn
            finished). Also polls meeting chat for a yes/no answer when
            a PermissionRequest is currently awaiting a reply.

          - permission_request_callback (`_on_permission_request`):
            fires when the operator-plugin hook writes a new request
            line mid-turn (yolo-off mode). Inert in yolo-on, where the
            hook never fires under --dangerously-skip-permissions.

        Tool-call narration is not operator's job — inner-claude
        narrates its own tool calls in its own voice, briefed via the
        provider's first-paste briefing. See ClaudeCLIProvider._BRIEFING.

        Caching `self._provider` lets stop() also stop the provider so
        SIGINT doesn't race a mid-turn teardown.
        """
        from _1_800_operator.pipeline.providers.claude_cli import ClaudeCLIProvider
        provider = getattr(self._llm, "_provider", None)
        if not isinstance(provider, ClaudeCLIProvider):
            return
        provider.set_tick_callback(self._on_provider_tick)
        provider.set_permission_request_callback(self._on_permission_request)
        self._provider = provider
        log.info(
            "ChatRunner: provider wired "
            "(tick + permission_request callbacks; Claude Code permission rules apply)"
        )

    def _on_provider_tick(self):
        """Per-tick callback fired by the provider during a turn.

        Runs on the polling thread (the same one that owns Playwright),
        so it may call connector methods directly. Two responsibilities:
        flush any queued off-thread sends, and (in yolo-off mode, when
        a permission question is currently waiting on a chat reply)
        poll chat for the answer.
        """
        self._drain_pending_sends()
        if self._permreq_active is not None:
            self._check_permreq_chat_for_answer()

    @staticmethod
    def _wrap_meet_chat(text: str, sender: str) -> str:
        # Surface marker for the inner-claude subprocess: this turn came
        # from meeting chat, not from the user's Claude Code chat. Wire-
        # only — the meeting JSONL and chat panel store the raw text;
        # the envelope is added at forward time. XML framing rather than
        # a bracketed prefix because the model attends to XML structurally
        # and is far less likely to echo the wrapper back into a reply.
        body = _xml_escape(text)
        if sender:
            return f"<meet_chat from={_xml_quoteattr(sender)}>\n{body}\n</meet_chat>"
        return f"<meet_chat>\n{body}\n</meet_chat>"

    def run(self, meeting_url):
        """Join the meeting and start the chat polling loop."""
        log.info(f"ChatRunner: joining {meeting_url} (mode={self._mode})")
        if self._llm is not None:
            self._wire_provider()
        # Open a meeting record for this URL if one wasn't provided.
        if self._record is None:
            slug = slug_from_url(meeting_url)
            self._record = MeetingRecord(
                slug=slug,
                meta={"meet_url": meeting_url, "mode": self._mode},
            )
            if self._llm is not None:
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
                _write_last_failure(
                    self._record, self._provider,
                    RuntimeError(f"join timed out after {join_timeout}s — "
                                 f"meeting may be invalid or unreachable"),
                )
                self._safe_leave()
                return
            if not join_status.success:
                reason = join_status.failure_reason or "unknown"
                log.error(f"ChatRunner: join failed: {reason}")
                ui.err(f"Join failed: {reason}")
                _write_last_failure(
                    self._record, self._provider,
                    RuntimeError(f"join failed: {reason}"),
                )
                self._safe_leave()
                return

        log.info("ChatRunner: joined")
        # pre_warm is fired upstream in __main__.py right after the
        # provider is built, so claude's Node boot + MCP attach +
        # --resume JSONL parse can land in parallel with the ~30s join
        # sequence (Chrome attach + lobby wait + whisper warm). By the
        # time we reach this point the warm slot is typically already
        # populated; a re-fire here would be a no-op (pre_warm is
        # idempotent under its _warm_lock).
        # Fire-and-forget plugin update check. If a newer version is on
        # the marketplace, log a hint (log-only — not posted to chat).
        # Network-bound (one HTTPS GET, 5s timeout); silent on failure.
        # Daemon thread so the join return isn't delayed.
        threading.Thread(target=self._post_update_hint_if_newer, daemon=True).start()
        if self._mode == "wiretap":
            slug = self._record.slug if self._record is not None else "?"
            ui.ok(
                f"Wiretap mode — recording to ~/.operator/history/{slug}.jsonl. "
                f"Claude is not in the meeting; only listening."
            )
        else:
            trigger = config.TRIGGER_PHRASE
            ui.ok(f"Listening for {trigger} — claude only replies when addressed.")
        log.info("ChatRunner: starting chat loop")
        self._loop()

    def stop(self):
        """Signal the polling loop to exit and tear down the LLM provider.

        provider.stop() and classifier.stop() each take ~0.85s post-S243
        (SIGTERM grace + PTY reader join + SIGKILL); they're independent
        subprocesses so we run them in parallel here. Sequential cost was
        ~1.7s in the empirical sweep; parallel ~0.85s.

        Calling provider.stop() before the safety net SIGKILLs the
        subprocess closes the race where the provider's mid-turn
        restart path would otherwise spawn a fresh claude subprocess
        right as operator is shutting down. Also tears down the
        classifier sidecar (yolo-off mode), which is otherwise a
        long-lived child that would survive past the parent.
        """
        t_start = time.monotonic()
        self._stop_event.set()

        # Per-branch end timestamps so we keep separate TIMING visibility
        # despite parallel execution. Initialized to t_start so a no-op
        # branch (provider/classifier is None) reports 0.00s rather than
        # garbage.
        t_provider_end = [t_start]
        t_classifier_end = [t_start]

        def _stop_provider():
            try:
                if self._provider is not None:
                    self._provider.stop()
            except Exception as e:
                log.warning(f"ChatRunner: provider.stop raised: {e}")
            finally:
                t_provider_end[0] = time.monotonic()

        def _stop_classifier():
            try:
                if self._classifier is not None:
                    self._classifier.stop()
            except Exception as e:
                log.warning(f"ChatRunner: classifier.stop raised: {e}")
            finally:
                t_classifier_end[0] = time.monotonic()

        prov_thread = threading.Thread(target=_stop_provider, daemon=True)
        clf_thread = threading.Thread(target=_stop_classifier, daemon=True)
        prov_thread.start()
        clf_thread.start()

        # 30s safety join — if a branch wedges (PTY reader stuck, dead
        # lock on subprocess.wait), don't hang shutdown forever. The
        # orphan reaper in __main__._shutdown catches anything left.
        prov_thread.join(timeout=30)
        clf_thread.join(timeout=30)
        if prov_thread.is_alive():
            log.warning("ChatRunner: provider.stop branch did not complete in 30s — abandoning")
        if clf_thread.is_alive():
            log.warning("ChatRunner: classifier.stop branch did not complete in 30s — abandoning")

        t_done = time.monotonic()
        log.info(
            f"TIMING runner_stop "
            f"provider={t_provider_end[0] - t_start:.2f}s "
            f"classifier={t_classifier_end[0] - t_start:.2f}s "
            f"total={t_done - t_start:.2f}s"
        )

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

            # Dial mode is "speak when spoken to" — claude only responds
            # to messages containing the trigger phrase OR to follow-ups
            # from the same sender inside the sticky conversation window.
            self._process_messages(messages)

            # If a debounced continuation has settled, dispatch it now.
            # Blocking (an LLM call) — pause the poll loop until it
            # returns; matches the regular trigger-message dispatch.
            self._flush_continuation_if_ready()

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

        # Piggyback the roster write on the same tick. Best-effort: a
        # connector failure here must not interfere with auto-leave.
        self._refresh_roster_file()

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
            # count == 1 and _saw_others == False (dial-mode lobby wait,
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
            if self._is_self_sender(sender):
                log.debug(f"ChatRunner: skipping own message (sender match)")
                continue
            if not sender and text in self._own_messages:
                log.debug(f"ChatRunner: skipping own message (text match)")
                own_matched.add(text)
                continue

            # Any non-self incoming message clears the `?`-driven indefinite
            # window. The flag's contract is "claude is waiting for a
            # response to its question" — that wait ends on the first reply
            # from the room, regardless of whether the reply actually
            # answers the question. If the reply doesn't answer and is
            # just side chat, the user can re-`@claude` to resume.
            self._last_reply_had_question = False

            # SECURITY: don't log chat text. /tmp/operator.log is world-
            # readable by default; meeting content lives at
            # ~/.operator/history/<slug>.jsonl. Length only.
            log.info(
                "ChatRunner: new message id=%r len=%d",
                msg_id, len(text or "")
            )

            # Per-message receive-lag breadcrumb. t_dom is stamped by the JS
            # MutationObserver at DOM-arrival; t_drained by the adapter at
            # `page.evaluate` drain time. Both ms-since-epoch on the same wall
            # clock. now_ms - t_drained captures any Python-side queueing
            # between the adapter's return and our process loop reaching this
            # message. Fires for every received message (trigger or not) so we
            # can see whether non-triggers experience the same lag.
            t_dom = int(msg.get("t_dom") or 0)
            t_drained = int(msg.get("t_drained") or 0)
            if t_dom and t_drained:
                now_ms = int(time.time() * 1000)
                log.info(
                    f"TIMING msg_received id={msg_id!r} "
                    f"poll_lag_ms={t_drained - t_dom} "
                    f"read_to_process_ms={now_ms - t_drained}"
                )

            if self._record is not None:
                self._record.append(sender=sender, text=text, kind="chat")

            self._dispatch_user_message(text, sender=sender, t_dom=t_dom, t_drained=t_drained)

        self._own_messages -= own_matched

    def _dispatch_user_message(self, text: str, sender: str = "", *, t_dom: int = 0, t_drained: int = 0):
        """Route a chat message to the LLM based on the runner's `mode`.

        Per-mode behavior:
          - "wiretap": no-op (no LLM in the loop).
          - "yolo":   every message goes through, no trigger check, no
                      window. Echo filtering happens upstream.
          - "strict": message dispatched only if it contains the trigger
                      phrase. No continuation window — every prompt
                      requires `@claude`.
          - "dial":   trigger phrase → dispatch + open sticky window.
                      No trigger but window is open → buffer as
                      continuation (debounced to coalesce rapid
                      corrections). The window is NOT sender-scoped
                      (anyone in the room can reply or follow up). A
                      `?` in claude's last chat post keeps the window
                      open until the first reply lands; otherwise the
                      window closes after CONTINUATION_WINDOW_SECONDS.

        Pure routing — message persistence and seen-id tracking happen
        upstream, before this is invoked.
        """
        if self._mode == "wiretap":
            return

        if self._mode == "yolo":
            # Forward every message verbatim. The wrap envelope still
            # carries the sender so claude knows who said what.
            self._handle_message(text, sender=sender, t_dom=t_dom, t_drained=t_drained)
            return

        trigger = config.TRIGGER_PHRASE.lower()
        triggered = trigger in text.lower()

        if triggered:
            prompt = re.sub(
                re.escape(config.TRIGGER_PHRASE) + r'[,:]?\s*',
                '', text, count=1, flags=re.IGNORECASE,
            ).strip()
            if prompt:
                self._handle_message(prompt, sender=sender, t_dom=t_dom, t_drained=t_drained)
                if self._mode == "dial":
                    self._open_continuation_window(sender)
            return

        if self._mode == "strict":
            # No window in strict mode — non-trigger messages are dropped.
            log.debug("ChatRunner: stored as context (strict mode, no trigger)")
            return

        # dial mode: maybe in the continuation window.
        if self._continuation_active():
            self._continuation_pending = {
                "sender": sender,
                "text": text,
                "ts": time.time(),
                "t_dom": t_dom,
                "t_drained": t_drained,
            }
            log.debug(
                f"ChatRunner: buffered continuation from {sender!r}: {text!r}"
            )
            return

        log.debug("ChatRunner: stored as context (no trigger phrase, window closed)")

    def _open_continuation_window(self, sender: str):
        """Mark the start (or extension) of a sticky conversation window.

        Any participant's subsequent non-trigger messages — not just the
        opener's — are treated as follow-ups for the next
        CONTINUATION_WINDOW_SECONDS (or indefinitely while claude's last
        chat post contained a `?`). `sender` is recorded for logging
        only; the window is not scoped to them.
        """
        self._continuation_sender = sender or None
        self._continuation_open_until = time.time() + CONTINUATION_WINDOW_SECONDS

    def _continuation_active(self) -> bool:
        """True iff the sticky window is currently open. Dial mode only.

        Two paths to True:
          - claude's last chat post contained a `?` (set by `_send`,
            cleared by `_process_messages` on any incoming reply) →
            indefinite-open until someone speaks.
          - the time-based window from `_open_continuation_window` is
            still in its CONTINUATION_WINDOW_SECONDS budget.

        Not sender-scoped — any participant can follow up while the
        window is open.
        """
        if self._last_reply_had_question:
            return True
        return time.time() < self._continuation_open_until

    def _refresh_roster_file(self):
        """Snapshot the current + cumulative participant roster to disk.

        Read by the transcript MCP's `list_participants` tool so claude
        can answer "who's in the meeting?" and "who attended?" against
        live DOM state rather than the indirect spoken/chatter subset.

        The bot's own display name is filtered out of both lists so
        claude doesn't get confused about whether it should address
        itself (the local tile shows up alongside remote participants
        in the same DOM query).

        Best-effort: failure to read names or write the file is logged
        and discarded — the auto-leave path that shares this tick must
        not be blocked by roster bookkeeping.
        """
        try:
            names = self._connector.get_participant_names()
        except Exception as e:
            log.debug(f"ChatRunner: get_participant_names failed: {e}")
            names = None
        try:
            self_name = self._connector.get_self_name() or ""
        except Exception as e:
            log.debug(f"ChatRunner: get_self_name failed: {e}")
            self_name = ""
        if self_name:
            self._last_self_name = self_name

        if not isinstance(names, list):
            return

        bot = (self._last_self_name or "").strip().lower()
        currently_present = [
            n for n in names if n and (not bot or n.strip().lower() != bot)
        ]
        for n in currently_present:
            self._attended_participants.add(n)

        payload = {
            "currently_present": currently_present,
            "attended": sorted(self._attended_participants),
            "self_name": self._last_self_name,
            "updated_at": time.time(),
        }
        path = Path(config.CURRENT_MEETING_PARTICIPANTS_PATH)
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as e:
            log.warning(f"ChatRunner: could not write roster {path}: {e}")
        # S244: push the same snapshot to the connector so the whisper_worker
        # can seal with rich attended/self_name even when the page-close
        # path beats _shutdown to closing worker stdin.
        update_fn = getattr(self._connector, "update_pending_shutdown_payload", None)
        if callable(update_fn):
            try:
                update_fn(
                    attended=sorted(self._attended_participants),
                    currently_present=currently_present,
                    self_name=self._last_self_name,
                )
            except Exception as e:
                log.debug(f"ChatRunner: update_pending_shutdown_payload raised: {e}")

    def _flush_continuation_if_ready(self):
        """Dispatch the buffered continuation if the debounce window
        has elapsed. Called from the polling loop between iterations.
        No-op when nothing is buffered or the user is still typing
        (debounce window not yet elapsed)."""
        pending = self._continuation_pending
        if pending is None:
            return
        if (time.time() - pending["ts"]) < CONTINUATION_DEBOUNCE_SECONDS:
            return
        self._continuation_pending = None
        log.info(
            f"ChatRunner: dispatching debounced continuation from "
            f"{pending['sender']!r}: {pending['text']!r}"
        )
        self._handle_message(
            pending["text"],
            sender=pending["sender"],
            t_dom=pending["t_dom"],
            t_drained=pending["t_drained"],
        )
        self._open_continuation_window(pending["sender"])

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
        # End-to-end TIMING summary. Pair with the provider's
        # `TIMING claude_cli_turn …` line (ttft, first_flush) emitted from
        # claude_cli.py to get the full picture of the LLM-internal slice.
        t = self._turn_timing or {}
        parts = [f"turn={self._turn_count}"]
        if t.get("t_dom") and t.get("t_drained"):
            parts.append(f"poll_lag_ms={t['t_drained'] - t['t_dom']}")
        if t.get("t_drained") and t.get("t_handle_start"):
            parts.append(f"gate_ms={t['t_handle_start'] - t['t_drained']}")
        if t.get("t_handle_start") and t.get("t_first_visible"):
            parts.append(f"to_first_visible_ms={t['t_first_visible'] - t['t_handle_start']}")
        if "send_chat_first_ms" in t:
            parts.append(f"send_chat_first_ms={t['send_chat_first_ms']}")
        if "send_chat_max_ms" in t:
            parts.append(f"send_chat_max_ms={t['send_chat_max_ms']}")
        parts.append(f"total_ms={int(elapsed * 1000)}")
        if failed:
            parts.append("failed=1")
        log.info("TIMING turn_complete " + " ".join(parts))
        self._turn_timing = None
        if failed:
            ui.err(f"Turn {self._turn_count} failed — {elapsed:.1f}s")
        else:
            ui.ok(f"Replied — {elapsed:.1f}s")

    def _handle_message(self, text, sender: str = "", *, t_dom: int = 0, t_drained: int = 0):
        """Process a single chat message via LLM."""
        self._turn_count += 1
        self._turn_start_ts = time.time()
        # Open the per-turn timing trace. _send populates t_first_visible on
        # first chat post; _emit_turn_done drains + logs `TIMING turn_complete`.
        self._turn_timing = {
            "t_dom": t_dom,
            "t_drained": t_drained,
            "t_handle_start": int(time.time() * 1000),
        }
        wrapped = self._wrap_meet_chat(text, sender)
        try:
            result = self._llm.ask(
                wrapped, on_paragraph=self._streaming_callback(),
            )
        except Exception as e:
            log.error(f"ChatRunner: LLM call failed: {e}")
            # One uniform failure surface: by the time we get here, the
            # provider has already done its single retry. We never echo
            # str(e) into chat (it can carry response payloads / tokens
            # / upstream secrets — the full detail lives in operator.log
            # via the log.error above, and the snapshot below is what
            # doctor reads). Say "unavailable" exactly once per *failure
            # episode*; subsequent failures within the same episode stay
            # log-only. A successful turn (below) clears the announce
            # flag so a future regression (transient recovers, then
            # breaks again) is re-announced rather than silently dropped
            # — H-18 made provider retry on every mention, so meetings
            # genuinely do cycle through broken→working→broken now.
            _write_last_failure(self._record, self._provider, e)
            if self._claude_unavailable_announced:
                self._emit_turn_done(failed=True)
                return
            self._claude_unavailable_announced = True
            self._narrate_failure(
                "claude is unavailable — run /operator:doctor to see what's wrong"
            )
            return
        # Successful turn — clear the announce flag so the next failure
        # episode (if any) re-announces, rather than being silent.
        self._claude_unavailable_announced = False
        self._dispatch_result(result)

    def _dispatch_result(self, result):
        """Route an LLM result.

        claude_cli owns its own tool loop, so the only result shapes that
        reach here are text (streamed or non-streamed). Anything else is
        a bug — operator posts a plain failure line.
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
            # Operator-observed notices (e.g. a foreign hook redirected
            # the turn) — posted after the reply, in the bot's own voice.
            for notice in result.get("notices") or []:
                self._send(notice, kind="chat")
            self._emit_turn_done()
        else:
            log.error(f"_dispatch_result: unknown result shape {result!r}")
            self._narrate_failure(
                "something came back I couldn't render — try @mentioning again",
            )

    def _post_update_hint_if_newer(self):
        """Daemon-thread worker: query the marketplace, log a hint if a
        newer plugin version exists.

        Log-only — a plugin-version notice is noise for the meeting
        participants (it concerns whoever runs operator, not the room),
        so it never reaches chat. The `/operator:update` skill is the
        actual update path.
        """
        try:
            from _1_800_operator.pipeline.update_check import check_for_newer_plugin
            hint = check_for_newer_plugin()
        except Exception as e:
            log.debug(f"ChatRunner: update check raised: {e}")
            return
        if not hint:
            return
        log.info(f"ChatRunner: operator-plugin update available — {hint}")

    def _narrate_failure(self, message: str):
        """Post a plain failure line and close the turn.

        When operator *itself* can't render a result (an unknown result
        shape, a crashed subprocess), it still owes the room a reply —
        the user @mentioned and silence is worse than a stumble. The
        message goes out on the normal `[🤖 Claude] ` path: from the
        meeting's point of view there is no "operator," just the bot,
        so the bot says it stumbled. No model in the loop, no
        operator-authored prompt — a direct chat post.

        Skipped during shutdown: the only "failures" reaching us post-
        stop are subprocess-killed-by-safety-net and other shutdown
        artifacts — posting them would land in a chat panel that's
        already detaching.
        """
        if self._stop_event.is_set():
            log.info("ChatRunner: skipping failure narration — shutdown in progress")
            self._emit_turn_done(failed=True)
            return
        try:
            self._send(message, kind="chat")
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
            self._send(text, is_reply=True)
            last[0] = time.monotonic()
        return on_paragraph

    def _send(self, text, kind: str = "chat", *, is_reply: bool = False):
        """Send a chat message, append it to the meeting record, and track it as our own.

        `kind` is persisted to the record but filtered by `pipeline/llm.py` when
        building the LLM prompt (only `chat` and `caption` are replayed).

        Everything goes out through the connector's `send_chat`, which
        prepends the dial bot prefix `[🤖 Claude] ` — there is no
        unprefixed/operator-voice send path anymore (removed S228).

        Own-message dedup: primary path is by message ID — when the connector
        returns the new `data-message-id` it captured post-send, we add it to
        `_seen_ids` so the read path's later observation gets short-circuited
        at the ID check. The text-match path (`_own_messages`) is the fallback
        for adapters that can't return an ID (linux) or when the ID read-back
        times out; we store text stripped so DOM normalization (trailing
        newlines etc.) doesn't break the comparison.

        Off-thread callers get their send enqueued instead of executed inline —
        Playwright's sync API rejects calls from any thread other than the
        one that opened the Page, and silent failure inside the connector
        would otherwise look like a successful post in the log. The polling
        loop and the provider's out-queue tick drain the queue on the main
        thread.

        Returns the connector's msg_id on success (may be empty string for
        adapters that don't return one), None on send failure or off-thread
        deferral. Most callers ignore the return; the permreq round-trip
        uses it to detect a send failure and resolve eagerly with deny.
        """
        if threading.current_thread() is not self._main_thread:
            self._send_queue.put((text, kind, is_reply))
            return None
        text_normalized = text.strip()
        with self._send_lock:
            self._own_messages.add(text_normalized)
            t_send_start = time.monotonic()
            try:
                msg_id = self._connector.send_chat(text)
            except Exception as e:
                log.error(f"ChatRunner: send_chat failed: {e}")
                self._own_messages.discard(text_normalized)
                return None
            send_chat_ms = int((time.monotonic() - t_send_start) * 1000)
            # Per-turn first + max send_chat round-trip into self._turn_timing.
            # Only chat-kind paragraphs (claude replies) count — permreq /
            # narration sends use the same path but aren't part of the
            # "reply latency" question the TIMING summary answers.
            if kind == "chat" and self._turn_timing is not None:
                if "send_chat_first_ms" not in self._turn_timing:
                    self._turn_timing["send_chat_first_ms"] = send_chat_ms
                cur_max = self._turn_timing.get("send_chat_max_ms", 0)
                if send_chat_ms > cur_max:
                    self._turn_timing["send_chat_max_ms"] = send_chat_ms
            # Record only after successful send — otherwise the LLM's next
            # turn replays a phantom assistant message the user never
            # received.
            if self._record is not None:
                self._record.append(sender=config.AGENT_NAME, text=text, kind=kind)
            if msg_id:
                self._seen_ids.add(msg_id)
            self._last_send_time = time.time()
            # First-paragraph DOM-visibility stamp for the end-to-end TIMING
            # trace. Gated on `is_reply` (set only by the streaming on_paragraph
            # callback) — permreq questions and failure-narration also flow
            # through here as `kind="chat"`, but they shouldn't stamp the
            # "first claude reply visible" metric.
            if (
                is_reply
                and self._turn_timing is not None
                and "t_first_visible" not in self._turn_timing
            ):
                self._turn_timing["t_first_visible"] = int(time.time() * 1000)
            # `?`-driven indefinite window tracking (dial mode). Permreq
            # questions count too — they end with "— OK?" and that's a
            # legitimate signal to wait for a reply. The first non-self
            # incoming message in _process_messages clears the flag, so
            # we never get stuck open across an unanswered permreq.
            if kind == "chat":
                self._last_reply_had_question = "?" in text
            return msg_id if msg_id else ""

    def _drain_pending_sends(self):
        """Flush any queued off-thread sends on the main thread.

        Called from two places, both on the main (Playwright-owning)
        thread: the polling loop between iterations (covers between-turn
        sends) and the provider's out-queue tick (covers during-turn
        sends, when the polling thread is blocked inside the LLM call).
        Bounded per call so a flood doesn't starve the caller.

        While a permreq is active, the drain is a no-op: pre-tool
        narration claude emitted before the tool call is held in the
        queue until the verdict lands. On allow the next drain flushes
        normally; on deny `_resolve_permreq` purges the held items so
        claude's "marking it done now" doesn't ship after the room
        already said no. Pre-allowed tools never trigger a permreq, so
        they keep the historical immediate-drain behavior.
        """
        if self._permreq_active is not None:
            return
        drained = 0
        while drained < 16:
            try:
                text, kind, is_reply = self._send_queue.get_nowait()
            except queue.Empty:
                return
            self._send(text, kind=kind, is_reply=is_reply)
            drained += 1

    def _purge_held_sends(self, reason: str):
        """Discard everything in `_send_queue` — used on permreq deny.

        Logs each discarded item so the operator log retains an audit
        trail of what was suppressed. Only called from permreq paths;
        the regular `_drain_pending_sends` flush path is unaffected.
        """
        discarded = 0
        while True:
            try:
                text, _kind, _is_reply = self._send_queue.get_nowait()
            except queue.Empty:
                break
            log.info(
                f"ChatRunner: dropping held pre-tool send on {reason}: {text!r}"
            )
            discarded += 1
        if discarded:
            log.info(
                f"ChatRunner: dropped {discarded} held pre-tool send(s) on {reason}"
            )

    # ---- PermissionRequest round-trip (yolo-off mode) ---------------

    def _on_permission_request(self, req):
        """Provider callback: a new PermissionRequest just landed.

        Queue it. If nothing is currently awaiting a chat reply, post
        the question now; otherwise it gets picked up after the active
        one resolves. (Claude can batch multiple tool calls per turn —
        we serialise the questions one at a time so the room isn't
        spammed with parallel asks.)
        """
        self._permreq_queue.append(req)
        if self._permreq_active is None:
            self._post_next_permreq()

    def _post_next_permreq(self):
        """Post the head of the queue to chat and mark it active.

        Snapshots `_seen_ids` at post time — anything new after this
        point is a candidate to hand to the classifier. The exact
        question text is stashed on the req dict so the classifier
        gets the same wording the participant saw. If the chat send
        itself fails, we resolve immediately with deny so the hook
        isn't left polling a question nobody saw.
        """
        if not self._permreq_queue:
            return
        req = self._permreq_queue.pop(0)
        self._permreq_seen_at_post = set(self._seen_ids)
        req["_active_since_mono"] = time.monotonic()
        question = self._format_permreq_question(req)
        req["_question_text"] = question
        self._permreq_active = req
        log.info(
            f"ChatRunner: permreq {req['request_id']} active — "
            f"tool={req.get('tool_name')!r}"
        )
        # `_send` swallows connector exceptions and returns None on
        # failure (its existing contract — meeting-chat sends can't
        # raise into the polling loop). For permreq we need that
        # signal: a None means the user never saw the question, so
        # leaving the hook to time out at 120s is worse than denying
        # eagerly. Eager deny lets claude move on right away.
        msg_id = self._send(question, kind="chat")
        if msg_id is None:
            log.warning(
                f"ChatRunner: permreq {req['request_id']} chat post failed — eager deny"
            )
            self._resolve_permreq(
                req, allowed=False,
                deny_message="operator could not post the question to meeting chat",
            )

    def _check_permreq_chat_for_answer(self):
        """Read meeting chat for the answer to the active permreq.

        Called from `_on_provider_tick` on the polling thread, only
        when `_permreq_active` is set. Walks new messages and takes the
        FIRST non-self post-question reply (any participant — the
        documented H1 tradeoff) as the answer. Hands the verbatim
        reply text to the PermissionClassifier sidecar, which returns
        a YES/NO interpretation via a single tiny claude turn (~2-3s).
        Operator does no pattern-matching of its own — the model
        decides whether the participant's words were an approval.

        Also enforces a safety timeout slightly past the hook's own
        120s ceiling: if the hook self-denied without us being
        notified (network glitch, hook crash), this clears
        `_permreq_active` so the queue advances.
        """
        active = self._permreq_active
        if active is None:
            return

        # Safety timeout — defensive cleanup past the hook's own ceiling.
        active_since = active.get("_active_since_mono", time.monotonic())
        if (time.monotonic() - active_since) > self._permreq_safety_timeout_s:
            log.warning(
                f"ChatRunner: permreq {active['request_id']} hit "
                f"{self._permreq_safety_timeout_s:.0f}s safety timeout — "
                "clearing (the hook will have already self-denied)"
            )
            # Hook self-denied; treat the held narration the same way
            # we treat any other deny — discard it.
            self._purge_held_sends(reason="safety timeout (hook self-denied)")
            self._permreq_active = None
            self._permreq_seen_at_post = set()
            # The permreq question's "— OK?" tripped _last_reply_had_question
            # and this path consumes the resolution without routing through
            # _process_messages, so reset the flag here to avoid leaving the
            # indefinite continuation window open past the timeout.
            self._last_reply_had_question = False
            self._post_next_permreq()
            return

        try:
            messages = self._connector.read_chat()
        except Exception as e:
            log.warning(f"ChatRunner: read_chat failed during permreq poll: {e}")
            return

        for msg in messages:
            msg_id = msg.get("id", "")
            text = (msg.get("text") or "").strip()
            sender = (msg.get("sender") or "").strip()
            if not text:
                continue
            if msg_id and msg_id in self._permreq_seen_at_post:
                continue  # was already visible when we posted the question
            if msg_id and msg_id in self._seen_ids:
                continue  # already routed elsewhere
            # Skip our own messages (sender filter + text-match fallback).
            if self._is_self_sender(sender):
                continue
            if not sender and text in self._own_messages:
                continue

            # First non-self, post-question reply — this is the answer
            # candidate. Mark seen so _loop's _process_messages won't
            # re-route it as a new @claude trigger after the turn ends.
            if msg_id:
                self._seen_ids.add(msg_id)
            # Persist to the meeting record so the audit trail reflects
            # who approved/denied (the participant's words are the
            # primary record of consent).
            if self._record is not None:
                self._record.append(sender=sender, text=text, kind="chat")
            # Sender + text are NOT logged here — they're already
            # appended to the meeting record above (the audit trail).
            # The log line just notes that a reply landed.
            log.info(
                "ChatRunner: permreq %s got chat reply (len=%d)",
                active['request_id'], len(text or "")
            )

            # Hand to the classifier (blocks ~2-3s typically). Falls
            # back to deny on classifier failure / no-classifier
            # configured. The main inner-claude is paused waiting on
            # this answer, so the brief block doesn't starve anything.
            #
            # `chat_context` gives the classifier the 5 chat turns BEFORE
            # this permreq exchange — so it can recognize "actually pink"
            # as a redirect (→ NO) when prior chat shows the user just
            # asked for blue. tail_chat is in-memory + ordered oldest-
            # first; the last two entries (permreq question + this reply)
            # are already passed explicitly as `question`/`reply`, so
            # drop them from the context to avoid duplication.
            recent = self._record.tail_chat(7) if self._record is not None else []
            chat_context = recent[:-2] if len(recent) >= 2 else []
            allowed = False
            if self._classifier is not None:
                try:
                    allowed = self._classifier.classify(
                        text,
                        active.get("_question_text", ""),
                        chat_context=chat_context,
                    )
                except Exception as e:
                    log.warning(
                        f"ChatRunner: classifier raised {e} → deny"
                    )
                    allowed = False
            else:
                log.warning(
                    "ChatRunner: no classifier configured (yolo-off path) → deny"
                )
            self._resolve_permreq(active, allowed=allowed, raw_reply=text)
            return

    def _resolve_permreq(self, req, *, allowed, raw_reply="", deny_message=None):
        """Build the answer payload and atomically write it to the
        path the hook is polling. Then clear active state and post the
        next queued request, if any.

        Atomic-write contract (write-to-tmp + os.replace) matches the
        hook's polling assumption — it never reads a half-written
        file. On deny, the message field carries either an
        operator-supplied reason (deny_message) or the participant's
        verbatim words (raw_reply) — claude reads the message and can
        narrate the refusal in chat with the right context.
        """
        if allowed:
            answer = {"behavior": "allow"}
        else:
            if deny_message:
                msg = deny_message
            elif raw_reply:
                # Directive phrasing: the verbatim reply IS the next
                # instruction. Without this, claude reads the bare
                # "user replied in chat: actually silver" as just a
                # refusal and retries the original plan, ignoring the
                # redirect inside the user's words (observed 2026-05-16
                # in the grey→silver live test). Explicit pivot guidance
                # cues claude to treat redirect-shaped replies as the
                # new goal.
                msg = (
                    f"The user did not approve this action. They wrote "
                    f"in meeting chat: {raw_reply!r}. Treat their reply "
                    f"as your next instruction: if it's a flat refusal, "
                    f"stop; if it suggests a different action or "
                    f"redirects to another goal, pivot to that instead "
                    f"of retrying the action you just attempted."
                )
            else:
                msg = "denied"
            answer = {"behavior": "deny", "message": msg}
            # Drop any pre-tool narration claude queued before this
            # call landed. The room rejected the action — shipping
            # "marking it done now" right after the user said no would
            # contradict the verdict. Items queued AFTER this purge
            # (claude's response to the deny tool_result) flush normally
            # on the next tick once `_permreq_active` is cleared.
            self._purge_held_sends(reason="deny")

        answer_path = req["answer_path"]
        try:
            answer_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = answer_path.parent / (answer_path.name + ".tmp")
            tmp.write_text(json.dumps(answer))
            os.replace(tmp, answer_path)
        except OSError as e:
            log.warning(
                f"ChatRunner: could not write permreq answer "
                f"{answer_path}: {e}"
            )

        self._permreq_active = None
        self._permreq_seen_at_post = set()
        # The permreq question's "— OK?" tripped _last_reply_had_question
        # when _post_next_permreq sent it via _send(kind="chat"). The answer
        # was consumed by _check_permreq_chat_for_answer (above) without
        # going through _process_messages, so the flag never got cleared
        # there. Clear it here at resolution — once the permreq is answered,
        # subsequent casual chat shouldn't be force-routed to claude on the
        # back of an unrelated `?`-driven indefinite continuation window.
        self._last_reply_had_question = False
        self._post_next_permreq()

    def _is_self_sender(self, sender) -> bool:
        """True iff `sender` is the local Meet tile's display name.

        SECURITY: this is the own-message dedup gate. It was previously a
        hardcoded compare against config.AGENT_NAME ("Claude"), which let
        any attendee silently mute themselves (and weaponise the mute by
        guaranteeing only their accomplice's permreq reply reached the
        classifier) by setting their Meet display name to "Claude". The
        connector-authoritative self-name is read lazily on first call
        (the browser must be alive — a tick will have happened) and
        cached. If the scrape fails (DOM shape changed, etc.), this
        returns False and we fall back to the ID-based _seen_ids dedup
        and the text-match _own_messages fallback elsewhere.
        """
        if not sender:
            return False
        if self._self_name is None:
            try:
                n = self._connector.get_self_name()
            except Exception:
                n = ""
            if n:
                self._self_name = n.strip()
        if not self._self_name:
            return False
        return sender.strip().lower() == self._self_name.lower()

    def _format_permreq_question(self, req):
        """Build the chat post for a permission question.

        Plain-language, open-ended ask. The participant's free-form
        reply (sure / nah / 👍 / sí adelante / anything) is interpreted
        by the PermissionClassifier sidecar — operator does no
        pattern-matching. The wording is deliberately not "reply yes or
        no" because that misrepresents what the classifier accepts (a
        bare "ok" works fine), and gives the room a free-form prompt.
        """
        tool_name = req.get("tool_name") or "?"
        summary = self._summarize_tool_input(tool_name, req.get("tool_input"))
        return f"Claude wants to use `{tool_name}`{summary} — OK?"

    def _summarize_tool_input(self, tool_name, tool_input):
        """Render the tool's input as a short, chat-friendly fragment.

        Per-tool special cases for the most informative field (Bash
        command, Edit/Write/Read file_path); everything else gets a
        generic compact-JSON dump. Capped at 200 chars with head…tail
        truncation.
        """
        if not isinstance(tool_input, dict) or not tool_input:
            return ""
        if tool_name == "Bash":
            cmd = tool_input.get("command")
            if isinstance(cmd, str) and cmd:
                return f" to run: `{self._truncate(cmd, 200)}`"
        if tool_name in ("Edit", "Write", "Read", "MultiEdit"):
            path = tool_input.get("file_path")
            if isinstance(path, str) and path:
                return f" on `{self._truncate(path, 200)}`"
        try:
            s = json.dumps(tool_input, separators=(", ", ": "))
        except (TypeError, ValueError):
            s = str(tool_input)
        return f" with: `{self._truncate(s, 200)}`"

    @staticmethod
    def _truncate(s, n):
        # Collapse whitespace and strip ASCII control chars before
        # truncating. Same string then flows into the permreq question
        # posted to chat AND into the classifier prompt's {question}
        # slot, so a hostile tool_input containing newlines or escape
        # sequences can't visually break the chat message or fake new
        # paragraphs inside the classifier prompt.
        s = "".join(c if (c == " " or c.isprintable()) else " " for c in s)
        s = " ".join(s.split())
        if len(s) <= n:
            return s
        return s[: n - 3] + "..."
