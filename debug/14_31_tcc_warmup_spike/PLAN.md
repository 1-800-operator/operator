# 14.31 — TCC warmup mechanism spike

**Question:** which spawn mechanism for the install-time TCC warmup produces deterministic responsibility attribution to the helper bundle itself, rather than to the parent IDE/terminal that operator was launched from?

**Why this matters:** S243 surfaced that `CGPreflightScreenCaptureAccess()` resolves grants against the *responsible-process chain*, not the helper's own bundle identity. The slip-live path handles this correctly via `_disclaimed_spawn.spawn_disclaimed` (the child becomes its own responsible process). The install-time path uses `open -W -n -a` and we *think* Launch Services attributes to the launched bundle — but we never measured it directly.

If `_disclaimed_spawn` works for the warmup too, we get one unified mechanism for both contexts and eliminate the dual-mechanism complexity.

## Test mechanism

The private API `responsibility_get_pid_responsible_for_pid(pid_t)` in `libSystem` returns the PID macOS treats as responsible for a given PID. We:
1. Spawn a child via each mechanism
2. Call `responsibility_get_pid_responsible_for_pid(child_pid)`
3. Translate responsible PID → process command name via `ps`
4. Report

A responsible PID == child PID means the child is its own responsible process (good — TCC checks against child's own identity).
A responsible PID == operator/Cursor PID means TCC checks against the caller (bad — the parent's grant gates the helper).

## Mechanisms tested

| Label | How |
|-------|-----|
| **A** — plain Popen | `subprocess.Popen([helper])` from this Python (control: known-bad pattern) |
| **B** — disclaimed spawn | `_disclaimed_spawn.spawn_disclaimed([helper])` (slip-live's mechanism) |
| **C** — open -W -n -a | `subprocess.run(["open", "-W", "-n", "-a", helper_app])` (install-time mechanism) |
| **D** — open -g -n -a | Same as C but `-g` (background, don't bring to foreground) — variant worth ruling in/out |

## Expected outcomes

- A: responsible == this Python's responsible-process chain (likely Cursor)
- B: responsible == child PID itself (disclaim takes effect)
- C: responsible == ??? (the actual question; presumably launchd via Launch Services, but unverified on macOS 15)
- D: ??? (variant)

## Decision tree

- If C and B both produce child-self-attribution → use either; pick `_disclaimed_spawn` for parity with slip-live
- If C produces parent-attribution and B produces self-attribution → switch warmup to `_disclaimed_spawn`
- If both fail → deeper problem; investigate before any fix lands

## Files

- `responsible.py` — wrapper around the private `responsibility_get_pid_responsible_for_pid` API
- `spike.py` — orchestrator: runs all four mechanisms, prints a table
- `RESULTS.md` — fills in after running
- `DECISION.md` — picks the warmup mechanism based on results
