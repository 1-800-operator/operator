# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

Operator is a chat-based AI meeting participant. It CDP-attaches to a dedicated slip Chrome window running a Google Meet, opens the chat panel, watches for messages addressed to it (via the `@claude` trigger phrase), and forwards each one to a long-lived interactive `claude` subprocess — one per meeting, driven over a PTY — that owns its own tool loop. Claude's reply is relayed back into meeting chat in real time by tailing the Claude Code transcript. v1 ships claude as the only agent; the inner-claude inherits its MCPs and skills from the user's own `~/.claude/` hierarchy.

## Commands

### Run

One meeting mode:

```bash
operator slip claude <meet-url>       # CDP-attach to a dedicated slip Chrome
```

One utility command:

```bash
operator doctor                       # diagnostic: claude CLI + auth, Chrome, git, TCC perms
```

Bare `operator` prints usage. v1 ships claude only; codex / gemini bridges would be sibling modules under `bridges/` if added — there is no per-bot YAML, no `~/.operator/agents/`, and no setup wizard (all of that machinery was deleted in Phase 14.19.7).

`operator slip claude` exits 2 with a clear stderr message if `claude` isn't on PATH or `claude auth status --json` reports not logged in. The inner-claude spawn always carries `--dangerously-skip-permissions` — the meeting flow needs tools to run without per-call prompts and operator has no permission layer of its own; the `--yolo` flag is still parsed for plugin-slash-command back-compat but is now a no-op. The `--resume-session <id>` flag bridges an existing Claude Code session into the meeting (the plugin's slash command passes this automatically); without it a fresh session is born on first @mention.

### Logs & Diagnostics

```bash
tail -f /tmp/operator.log
grep "TIMING" /tmp/operator.log          # latency markers
grep "LLM\|MCP\|ChatRunner" /tmp/operator.log
```

### Tests

Tests are standalone scripts — no pytest runner. Run them individually:

```bash
source venv/bin/activate
python tests/test_chat_hardening.py            # history cap, trigger gating, sender filter
python tests/test_915_reconnection.py          # disconnect + grace-period exit
python tests/test_claude_cli_provider.py       # claude_cli subprocess lifecycle + restart
python tests/test_llm_client.py                # LLMClient ask/streaming
python tests/test_transcript_mcp.py            # captions → MCP search
python tests/test_heartbeat.py                 # operator failure-narration (_narrate_failure)
```

Or run all at once: `for f in tests/test_*.py; do python "$f" || echo "FAIL: $f"; done`

## Architecture

### Layer Overview

```
Entry
  __main__.py                 — CLI dispatch (slip/doctor); preflights;
                                 builds connector + LLM, runs ChatRunner

Connectors (implement MeetingConnector — kept as a seam for future bridges)
  connectors/base.py          — abstract: join(), send_chat(), read_chat(),
                                 get_participant_count/names(), is_connected(),
                                 set_caption_callback(), leave()
  connectors/attach_adapter.py — slip mode: CDP-attach to dedicated slip Chrome,
                                 spawns the Swift audio helper, wires whisper
                                 utterances + chat-message MutationObserver
  connectors/session.py       — JoinStatus state, Meet-URL matcher, save_debug
  connectors/chat_dom_js.py   — Meet chat-panel DOM payloads injected via page.evaluate

Pipeline
  pipeline/chat_runner.py     — polling loop; trigger detection, off-thread
                                 send queue + tick drain, participant-based
                                 auto-leave
  pipeline/meeting_record.py  — append-only JSONL per meeting at ~/.operator/history/<slug>.jsonl;
                                 single source of truth for chat + caption history (meta header + tail(n))
  pipeline/llm.py             — LLMClient: feeds latest user_text + meeting-record tail to provider
  pipeline/providers/         — LLMProvider abstract + ClaudeCLIProvider (the only backend in v1)
  pipeline/audio.py           — Whisper transcription pipeline (consumes audio-helper output)
  pipeline/doctor.py          — `operator doctor` checks (claude CLI, Chrome, git, TCC)

Bridge + bundled MCP
  bridges/claude.py           — claude-specific constants (trigger phrase, reply prefixes)
  mcp_servers/transcript_server.py — bundled MCP exposing the meeting JSONL as
                                 search_captions / list_captions / list_speakers
```

### Key Data Flow

