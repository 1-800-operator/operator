# Session 186 handoff (2026-05-04) — closed-source pivot split into operator-cloud; OSS main reverted to S182 state; two loose ends queued for S187

This was a structural session. **Reversal of the session-183/184/185 closed-source pivot decision** as it applied to *this* repo: the user decided to ship the OSS product before the closed-source one, not after, so the pivot work that had landed on `main` here was relocated rather than continued. Pivot continues as a separate product on its own repo.

**What happened this session:**

1. **Cloned this repo to `~/Desktop/operator-cloud/`.** That repo carries the full git history including the seven pivot commits (`beff05b` through `5d6cd06`) — all S183–S185 strategy work, the `experiments/cloud-browser-spike/` artifacts, and the four pivot-era memory files. Closed-source v1 work resumes there starting at the S185 head (`docs/product-strategy.md`'s Phase A1 plan: web stack pick + DB pick + trial-trigger scaffold).
2. **Reverted the seven pivot commits on this repo's `main`** via `git revert --no-commit beff05b..5d6cd06` (folded into this session's single commit). `docs/agent-context.md`, `docs/handoff.md`, `docs/roadmap.md` are now bit-identical to their state at `1292593` (session 182, pre-pivot).
3. **Removed `experiments/cloud-browser-spike/`** from the working tree (the dir was untracked — never committed here). Kept `experiments/captions/` which is OSS-relevant and referenced by the `reference_caption_experiments` memory.
4. **Cleaned the auto-memory dir** for this project (`/Users/jojo/.claude/projects/-Users-jojo-Desktop-operator/memory/`):
   - Deleted: `project_closed_source_pivot.md`, `project_cloud_browser_auth_requirement.md`, `project_v1_track_a_only.md`.
   - Restored `project_oss_ethos.md` to its pre-pivot wording (dropped the "SUPERSEDED at session 183" header + frontmatter description).
   - Rebuilt `MEMORY.md` index.
   - The cloud repo gets its own memory dir at `/Users/jojo/.claude/projects/-Users-jojo-Desktop-operator-cloud/memory/` — populated as a full mirror of the pre-cleanup state, so all four pivot files survive there.
5. **Roadmap loose ends queued.** Added a `## Loose Ends — Hash Out Next Session (S187)` block at the top of `docs/roadmap.md` (between the prior-status blockquotes and `## Completed Phases`) with two items: (a) Claude `--resume` flag — discovered during pivot-era spikes; we aren't yet using it in `pipeline/providers/claude_cli.py` and likely should. (b) Codex captions parity (R8) — deferred at S179 with a documented gap (codex's `mcp-server` mode doesn't accept `--mcp-config` to inherit the bundled transcript MCP).

**Exact next step (session 187): hash out the two loose ends.** They are placeholder entries, not yet phased. Open S187 by discussing each with the user — clarify the `--resume` flag's exact name + behavior + use case (continuity across reconnects? mid-meeting brain reset?), and decide the codex captions strategy (plugin manifest? sidecar? live with the gap?). Then sequence both into the active phase plan and start implementation. After that, pick up where session 182 left off — Phase 14.13.3 (Cloudflare Pages walkthrough was already delivered; flip repo public + drive dashboard steps 2–3 + second-Mac QA via `curl | sh`) → Phase 14.13.4 (archive `dufis1/operator`) → Phase 16 (README rewrite, demo GIF, landing site, launch).

**Open carry-overs from S182 (still applicable on OSS main):**

1. **Repo must be flipped public again before QA.** `gh repo edit 1-800-operator/operator --visibility public --accept-visibility-change-consequences`. `uv tool install git+https://...` resolves via anonymous git clone, so private won't work for the curl|sh install flow.
2. **Re-snapshot procedure** for any time `install.sh`, `_redirects`, or any other ship-list file changes: commit on `main`, then `git checkout --orphan public-snapshot-vN` → `git rm -rf --cached .` → `git add` the 11-item ship list → `git commit -m "Release vN"` → `git push -f public public-snapshot-vN:main` → `git checkout -f main`.
3. **Pages shape A vs B.** Currently planning shape A (Pages deploys operator public repo, output `/`). Shape B (separate landing repo) cleaner but not needed until Phase 16 lands a real `index.html`.
4. **install.sh end-to-end is unverified.** Bash syntax + Python detection one-liners confirmed locally; the actual `uv tool install` → `playwright install chromium` → working `operator` binary chain is what second-Mac QA exists to validate.
5. **Older carry-overs from S181:** PyPI bump from v0.0.1 (post-launch), tier-2 audit + ~70 appendix nits from `docs/code-quality-audit-session-178.md` (post-launch), S177 nits (`_do_send_chat` ID-readback race, `MeetingRecord.append` memory-vs-disk divergence, `_on_tool_use` docstring drift) (post-launch), 5 test files needing in-file `OPERATOR_BOT` (post-launch). **Note:** the prior S181 carry-over "codex R8 caption parity + outbound MCP-import sync" is now formalized as a Loose End (item #2 above) — pull it up from "post-launch" into the active discussion.

**Where the closed-source work continues:** `~/Desktop/operator-cloud/`. Open that directory in a fresh Claude Code session to resume Phase A1 (web stack + DB pick + signup/trial-trigger scaffold). The four pivot-era memory files survive there; this repo's memory no longer references them.
