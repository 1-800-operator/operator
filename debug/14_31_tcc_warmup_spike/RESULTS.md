# 14.31 — Results

## Raw measurements

| Mechanism | Helper PID | Responsible PID | Responsible comm | Self-attributed? |
|-----------|-----------:|----------------:|------------------|:----------------:|
| **A** plain Popen | 5483 | 3674 | `Cursor.app` | ✗ |
| **B** `_disclaimed_spawn` | 5488 | 5488 | `Operator.app` | ✓ |
| **C** `open -W -n -a` | 5496 | 5496 | `Operator.app` | ✓ |
| **D** `open -g -n -a` | 5505 | 5505 | `Operator.app` | ✓ |

Baseline: this Python (PID 5481) has responsible PID 3674 = Cursor.app. So `Cursor` is the upstream responsible process for the entire test harness, and any spawn that doesn't either (a) explicitly disclaim or (b) re-launch via Launch Services inherits Cursor as the responsible process.

## What this tells us

1. **`open -W -n -a` IS deterministic and correct on macOS 15.** The Cursor permission prompt the user saw during S243 was NOT triggered by `open` — it was triggered by my direct-Bash debug invocations (mechanism A pattern). The production install-time warmup path was always doing the right thing; we just couldn't see the truth through the noise of parallel manual testing.

2. **`_disclaimed_spawn` works identically.** Disclaim attribute correctly makes the child its own responsible process. This is what slip-live uses; it would work for the install warmup too.

3. **`-W` vs `-g` doesn't change attribution.** Foreground vs background launch flag is orthogonal to the responsibility chain.

4. **Plain Popen is the failure mode.** Any code path that spawns the helper without either disclaim or `open` inherits the IDE/terminal's responsibility — and TCC grants applied to "Operator" silently won't apply.

## Confidence

High. The private API (`responsibility_get_pid_responsible_for_pid`) is what Apple's own Activity Monitor uses; the answers are structural, not subject to TCC cache or grant state. Test is repeatable.

## What this does NOT tell us

- Whether the macOS TCC permission dialog actually surfaces interactively when using `_disclaimed_spawn` for a warmup. (Slip-live uses _disclaimed_spawn after permissions are already granted, so this code path is unexercised.) Worth a 1-minute follow-up test before committing to mechanism B for the warmup.
- Whether macOS 14 or earlier behaves identically. Tested on macOS 15.7.4 (Sequoia) only.