1. `AttachAdapter.join()` CDP-attaches to slip Chrome (a dedicated user-data-dir under `~/.operator/slip_profile/`, separate from the user's main Chrome to dodge Chrome 121+ CDP restrictions), navigates to the meeting URL, signs in if needed via the persisted slip-profile cookies, enters the meeting, opens the chat panel, installs the chat-message MutationObserver, and spawns the Swift audio helper that pipes mic + system audio into the whisper pipeline.
2. `ChatRunner._loop()` polls `read_chat()` every 500 ms, drops already-seen / own messages, and only forwards messages containing the `@claude` trigger phrase. Slip mode is "speak when spoken to" — no 1-on-1 bypass.
3. `LLMClient.ask()` reads the meeting JSONL tail via `MeetingRecord.tail(n)` and sends the latest user turn to `ClaudeCLIProvider`. The inner-claude subprocess owns its full tool loop, system prompt, and context. The **spawn** stays naked — no `--append-system-prompt`, no `--mcp-config`, no `-p` — see the `project_anthropic_detection_vector.md` memory for why. But operator's *first bracketed-paste* (turn 0) is an operator-authored briefing (`ClaudeCLIProvider._BRIEFING`): it tells inner-claude it's in a live meeting and to narrate its tool calls. A first-turn paste rides the channel a human types on, so it carries no spawn-signature weight — the naked-spawn invariant constrains spawn *flags*, not the message stream (narrowed S228). Turn 0's reply is consumed and never posted.
4. Claude narrates its own tool calls in its own voice (`[🤖 Claude] …`), because the briefing asked it to — there is no operator-side narration layer. The only provider callback ChatRunner wires is `tick` (off-thread send-queue drain during the in-turn reply tail). The Phase 14.22 "section G" operator-side `progress`/`denial`/`connection` narration callbacks were built, live-tested, and removed in S228: the raw `running Bash: <command>` lines were cryptic, `PostToolUseFailure` got misclassified as a permission denial, and Claude self-narrating in plain language is simply better.
5. The reply text flows back through `connector.send_chat()` paragraph-by-paragraph; the slip-mode adapter prefixes claude's reply with `[🤖 Claude] ` so the room can distinguish bot replies from the user's own messages. Operator's own failure surface (`ChatRunner._narrate_failure` — for when *operator itself* can't render a result) bypasses that prefix via `send_chat_raw()`.

### Configuration

There are no user-editable config files. All runtime knobs live in code:

- `bridges/claude.py` — claude-specific constants: `TRIGGER_PHRASE = "@claude"`, `REPLY_PREFIX_SLIP = "[🤖 Claude] "`, `REPLY_PREFIX_OPERATOR = "[☎️ Operator] "`.
- `config.py` — shared runtime tunables in the `INTERNAL TUNING` block (`MAX_TOKENS`, `LOBBY_WAIT_SECONDS`, `ALONE_EXIT_GRACE_SECONDS`), plus the surviving user-data paths (`ENV_FILE`, `DEBUG_DIR`). Edit here to change runtime behavior globally.
- `~/.operator/.env` — secrets file. Loaded at `config.py` import via `load_dotenv(config.ENV_FILE)`. The user populates it themselves; nothing in operator writes to it post-14.19.7.

User-scoped state (never inside the repo):

- `~/.operator/slip_profile/` — dedicated Chrome profile for slip mode (separate from the user's main Chrome to dodge Chrome 121+ CDP restrictions).
- `~/.operator/history/<slug>.jsonl` — append-only meeting record (chat + captions + meta).
- `~/.operator/.current_meeting` — marker file written at meeting-join, deleted at leave; lets statically-registered MCPs find the active meeting JSONL.
- `~/.operator/bin/operator-audio-capture.app` — the signed + notarized Swift audio helper (installed by `install.sh` from the wheel).
- `~/.operator/debug/` — screenshots + HTML dumps from `session.save_debug` and adapter failure paths.

Inner-claude inherits its MCPs and skills from the user's own `~/.claude/` hierarchy (`~/.claude.json` for stdio servers, `claude mcp list` for hosted connectors like Gmail/Drive/Linear, `~/.claude/skills/` for skills). Operator does not pass `--mcp-config` at spawn time (naked-spawn invariant); the bundled transcript MCP server is registered client-side and reads from `OPERATOR_MEETING_RECORD_PATH` or the `.current_meeting` marker file.

### Tool Permissions

Operator does not have its own permission layer. The inner-claude spawn carries `--dangerously-skip-permissions` unconditionally (since the 14.22 PTY pivot) — the meeting flow needs tools to run without per-call prompts, and operator has nothing to gate them with. The old `--yolo`-gated two-mode behavior is gone; the `--yolo` flag is still parsed for back-compat with the plugin slash command but does nothing.

Per-tool narration in chat is **Claude's own**, prompted by the first-paste briefing — not an operator-side observation layer and not an `--append-system-prompt` directive (the spawn stays naked). Participants see what the bot is doing because Claude tells them, in its own voice.

### Participant-based Auto-leave

When the bot has seen at least one other participant and is then alone for `ALONE_EXIT_GRACE_SECONDS` (default 60s), it leaves automatically. The trigger phrase is always required — slip mode does not have a 1-on-1 bypass.

## Development Notes

- `docs/agent-context.md` tracks the current dev phase, hard-won debugging knowledge, and working context — read it before making structural changes.
- `docs/roadmap.md` has the phase checklist and strategic direction.
- `docs/pre-launch-audit.md` tracks the four-lens audit pass currently underway across Tier 1 (live-meeting hot path), Tier 2 (supporting infrastructure), and Tier 3 (setup / cold path).
- The voice pipeline was decoupled in session 93 (April 2026) and preserved on the `voice-preserved` branch. `main` is chat-only.
- The dial/deploy/login modes (Playwright + persistent profile + Google sign-in flow) shipped through Phase 14.22.3 and were deleted in Phase 14.22.4 (May 2026). Preserved in git history; the `voice-preserved` branch carries the last voice-era snapshot.
