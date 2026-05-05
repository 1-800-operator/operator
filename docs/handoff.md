# Session 191 handoff (2026-05-05) — strategic pivot, no code changes

Conversation-and-roadmap session, no implementation. The product surface for v0.0.1 was massively reduced: cut the entire custom-bot construction surface (wizard, edit command, `pm`/`engineer`/`designer` agents, `OPERATOR_BOT` routing, system_prompt composition, READ_TOOLS allowlist, `.env`, `config.yaml`) and replace it with a thin three-command bridge to Claude Code: **slip → dial → deploy**. Wizard work deferred to Post-MVP as "Operator Studio". Claude Code Plugin queued in Post-MVP as Shape E (slash commands shell out to locally-installed Operator — terminal-free experience for users who don't want to live in iTerm). One commit landed on origin/main: `0870966` — `docs/roadmap.md` + new `docs/scratchpad.md` (marketing copy + verb logic).

## Exact next step (session 192)

**Begin Phase 14.19 — bridge architecture cutover.** Start with step 14.19.1 (create `bridges/claude.py` with hardcoded constants: claude spawn cmd, `@claude` trigger, robot-emoji default reply prefix, transcript MCP wiring). Then step 14.19.2 (wire `slip`/`dial`/`deploy` commands in `__main__.py` with JIT preflights and `--yolo` flag). Step 14.19.3 (CDP attach for slip mode in new `connectors/attach_adapter.py`) is the meaty step — quit-Chrome-relaunch-with-debug-flag dance. Mass deletion (step 14.19.7) and permission-flow rewrite (step 14.19.8) come once the new shape is functional. Total phase est: ~22h / 3 days.

## Open questions / blockers

- Reply prefix (step 14.19.6) needs side-by-side mockup in a real Meet before final pick — don't lock from spec; eye-check brackets vs robot vs italics in actual Meet chat rendering.
- S190 carry-over warnings (`[claude] ⚠ MCP needs attention`) likely become moot once `_sync_claude_imports` is deleted in step 14.19.7 — verify when that step lands.
- `docs/pre-launch-audit.md` (still untracked from S187) — defer running until *after* Phase 14.19 lands; Pass 4 (dead code) gets easier when we've already deleted half the codebase.
- Nothing pushed to `public/main` this session — pure dev-only planning. The bridge cutover lands on origin/main first; public-snapshot worktree-push pattern (per S189/190) when implementation begins shipping.
