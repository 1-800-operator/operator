# Session 229 handoff (2026-05-13) — 14.22 section L landed; `phase-14.22-pty-pivot` merged to `main`

The PTY+hooks production refactor (Phase 14.22) is now **code-complete and merged to `main`**. The only thing left before it's fully shipped is the DECISION.md 20–25 integration pass, which needs live Meet runs.

## What happened this session

**Section L part 2 — rewrote `tests/test_claude_cli_provider.py`.** The old file asserted the gone per-`@mention` `claude -p --resume` shellout shape and was the one failing test in the suite. Replaced with 21 fully-mocked tests (no real `claude` spawn) for the PTY+hooks architecture: naked-spawn invariant (`_build_cmd` carries `--dangerously-skip-permissions`, no `-p`/`--append-system-prompt`/`--mcp-config`), `replies.jsonl` tailing (pickup / timeout / teardown-bail on `_stopping` / dead-proc crash), transcript tailing + seek-and-buffer with partial-line handling, Stop-payload field extraction (wrapped `{ts,kind,input}` + bare shapes), foreign-hook `"Stop hook feedback:"` detection, and three full mocked turns through `_run_turn` (streaming paragraphs, Stop-text backstop, foreign-hook notice, respawn-after-crash). Full suite green — 11 test files.

**Section L part 1 — plugin install smoke test.** No code change needed. The operator-plugin hook scripts (`_common.sh`, `session_start.sh`, `stop.sh`) are git-tracked `100755`, and `hooks/hooks.json` invokes them interpreter-explicit (`bash $CLAUDE_PLUGIN_ROOT/hooks/scripts/...`). So exec bits aren't load-bearing at runtime and `git` preserves mode bits across the desktop-app plugin sync (a `git pull` of the marketplace cache). The operator wheel doesn't bundle them at all. Verified, documented, done.

**Merged `phase-14.22-pty-pivot` → `main`.** ~20 branch commits: S227 `daedf21` (production refactor) + `a857412` (setsid fix), the full S228 G/I/J/K reshape (~14 commits), and S229 `804097d` (the test rewrite). Three doc conflicts — `roadmap.md`, `agent-context.md`, `handoff.md` — because the branch forked at S226's end-session and missed S227's main-side docs sweep. All reconciled by hand: roadmap + agent-context now carry S229 → S228 → S227(×2) → S226 in reverse-chronological order; the stale "this branch is behind main" docs notes were dropped. Code files (`attach_adapter.py`, `doctor.py`) auto-merged cleanly — main's AEC changes and the branch's PTY changes touched disjoint regions.

**Same-session `main` fix folded in:** `doctor.py` now redirects fd 2 to `/dev/null` during the mlx-whisper warmup (and prints the progress hint to stdout, not stderr) — tqdm from `mlx_whisper` writes to fd 2 directly, and the desktop-app harness reads any stderr output as a failure and silences the doctor result. Committed to `main` as `011a7b1` before the merge.

## Exact next step

**Run the DECISION.md 20–25 integration pass** — this is all that's left for Phase 14.22. It needs live Meet runs with the operator-plugin installed:

- **20 — long-meeting compaction.** Does inner-claude's context compaction mid-meeting break the transcript tail or the session resume?
- **21 — hook latency on the hot path.** Measure the Stop-hook-fire → reply-posted gap under real load; confirm it stays in the 3–7 s TTFR band the S227/S228 live tests showed.
- **22 — foreign-hook interference.** A real project-level `.claude/settings.json` Stop hook firing inside a meeting — confirm `_has_foreign_hook_feedback` surfaces the notice and the turn still completes.
- **23 — tear-down race.** `/operator:hangup` mid-turn — confirm the clean-exit path (`_wait_for_next_reply` checking `_stopping`) exits quietly with no crash-dump.
- **24 — resume from desktop-app session.** `/operator:slip` from the desktop app with `--resume-session ${CLAUDE_CODE_SESSION_ID}` — confirm the bridged session carries pre-meeting context.
- **25 — `--fresh` mode.** Confirm `--fresh` spawns with no `--resume` and `cwd=~/.operator/sessions/<id>/` (clean-room session).

Before the live pass: `git push origin main` (and `public`), then `uv tool install --reinstall .` so the live slip runs exercise the merged code.

## State of the repos

**operator (main):** the merge commit is in place locally, on top of `011a7b1` (doctor fd-2 fix) — both **unpushed**. `git push origin main` + `git push public main` when ready.

**operator (phase-14.22-pty-pivot branch):** fully merged into `main`. Safe to delete (`git branch -d phase-14.22-pty-pivot`) once `main` is pushed. The worktree at `.claude/worktrees/phase-14-22-pty-pivot/` can be removed too (`git worktree remove`).

**operator-plugin (main):** at `57c7f6a` (version 0.1.15), pushed to `origin`. No plugin change this session.

**Tracked-but-modified on operator main (do not commit):** `README.md` — user-owned billing-protection wording. Untracked: the same set of `debug/` and stale-doc artifacts as before (`debug/14_20_*`, `debug/14_21_*`, `debug/14_23_aec_spike/`, `debug/claude_idle_overnight/`, `debug/resume_spike/`, `docs/landing-page.md`, `hooks.md`, `mvp.md`, `mvp-copy.md`, `operator-architecture-handoff.md`, `public/`).

## Open follow-ups (carried)

- **Anthropic's classification past June 15.** Interactive PTY-driven claude — subscription usage (planned) or reclassified as programmatic (bad)? Untestable until June 15. Watch Claude Code release notes for new flags/env vars patterning against this. If reclassified, BYO-API-key (DECISION.md section M, ~10 lines) becomes the only option.
- **Claude reply-delivery feedback loop** (S224 carryover). When `send_chat` fails, the claude subprocess gets no signal — it confidently says "I replied" after every reply got dropped. Wiring failure back as a tool-result error would close the loop. Independent of 14.22; could land any time.
- **`--dangerously-skip-permissions` + foreign-hook write hazard.** A foreign Stop hook whose `reason` reads as a benign instruction → inner-claude acts on it (during S228 testing it edited the user's global `~/.claude/settings.json`, since reverted). Captured in agent-context Hard Won Knowledge — a project threat-model concern, not a blocker.
- **Post-MVP:** gate `operator slip` behind the plugin (deprecate terminal entry).
- **Python 3.14 + MLX shader-compile flakiness.** Mitigated via doctor pre-warm; root cause is upstream. Consider pinning `requires-python` to `>=3.10,<3.14` if it recurs.

## How to verify in a fresh session

```bash
cd /Users/jojo/Desktop/operator
source venv/bin/activate
for f in tests/test_*.py; do echo "=== $f ==="; python "$f" 2>&1 | tail -2; done
# All 11 test files should pass, including test_claude_cli_provider.py (21 tests).
operator doctor
```
