# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

Operator is a chat-based AI meeting participant. It joins (or attaches to) Google Meet, opens the chat panel, watches for messages addressed to it (via the `@claude` trigger phrase, or any message in a 1-on-1), forwards the user message to a long-lived `claude -p` subprocess that owns its own tool loop, and posts the streamed reply back into meeting chat. v1 ships claude as the only agent; the inner-claude inherits its MCPs and skills from the user's own `~/.claude/` hierarchy.

## Commands

### Run

Three meeting modes:

```bash
operator slip   claude <meet-url>     # CDP-attach to a dedicated slip Chrome ("decide before joining")
operator dial   claude [meet-url]     # join as a separate participant (auto-opens meet.new if no URL)
operator deploy claude <meet-url>     # join as a separate participant into an existing meeting (URL required)
```

Two utility commands:

```bash
operator login  claude                # open Chrome and sign into Google for dial/deploy
operator doctor                       # diagnostic: claude CLI + auth, Chrome, Playwright, git, auth_state.json
```

Bare `operator` prints usage. `dial` is canonical (1-800-Operator phone metaphor); `run` is kept as a hidden alias for muscle memory + external links — same dispatch, not advertised in `--help`.

The `claude` "agent" is hardcoded into `bridges/claude.py` (trigger phrase, slip reply prefix). There is no per-bot YAML, no `~/.operator/agents/`, and no setup wizard — all of that machinery was deleted in Phase 14.19.7. v1 ships claude only; codex / gemini bridges would be sibling modules under `bridges/` if added.

`operator dial claude` exits 2 with a clear stderr message if `claude` isn't on PATH or `claude auth status --json` reports not logged in. The `--yolo` flag (dial/deploy/slip) appends `--dangerously-skip-permissions` to the inner-claude spawn so per-tool prompts are skipped.

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
```

Or run all 15 at once: `for f in tests/test_*.py; do python "$f" || echo "FAIL: $f"; done`

## Architecture

### Layer Overview

```
Entry
  __main__.py                 — CLI dispatch (slip/dial/deploy/login/doctor); preflights;
                                 builds connector + LLM, runs ChatRunner

Connectors (platform-specific — implement MeetingConnector)
  connectors/base.py          — abstract: join(), send_chat(), read_chat(),
                                 get_participant_count/names(), is_connected(),
                                 set_caption_callback(), leave()
  connectors/macos_adapter.py — dial mode: Playwright + persistent Chrome profile
  connectors/attach_adapter.py — slip mode: CDP-attach to dedicated slip Chrome
  connectors/linux_adapter.py — Linux dial: Playwright + headless Chromium
  connectors/session.py       — JoinStatus state, single-instance guard, save_debug
  connectors/{captions,chat_dom}_js.py — Meet DOM payloads injected via page.evaluate

Pipeline (platform-agnostic)
  pipeline/chat_runner.py     — polling loop; trigger detection, 1-on-1 mode,
                                 heartbeat side-channel, off-thread send queue,
                                 participant-based auto-leave
  pipeline/meeting_record.py  — append-only JSONL per meeting at ~/.operator/history/<slug>.jsonl;
                                 single source of truth for chat + caption history (meta header + tail(n))
  pipeline/llm.py             — LLMClient: feeds latest user_text + meeting-record tail to provider
  pipeline/providers/         — LLMProvider abstract + ClaudeCLIProvider (the only backend in v1)
  pipeline/transcript.py      — caption silence-window finalizer (dial mode)
  pipeline/audio.py           — Whisper transcription pipeline (slip mode, audio-helper output)

Bridge + bundled MCP
  bridges/claude.py           — claude-specific constants (trigger phrase, slip reply prefix)
  mcp_servers/transcript_server.py — bundled MCP exposing the meeting JSONL as
                                 search_captions / list_captions / list_speakers
