# Session 181 handoff (2026-05-02) — wizard polish + dial rename

This session shipped three commits on top of session 180. **`d053a74`** finished the codex-parity wizard polish (dynamic step numbering, codex skills/MCPs as real acknowledgement steps, content-aware reveal card width via `build_card.width_for_reveal`) and absorbed three end-of-session follow-ups: `(brain)` → `(MCP bridge)` for the codex MCP step + reveal card via `equipped_mcps()` decoration; step 6 (API keys) only burns a step number when there's actually a key to prompt for, so codex sign-in renders as step 6 instead of step-7-after-flash; the build-card right pane was removed from the in-flight MCPs + Skills picker steps so terminal resizes mid-step can't mangle it (final reveal card is the only card the user sees now). **`598cfb7`** filed the cursor CLI agent preset under Post-MVP → Agent Presets (same pattern as claude/codex) and fixed the `docs/promo-video-script.md` trailing newline. **`80cfd2f`** renamed `operator run <bot>` → `operator dial <bot>` to match the 1-800-Operator phone metaphor, with `run` kept as a hidden alias (same dispatch arm, undocumented in `--help`) — 15 files updated across code, tests, top-level docs, and active step-by-step docs; `tests/test_entry_cli.py` got 4 renames + a new `test_main_run_alias_still_dispatches_to_run_bot` to pin the alias against accidental drop. All 29 entry-CLI tests pass; setup + codex regression tests pass. Historical narrative in `docs/agent-context.md` and the body of `docs/roadmap.md` deliberately left intact.

**Exact next step (session 182): Phase 14.13.1 — repo creation + initial push (still pending from sessions 180/181).**
1. Confirm org name choice (`1-800-operator/operator` vs just `operator` under personal account — counter-argument written in this handoff and in s180's)
2. `gh repo create 1-800-operator/operator --private --description "Claude Code, in your Google Meet"`
3. `git remote set-url origin git@github.com:1-800-operator/operator.git`
4. `git push -u origin main` (21 commits ahead at session end) + `git push --tags`
5. Manual sanity check the new repo
6. Flip public via `gh repo edit --visibility public`

Then **Phase 14.13.4** — archive `dufis1/operator` (0 stars, 0 forks, 3 closed test issues, 4 PRs) with description "moved to github.com/1-800-operator/operator". Then **Phase 16** (README rewrite mentioning both claude + codex, demo GIF, landing site at `1-800-operator.com`, launch).

**Open carry-overs:**
1. **Pages attachment** for `1-800-operator.com/install` — gated on Phase 14.5 producing `install.sh`. Cloudflare zone is empty + ready.
2. **PyPI version bump** for placeholder package `1-800-operator` (currently v0.0.1).
3. **Codex carry-overs** (from S179, still v2): R8 caption parity, outbound MCP-import sync from `~/.codex/config.toml`.
4. **Tier-2 audit** + ~70 appendix nits from `docs/code-quality-audit-session-178.md` still unbroached. T1.3 / T1.8 / T1.11 also deferred.
5. **S177 nits**: `_do_send_chat` ID-readback race, `MeetingRecord.append` memory-vs-disk divergence, `_on_tool_use` docstring drift.
6. **5 test files** (`test_anthropic_provider`, `test_claude_cli_provider`, `test_openai_provider`, `test_permission_chat_handler`, `test_streaming_paragraph_flush`) don't set `OPERATOR_BOT` in-file; pass only when wrapped (`OPERATOR_BOT=claude python ...`). Pre-existing pattern.
7. **Forward-facing roadmap status blurbs** still contain ~27 `operator run` references inside session-by-session blockquotes. Top-line status was rewritten to `dial`; if you want the historical fold swept too, it's a 5-minute sed pass — just decide whether you want to preserve the historical record verbatim.
8. **Open product question still on the table:** confirm `1-800-operator/operator` org-name choice before pushing public. User pushed back briefly in S180; counter-argument: parallels domain handle, separates project identity from personal account, matches `astral-sh/uv` / `vercel/next.js` shape, costs one extra UI hop.
