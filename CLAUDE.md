# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

Operator is a chat-based AI meeting participant. It joins Google Meet, opens the chat panel, watches for messages addressed to it (via the `@operator` trigger phrase, or any message in a 1-on-1), queries an LLM with tool access via MCP (Linear, GitHub), and posts the reply back into meeting chat.

## Commands

### Run

```bash
operator run pm https://meet.google.com/xxx-yyyy-zzz   # join a specific Meet
operator run pm                                        # auto-open meet.new
operator                                               # usage + agent list
```

Replace `pm` with any bot under `agents/` (`engineer`, `designer`, `claude`, …). Every
run selects an agent explicitly — there is no ambient root `config.yaml`
anymore. The `operator` wrapper (symlinked into `~/.local/bin/`) handles venv
activation; you can also call `python __main__.py run <name> [url]` directly
if the venv is already active.

The `claude` agent (session 151, Phase 15.9) is different from the other three
in one respect: it hard-depends on the Claude Code CLI being installed and
logged in. `operator run claude` exits 2 with a clear stderr message if
`claude` isn't on PATH or `claude auth status --json` reports not logged in.
On first run it auto-imports the user's Claude Code MCP servers (both from
`~/.claude.json#mcpServers` and `claude mcp list` — the latter is how
claude.ai-hosted connectors like Gmail/Drive/Linear reach us) and skills
(`~/.claude/skills/`). Hosted MCPs get auto-wrapped via `mcp-remote@0.1.38`
(same bridge as bundled Linear/Sentry) and auth flows through the existing
15.7.3 `operator auth <name>` path. Idempotent — marker
`_claude_import_done: true` in `~/.operator/agents/claude/config.yaml`
short-circuits re-import on subsequent boots.

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
python tests/test_chat_hardening.py         # history cap, trigger gating, sender filter
python tests/test_911_size_management.py    # tool-result size + context overflow
python tests/test_912_tool_timeout.py       # tool heartbeat + hard timeout
python tests/test_913_tool_history_collapse.py
python tests/test_915_reconnection.py       # disconnect + grace-period exit
python tests/test_guardrails.py             # binary/null-byte blocking
python tests/test_anthropic_provider.py
python tests/test_mcp_client.py
python tests/test_mcp_shutdown.py
```

## Architecture

### Layer Overview

```
Entry
  __main__.py                 — CLI entry; preflights, builds connector + LLM + MCP, runs ChatRunner

Connectors (platform-specific — implement MeetingConnector)
  connectors/base.py          — abstract: join(), send_chat(), read_chat(),
                                 get_participant_count(), is_connected(), leave()
  connectors/macos_adapter.py — Playwright + persistent Chrome profile
  connectors/linux_adapter.py — Playwright + headless Chromium
  connectors/session.py       — JoinStatus state + browser session bookkeeping

Pipeline (platform-agnostic)
  pipeline/chat_runner.py     — polling loop; trigger detection, 1-on-1 mode,
                                 tool-confirmation flow, participant-based auto-leave
  pipeline/meeting_record.py  — append-only JSONL per meeting at ~/.operator/history/<slug>.jsonl;
                                 single source of truth for chat history (meta header + tail(n))
  pipeline/llm.py             — LLMClient: builds prompt from MeetingRecord tail + in-memory
                                 scratchpad (tool calls/results), MCP status/hints injection
  pipeline/providers/         — neutral LLMProvider interface + OpenAI + Anthropic backends
  pipeline/mcp_client.py      — stdio MCP transport, tool discovery, failure backoff
  pipeline/guardrails.py      — validate tool results (binary/null-byte rejection)
