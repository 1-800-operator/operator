# 14.31 — Decision

## Original question

Should the install-time TCC warmup switch from `open -W -n -a` (current) to `_disclaimed_spawn` (slip-live's mechanism), to unify on one mechanism and rule out responsibility-chain attribution as a source of bugs?

## Answer

**Keep `open -W -n -a` for the install warmup. Both mechanisms work, but `open` is the better fit for the warmup context — there's no parity win that justifies switching, and the install path has a documented Apple-supported lifecycle.**

## Reasoning

The spike (RESULTS.md) confirmed both B (`_disclaimed_spawn`) and C (`open -W -n -a`) attribute correctly to the helper bundle. The naive parity argument ("one mechanism, less to remember") is real but weak because the two contexts genuinely differ:

| Context | Why this mechanism fits |
|---------|------------------------|
| **slip-live** uses `_disclaimed_spawn` | Need to plumb stdin/stdout pipes to the helper for the lifetime of the meeting. `open` returns immediately and can't expose those pipes to the parent. |
| **install warmup** uses `open -W -n -a` | One-shot foreground launch where we just want the helper to fire its TCC dialogs and exit. `open -W` blocks until exit cleanly; `_disclaimed_spawn` would require us to manually `wait()` + handle the helper's 10s watchdog timeout. |

Switching the warmup to `_disclaimed_spawn` would add code (PID tracking, wait loop, timeout handling) to replicate what `open -W` provides for free, with no behavioral improvement.

## What the spike DID resolve

- **Fix C (parent-IDE detection at slip-cold-start) — UNNEEDED.** The production warmup path always attributes correctly. No user-facing failure mode exists from the warmup mechanism itself. The S243 friction was 100% from manual debugging through Bash (mechanism A pattern), not from any production code path.

- **Fix D (document dual-mechanism design) — STILL VALUABLE.** Both production paths are correct, but the *reason* they differ (and why neither one is wrong) is non-obvious. A short docstring near `_preflight_audio_helper_tcc` and `spawn_disclaimed` explaining the two contexts saves a future agent from re-running this spike.

- **Mechanism A (plain Popen) is the smoking gun for any "helper says denied but Settings shows granted" report.** Generalizable rule for future debugging: when a user reports that symptom, ASK "how did you invoke the helper?" before reaching for `tccutil`.

## Revised fix order

| # | Fix | Status |
|---|-----|--------|
| A | `lsregister -u` cleanup in install.sh + `_preflight_audio_helper_tcc` | **DO** — addresses real LS-duplicate issue we observed |
| B | Distinguish `denied` vs `not_determined` in preflight + Settings deep-link | **DO** — actual user-facing UX gap when user toggles off |
| C | Parent-IDE attribution detection | **SKIP** — spike confirmed not needed |
| D | Document dual-mechanism design | **DO** — cheap, prevents future spike-replay |

Net: drop 1 of 4 planned fixes.

## Outstanding small uncertainty

The spike didn't verify whether the macOS TCC dialog *actually surfaces interactively* when `_disclaimed_spawn` is used for a warmup. Slip-live's use of `_disclaimed_spawn` runs after TCC is already granted, so no dialog ever fires there. Since we're keeping `open -W` for the warmup (which we already know surfaces dialogs correctly per S243's empirical run), this uncertainty is academic.
