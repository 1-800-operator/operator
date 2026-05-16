# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

Operator is a chat-based AI meeting participant. It CDP-attaches to a dedicated slip Chrome window running a Google Meet, opens the chat panel, watches for messages addressed to it (via the `@claude` trigger phrase), and forwards each one to a long-lived interactive `claude` subprocess — one per meeting, driven over a PTY — that owns its own tool loop. Claude's reply is relayed back into meeting chat in real time by tailing the Claude Code transcript. v1 ships claude as the only agent; the inner-claude inherits its MCPs and skills from the user's own `~/.claude/` hierarchy.

## Commands

### Run

Two meeting modes (slip vs. slip-guarded — see Tool Permissions):

```bash
operator slip claude <meet-url>           # CDP-attach; inner-claude runs unattended
operator slip-guarded claude <meet-url>   # same, but bridge permission asks to chat
```

Utility commands:

```bash
operator hangup                           # gracefully disconnect the running slip session
operator doctor                           # diagnostic: claude CLI + auth, Chrome, git, TCC perms
```

Bare `operator` prints usage. v1 ships claude only; codex / gemini bridges would be sibling modules under `bridges/` if added — there is no per-bot YAML, no `~/.operator/agents/`, and no setup wizard (all of that machinery was deleted in Phase 14.19.7).

`operator slip claude` exits 2 with a clear stderr message if `claude` isn't on PATH or `claude auth status --json` reports not logged in. The inner-claude spawn always carries `--dangerously-skip-permissions`; in plain `slip` mode that means tools run silently, in `slip-guarded` mode operator intercepts the would-be permission asks via a hook and bridges them to chat (see Tool Permissions below). The `--yolo` flag is still parsed for plugin-slash-command back-compat but is now a no-op. The `--resume-session <id>` flag bridges an existing Claude Code session into the meeting (the plugin's slash command passes this automatically); without it a fresh session is born on first @mention.

### Logs & Diagnostics

```bash
tail -f /tmp/operator.log
grep "TIMING" /tmp/operator.log          # latency markers
grep "LLM\|MCP\|ChatRunner" /tmp/operator.log
```

### Tests

Tests are standalone scripts — no pytest runner. Representative coverage (full list in `tests/`):

```bash
source venv/bin/activate
python tests/test_chat_hardening.py            # history cap, trigger gating, sender filter
python tests/test_continuation_window.py       # sticky 90s window for same-sender follow-ups
python tests/test_participants_roster.py       # list_participants snapshot + cumulative
python tests/test_claude_cli_provider.py       # PTY subprocess lifecycle + restart
python tests/test_llm_client.py                # LLMClient ask/streaming
python tests/test_transcript_mcp.py            # captions → MCP search + byte ceiling
python tests/test_permreq_round_trip.py        # slip-guarded permission-ask flow
python tests/test_pretool_narration_hold.py    # hold/drop pre-tool narration on permreq
python tests/test_permission_classifier.py     # yes/no classifier for free-form replies
python tests/test_attach_audio_wiring.py       # speaker attribution timeline overlap
python tests/test_audio_processor.py           # whisper pipeline + utterance windowing
python tests/test_heartbeat.py                 # operator failure-narration (_narrate_failure)
python tests/test_915_reconnection.py          # disconnect + grace-period exit
python tests/test_doctor.py                    # operator doctor checks
```

Or run all at once: `for f in tests/test_*.py; do python "$f" || echo "FAIL: $f"; done`

## Architecture

### Layer Overview

