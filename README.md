# Operator

> Operator is an open-source CLI tool that drops AI participants into your Google Meet. Install via pip, configure via YAML, run from your terminal.

Joins, reads chat, replies via an LLM with tool access (Linear, GitHub, and
other MCP servers you wire up), and leaves when everyone else does.

```bash
operator dial pm                                       # open a fresh Meet
operator dial pm https://meet.google.com/xxx-yyyy-zzz  # join a specific Meet
operator try pm                                        # terminal test-drive, no Meet
operator                                               # show available agents
```

`pm` is a sample bot under `agents/`. Drop in `operator build` to create your own.

## Privacy & logs

Operator writes a detailed diagnostic log to **`/tmp/operator.log`** on every
run. For now, this file contains:

- The Meet URL the bot joined (a capability token — anyone with it can join).
- Chat messages the bot sees, including sender names.
- LLM prompt/response metadata and tool call arguments + results.
- Captions, when `transcript.captions_enabled: true` in the agent config.

**The file never leaves your machine**, but it is plain text in a shared
directory — treat it like any other local artifact. macOS typically clears
`/tmp` on reboot; Linux may not. Delete it manually if it matters.

Chat history also lands in `~/.operator/history/<slug>.jsonl` — that's the
durable record the bot replays from between turns. Same sensitivity profile.

### Resume your meeting in Claude Code

For the `claude` agent specifically, the bot's brain is a `claude -p`
subprocess running under your Claude Max subscription, which means meetings
land in **claude's own session store** — the same place your regular Claude
Code work is persisted:

```
~/.claude/projects/<encoded-working-dir>/<session-id>.jsonl
```

The folder name is whatever working directory you launched `operator dial
claude` from (URL-encoded). Each meeting writes one session file with the
full message history including tool calls and tool results. Operator uses
this to recover gracefully if the brain subprocess dies mid-meeting (it
respawns with `claude -p --resume <session-id>` and inherits everything).

**Bonus workflow:** because these are normal Claude Code sessions, you can
pick the meeting back up in your terminal afterwards. From the same
directory you ran operator from:

```bash
claude --resume                 # picker — meeting sessions appear next to
                                # your coding sessions
claude --resume <session-id>    # jump straight in
```

Now you're chatting with the same brain that just left the meeting — full
context, every tool call, every Linear ticket it filed. Great for follow-up
work the bot started and you want to finish.

**What this means for your filesystem:** meeting transcripts and your
regular Claude Code coding sessions share `~/.claude/projects/<dir>/`.
Everything stays local; nothing ships off your machine. But two things are
worth knowing:

1. The `claude --resume` picker (no args) lists everything in the current
   directory, mixed together. Meeting sessions appear alongside coding
   sessions — that's the feature, but also: don't accidentally pick the
   wrong one.
2. The folder grows over time. Same retention semantics as your regular
   Claude Code work — manage it however you already manage that. To
   inspect or prune by hand: `ls -lt ~/.claude/projects/<dir>/`.

The other bundled agents (`pm`, `codex`, custom bots) don't use this path —
they talk to the LLM via API and don't write to `~/.claude/projects/`.

### Never commit these

API keys live in a single `.env` at `~/.operator/.env`, shared across all
bots. The following files hold secrets or logged-in Google session state and
must stay local:

- `~/.operator/.env` — API keys (OPENAI_API_KEY, ANTHROPIC_API_KEY, GITHUB_TOKEN, …)
- `credentials.json` — Google OAuth client secrets
- `token.json` — Google OAuth access/refresh tokens
- `~/.operator/auth_state.json` — Playwright storage state (Google session cookies)
- `~/.operator/browser_profile/` — persistent Chrome profile (Google session cookies)

All of the above are ignored by `.gitignore`. If you see one show up in
`git status` untracked, something has gone wrong — don't `git add .` blindly.
See `docs/security.md` for the full threat model.

## Voice mode

Each bot has a `voice` setting under `agent:`. It controls how much
detail operator puts into the *system's* messages (the sterile
approval challenge for destructive tools, the optional progress
narrator). The bot's actual conversational voice — friendly,
technical, in Spanish, or even pirate — lives in the `system_prompt`
block of the bot's config.yaml. Operator doesn't template persona;
the bot speaks for itself.

- **`plain`** — meeting-friendly. The system's approval challenge is
  a one-line summary that hides bulk content (Write body, MultiEdit
  edits) but keeps imperative fields (Bash command, file paths, URLs)
  verbatim. Operator's narrator stays silent — the bot self-narrates
  in chat in its own voice via a `system_prompt` directive. **Default.**
- **`technical`** — developer-flavored. The approval challenge is a
  full parameter dump with head…tail truncation. Operator's narrator
  emits deterministic "Working: …" lines for auto-approved tools.

Switch in `agents/<bot>/config.yaml`:

```yaml
agent:
  name: "MyBot"
  voice: plain        # or technical
```

The conversational shape — "Let me check Sentry first, ok?" or "Aye
matey, time to peek at yer files" — comes from the bot's prompt, not
from operator. So if you set the bot's `system_prompt` to talk like a
pirate, every chat message it sends in plain mode reads like a pirate.
The system's approval challenge stays neutral underneath as a
machine-readable safety gate.

Imperative fields (URLs, file paths, Bash commands) are shown verbatim
in **both** modes — these describe what's about to happen and you need
to see them to make a sensible yes/no decision.

The pre-session-169 `permission_verbosity: terse | verbose` field still
loads with a deprecation log (`terse` → `plain`, `verbose` → `technical`).
Move the value to `agent.voice` to silence the warning.

## MCP permissions

For the `claude` agent (track A), built-in tools (Read, Bash, Write, …) are
gated by the `permissions` block in `agents/<bot>/config.yaml`. The `operator
build` wizard walks you through the built-in tools as a checklist; tools listed
under `auto_approve` run silently, anything under `always_ask` (and anything
not on either list) pauses the bot for a chat confirmation.

**MCP tools** (Sentry, Linear, GitHub, etc.) ask by default — every Sentry
issue lookup, every Linear ticket fetch, every GitHub PR read. To skip the
prompt for routine reads, edit the YAML and add fnmatch patterns:

```yaml
permissions:
  auto_approve:
    - Read
    - Grep
    - Glob
    - LS
    - WebSearch
    - ToolSearch
    # Per-server read auto-approval. Patterns are fnmatch globs.
    - "mcp__sentry__get_*"
    - "mcp__sentry__list_*"
    - "mcp__sentry__search_*"
    - "mcp__claude_ai_Linear__get_*"
    - "mcp__claude_ai_Linear__list_*"
  always_ask:
    - Bash
    - Write
    - Edit
    - MultiEdit
    - NotebookEdit
    - WebFetch
    - Task
    # Specific deny on top of a broad allow — always_ask wins on overlap:
    - "mcp__sentry__analyze_issue_with_seer"
```

`always_ask` is matched first, so an explicit deny pattern beats a broader
allow pattern on the same tool.

**Audit your patterns after upgrading an MCP server.** MCP tool names are
server-controlled. If a server renames `get_resource` → `fetch_resource`, your
`get_*` glob silently stops covering the renamed tool — which fails safe (the
bot starts asking again) but is worth a glance after `claude mcp` upgrades.

## Uninstall

```bash
uv tool uninstall operator   # removes the CLI + PATH shim
rm -rf ~/.operator           # removes agents, history, and .env
```

## More

- `CLAUDE.md` — architecture, commands, configuration layout.
- `docs/roadmap.md` — phase plan.
- `docs/agent-context.md` — current development state.
