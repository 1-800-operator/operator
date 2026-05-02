"""
ChatRunner — polling loop that reads meeting chat and responds via LLM.

Usage:
    runner = ChatRunner(connector, llm)
    runner.run(meeting_url)   # blocks until stop() is called
"""
import json
import logging
import re
import threading
import time
from pathlib import Path

from _1_800_operator import config
from _1_800_operator.pipeline import ui
from _1_800_operator.pipeline.confirmation import is_yes
from _1_800_operator.pipeline.meeting_record import MeetingRecord, slug_from_url

log = logging.getLogger(__name__)

POLL_INTERVAL = 0.5  # seconds between read_chat() calls
PARTICIPANT_CHECK_INTERVAL = 3  # seconds between participant count checks
ONE_ON_ONE_THRESHOLD = 2  # participant count at or below = 1-on-1 mode (skip trigger phrase)

LOAD_SKILL_TOOL = "load_skill"  # synthetic local tool — not routed to MCP
CLAUDE_CODE_DELEGATE_TOOL = "claude-code__delegate_to_claude_code"
_SLASH_RE = re.compile(r"^/([A-Za-z0-9_\-]+)\s*")


def _format_inner_tool_use(block: dict) -> str | None:
    """Render an inner-claude `tool_use` block as a one-line chat message.

    Returns None for tool calls we deliberately suppress (TodoWrite is the
    inner agent's planning scratchpad — too noisy to surface).
    """
    name = block.get("name", "")
    inp = block.get("input") or {}
    if name == "Read":
        return f"Reading {Path(inp.get('file_path', '?')).name}..."
    if name in ("Edit", "MultiEdit"):
        return f"Editing {Path(inp.get('file_path', '?')).name}..."
    if name == "Write":
        return f"Writing {Path(inp.get('file_path', '?')).name}..."
    if name == "Bash":
        cmd_str = (inp.get("command") or "").strip()
        first = cmd_str.splitlines()[0] if cmd_str else ""
        short = (first[:60] + "...") if len(first) > 60 else first
        return f"Running: {short}" if short else "Running command..."
    if name in ("Glob", "Grep"):
        pat = inp.get("pattern", "") or ""
        return f"Searching: {pat[:50]}" if pat else "Searching..."
    if name == "Task":
        desc = (inp.get("description") or "")[:50]
        return f"Spawning sub-agent: {desc}..." if desc else "Spawning sub-agent..."
    if name == "WebFetch":
        return f"Fetching {(inp.get('url') or '?')[:60]}..."
    if name == "WebSearch":
        return f"Web search: {(inp.get('query') or '?')[:50]}..."
    if name == "TodoWrite":
        return None
    return f"Calling {name}..."


def _stream_event_to_chat_msg(event: dict) -> str | None:
    """Translate one stream-json event from the inner claude CLI into a
    chat-friendly progress line. Only `assistant` events containing a
    `tool_use` content block surface — text deltas, system init,
    user/tool_result, and the final result event are suppressed (the
    final return path renders the answer; in-flight text deltas are
    redundant noise here).
    """
    if event.get("type") != "assistant":
        return None
    content = (event.get("message") or {}).get("content") or []
    for block in content:
        if block.get("type") == "tool_use":
            return _format_inner_tool_use(block)
    return None