```

### Key Data Flow

1. `MeetingConnector.join()` launches Chrome, signs in via saved session, enters the meeting, opens the chat panel and installs a MutationObserver over the chat DOM.
2. `ChatRunner._loop()` polls `read_chat()` every 500 ms, drops already-seen/own messages, and checks for the trigger phrase (or treats any message as addressed in a 1-on-1).
3. `LLMClient.ask()` reads the tail of the meeting's JSONL via `MeetingRecord.tail(n)` and sends those messages — plus the in-memory tool-loop scratchpad — to the configured provider with MCP tool schemas attached.
4. If the model returns a `tool_call`, `ChatRunner` either auto-executes (read-only tools in the allowlist) or requests user confirmation in chat. Tool result is fed back via `send_tool_result`; the model summarizes or chains.
5. The final text reply goes back through `connector.send_chat()`.

### Configuration

Every run names an agent explicitly (`operator run <name> [url]`). Config loading is driven by the `OPERATOR_BOT` env var — the CLI sets this before importing `config`, which then reads `agents/<name>/config.yaml` into module-level constants. There is no root `config.yaml`; there is one config file per bot under `agents/`. User-facing blocks (top-to-bottom ordering mirrors the setup wizard's four-layer view of a bot):
- `agent` — `name`, `trigger_phrase`, `first_contact_hint`, `tagline`, `intro_on_join`
- `llm` — `provider` (`openai` | `anthropic`), `model`, `history_messages` (tail size replayed from the meeting record)
- `transcript` — `captions_enabled`
- `mcp_servers` (wizard: **Tools**) — per-server `command`, `args`, `env`, `hints`, `read_tools`, `confirm_tools`, and an optional `tool_timeout_seconds` override for slow servers like `claude-code`
- `skills` (wizard: **Playbooks**) — `enabled: [names]` names the skills this agent activates. They resolve against the shared library at `~/.operator/skills/<name>/SKILL.md` (seeded from the bundled package on first run, additive; user edits never overwritten) plus `external_paths: [...]` for per-agent opt-in sources. **External paths must be tilde-prefixed (`~/...`) or absolute (`/...`)** — relative paths are CWD-dependent and WARN + skip at load time. The claude agent ships with `external_paths: [~/.claude/skills]` so Claude Code skills flow in live (no copy; edits propagate on next meeting join). `progressive_disclosure` controls whether the LLM gets a menu (lazy `load_skill` tool) or sees every skill body up-front. Individual SKILL.md files may declare `mcp-required: [server, ...]` in frontmatter; the wizard locks those MCP toggles on so the skill can't be chosen without the server it needs. If the LLM calls a tool from a disabled server anyway, the runtime raises a granular "server disabled" error (`pipeline/mcp_client.disabled_server_for_tool`) that the bot relays to the user in chat. Legacy `paths: [...]` shape is still accepted on load — `config.py` translates in-memory and logs a one-line nudge to re-run setup.
- `ground_rules` — always-true constraints (string). Composed *last* into the system prompt.
- `personality` — who the bot is; voice, tone, disposition (string). Composed *first* into the system prompt.

`config.py` joins `personality` + `ground_rules` with a blank line to produce `SYSTEM_PROMPT`. Keeping them as two top-level blocks (vs one `llm.system_prompt` blob) reflects that they're two distinct authoring concerns — voice/identity and always-on rules — and the wizard walks them as separate steps.

Tuned-once internals (LLM max_tokens, tool-call timeout/heartbeat, tool-result truncation, Meet lobby wait, caption silence gap, browser profile path, `ALONE_EXIT_GRACE_SECONDS`) live in the `INTERNAL TUNING` block at the top of `config.py` — identical across bots, edit there to change globally.

API keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GITHUB_TOKEN`, etc.) live in a single `.env` at `~/.operator/.env`, shared across all bots. The wizard writes into that file; `config.py` loads from it via `load_dotenv(Path.home() / ".operator" / ".env")`. Debug dumps from `session.save_debug` and the adapters' failure paths land in `~/.operator/debug/`. Google-session artifacts (`~/.operator/browser_profile/`, `~/.operator/auth_state.json`) live next to them. A one-shot migration in `__main__.py:_migrate_legacy_user_artifacts` relocates any pre-Phase-14.5 copies (`.env`, `browser_profile/`, `auth_state.json`) that were stored at the repo root.

### Tool Confirmation

`chat_runner.py` defines `READ_TOOLS` — a set of known read-only MCP tools that auto-execute without confirmation. Any tool not in that set prompts the user in chat before running. Per-server overrides (`confirm_tools`) in the bot's `agents/<name>/config.yaml` can force confirmation on specific tools.

### Imported MCP Servers — cwd & Secrets Contract

The `claude` agent re-imports MCPs from `~/.claude.json` and `claude mcp list` on every boot (via `_sync_claude_imports` in `__main__.py`). Two conventions imported servers must follow to spawn cleanly under operator:

- **Absolute paths.** Operator spawns MCP subprocesses from the user's invocation cwd (or `$HOME` for the bundled transcript server). Relative `command` or `args` paths in `~/.claude.json` (e.g. `./mcp-server/index.js`) won't resolve. Use `~/...` (expanded) or `/abs/path/...` instead. Servers using internal `dotenv.config()` to load `./.env` from cwd will not find that file under operator — move secrets to `~/.operator/.env` and reference via `${VAR}` in the server's env block.
- **Secrets in `~/.operator/.env`.** API keys and tokens belong in the shared `.env` file (loaded by `config.py:load_dotenv`), referenced as `${VAR}` in the MCP server's `env` block. Don't paste literal secrets into `~/.operator/agents/<name>/config.yaml` — the file is opened routinely via `operator edit` and is more likely to leak (screenshots, bug reports, accidental git-add) than `~/.claude.json` itself.

Imported entries split field ownership between the source and the user. Re-imports overwrite `command`, `args`, `env`, `auth`, `auth_url`, `description` from `~/.claude.json`. They preserve `enabled`, `hints`, `read_tools`, `confirm_tools` so meeting-scope edits via `operator edit claude` survive each sync. Servers removed from `~/.claude.json` are dropped on the next sync.

### Participant-based Auto-leave

When the bot has seen at least one other participant and is then alone for `ALONE_EXIT_GRACE_SECONDS`, it leaves automatically. 1-on-1 mode (participant count ≤ `ONE_ON_ONE_THRESHOLD`) skips the trigger-phrase requirement.

## Development Notes

- `docs/agent-context.md` tracks current dev phase, hard-won debugging knowledge, and working context — read it before making structural changes.
- `docs/roadmap.md` has the phase checklist and strategic direction.
- The voice pipeline was decoupled in session 93 (April 2026) and preserved on the `voice-preserved` branch. `main` is chat-only.
- `~/.operator/browser_profile/` and `~/.operator/auth_state.json` hold logged-in Google session state. They are user-scoped, never inside the repo.
