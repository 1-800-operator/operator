# Session 180 handoff (2026-05-02) — Phase 14.12 + 14.13.3 closed

This session closed two launch-blocking phases and queued the next one. **14.12.3 / 14.12.4 (`0c22b42`):** v1 product surface pruned to Track A — bundled `agents/{pm,engineer,designer}/` deleted outright per user direction ("we just ship with claude or custom"); from-scratch wizard baseline moved to `src/_1_800_operator/custom_template.yaml` carrying the full MCP gallery (all `enabled: false`); wizard step 1 hardcoded to `claude` (preset, prereq-gated) or `custom`; `claude_code` MCP module relocated `agents/engineer/` → `mcp_servers/`; tests updated (`_PM_CONFIG` → `_CUSTOM_TEMPLATE`, `based_on="pm"` → `"custom"`, bundled-MCP static check extended to validate `custom_template.yaml`). 14.12.4's original "copy bundled skills into ~/.claude/skills/" plan was DROPPED — bundled gallery is exclusively for the custom-bot path, not injected into the user's Claude Code env. **14.13.3 (DNS, no commit — registrar-side):** `1-800-operator.com` migrated Porkbun → Cloudflare nameservers (`deb.ns.cloudflare.com` + `devin.ns.cloudflare.com`); zone empty (9 Porkbun parking records deleted); Pages attachment deferred to Phase 14.6 proper. Forward-facing copy in roadmap (14.6 / 14.8 / 16.1 / 16.3) flipped from `operator.build` → `1-800-operator.com`; historical narrative preserved verbatim. **Side ship (`7796f39`):** codex preset polish, post-MVP roadmap entry for "mid-meeting brain reset" (`@bot reset` chat command), and a 90s promo video script.

**Exact next step (session 181): Phase 14.13.1 — repo creation + initial push.**
1. `gh repo create 1-800-operator/operator --private --description "Claude Code, in your Google Meet"`
2. `git remote set-url origin git@github.com:1-800-operator/operator.git`
3. `git push -u origin main` (15 commits ahead at session end) + `git push --tags`
4. Manual sanity check the new repo
5. Flip public via `gh repo edit --visibility public`

Then **Phase 14.13.4** — archive `dufis1/operator` (0 stars, 0 forks, 3 closed test issues, 4 PRs) with description "moved to github.com/1-800-operator/operator". Fresh push is fine — no transfer needed.

**Open product question to settle before pushing public**: user briefly pushed back on the `1-800-operator` GitHub org name during the session ("I want it to be called just operator"). Counter-argument written down: `1-800-operator/operator` parallels the domain handle, separates project identity from personal account, matches `astral-sh/uv` / `vercel/next.js` shape, costs one extra UI hop. Confirm the org choice early next session, then proceed to 14.13.1.

**Open carry-overs:**
1. **Pages attachment** for `1-800-operator.com/install` — gated on Phase 14.5 producing an actual `install.sh`. Cloudflare zone is empty + ready.
2. **PyPI version bump** for placeholder package `1-800-operator` (currently v0.0.1).
3. **Codex carry-overs** (from S179): R8 caption parity, outbound MCP-import sync from `~/.codex/config.toml`.
4. **Tier-2 audit** + ~70 appendix nits from `docs/code-quality-audit-session-178.md` still unbroached. T1.3 / T1.8 / T1.11 also deferred.
5. **S177 nits**: `_do_send_chat` ID-readback race, `MeetingRecord.append` memory-vs-disk divergence, `_on_tool_use` docstring drift.
6. **5 test files** (`test_anthropic_provider`, `test_claude_cli_provider`, `test_openai_provider`, `test_permission_chat_handler`, `test_streaming_paragraph_flush`) don't set `OPERATOR_BOT` in-file; pass only when wrapped (`OPERATOR_BOT=claude python ...`). Pre-existing pattern.