```

### Key Data Flow

1. `MeetingConnector.join()` launches (dial) or CDP-attaches to (slip) Chrome, signs in via saved Google session, enters the meeting, opens the chat panel, and installs the chat-message MutationObserver (and the captions observer in dial mode).
2. `ChatRunner._loop()` polls `read_chat()` every 500 ms, drops already-seen / own messages, and checks for the `@claude` trigger phrase (or treats any message as addressed in 1-on-1 mode).
3. `LLMClient.ask()` reads the meeting JSONL tail via `MeetingRecord.tail(n)` and sends the latest user turn to `ClaudeCLIProvider`. The inner-claude subprocess owns its full tool loop, system prompt, and context — operator does not see the individual tool calls.
4. Inner-claude runs tools under Claude Code's native permission system — operator does not impose its own permission layer. With `--yolo` operator forwards `--dangerously-skip-permissions` to the spawn so claude runs unconstrained; without it operator passes nothing extra and Claude Code applies the user's `~/.claude/settings.json` rules. Per-tool narration before each call is steered by `_PRE_TOOL_VOICE_RULE` in the system prompt so meeting participants see what's happening.
5. The streamed reply text flows back through `connector.send_chat()` paragraph-by-paragraph; the slip-mode adapter prefixes outgoing chat with `[🤖 Claude] ` so the room can distinguish bot replies from the user's own messages.

### Configuration

There are no user-editable config files. All runtime knobs live in code:

- `bridges/claude.py` — claude-specific constants. `TRIGGER_PHRASE = "@claude"`, `REPLY_PREFIX_SLIP = "[🤖 Claude] "`.
- `config.py` — shared runtime tunables in the `INTERNAL TUNING` block (`MAX_TOKENS`, `LOBBY_WAIT_SECONDS`, `CAPTION_SILENCE_SECONDS`, `ALONE_EXIT_GRACE_SECONDS`, `HOLD_DURATION_SECONDS`), plus the canonical user-data paths (`BROWSER_PROFILE_DIR`, `AUTH_STATE_FILE`, `GOOGLE_ACCOUNT_FILE`, `ENV_FILE`, `DEBUG_DIR`). Edit here to change runtime behavior globally.
- `~/.operator/.env` — secrets file. Loaded at `config.py` import via `load_dotenv(config.ENV_FILE)`. The user populates it themselves; nothing in operator writes to it post-14.19.7.

User-scoped state (never inside the repo):

- `~/.operator/browser_profile/` — persistent Chrome profile for dial/deploy (cookies, Google login).
- `~/.operator/slip_profile/` — dedicated Chrome profile for slip mode (separate from main Chrome to dodge Chrome 121+ CDP restrictions).
- `~/.operator/auth_state.json` — Playwright storageState; recovery seed for the Linux adapter.
- `~/.operator/google_account.json` — `{"email": "..."}` cache for the doctor's "✓ signed in as X" detect.
- `~/.operator/history/<slug>.jsonl` — append-only meeting record (chat + captions + meta).
- `~/.operator/.current_meeting` — marker file written at meeting-join, deleted at leave; lets statically-registered MCPs find the active meeting JSONL.
- `~/.operator/debug/` — screenshots + HTML dumps from `session.save_debug` and adapter failure paths.

Inner-claude inherits its MCPs and skills from the user's own `~/.claude/` hierarchy (`~/.claude.json` for stdio servers, `claude mcp list` for hosted connectors like Gmail/Drive/Linear, `~/.claude/skills/` for skills). Operator contributes one MCP — the bundled transcript server — by passing `--mcp-config` at spawn time; that server reads from `OPERATOR_MEETING_RECORD_PATH` (set per-meeting) or the `.current_meeting` marker file.

### Tool Permissions

Operator does not have its own permission layer. Two modes:

- **with `--yolo`**: operator appends `--dangerously-skip-permissions` to the inner-claude spawn. Claude runs every tool unconstrained.
- **without `--yolo`**: operator passes nothing extra. Claude Code applies its native rules from `~/.claude/settings.json` (`permissions.allow` / `permissions.deny` / `permissions.ask`). Tools the user hasn't allowed are denied at the Claude Code layer; the model handles or surfaces the denial in its reply.

The bot still narrates each tool call in chat — that's a one-sentence declarative content block emitted before the `tool_use` block, steered by `_PRE_TOOL_VOICE_RULE` in `claude_cli.py`. Narration is purely informational; participants see what the bot is doing without being asked to approve.

### Participant-based Auto-leave

When the bot has seen at least one other participant and is then alone for `ALONE_EXIT_GRACE_SECONDS` (default 60s), it leaves automatically. 1-on-1 mode (participant count ≤ `ONE_ON_ONE_THRESHOLD = 2`) skips the `@claude` trigger-phrase requirement and treats every user message as addressed.

## Development Notes

- `docs/agent-context.md` tracks the current dev phase, hard-won debugging knowledge, and working context — read it before making structural changes.
- `docs/roadmap.md` has the phase checklist and strategic direction.
- `docs/pre-launch-audit.md` tracks the four-lens audit pass currently underway across Tier 1 (live-meeting hot path), Tier 2 (supporting infrastructure), and Tier 3 (setup / cold path).
- The voice pipeline was decoupled in session 93 (April 2026) and preserved on the `voice-preserved` branch. `main` is chat-only.
- `~/.operator/browser_profile/` and `~/.operator/auth_state.json` hold logged-in Google session state. They are user-scoped, never inside the repo.
