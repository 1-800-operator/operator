# Session 226 handoff (2026-05-13) — 14.22 architecture pivot validated; refactor not yet started

## What landed this session

One commit on `main`: `8816cef` (S226: 14.22 pivot spike — claude -p → interactive claude via PTY+hooks). No production code changed; the commit ships seven spike scripts, the bench/ workdir with proof-of-concept hooks, captured byte streams as evidence, and the full architectural decision in `debug/14_22_pty_spike/DECISION.md`.

Anthropic announced this morning (2026-05-13) that starting **2026-06-15**, `claude -p` and Agent SDK usage no longer count toward Claude subscription limits — they draw from a small per-plan Agent SDK credit ($20 Pro / $100 Max 5x / $200 Max 20x), then API rates. Operator's entire architecture spawns `claude -p --resume <id>` per meeting; the economic premise dies. The session was spent validating an alternative architecture and committing the plan.

**Architecture decided:**
```
INPUT:  bracketed-paste wrap + \r into PTY  (\x1b[200~ <msg> \x1b[201~\r)
OUTPUT: hooks (operator-plugin ships hooks/hooks.json — Stop, PreToolUse,
                PostToolUseFailure, PermissionDenied, StopFailure, SessionStart)
PERMS:  claude --dangerously-skip-permissions (unconditional)
SPAWN:  cwd = user's project dir (so --resume finds the session JSONL)
```

**Critical findings (all in DECISION.md):**
1. **Stop-block as input is dead** — claude's prompt-injection defense refuses every hook-injected message; even a counter-instruction at session start doesn't override it. Filtering "Stop hook feedback:" at an API proxy bypasses an Anthropic safety feature (strategic non-starter).
2. **Bracketed-paste wrap is universal** — survives quotes, backslashes, multi-line, emoji, code fences (SHA-256 verified byte-for-byte). Char-by-char typing silently drops chars on long messages.
3. **Hooks deliver everything** — `Stop.last_assistant_message`, `PreToolUse{tool_name, tool_input}`, `PostToolUseFailure`, `PermissionDenied`, `StopFailure`. No screen scraping needed.
4. **`claude --resume` is cwd-scoped** — must spawn inner-claude with `cwd=<user-project-dir>`. No `--working-directory` flag exists. Implication: user's project hooks fire inside meetings (same as today's `-p` behavior — not a regression).

All four user flows preserved: `/operator:slip` plugin entry, pre-loaded context via `--resume`, post-meeting continuation via shared session_id, transcript MCP unchanged.

## Next step

**Start the production refactor following `debug/14_22_pty_spike/DECISION.md` sections A–M in order.**

Section ordering is intentional: A (spawn) → B (spawn-ready handshake via SessionStart hook) → C (bracketed-paste send) → D (hook handlers + plugin layout) → E (state dir) → F (env-var contract `OPERATOR_SESSION_DIR`) → G–H (callback remap + reply assembly) → I (foreign-hook detector) → J (tear-down race fix) → K (Claude Code version floor) → L (plugin install validation). Each section in DECISION.md has the concrete change spelled out. Estimate: 1-2 focused sessions for A through H, 1 session for I-L plus integration tests (numbered 20–25 in DECISION.md).

The first concrete action: open `pipeline/providers/claude_cli_provider.py` and replace the `claude -p` spawn with the interactive PTY-driven spawn from section A. Per `feedback_surgical_changes`, ship one section at a time, test before moving on.

## Open follow-ups

- **Anthropic's classification past June 15** — untestable until then; the only existential unknown. Watch Claude Code release notes and `claude --help` for new flags/env vars that signal Anthropic is patterning against PTY-driven use. If they reclassify, BYO-API-key (DECISION.md section M) becomes the only option.
- **Codex addition** — revisited and decided NOT to build now (was previously resolved NO per `project_second_provider_resolved_no.md`, now back on the table as eventual). Preparation: keep `LLMProvider` abstraction clean during the refactor — no Claude-specific concepts (hooks, PTY, bracketed paste, "Stop hook feedback:") should leak into `pipeline/llm.py` or `pipeline/chat_runner.py`. Document the contract in a docstring. Optionally add a stub `CodexCLIProvider(LLMProvider)` that raises `NotImplementedError` as a compile-time guard.
- **Foreign-hook safety net** — operator's `doctor` should survey `~/.claude/settings.json` for slow/blocking hooks and warn the user before relying on operator for time-sensitive meetings. Detector pattern in DECISION.md section I.
- **From S225 (still open):** AEC bleed mitigation integration — `debug/14_23_aec_spike/` has the design + parameters locked; 7-step integration plan documented in roadmap. Not blocked by the 14.22 pivot; can land in parallel.

## State of the repo

`origin` is ahead by 2 commits — `72e0005` (S225 AEC spike notes) and `8816cef` (this session). Neither pushed yet. Both should go to both `origin` and `public` per project convention. README.md still has uncommitted user-owned billing-protection wording — convention is not to commit.

## How to verify the pivot plan in a live meeting (after refactor lands)

1. `uv tool install --reinstall .` after each refactor section.
2. Spawn-ready handshake: confirm `~/.operator/sessions/<id>/ready.flag` appears within 2s of inner-claude spawn (via the SessionStart hook).
3. Send a long meeting message (>300 chars with emoji + code block); confirm `replies.jsonl` row appears with `last_assistant_message` matching expectation.
4. Trigger a multi-tool turn; confirm N `PreToolUse` events in `tools.jsonl` + 1 `Stop` event in `replies.jsonl`.
5. Trigger a tool failure; confirm `PostToolUseFailure` event in `errors.jsonl` and operator's `denial` callback fires.
6. Cross-meeting context: pre-load a memorable fact in the user's main Claude Code session, run `/operator:slip`, ask claude in the meeting to recall, confirm reply mentions the fact.
7. Post-meeting recall: end the meeting, return to the user's main Claude Code session, ask "what did we discuss?" — confirm meeting interactions are in context.
