# 14.24 — PermissionRequest PTY-mode spike ("yolo off" feasibility)

## The question

Operator spawns inner-claude with `--dangerously-skip-permissions` because
a normal spawn hits interactive approval dialogs in a PTY TUI nobody is
watching — the meeting hangs. We want a **"yolo off"** mode.

Proposed mechanism: spawn *without* the bypass flag, and have operator-plugin
ship a **`PermissionRequest`** hook that bridges each approval question into
meeting chat — operator posts "Claude wants to run X, reply yes/no", watches
chat for the answer, and feeds it back to the hook as an allow/deny decision.

`PermissionRequest` is the right event because it fires *only* for the "ask
bucket" — tools that are neither pre-allowed nor pre-denied — i.e. exactly
the uncategorised new-MCP-tool case the mode exists for. Pre-allowed and
pre-denied tools resolve natively and never reach the hook.

**The one unverified thing:** does `PermissionRequest` actually fire in
*interactive PTY* mode (not headless `claude -p`), and can a *blocking* hook
resolve the dialog without the TUI hanging? `defer` is documented as
headless-only — we need to confirm `PermissionRequest` is not similarly
restricted before building anything.

## Run it

```bash
cd /Users/jojo/Desktop/operator
source venv/bin/activate
python debug/14_24_permreq_spike/spike_permreq.py
```

Needs `claude` on PATH and logged in. Each test spawns a fresh
`claude --permission-mode default` (permission layer ON — the opposite of
operator's current spawn), cwd `bench/`, with hooks registered via a
runtime-generated `bench/.claude/settings.json`.

## Tests

| Test | Mode | Proves |
|---|---|---|
| **T1** | `allow` | `PermissionRequest` fires in PTY mode; an immediate allow lets the tool run and the turn end. |
| **T2** | `block_allow` | The real operator round-trip: the hook writes a request, **blocks** ~3s while the driver simulates a human chat reply, then returns allow. Proves a synchronous blocking hook resolves the dialog without hanging the TUI. |
| **T3** | `deny_exit2` | Fail-safe: hook exits 2 → tool blocked, turn still completes. |
| **T4** | `deny_json` | Structured deny (`behavior:"deny"` + message) → tool blocked, reason reaches claude. |
| **T5** | `allow` + bench `allow:["Bash"]` | `PermissionRequest` does **not** fire for a pre-allowed tool — the narrow-firing property the design depends on. |

Headline verdict hinges on **T1 + T2**. A `FAIL-CRITICAL` (the hook never
fires *and* the turn hangs) kills the proposed design.

## Reading the result

- **PASS** — T1 and T2 both pass: the mechanism is viable, proceed to the
  full design (hook script + operator-side chat round-trip + `--guarded`
  toggle).
- **FAIL-CRITICAL** — `PermissionRequest` does not fire in PTY mode; the
  dialog is stuck in the TUI. The yolo-off approach needs a rethink.
- **INCONCLUSIVE** — most likely the probe command was pre-allowed (or
  pre-denied) by the *user's own* `~/.claude/settings.json`, so
  `PermissionRequest` correctly didn't fire. The driver dumps your global
  allow/deny at startup so this is self-explaining; pick a more exotic
  probe command (`probe_prompt()` in the driver) and re-run.

Full structured output lands in `out_permreq_results.json`.

## Files

- `spike_permreq.py` — the driver.
- `bench/hook_permreq.sh` — the `PermissionRequest` hook under test;
  behaviour selected by `$PERMREQ_MODE`. Stands in for the real
  operator-plugin hook that would bridge to meeting chat.
- `bench/hook_stop.sh` — Stop hook → `state/replies.jsonl` (turn-done signal).
- `bench/hook_pretool.sh` — PreToolUse hook → `state/tool_events.jsonl`
  (permission-independent "tool was attempted" signal).
- `bench/.claude/settings.json` — generated at runtime by the driver.
- `bench/state/` — per-run JSONL artifacts.
