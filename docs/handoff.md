# Session 204 handoff (2026-05-06) — 1-hour endurance audit kickoff + closed deferred llm.py call

Started a new audit axis distinct from the pre-launch static-cleanup work: how the bot behaves over a 1-hour meeting (resource growth, time drift, stale handles, etc.). Produced a 20-row suspect catalog across five failure-mode buckets via grep-driven sweep. Diagnosed the three 🔴 candidates in detail. **One landed as commit `6b20483`:** the long-deferred `llm.py` product call (`docs/pre-launch-audit.md` line 161, carried since S201) is now resolved by a design refinement — chose option (b) "keep tail but chat-only" rather than the binary delete-vs-keep the audit doc framed. Captions are dropped from the prompt entirely (transcript MCP is the on-demand channel); chat stays in the prompt because people assume the bot saw what they typed. Also fixed the underlying full-file-read latency bug at the same time: `MeetingRecord` now has a `deque(maxlen=200)` chat tail populated by `append()`, served by new `tail_chat(n)` with no I/O; JSONL + `tail()` for generic queries unchanged. 19/19 tests green.

## Exact next step (session 205)

**Pick one of three directions:**

1. **Continue endurance audit** — start with A3 (`claude_cli._stderr_buf` is a daemon-thread `extend()` accumulator with no size cap; trivial deque swap, ~1 LOC), then read the remaining 🟡 items: C1 (CDP staleness — `macos_adapter.is_connected()` only checks an internal flag, doesn't probe real page health → bot zombies post-sleep/blip), C2 (MCP subprocess death detection over an hour), C3 (OAuth token mid-meeting expiry on hosted MCPs), C5 (audio helper hour-long behavior).
2. **Standalone workflow passes** — install dry-run on a fresh Mac (`docs/pre-launch-audit.md` Pass 1, the #1 launch-day failure mode), dep pinning audit (Pass 7), runbook draft (Pass 8).
3. **Close S203 phantom-feature product calls** — `install_preflight` + `readiness.preflight_mcp_readiness` orphans (~600 LOC of test coverage of unreachable code, plus a latent `from _1_800_operator.pipeline.auth import run_auth` ImportError landmine). Decision needed: add `operator setup` subcommand or delete the orphans.

## Open questions / blockers

- **Apple Dev cert** still in flight from S198. Blocks 14.20.5 (productized .app + notarization) only.
- **`openai` + `anthropic` Python deps** — paired with the resolved llm.py call in S203's narrative but actually independent. Still open: keep if a second provider is in v1 scope, drop otherwise.
- **Two phantom-feature product calls** carried from S203: `install_preflight.run_install_preflight` (no `operator setup` subcommand exists today) and `readiness.preflight_mcp_readiness` orphan with ImportError landmine. Same shape as prior wizard-era cleanups.
- **Heartbeat watchdog from S199 still has no tests.** Nice-to-have follow-up.

## Don't forget

- **A3 stderr buffer is a real candidate for memory growth in long meetings** but won't OOM a modern Mac in one hour. Cosmetic priority.
- **C1 (CDP staleness) is the biggest endurance UX risk** — without it, the bot enters a silent zombie state after Mac sleep / network blips and the user has to kill + restart. Fix is detection-only (probe `page.is_closed()` + `context.browser.is_connected()`); reconnect-and-resume is a much bigger feature.
- **`wrap_spoken` / `_neutralize_close` / `SAFETY_RULES` in `llm.py` are now dead in src/** post-`6b20483` (still tested in `test_wrap_spoken_sanitizes_speaker`). One-commit cleanup whenever convenient.
- **Two unpushed local commits ahead of `origin/main`** — one from a parallel session before this one (unclear provenance, didn't investigate), plus this session's `6b20483` and the docs sweep about to land at end-session. Push when ready.
- **Lesson saved to memory** (`feedback_no_git_acrobatics.md`): when asked for "separate commits" on coupled changes, never revert+reapply to disentangle — use `git add -p` or push back on the split request.
