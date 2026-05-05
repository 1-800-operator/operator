# End-to-End QA Checklist (Phase 14.8)

Fresh macOS environment. User-facing surface only.

## Install paths

- A — `curl -fsSL https://1-800-operator.com/install | bash`
- B — `curl -fsSL https://1-800-operator.com/install | less` → inspect → pipe to `bash`
- C — `uv tool install git+https://github.com/...` → `operator setup`

## Preflight & first-run

- `~/.operator/.env` seeded, mode 0600
- Playwright Chromium install prompt + completion
- Missing Chrome.app → soft warn (preflight) / hard exit (dial)
- Legacy artifact migration (if any pre-existing `.env` / `browser_profile/` at repo root)

## Wizard — `operator setup`

- Preset: `claude` (gated on Claude Code installed + logged in)
- Preset: `codex` (gated on Codex CLI installed + logged in)
- Preset: `custom` (from-scratch path)
- Steps 1–4 complete: agent identity → LLM → tools (MCP) → playbooks (skills) → system prompt
- API key prompts write to `~/.operator/.env` (not agent yaml)
- MCP `mcp-required` skill locks correct server toggles on
- Wizard exit + re-entry resumes cleanly

## Wizard — `operator edit <bot>`

- Edit existing preset bot
- Edit custom bot
- `operator edit env` opens env file
- Edits persist across re-imports (claude agent: `enabled`/`hints`/`read_tools`/`confirm_tools` survive sync)

## CLI surface

- `operator` (no args) — usage + agent list
- `operator dial <bot> <meet-url>` — joins specific meeting
- `operator dial <bot>` — auto-opens meet.new
- `operator run <bot>` — hidden alias still works
- `operator auth <bot>` — OAuth flow for hosted MCP
- Unknown bot → clear error
- Missing CLI dep (claude/codex) → exit 2 + clear stderr

## Live meeting — group (single session covers most)

- Bot joins, opens chat, posts intro
- `@operator` trigger fires
- Non-trigger messages ignored
- Sender filter: bot ignores own messages
- History tail replayed correctly across multiple turns
- Tool call — read-only auto-executes
- Tool call — write requires chat confirmation
- Tool call — user denies, bot acknowledges
- Tool result truncation on oversized output
- Tool timeout / heartbeat behavior
- Disabled-server error surfaced in chat
- Skill invocation (progressive disclosure: `load_skill`)
- Skill invocation (eager mode: full bodies in prompt)
- Captions on/off toggle works
- Bot leaves when alone past grace period

## Live meeting — 1-on-1 (separate session)

- Trigger phrase NOT required
- All messages addressed to bot
- Auto-leave behavior identical

## Live meeting — recovery

- Network blip → reconnect
- Tab close mid-meeting → graceful exit
- Lobby wait → admit
- Lobby wait → denied

## Claude agent specifics

- `_sync_claude_imports` runs on first dial
- Re-import preserves user toggles
- Hosted MCPs (Gmail/Drive/Linear) wrap via `mcp-remote`
- Skills from `~/.claude/skills/` flow live (edit propagates next join)
- Removed servers in `~/.claude.json` drop on next sync

## Codex agent specifics

- First-dial import from Codex config
- MCP bridge labeling correct in wizard

## Custom agent specifics

- From-scratch build produces working bot
- System prompt voice + rules render correctly in chat

## Logs & debug

- `/tmp/operator.log` populated
- `~/.operator/debug/` captures failure dumps
- No secrets in log output

## Uninstall / cleanup

- `uv tool uninstall` removes binary
- `~/.operator/` survives (user opt-in to delete)