```
Entry
  __main__.py                 — CLI dispatch (slip / slip-guarded / hangup / doctor);
                                 preflights (claude CLI, TCC warmup); slip.pid lock;
                                 builds connector + provider + classifier, runs ChatRunner

Connectors (implement MeetingConnector — kept as a seam for future bridges)
  connectors/base.py          — abstract: join(), send_chat(), read_chat(),
                                 get_participant_count/names(), is_connected(),
                                 set_caption_callback(), leave()
  connectors/attach_adapter.py — slip mode: CDP-attach to dedicated slip Chrome,
                                 spawns the Swift audio helper, wires whisper
                                 utterances + chat-message MutationObserver,
                                 timeline-based speaker attribution
  connectors/session.py       — JoinStatus state, Meet-URL matcher, save_debug
  connectors/chat_dom_js.py   — Meet chat-panel DOM payloads injected via page.evaluate

Pipeline
  pipeline/chat_runner.py     — polling loop; trigger detection, sticky conversation
                                 window, off-thread send queue + tick drain,
                                 participant-roster snapshot, auto-leave
  pipeline/meeting_record.py  — append-only JSONL per meeting at ~/.operator/history/<slug>.jsonl;
                                 single source of truth for chat + caption history (meta header + tail(n))
  pipeline/llm.py             — LLMClient: feeds latest user_text + meeting-record tail to provider
  pipeline/providers/         — LLMProvider abstract + ClaudeCLIProvider (the only backend in v1)
  pipeline/classifier.py      — PermissionClassifier sidecar (slip-guarded only): tiny
                                 claude subprocess that classifies free-form chat replies
                                 to permission asks as allow/deny/unrelated
  pipeline/audio.py           — Whisper (faster-whisper, CPU) transcription pipeline,
                                 consumes Swift audio-helper output
  pipeline/aec_cleaner.py     — Optional AEC3 echo cancellation (Rust crate via PyO3)
  pipeline/doctor.py          — `operator doctor` checks (claude CLI, Chrome, git, TCC,
                                 workspace trust)
  pipeline/update_check.py    — Background check for newer operator-plugin version
  pipeline/_disclaimed_spawn.py — posix_spawn helper for disclaiming child TCC identity

Bridge + bundled MCP
  bridges/claude.py           — claude-specific constants: TRIGGER_PHRASE,
                                 REPLY_PREFIX_SLIP (the only reply prefix — operator
                                 has no separate voice; old REPLY_PREFIX_OPERATOR
                                 removed S228)
  mcp_servers/record_server.py — bundled MCP (`operator-meeting-record`)
                                 exposing the meeting JSONL as
                                 search_captions, list_captions, list_speakers,
                                 list_participants, list_meetings,
                                 list_meeting_record, search_meeting_record,
                                 find_meetings
```

### Key Data Flow

1. `AttachAdapter.join()` CDP-attaches to slip Chrome (a dedicated user-data-dir under `~/.operator/slip_profile/`, separate from the user's main Chrome to dodge Chrome 121+ CDP restrictions), navigates to the meeting URL, signs in if needed via the persisted slip-profile cookies, enters the meeting, opens the chat panel, installs the chat-message MutationObserver, and spawns the Swift audio helper that pipes mic + system audio into the whisper pipeline. Speaker attribution for each utterance is computed at finalize time by maximum interval-overlap against a bounded `_speaking_history` deque populated by the JS observer (S235 fix — naive snapshot-at-finalize used to flip neighboring speakers).
2. `ChatRunner._loop()` polls `read_chat()` every 500 ms, drops already-seen / own messages, and forwards messages containing the `@claude` trigger phrase. A **sticky conversation window** (S234) lets the same sender follow up without `@claude` for `CONTINUATION_WINDOW_SECONDS` (90s), with `CONTINUATION_DEBOUNCE_SECONDS` (2s) coalescing rapid corrections. The window is sender-scoped — a different participant has to @claude to take the floor. Slip mode is "speak when spoken to" — no 1-on-1 bypass. The same polling tick snapshots the participant roster to `~/.operator/.current_meeting_participants.json` for the `list_participants` MCP tool.
3. `LLMClient.ask()` reads the meeting JSONL tail via `MeetingRecord.tail(n)` and sends the latest user turn to `ClaudeCLIProvider`. The inner-claude subprocess owns its full tool loop, system prompt, and context. The **spawn** stays naked — no `--append-system-prompt`, no `--mcp-config`, no `-p` — see the `project_anthropic_detection_vector.md` memory for why. But operator's *first bracketed-paste* (turn 0) is an operator-authored briefing (`ClaudeCLIProvider._BRIEFING`): it tells inner-claude it's in a live meeting and to narrate its tool calls. A first-turn paste rides the channel a human types on, so it carries no spawn-signature weight — the naked-spawn invariant constrains spawn *flags*, not the message stream (narrowed S228). Turn 0's reply is consumed and never posted.
4. Claude narrates its own tool calls in its own voice (`[🤖 Claude] …`), because the briefing asked it to — there is no operator-side narration layer. The only provider callback ChatRunner wires is `tick` (off-thread send-queue drain during the in-turn reply tail). The Phase 14.22 "section G" operator-side `progress`/`denial`/`connection` narration callbacks were built, live-tested, and removed in S228: the raw `running Bash: <command>` lines were cryptic, `PostToolUseFailure` got misclassified as a permission denial, and Claude self-narrating in plain language is simply better.
5. The reply text flows back through `connector.send_chat()` paragraph-by-paragraph; the slip-mode adapter prefixes claude's reply with `[🤖 Claude] ` so the room can distinguish bot replies from the user's own messages. Operator's own failure surface (`ChatRunner._narrate_failure` — for when *operator itself* can't render a result) posts on the same `[🤖 Claude] `-prefixed path: from the room's point of view there is no separate "operator" voice, just the bot stumbling. Operator never posts unprompted — failures during shutdown or with no in-flight `@mention` are held silently.

### Configuration

There are no user-editable config files. All runtime knobs live in code:

