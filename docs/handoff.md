# Session 205 handoff (2026-05-07) — endurance audit A3+C1 + Tier 3 doc reconciliation + two small cleanups

Four atomic-per-axis commits this session, all on `main` (branch is now 7 commits ahead of `origin/main`). `58adf88` reconciled `docs/pre-launch-audit.md` against the S203 parallel-session work that closed Tier 3 without ever propagating checkmarks into the audit doc itself — all 32 cells across Tier 1/2/3 are now marked closed in the matrix and a populated revisit/wontfix log captures five concrete deferred items. `3d5cc1d` landed the two endurance-audit fixes from the S204 catalog: A3 swapped `claude_cli._stderr_buf` to `deque(maxlen=500)`, and C1 added a `_browser_alive` threading.Event flag in `macos_adapter` (distinct from `_browser_closed`) populated by a per-tick `page.is_closed()` probe + a reduced-from-30s-to-10s active health probe, so `is_connected()` now reflects real page health within ~10s instead of waiting up to 30s. Same commit added a CHAINED TURNS section to `_PRE_TOOL_VOICE_RULE` in claude_cli + a "self-correcting MCP-hint loop" deferred entry to roadmap.md. C2/C3/C5 dispositioned: C2/C3 out of operator scope (mcp-remote and inner-claude own subprocess + token lifecycles); C5 has two findings that stay deferred (no helper-restart-on-death, O(N²) byte-string concat) but no v1 fix needed. Two small cleanups landed: `49ce78e` purged the now-dead `wrap_spoken`/`SAFETY_RULES`/`_neutralize_close`/`_ZWSP` chain in llm.py post-S204's caption-out-of-prompt change, and `e827677` deduped google_signin.py's hardcoded chrome_path via the canonical `chrome_preflight.CHROME_PATH`. 19/19 tests green throughout.

## Three uncommitted user-authored fixes in the working tree

User asked me at end-session to leave these for them to commit separately:

- **`attach_adapter.py`** — self-heal for CDP-reuse zombie Chrome. When a previous slip session detached cleanly and the user later closed the last slip window, on macOS Chrome stays alive in the menu bar with zero browser contexts. CDP socket is open, `_cdp_belongs_to_slip()` says yes, but Playwright's `connect_over_cdp` fails with "Browser context management is not supported." Fix tracks reuse-path with a local bool, evicts + relaunches + retries connect once on reuse-path failure only.
- **`chat_runner.py`** — auto-leave when participant count drops to 0 (bot was booted from meeting). Previously only fired on count==1 after `_saw_others=True`; new branch covers count==0 regardless. Also adds `else: self._alone_since = None` reset for the lobby-wait case.
- **`docs/agent-context.md`** — Hard-Won Knowledge entry at line 2232 documenting the zombie Chrome CDP issue. Preserved in this session's docs sweep.

## Exact next step (session 206)

User commits the three uncommitted live-session fixes above, then picks one of:

1. **Standalone workflow passes** — Pass 1 install dry-run on a fresh Mac (the audit doc calls it "the #1 launch-day failure mode"), Pass 7 dep pinning audit, Pass 8 runbook draft (capture the OAuth re-auth + Google session at-next-join expiry papercuts).
2. **install_preflight + readiness orphan product call** — decide whether to build `operator setup` + `operator auth` subcommands + `pipeline/auth.py` (wires the orphans into runtime), auto-invoke install preflight at top of dial/slip/login (silent when ready), document curl|sh as the only path + delete orphans + ~600 LOC of test coverage, or keep as scaffolding. The latent `from _1_800_operator.pipeline.auth import run_auth` ImportError landmine in `readiness.preflight_mcp_readiness` either vanishes (orphan deleted) or needs `pipeline/auth.py` written.
3. **openai + anthropic Python deps** — gated on the second-provider scope question: is a second LLM provider in v1 scope?

## Open questions / blockers

- **Apple Dev cert** still in flight from S198. Blocks 14.20.5 (productized .app + notarization) only.
- **`/ultrareview` + `/security-review`** remain deferred until slip captions ship (per `project_ultrareview_gated_on_slip_captions.md` memory). For interim security reads, suggest static tools (Bandit/Semgrep) or focused manual agent passes.
- **No tests for the S199 heartbeat watchdog.** Nice-to-have; runtime path is exercised in production but not unit-tested.

## Don't forget

- **C1 fix is detection-only.** Reconnect-and-resume after a transient network blip / Mac sleep is a much bigger feature (would need session-state rehydration, captions observer re-injection, chat panel re-open, participant-name cache rebuild). The new `_browser_alive` flag makes detection visible to ChatRunner within ~10s, but `is_connected() == False` still means the user has to leave + rejoin.
- **C5's helper-restart-on-death is the parallel deferred feature** for the audio-helper Swift binary. Same shape: detection works, recovery doesn't. Document together when the runbook lands.
- **The CHAINED TURNS prompt rule is load-bearing UX guidance** that lives in code (per `feedback_capability_in_code_over_prompt`) — `_PRE_TOOL_VOICE_RULE` in claude_cli.py:100. Don't migrate it to a user-editable system_prompt; a hand-edit could silently drop the chained-turns requirement.
- **Cross-thread Playwright restriction is permanent for macos_adapter.** If you ever need to add another live page probe in dial mode, it has to run on the browser thread (the `_browser_session` daemon) — `is_connected()` from outside that thread can only read flags, never touch Playwright objects.
- **Lesson saved to memory in S204**: when asked for "separate commits" on coupled changes, never revert+reapply manually — use `git add` per-file (clean) or `git add -p` for hunk-splits within a file (and acknowledge that's the kind of git acrobatics the lesson warns against). This session's two-commit decision (Tier 3 docs separate from endurance fixes) was clean per-file selection, no acrobatics.
