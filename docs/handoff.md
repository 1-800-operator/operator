# Session 181 handoff (2026-05-02) — wizard polish + reveal card width

This session shipped two commits (`d053a74`, `598cfb7`) closing out the codex wizard parity round. **Wizard polish (`d053a74`):** dynamic step numbering threaded through every step function so edit mode reads 1..6 and setup mode 1..7 with no duplicates or gaps (fixes pre-existing duplicate `4.` and off-by-one on API keys + sign-in); codex skills + MCPs steps now render the inheritance content as locked `[✓]` checkbox glyphs in dim styling and pause on `Press Enter to continue…` so users see real acknowledgement steps rather than feeling like ones were silently skipped; codex spinner copy aligned with claude's loading beat; codex add/remove instruction blocks trimmed to one dim line each; stale "Codex's internal allowlist…" instruction line removed from the codex permissions step plus the "Permissions — space to toggle, enter to confirm" `select_many` title from the claude permissions step; reveal card width is now content-aware via `build_card.width_for_reveal(console, items=...)` so long MCP/skill names like `claude-ai-google-calendar` render on one line, capped by terminal width minus a small margin. **Cursor CLI preset (`598cfb7`):** filed under Post-MVP → Agent Presets in `docs/roadmap.md` — same pattern as claude/codex (CLI-prereq-gated step 1, brain handoff via `cursor-agent`, `pipeline/cursor_import.py` peer to `pipeline/codex_import.py`, read-only inheritance panel, regression test). Inherits Phase-15.9 brain-MCP plumbing for free.

**Hard-won lesson logged.** First pass on `width_for_reveal` undercounted the glyph segment by 2 cells because I treated `"⚡ "` as 2 cells (1 char + 1 space) when it's actually 3 cells (the lightning-bolt is a wide glyph at `cell_len == 2`). Long names wrapped anyway. Fix bumped `_ITEM_LINE_OVERHEAD` 18 → 20. Lesson: budget glyph segments via `rich.cells.cell_len`, not `len()`, when wide glyphs are in the mix. Entry added to `agent-context.md` Hard-Won Knowledge.

**Exact next step (session 182): Phase 14.13.1 — repo creation + initial push** (still pending from session 180/181).
1. **First: confirm the org-name choice.** User pushed back on `1-800-operator` org name in session 180 ("I want it to be called just operator"). Counter-argument: `1-800-operator/operator` parallels the domain handle, separates project identity from personal account, matches `astral-sh/uv` / `vercel/next.js` shape, costs one extra UI hop. Settle this before pushing.
2. `gh repo create 1-800-operator/operator --private --description "Claude Code, in your Google Meet"`
3. `git remote set-url origin git@github.com:1-800-operator/operator.git`
4. `git push -u origin main` (20 commits ahead at session end) + `git push --tags`
5. Manual sanity check the new repo
6. Flip public via `gh repo edit --visibility public`

Then **Phase 14.13.4** — archive `dufis1/operator` (0 stars, 0 forks, 3 closed test issues, 4 PRs) with description "moved to github.com/1-800-operator/operator". Then **Phase 16** (README rewrite mentioning both claude + codex, demo GIF, landing site at `1-800-operator.com`, launch).

**Open carry-overs (unchanged from session 180):**
1. **Pages attachment** for `1-800-operator.com/install` — gated on Phase 14.5 producing an actual `install.sh`. Cloudflare zone is empty + ready.
2. **PyPI version bump** for placeholder package `1-800-operator` (currently v0.0.1).
3. **Codex carry-overs** (from S179, still v2): R8 caption parity, outbound MCP-import sync from `~/.codex/config.toml`.
4. **Tier-2 audit** + ~70 appendix nits from `docs/code-quality-audit-session-178.md` still unbroached. T1.3 / T1.8 / T1.11 also deferred.
5. **S177 nits**: `_do_send_chat` ID-readback race, `MeetingRecord.append` memory-vs-disk divergence, `_on_tool_use` docstring drift.
6. **5 test files** (`test_anthropic_provider`, `test_claude_cli_provider`, `test_openai_provider`, `test_permission_chat_handler`, `test_streaming_paragraph_flush`) don't set `OPERATOR_BOT` in-file; pass only when wrapped (`OPERATOR_BOT=claude python ...`). Pre-existing pattern.