- `bridges/claude.py` — claude-specific constants: `TRIGGER_PHRASE = "@claude"`, `REPLY_PREFIX_SLIP = "[🤖 Claude] "`. (There is no separate operator voice — the old `REPLY_PREFIX_OPERATOR` and its raw-send path were removed in S228.)
- `config.py` — shared runtime tunables in the `INTERNAL TUNING` block (`MAX_TOKENS`, `LOBBY_WAIT_SECONDS`, `ALONE_EXIT_GRACE_SECONDS`, `CONTINUATION_WINDOW_SECONDS`, `CONTINUATION_DEBOUNCE_SECONDS`, `PARTICIPANT_CHECK_INTERVAL`), plus the surviving user-data paths (`ENV_FILE`, `DEBUG_DIR`). Edit here to change runtime behavior globally.
- `~/.operator/.env` — secrets file. Loaded at `config.py` import via `load_dotenv(config.ENV_FILE)`. The user populates it themselves; nothing in operator writes to it post-14.19.7.

User-scoped state (never inside the repo):

- `~/.operator/slip_profile/` — dedicated Chrome profile for slip mode (separate from the user's main Chrome to dodge Chrome 121+ CDP restrictions).
- `~/.operator/history/<slug>.jsonl` — append-only meeting record (chat + captions + meta).
- `~/.operator/.current_meeting` — marker file written at meeting-join, deleted at leave; lets statically-registered MCPs find the active meeting JSONL.
- `~/.operator/.current_meeting_participants.json` — participant-roster snapshot updated each tick; read by the `list_participants` MCP tool.
- `~/.operator/slip.pid` — singleton lockfile; gates `operator slip` to one live session and powers `operator hangup` / `operator status`. Released early in `_shutdown` so retries don't have to wait the full ~10s teardown.
- `~/.operator/bin/operator-audio-capture.app` — the signed + notarized Swift audio helper (installed by `install.sh` from the wheel). `install.sh` runs a TCC warmup via `open -W -a` so Mic + Screen Recording prompts attribute to the helper bundle itself, not to the parent terminal/IDE; the slip path re-runs the warmup if perms drift post-install.
- `~/.operator/debug/` — screenshots + HTML dumps from `session.save_debug` and adapter failure paths.

Inner-claude inherits its MCPs and skills from the user's own `~/.claude/` hierarchy (`~/.claude.json` for stdio servers, `claude mcp list` for hosted connectors like Gmail/Drive/Linear, `~/.claude/skills/` for skills). Operator does not pass `--mcp-config` at spawn time (naked-spawn invariant); the bundled transcript MCP server is registered client-side and reads from `OPERATOR_MEETING_RECORD_PATH` or the `.current_meeting` marker file.

### Tool Permissions

Inner-claude always spawns with `--dangerously-skip-permissions` (since the 14.22 PTY pivot). Operator has two modes that diverge on what happens when a tool would otherwise prompt:

- **`operator slip`** — pure unattended. The flag suppresses the prompt, the tool runs, and the room sees Claude's plain-language narration of what it did.
- **`operator slip-guarded`** (S232) — operator registers a `PreToolUse` hook that intercepts would-be permission asks, posts the question to meeting chat ending with "— OK?", and resolves the hook based on the room's reply. Free-form replies ("sure", "nah", "👍", "sí adelante") are classified allow/deny/unrelated by a `PermissionClassifier` sidecar (a tiny claude subprocess spun up in parallel at slip start). Pre-tool narration (e.g. "marking it done now") is held during the permreq window so a denied verdict doesn't get contradicted by leaked narration; the hold is dropped on deny, drained in order on allow (S234).

Per-tool narration in chat is **Claude's own**, prompted by the first-paste briefing — not an operator-side observation layer and not an `--append-system-prompt` directive (the spawn stays naked). Participants see what the bot is doing because Claude tells them, in its own voice.

### Participant-based Auto-leave

When the bot has seen at least one other participant and is then alone for `ALONE_EXIT_GRACE_SECONDS` (default 60s), it leaves automatically. The trigger phrase is always required — slip mode does not have a 1-on-1 bypass.

## Development Notes

- `docs/agent-context.md` tracks the current dev phase, hard-won debugging knowledge, and working context — read it before making structural changes.
- `docs/roadmap.md` has the phase checklist and strategic direction.
- `docs/pre-launch-audit.md` tracks the four-lens audit pass currently underway across Tier 1 (live-meeting hot path), Tier 2 (supporting infrastructure), and Tier 3 (setup / cold path).
- `docs/handoff.md` is the rolling session handoff (last session's "what got done / exact next step / open items").
- The voice pipeline was decoupled in session 93 (April 2026) and preserved on the `voice-preserved` branch. `main` is chat-only.
- The dial/deploy/login modes (Playwright + persistent profile + Google sign-in flow) shipped through Phase 14.22.3 and were deleted in Phase 14.22.4 (May 2026). Preserved in git history; the `voice-preserved` branch carries the last voice-era snapshot.