# Confirmation message rendering — keep the prompt readable in Meet chat
# while still showing both ends of long values (so the user can spot a
# malicious trailing instruction).
CONFIRM_ARG_MAX = 160   # threshold above which an arg is head…tail-truncated
CONFIRM_ARG_HEAD = 70   # chars of head shown
CONFIRM_ARG_TAIL = 50   # chars of tail shown

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
        mcp_client=None,
        meeting_record: MeetingRecord | None = None,
        skills: list | None = None,
        skills_progressive: bool = True,
    ):
        self._connector = connector
        self._llm = llm
        self._mcp = mcp_client
        self._record = meeting_record
        self._stop_event = threading.Event()
        # Track messages we've sent so we can ignore our own echoes
        self._own_messages: set[str] = set()
        # Track message IDs we've already processed
        self._seen_ids: set[str] = set()
        # Pending tool call awaiting user confirmation
        self._pending_tool_call: dict | None = None
        # Skills — lookup by name; synthetic `load_skill` is offered only when
        # progressive disclosure is on AND there's at least one skill loaded.
        self._skills = {s.name: s for s in (skills or [])}
        self._skills_progressive = skills_progressive
        # SKILLS per-session counters for the summary log.
        self._turn_count = 0
        self._load_skill_calls = 0
        self._load_skill_by_name: dict[str, int] = {}
        # Per-turn heartbeat. Set in _handle_message, incremented in
        # _execute_and_respond, drained in _dispatch_result on the
        # terminal text branches. None means "no turn in flight" — the
        # heartbeat closer is a no-op so intro / failure-banner sends
        # don't get a phantom "Replied" line.
        self._turn_start_ts: float | None = None
        self._turn_tool_count = 0
        # Self-intro on join. Background thread generates the text; main loop
        # posts it (so send_chat stays single-threaded). User-message
        # processing is deferred until the intro lands; messages that arrive
        # during the gap are persisted to the record as normal and buffered
        # for in-order replay once the intro posts.
        self._intro_ready = threading.Event()
        self._intro_text = ""
        self._intro_posted = not config.INTRO_ON_JOIN
        self._hold_posted_at: float | None = None
        self._pre_intro_buffer: list[dict] = []
        # Progress narrator state (track A only). _last_send_time is
        # updated by _send so the narrator can stay quiet during fast
        # turns. Buffer accumulates tool_use events between flushes;
        # the throttle gate decides when to flatten it into one chat
        # line. Lock guards both since the callback runs on the
        # provider's pump thread, not the main loop.
        self._last_send_time = 0.0
        self._last_narration_time = 0.0
        self._narration_buffer: list[tuple[str, dict]] = []
        self._narration_lock = threading.Lock()
        self._narration_auto_approve: set[str] = set()
        # Serializes _send across threads. Playwright's sync API is
        # single-threaded by contract; under track A the progress
        # narrator (provider pump thread) and the main poll loop both
        # call _send, so concurrent connector.send_chat would race.
        # The lock also keeps _own_messages add + send_chat + record
        # append atomic, which prevents a partial-state observer from
        # the read loop seeing one without the other.
        self._send_lock = threading.Lock()

    def _wire_track_a_permissions(self):
        """If the LLM provider is ClaudeCLIProvider, plug in the chat handler.

        Track A relies on Claude Code's PreToolUse hook to gate every
        potentially-destructive tool call through meeting chat. The
        handler runs on the provider's pump thread and reads chat
        directly to await user replies — see permission_chat_handler.py.
        Track-B providers (anthropic / openai) are unaffected.
        """
        from _1_800_operator.pipeline.providers.claude_cli import ClaudeCLIProvider
        from _1_800_operator.pipeline.permission_chat_handler import PermissionChatHandler
        provider = getattr(self._llm, "_provider", None)
        if not isinstance(provider, ClaudeCLIProvider):
            return
        handler = PermissionChatHandler(
            connector=self._connector,
            runner=self,
            auto_approve=config.PERMISSIONS_AUTO_APPROVE,
            always_ask=config.PERMISSIONS_ALWAYS_ASK,
        )
        provider.set_permission_handler(handler)
        log.info(
            "ChatRunner: track-A permission handler wired "
            f"(auto_approve={sorted(handler._auto_approve)}, "
            f"always_ask={sorted(handler._always_ask)})"
        )
        if config.PROGRESS_NARRATION_ENABLED:
            self._narration_auto_approve = set(config.PERMISSIONS_AUTO_APPROVE)
            provider.set_progress_callback(self._on_tool_use)
            log.info(
                f"ChatRunner: progress narrator wired "
                f"(min_silence={config.PROGRESS_NARRATION_MIN_SILENCE_S}s, "
                f"throttle={config.PROGRESS_NARRATION_THROTTLE_S}s)"
            )

    def _on_tool_use(self, tool_name, tool_input):
        """Progress callback fired by ClaudeCLIProvider on every tool_use.

        Runs on the provider pump thread. Only narrates auto-approved
        tools — confirmation-gated tools post their own prompt, which
        is already feedback enough. Throttled by `min_silence_seconds`
        (skip if a user-facing send happened recently) and
        `throttle_seconds` (gap between narrator messages).

        Voice gating:
          technical — emit a deterministic "Working: <tool summary>"
                      line. Power users want to see exactly what the
                      bot is doing.
          plain     — narrator stays silent. The bot self-narrates in
                      its own persona-faithful voice via the
                      system_prompt directive ("when using a tool that
                      takes >2s, briefly say what you're doing").
                      Keeping a Python-templated narrator alongside
                      that creates duplicate / clashing voice — better
                      to let the LLM speak.
        """
        if tool_name not in self._narration_auto_approve:
            return
        # Plain voice → claude self-narrates; operator stays out of
        # the way to avoid dual-voice noise.
        if getattr(config, "VOICE", "plain") != "technical":
            return
        from _1_800_operator.pipeline.permission_chat_handler import _format_terse
        with self._narration_lock:
            self._narration_buffer.append((tool_name, tool_input or {}))
            now = time.time()
            silence = now - self._last_send_time
            since_narration = now - self._last_narration_time
            if silence < config.PROGRESS_NARRATION_MIN_SILENCE_S:
                return
            if since_narration < config.PROGRESS_NARRATION_THROTTLE_S:
                return
            buffered = self._narration_buffer
            self._narration_buffer = []
            self._last_narration_time = now
        if not buffered:
            return
        summaries = [_format_terse(name, args) for name, args in buffered]
        self._send("Working: " + "; ".join(summaries))

    def run(self, meeting_url):
        """Join the meeting and start the chat polling loop."""
        log.info(f"ChatRunner: joining {meeting_url}")
        self._wire_track_a_permissions()
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
        if seed_count and seed_count <= ONE_ON_ONE_THRESHOLD:
            ui.ok(f"Joined as @{trigger} · solo (1-on-1 mode)")
        elif seed_count:
            ui.ok(f"Joined as @{trigger} · {seed_count} participants")
        else:
            ui.ok(f"Joined as @{trigger} — listening for chat.")
        self._post_mcp_failure_banner()
        log.info("ChatRunner: starting chat loop")
        self._loop()

    # Short user-facing labels per startup failure kind. Compact by design —
    # the full "fix" hint lives in the system prompt (15.7.1e) so the LLM
    # can surface the detail when asked. Unknown kinds fall through to a
    # generic "error" label so the banner still fires.
    _FAILURE_KIND_LABELS = {
        "missing_creds": "missing {vars}",
        "binary_missing": "binary not found",
        "startup_timeout": "didn't respond",
        "handshake_crash": "crashed on startup",
        "oauth_needed": "needs auth — run `operator auth {name}`",
        "unknown": "error",
    }

    def _post_mcp_failure_banner(self):
        """Post one compact chat line if any MCP failed to load this session.

        Fires once on join, before the LLM-generated intro so the banner
        lands deterministically even if intro generation errors out.
        Silent when startup_failures is empty (the happy path). Runtime
        failures aren't possible at this moment — no tool call has fired
        yet — so they're not checked here; _record_mcp_outcome handles
        mid-session trips separately.
        """
        if not self._mcp or not getattr(self._mcp, "startup_failures", None):
            return
        fragments = []
        for name, info in self._mcp.startup_failures.items():
            kind = info.get("kind", "unknown")
            template = self._FAILURE_KIND_LABELS.get(kind, "error")
            if kind == "missing_creds":
                vars_list = info.get("vars") or []
                vars_str = vars_list[0] if len(vars_list) == 1 else "credentials"
                label = template.format(vars=vars_str)
            elif kind == "oauth_needed":
                label = template.format(name=name)
            else:
                label = template
            fragments.append(f"{name} didn't load ({label})")
        if not fragments:
            return
        line = "Heads-up — " + "; ".join(fragments) + ". Ask for details."
        self._send(line)

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
        self._log_skills_summary()

    def _log_skills_summary(self):
        """Emit the per-session SKILLS usage tally. Safe to call multiple times."""
        if not self._skills and not self._load_skill_calls:
            return
        by_name = dict(self._load_skill_by_name) if self._load_skill_by_name else {}
        log.info(
            f"SKILLS session summary: turns={self._turn_count} "
            f"load_skill_calls={self._load_skill_calls} by_name={by_name}"
        )

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
            if self._hold_posted_at is None and saw_others:
                self._send(f"Hold for {config.AGENT_NAME}...")
                self._hold_posted_at = time.time()
                if config.INTRO_ON_JOIN:
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

            one_on_one = participant_count <= ONE_ON_ONE_THRESHOLD

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

                # Persist every observed chat message to the meeting record —
                # this is the single source of truth the LLM replays from.
                # When a tool confirmation is pending, this message is a reply
                # to the harness ("ok"/"no"), not a turn in the real conversation.
                # Tag it `confirmation` so it's audited but filtered out of the
                # LLM prompt (pipeline/llm.py only replays kind in {chat, caption}).
                msg_kind = "confirmation" if self._pending_tool_call else "chat"
                if self._record is not None:
                    self._record.append(sender=sender, text=text, kind=msg_kind)

                # If we're waiting for tool confirmation, any message is a response
                if self._pending_tool_call:
                    self._handle_confirmation(text)
                    continue

                # Defer LLM-side processing until the self-intro has posted —
                # buffered messages drain in order at the top of the iteration
                # that observes the intro completing.
                if not self._intro_posted:
                    self._pre_intro_buffer.append({"text": text, "one_on_one": one_on_one})
                    continue

                self._dispatch_user_message(text, one_on_one)

            self._own_messages -= own_matched

            self._stop_event.wait(POLL_INTERVAL)

    def _tools_for_llm(self) -> list | None:
        """Combined tool list: MCP tools + synthetic `load_skill` when applicable."""
        tools = list(self._mcp.get_openai_tools()) if self._mcp else []
        if self._skills and self._skills_progressive:
            names = sorted(self._skills.keys())
            tools.append({
                "type": "function",
                "function": {
                    "name": LOAD_SKILL_TOOL,
                    "description": (
                        "Load the full instructions for a named skill from the available-skills "
                        "list. Call ONLY when the user's request clearly matches a skill's "
                        "description; never for unrelated chit-chat."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "enum": names,
                                "description": "Name of the skill to load.",
                            }
                        },
                        "required": ["name"],
                    },
                },
            })
        return tools or None

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

        Drains _turn_start_ts so the next call is a no-op (idempotent —
        confirmation paths and recursive _dispatch_result calls can each
        trigger this without doubling). Reports elapsed wall time and
        the count of tools auto-executed during the turn. NO message
        bodies, sender names, or tool arguments — heartbeat is metadata
        only by user constraint.
        """
        if self._turn_start_ts is None:
            return
        elapsed = time.time() - self._turn_start_ts
        n_tools = self._turn_tool_count
        self._turn_start_ts = None
        self._turn_tool_count = 0
        suffix = f" · {n_tools} tool{'s' if n_tools != 1 else ''}" if n_tools else ""
        if failed:
            ui.err(f"Turn {self._turn_count} failed — {elapsed:.1f}s{suffix}")
        else:
            ui.ok(f"Replied — {elapsed:.1f}s{suffix}")

    def _handle_message(self, text):
        """Process a single chat message via LLM."""
        self._turn_count += 1
        self._turn_start_ts = time.time()
        self._turn_tool_count = 0
        # Slash-invocation fast path — /<name> (after trigger phrase is already stripped).
        # If it matches a loaded skill, prepend the body to the user message so the
        # LLM sees the full instructions without the extra load_skill round-trip.
        extra_system = ""
        m = _SLASH_RE.match(text)
        if m:
            candidate = m.group(1)
            skill = self._skills.get(candidate)
            if skill:
                log.info(f"SKILLS turn={self._turn_count} slash-invoke: {candidate}")
                self._load_skill_calls += 1
                self._load_skill_by_name[candidate] = self._load_skill_by_name.get(candidate, 0) + 1
                extra_system = (
                    f"\n\nThe user invoked the \"{candidate}\" skill. Follow these instructions "
                    f"for this turn:\n{skill.body}"
                )
            else:
                log.debug(f"SKILLS turn={self._turn_count} unknown slash token: /{candidate}")

        try:
            tools = self._tools_for_llm()
            result = self._llm.ask(
                text, tools=tools, extra_system=extra_system,
                on_paragraph=self._streaming_callback(),
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

        # llm.ask returns a raw string only on the legacy non-streaming
        # no-tools path; streaming and tool-using paths return dict shapes
        # that _dispatch_result understands (and that respects the streamed
        # flag, so we don't double-send when on_paragraph already flushed).
        self._dispatch_result(result)

    def _needs_confirmation(self, tool_call):
        """Return True if this tool call requires user confirmation.

        Reads the unified permissions block via the same fnmatch-aware
        matcher as track-A's PermissionChatHandler. Track-B tool names
        arrive in `server__tool` form; we re-prefix with `mcp__` so a
        single pattern like `mcp__linear__get_*` covers both the
        chat_runner path (this call) and any track-A invocation
        exposing the same MCP server. Legacy per-server `read_tools` /
        `confirm_tools` entries are translated into the unified lists
        at config-load time, so configs written before session 169
        keep working unchanged.

        Default is "ask" — safe-by-default for tools the bundle
        didn't declare.
        """
        from _1_800_operator.pipeline.permission_chat_handler import _matches_any
        name = tool_call["name"]
        qualified = name if name.startswith("mcp__") else f"mcp__{name}"
        if _matches_any(qualified, config.PERMISSIONS_ALWAYS_ASK):
            return True
        if _matches_any(qualified, config.PERMISSIONS_AUTO_APPROVE):
            return False
        return True

    def _request_confirmation(self, tool_call):
        """Ask user for confirmation before executing a tool.

        Renders *every* argument so a write can't hide extra fields in the
        confirmation prompt. Long values are truncated with a head…tail
        snippet; the full payload is also logged at INFO so the user can
        cross-reference /tmp/operator.log if they need the full string.
        """
        self._pending_tool_call = tool_call
        name = tool_call["name"]
        args = tool_call["arguments"]

        # Strip 'limit' the LLM injects unprompted on Linear list calls
        if name.startswith("linear__"):
            args.pop("limit", None)

        # "linear__create_issue" -> tool "create_issue", server "linear"
        parts = name.split("__", 1)
        display_server = parts[0] if len(parts) == 2 else ""
        display_tool = parts[1] if len(parts) == 2 else name

        # Render every argument on its own line so the confirmation is
        # scannable. Strings show unquoted (less repr noise); other types
        # use repr so dicts/lists/numbers stay unambiguous. Long values are
        # head…tail-truncated; full payload is logged for cross-reference.
        truncated_any = False
        rendered = []
        for k, v in args.items():
            r = v if isinstance(v, str) else repr(v)
            if len(r) > CONFIRM_ARG_MAX:
                head = r[:CONFIRM_ARG_HEAD]
                tail = r[-CONFIRM_ARG_TAIL:]
                r = f"{head}…{tail}"
                truncated_any = True
            rendered.append(f"  • {k}: {r}")

        header = f"Run {display_tool}"
        if display_server:
            header += f" ({display_server})"
        header += "?"

        if rendered:
            msg = header + "\n" + "\n".join(rendered)
        else:
            msg = header + "\n  (no arguments)"
        if truncated_any:
            msg += "\nFull values in /tmp/operator.log."
        msg += "\nOK?"
        log.info(f"ChatRunner: requesting confirmation for {name} args={args!r}")
        self._send(msg, kind="confirmation")

    def _handle_confirmation(self, text):
        """Process user's yes/no response to a pending tool call.

        Yes/no detection is shared with the track-A permission handler
        via `pipeline.confirmation.is_yes` — same vocab, same negation
        gate, regardless of provider.
        """
        affirmative = is_yes(text)

        tc = self._pending_tool_call

        if affirmative:
            # Fall through to tool execution below
            pass
        else:
            # Not a clear yes — treat as a correction, not a cancellation.
            # Pass the user's feedback back to the LLM so it can re-propose
            # with adjusted parameters.
            self._pending_tool_call = None
            reason = (
                f"[TOOL NOT EXECUTED — user did not approve the call.] "
                f"The user replied: \"{text}\" instead of confirming. "
                f"The tool was NOT run; no output exists. "
                f"Either re-propose a corrected tool call, or ask the user a clarifying question."
            )
            try:
                tools = self._tools_for_llm()
                result = self._llm.send_tool_result(
                    tc["id"], tc["name"], reason, tools=tools,
                    on_paragraph=self._streaming_callback(),
                )
            except Exception as e:
                # Without this branch the heartbeat would leak (turn never
                # closes) and the user would be left silent after declining.
                log.warning(f"ChatRunner: correction result call failed: {e}")
                self._send("Got your reply — but I couldn't reach the model to follow up.")
                self._emit_turn_done(failed=True)
                return

            self._dispatch_result(result)
            return

        # User confirmed — execute
        self._pending_tool_call = None
        self._execute_and_respond(tc)

    def _dispatch_result(self, result):
        """Route an LLM result (text, tool_call, or context_overflow).

        The else-arm is the safety net for unknown result shapes — a future
        provider change, an SDK bug, or a new result kind that wasn't wired
        in. Hands the offending payload to `_narrate_failure` which asks
        the LLM to translate it into a user-friendly message; on any
        narration failure, falls through to a hardcoded message. One-shot —
        never re-enters the dispatcher.
        """
        if isinstance(result, str):
            self._send(result)
            self._emit_turn_done()
        elif result["type"] == "text":
            # Streaming path already posted each paragraph via on_paragraph.
            if not result.get("streamed"):
                self._send(result["content"])
            self._emit_turn_done()
        elif result["type"] == "tool_call":
            if result["name"] == LOAD_SKILL_TOOL:
                self._handle_load_skill(result)
            elif self._needs_confirmation(result):
                self._request_confirmation(result)
            else:
                self._execute_and_respond(result)
        elif result["type"] == "context_overflow":
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
        record=False (not persisted to the meeting record), tools=None
        (return shape is guaranteed to be a bare string), and
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
                prompt, record=False, tools=None, retry_rate_limits=False,
            )
        except Exception as e:
            log.error(f"_narrate_failure call failed: {e}")
            narrated = None
        if isinstance(narrated, str) and narrated.strip():
            self._send(narrated)
        else:
            self._send(fallback)
        self._emit_turn_done(failed=True)

    def _handle_load_skill(self, tc):
        """Resolve a load_skill call locally and feed the skill body back as the tool result."""
        name = (tc.get("arguments") or {}).get("name", "")
        skill = self._skills.get(name)
        available = ", ".join(sorted(self._skills.keys())) or "<none>"
        log.info(f"SKILLS turn={self._turn_count} load_skill called: {name!r} (available: {available})")
        self._load_skill_calls += 1
        self._load_skill_by_name[name] = self._load_skill_by_name.get(name, 0) + 1
        if skill:
            result_content = f"[skill: {skill.name}]\n{skill.body}"
        else:
            result_content = (
                f"Error: no skill named {name!r}. Available skills: {available}. "
                f"Proceed without a skill or ask the user to clarify."
            )
        try:
            tools = self._tools_for_llm()
            result = self._llm.send_tool_result(
                tc["id"], tc["name"], result_content, tools=tools,
                on_paragraph=self._streaming_callback(),
            )
        except Exception as e:
            log.error(f"ChatRunner: load_skill follow-up failed: {e}")
            self._narrate_failure(
                context=(
                    f"the meeting bot tried to run the /{name} skill but the "
                    f"LLM follow-up call failed with: {type(e).__name__}: {e}."
                ),
                fallback=f"Couldn't run /{name} just now — try again in a moment?",
            )
            return
        self._dispatch_result(result)

    def _execute_and_respond(self, tc):
        """Execute a tool call in a background thread, then feed result to LLM.

        The MCP layer owns the actual timeout (per-server override → ship
        default → global fallback). For claude-code delegations, a tailer
        thread reads the inner CLI's stream-json side log and posts real
        progress events ("Reading X.py...", "Editing Y.py...") to chat.
        Other tools run silently — they're short-lived enough that placeholder
        heartbeats added more noise than signal.
        """
        log.info(f"ChatRunner: auto-executing {tc['name']}")
        self._turn_tool_count += 1
        t_exec_start = time.monotonic()
        result_holder = [None]
        error_holder = [None]
        done_event = threading.Event()

        def _run_tool():
            try:
                result_holder[0] = self._mcp.execute_tool(tc["name"], tc["arguments"])
            except Exception as exc:
                error_holder[0] = exc
            finally:
                done_event.set()

        threading.Thread(target=_run_tool, daemon=True).start()

        if tc["name"] == CLAUDE_CODE_DELEGATE_TOOL:
            threading.Thread(
                target=self._tail_claude_stream, args=(done_event,), daemon=True,
            ).start()

        done_event.wait()

        if error_holder[0]:
            e = error_holder[0]
            log.error(f"ChatRunner: tool execution failed: {e}")
            self._record_mcp_outcome(tc["name"], success=False, error_text=str(e))
            self._handle_tool_failure(
                tc,
                f"The tool call failed with this error: {e}\n\n"
                f"Tell the user what went wrong in one short sentence (reference the "
                f"specific cause from the error — 404, auth, network, missing arg, etc.), "
                f"and suggest one concrete next step. Do not say 'check the logs'.",
                fallback=f"That tool call failed: {str(e)[:200]}",
            )
            return

        self._record_mcp_outcome(tc["name"], success=True)

        tool_result = result_holder[0]
        tool_elapsed = time.monotonic() - t_exec_start
        result_size = len(tool_result) if isinstance(tool_result, str) else 0
        log.info(f"TIMING tool_exec={tool_elapsed:.1f}s name={tc['name']} result_bytes={result_size}")

        # Feed result back to LLM — it may summarize or request another tool
        try:
            tools = self._tools_for_llm()
            result = self._llm.send_tool_result(
                tc["id"], tc["name"], tool_result, tools=tools,
                on_paragraph=self._streaming_callback(),
            )
        except Exception as e:
            log.error(f"ChatRunner: LLM summary failed: {e}")
            truncated = (tool_result[:600] + "...") if isinstance(tool_result, str) and len(tool_result) > 600 else tool_result
            self._narrate_failure(
                context=(
                    f"the {tc['name']} tool ran successfully and returned the "
                    f"following raw result, but the LLM summary call failed "
                    f"with: {type(e).__name__}: {e}. Pass the raw result "
                    f"through to the user verbatim if it looks useful, "
                    f"otherwise tell them what happened.\n\n"
                    f"--- raw tool result ---\n{truncated!r}"
                ),
                fallback=f"Got the result but couldn't summarize it. Raw output: {truncated}",
            )
            return

        self._dispatch_result(result)

    def _tail_claude_stream(self, done_event: threading.Event):
        """Stream live progress to chat while a claude-code delegate runs.

        Tails the inner claude CLI's stream-json side log (written by the
        claude-code MCP server during delegation) and posts a chat message
        per inner tool_use. Reads via seek-and-resume so we don't re-emit
        events on each pass. Exits when `done_event` fires.
        """
        from _1_800_operator.agents.engineer.claude_code import STREAM_LOG_PATH
        pos = 0
        while True:
            if STREAM_LOG_PATH.exists():
                try:
                    with STREAM_LOG_PATH.open("r") as f:
                        f.seek(pos)
                        for line in f:
                            line = line.rstrip("\n")
                            if not line:
                                continue
                            try:
                                event = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            msg = _stream_event_to_chat_msg(event)
                            if msg:
                                self._send(msg)
                        pos = f.tell()
                except OSError as e:
                    log.debug(f"ChatRunner: claude-stream tail read error: {e}")
            if done_event.wait(timeout=1.0):
                return

    def _handle_tool_failure(self, tc, error_signpost: str, fallback: str):
        """Let the LLM author the user-facing failure message.

        Signposts the tool result with the error + instructions (plain summary +
        suggested next step) and dispatches the follow-up. If the follow-up LLM
        call itself fails (rate limit, network), falls back to a terse one-liner
        so the user is never left silent.
        """
        try:
            tools = self._tools_for_llm()
            result = self._llm.send_tool_result(
                tc["id"], tc["name"], error_signpost, tools=tools,
                on_paragraph=self._streaming_callback(),
            )
        except Exception as llm_err:
            log.error(f"ChatRunner: LLM error-summary call failed: {llm_err}")
            self._send(fallback)
            self._emit_turn_done(failed=True)
            return
        self._dispatch_result(result)

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
        Harness plumbing — tool-confirmation prompts — passes `kind="confirmation"`
        so it's audited but invisible to the model (prevents the model from
        mimicking the harness's own wording back at the user).

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
            # Bookkeeping for the progress narrator: any user-facing send
            # resets the silence timer so the narrator only speaks during
            # actually quiet stretches.
            self._last_send_time = time.time()

    def _record_mcp_outcome(
        self,
        tool_name: str,
        success: bool,
        error_text: str | None = None,
    ):
        """Record a tool-call outcome against its server; announce if it tripped.

        On the first failure that disables a server, sends one chat message and
        reinjects MCP status so the LLM's next turn sees the updated picture.

        error_text is the upstream tool-error string (passed on the failure
        path) and feeds MCPClient's auth-sniff — see record_tool_result.
        """
        if not self._mcp:
            return
        server = self._mcp.server_for_tool(tool_name)
        if not server:
            return
        tripped = self._mcp.record_tool_result(server, success, error_text=error_text)
        if not tripped:
            return
        self._send(
            f"The {server} server seems to be having issues — skipping it for the rest of this session."
        )
        loaded = [
            n for n in config.MCP_SERVERS
            if n not in self._mcp.startup_failures and n not in self._mcp.runtime_failures
        ]
        try:
            self._llm.inject_mcp_status(
                loaded,
                self._mcp.startup_failures,
                self._mcp.runtime_failures,
            )
        except Exception as e:
            log.warning(f"ChatRunner: reinject mcp_status failed: {e}")
