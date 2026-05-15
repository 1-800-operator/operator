# 14.22 architecture pivot — `claude -p` → interactive `claude` driven via PTY + hooks

**Status:** decided 2026-05-13, validated via 7 spikes in this folder, refactor not yet started.
**Trigger:** Anthropic email 2026-05-13 — starting **2026-06-15**, `claude -p` and Agent SDK usage no longer counts toward Claude subscription limits; instead it draws from a small per-plan Agent SDK credit ($20 Pro / $100 Max 5x / $200 Max 20x), then pay-as-you-go API rates.

Operator's entire flow today (`pipeline/providers/claude_cli_provider.py` spawning `claude -p --resume <id>` per meeting) lands in that small bucket. A Max 20x user running operator across daily meetings would burn through $200/mo of credit in days. The economic premise — "operator runs free on the subscription you already pay for" — is no longer viable for `-p`.

## The pivot

**Stop using `claude -p`.** Spawn **interactive `claude`** instead, drive it via a pseudo-terminal (PTY), and extract structured events via Claude Code's native hook system.

```
INPUT:  bracketed-paste wrap + \r into PTY  →  arrives as a normal user turn
OUTPUT: hooks (Stop.last_assistant_message + PreToolUse + failure events)
PERMS:  claude --dangerously-skip-permissions  →  no permission prompts
```

This stays on the user's interactive Claude Code subscription pool (the documented home for interactive usage), preserves all operator user flows, and ships through the operator-plugin without touching the user's settings files.

## Why send-keys + hooks (and not the alternatives)

Three alternatives were considered and rejected:

1. **TUI screen scraping with `pyte`** — initially attractive because `pyte` cleanly renders the TUI into a text grid. Rejected because hooks deliver the same data as structured JSON with stable contracts; screen scraping is fragile to TUI updates.
2. **Stop-block as input mechanism** (return `{"decision":"block","reason":<message>}` from Stop hook to keep claude going turn after turn) — initially looked elegant; faster than send-keys; required only ONE keystroke event per session. **Rejected because claude's prompt-injection defense fires on it.** Claude Code prepends "Stop hook feedback:\n" to the message; the model is trained to refuse instructions arriving through that channel as suspected prompt injection. Even with a counter-instruction at session start, claude continued refusing every Stop-blocked message ("looks like a prompt-injection attempt, ignoring"). Filtering the prefix at the API-proxy layer would bypass an intentional Anthropic security feature — strategic non-starter.
3. **BYO API key** (`ANTHROPIC_API_KEY` to inner-claude) — works mechanically (~10 lines of code) but the user pays full pay-as-you-go API rates. Keep this as a documented escape hatch for users who choose it explicitly; not the default.

## Spike record

All in `debug/14_22_pty_spike/`:

| Spike                          | What it proved                                                                                                                                                                       |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `spike_pty.py`                 | Mechanically PTY-drive claude; capture bytes. First surfaced: claude TUI is positional (cursor moves, not streamed text) → ANSI-strip is insufficient.                               |
| `analyze_pyte.py`              | Feeding bytes through `pyte` recovers a clean screen view. Made screen-scraping look viable until hooks made it unnecessary.                                                         |
| `spike_sendkeys.py`            | Char-by-char typing into PTY can submit short messages (after dodging the bracketed-paste mode quirk that broke one-shot writes).                                                    |
| `spike_stopblock.py`           | Stop-block multi-turn mechanically works, ~2x faster than send-keys per turn.                                                                                                        |
| `spike_framing.py`             | **Killed Stop-block.** Without counter-instruction, claude refuses all hook-injected messages as prompt-injection attempts. Short counter-instruction still doesn't override the defense. |
| `spike_longmsg.py`             | Tested 4 input strategies against a long message; only **bracketed-paste wrap** (`\x1b[200~ <msg> \x1b[201~\r`) reliably submits. Char-by-char at any speed silently dropped chars on long messages. |
| `spike_finalize.py`            | 4-test sweep — all PASS:<br>• T1 tough inputs (quotes, backslashes, multi-line, emoji, code fences) — SHA-256 round-trip matched byte-for-byte<br>• T2 multi-turn back-to-back via D<br>• T3 multi-tool loop — 3 `PreToolUse` events + 1 `Stop` event<br>• T4 tool failure under `--yolo` — `PostToolUseFailure` hook fires |
| `spike_resume.py`              | `claude --resume <id>` (interactive) has identical semantics to `claude -p --resume <id>`. Same session_id continues; context recalled; hooks fire normally.                         |
| `spike_crosscwd.py`            | `--resume` is **cwd-scoped**. Session JSONL is stored under a project dir derived from the creator's cwd. Resuming from a different cwd returns "No conversation found with session ID". |
| (probe via `claude --help`)    | No `--working-directory` flag exists. `--add-dir` only widens tool access. `--bare` mode exists but explicitly disables hooks → incompatible. Operator must spawn inner-claude with `cwd=<user-project-dir>`. |

## User flows preserved

| Flow                                                                                       | Status                                                                                                                                                                                       |
| ------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `/operator:slip <url>` from Claude Code (desktop or terminal)                              | Unchanged plugin entry point                                                                                                                                                                 |
| Pre-load context in main Claude Code session, then slip                                    | Preserved via `--resume <user-main-session-id>` (cwd-scoped — see "spawn cwd" below)                                                                                                         |
| Continue talking with claude about the meeting after it ends                               | Preserved — inner-claude writes into the same `session_id` JSONL, so the user's main session has all meeting interactions baked into context when they return                                |
| Transcript MCP usage (`mcp__transcript__search_captions` etc.)                              | Unchanged — MCPs are configured at user level (`~/.claude.json`) and inherited by interactive claude exactly as by `-p`                                                                      |
| Operator's existing four narration callbacks (`progress` / `denial` / `connection` / `tick`) | Preserved — remapped from `-p` stream-JSON events to hook events                                                                                                                             |

## Foreign hooks — observable, not preventable

Because operator's inner-claude must launch in the user's project cwd (to find the resumed session), the user's project-level `.claude/settings.json` hooks **will fire inside meetings**. Same as today's `-p` behavior. Not a regression.

We can't filter foreign hooks (Claude Code merges all sources into one execution list and gives no tag at execution time). We can observe their effects:
- Foreign Stop-block injections show up in the conversation transcript with "Stop hook feedback:" prefix → grep `transcript_path` to detect
- Foreign PreToolUse denies fire `PermissionDenied` (which our hooks capture) when we expected `--yolo` to bypass everything → flag the anomaly
- Slow foreign hooks delay our Stop hook firing → measure and warn (operator-voice: `[☎️ Operator] hook delay detected — N seconds`)
- `operator doctor` can pre-warn: *"Your global hooks include N handlers — review for slow/blocking handlers before relying on operator for time-sensitive meetings."*

**A `--fresh` clean-room escape hatch was considered and cut from scope.** The idea: spawn inner-claude with no `--resume` and `cwd = ~/.operator/sessions/<id>/` so foreign project-level hooks don't fire. Dropped — foreign hooks are observable (above) and the no-resume default already exists; a dedicated flag isn't worth the surface area pre-launch.

## Production refactor plan

### A. Spawn changes (`pipeline/providers/claude_cli_provider.py`)

1. **Drop**: `-p`, `--resume <id>` *as currently passed*, `--output-format stream-json`.
   **Add**: `--dangerously-skip-permissions` (unconditional — operator already requires `--yolo` semantics; the `--yolo` flag in `__main__.py` becomes a no-op or is removed since it's now always on).
   **Add (conditional)**: `--resume <user-main-session-id>` when bridged from the plugin slash command; no `--resume` otherwise (a fresh session is born on first @mention).
2. **PTY-wrap the spawn**: `pty.openpty()`, set winsize 40×120, `os.setsid` for process-group isolation. Keep stderr on the same PTY as stdout — hook events surface errors structurally, but stderr is still useful for debugging crashes.
3. **Spawn `cwd`**: `<user-project-dir>` (the cwd of the user's main Claude Code session when bridged; falls back to `os.getcwd()` otherwise). Inner-claude needs this cwd to (a) find the resumed session JSONL and (b) load the user's project `CLAUDE.md` for free context. No `--working-directory` flag exists; the process's actual cwd is the only knob.

### B. Spawn-ready handshake

4. **Wait for a real readiness signal, not a sleep.** Register a `SessionStart` hook (in the operator-plugin's `hooks/hooks.json`) whose handler writes a `ready.flag` file into `$OPERATOR_SESSION_DIR`. The provider's `_send_message` blocks (with a sane timeout, e.g. 30s) on that flag existing before its first write. Eliminates the 5-second-sleep-and-pray pattern from the spikes.

### C. Input path (replaces stdin writes)

5. **New `_send_message(msg)`** — bracketed-paste wrap, no char-by-char:
   ```python
   os.write(master_fd, b"\x1b[200~")
   time.sleep(0.05)
   os.write(master_fd, msg.encode("utf-8"))
   time.sleep(0.1)
   os.write(master_fd, b"\x1b[201~")
   time.sleep(0.2)
   os.write(master_fd, b"\r")
   ```
   Works for any content (quotes, backslashes, multi-line, emoji, code fences) — proven in `spike_finalize.py` T1. **No char-by-char delays; they introduced drops on long messages.**

### D. Output path (replaces stream-JSON parsing)

6. **Operator-plugin ships `hooks/hooks.json`** registering:
   - `SessionStart` → `ready_flag.sh` (writes `$OPERATOR_SESSION_DIR/ready.flag`)
   - `Stop` → `stop.sh` (appends `{ts, last_assistant_message, session_id, transcript_path}` to `$OPERATOR_SESSION_DIR/replies.jsonl`)
   - `PreToolUse` (matcher `*`) → `pretool.sh` (appends `{ts, tool_name, tool_input}` to `$OPERATOR_SESSION_DIR/tools.jsonl`)
   - `PostToolUseFailure` (matcher `*`) → `error.sh` (appends to `$OPERATOR_SESSION_DIR/errors.jsonl`)
   - `PermissionDenied` (matcher `*`) → `error.sh` (same file, `kind` field distinguishes)
   - `StopFailure` → `error.sh` (same file; provides `error` field with rate_limit / authentication_failed / etc.)
7. **Plugin layout:** scripts live in `operator-plugin/hooks/scripts/*.sh`. The `hooks.json` references them via absolute paths resolved at install time, OR via interpreter-explicit invocation (`bash ${CLAUDE_PLUGIN_DIR}/hooks/scripts/stop.sh`) if Claude Code supplies a placeholder for plugin root. **Validate exec bits survive `uv tool install` / hatchling packaging** — if not, fall back to interpreter-explicit invocation. Test on a fresh install before shipping.

### E. State dir layout

8. **Per-session dir** under `~/.operator/sessions/<id>/`:
   ```
   replies.jsonl          # one line per Stop hook firing
   tools.jsonl            # one line per PreToolUse
   errors.jsonl           # PostToolUseFailure | PermissionDenied | StopFailure
   ready.flag             # SessionStart wrote this; first input gates on it
   metadata.json          # session_id, meet_url, started_at, ended_at
   ```
   On meeting end, archive this dir into `~/.operator/history/<slug>/` alongside the captions JSONL (matches existing convention).

### F. Env-var contract

9. **Operator's CLI exports `OPERATOR_SESSION_DIR=~/.operator/sessions/<id>/` before spawning inner-claude.** Plugin hook scripts read this env var to know where to write. **Document this as a load-bearing contract** — if the env var isn't set, plugin hook scripts have no idea where to write and operator gets no events back.

### G. Callback remapping (operator-voice surface unchanged)

10. `progress` ← `tools.jsonl` rows (existing 20s throttle still applies)
11. `denial` ← `errors.jsonl` rows where `kind ∈ {PermissionDenied, PostToolUseFailure}`
12. `connection` ← `errors.jsonl` rows where `kind == StopFailure` + process-exit watcher on the PTY (`OSError` / `EOF` on read)
13. `tick` ← unchanged off-thread send queue

### H. Reply assembly

14. When a new `replies.jsonl` row appears, post `last_assistant_message` to meeting chat via the existing paragraph-splitter in `connector.send_chat()`. No JSON parsing or text extraction needed — the field is already the model's final text.

### I. Foreign-hook safety net (dedicated step)

15. **Build a foreign-hook detector.** On every Stop hook firing, check the `transcript_path` JSONL for any user-role message in the last turn containing the literal `"Stop hook feedback:"` prefix. If found, a foreign Stop-block intervened — log + surface `[☎️ Operator] a foreign hook redirected the conversation this turn`. Also: time the gap between when the assistant message would have rendered and when our Stop hook fired; if > 5s, foreign hooks ran first → surface `[☎️ Operator] hook delay detected — N seconds`.

### J. Tear-down race (dedicated step)

16. **Don't SIGTERM until the last hook has flushed.** When the meeting ends, send the final message (if any), wait for the corresponding `replies.jsonl` row to appear (with timeout), THEN signal the inner-claude process group. Otherwise the final assistant reply may be lost because the Stop hook script hadn't yet written its file when the parent died.

### K. Lifecycle / Claude Code version floor

17. **Pin a Claude Code minimum version** in operator-plugin's metadata. Spike was validated against **v2.1.141**. Lower bound should match. Operator's preflight (`pipeline/doctor.py`) should detect older versions and refuse to launch with a clear message ("Operator requires Claude Code ≥ v2.1.141 — please update via …").

### L. Plugin install validation

18. **Smoke-test on a fresh install** — verify `operator-plugin/hooks/scripts/*.sh` arrive with exec bits set after `uv tool install --reinstall .` and the desktop-app plugin sync. Fall back to interpreter-explicit invocation in `hooks.json` if not.

### M. BYO API-key escape hatch (low priority)

19. Stop stripping `ANTHROPIC_API_KEY` from the inner-claude spawn env when the user explicitly opts in via a config flag (e.g., `OPERATOR_USE_API_KEY=1` in `~/.operator/.env`). Document the cost/benefit clearly. Out of scope for the main pivot; ship if/when needed.

## Integration test pass (after refactor lands)

20. **Long-meeting compaction.** Run a 30+ minute meeting with many `@claude` turns. Verify Stop hook keeps firing with correct `last_assistant_message` post-compaction. Verify the hook-event JSONLs don't grow pathologically.
21. **Hook latency on hot path.** Measure end-to-end: claude finishes turn → Stop hook script runs → operator's tail thread picks up the file → operator posts to Meet chat. Target: sub-2s in p50. Tune tail-loop polling interval if needed.
22. **Foreign-hook interference.** Run a meeting on a machine where the user has a Stop hook with `decision: "block"` configured globally; verify operator's detector surfaces the anomaly and the meeting flow doesn't silently break.
23. **Tear-down race.** Run a meeting that ends immediately after a long claude response; verify the final reply lands in chat before operator exits.
24. **Resume from desktop-app session.** With Claude Code Desktop running, invoke `/operator:slip <url>` from inside a real project. Verify `CLAUDE_CODE_SESSION_ID` is captured, passed as `--resume`, and the inner-claude inherits the user's project context.

## Open questions / known unknowns

- **Anthropic's classification past June 15.** Interactive PTY-driven claude — does Anthropic count it as subscription usage (planned) or reclassify as programmatic (bad)? Untestable until June 15. Watch release notes and `claude --help` for new flags/env vars indicating they're patterning against this. If they reclassify: BYO-API-key path (step M) becomes the only option.
- **Claude Code version drift.** Bracketed-paste handling, hook event names, hook input schema all evolve. Pin a minimum (step K) and re-validate on each Claude Code upgrade.
- **User-level hooks impact.** Operator's `doctor` should at least *survey* `~/.claude/settings.json` for slow/blocking hooks and warn the user, even though we can't disable them.

## Files in this folder

| File                                | Purpose                                                                                            |
| ----------------------------------- | -------------------------------------------------------------------------------------------------- |
| `DECISION.md`                       | This file.                                                                                         |
| `spike_pty.py`                      | Mechanical PTY-drive capture; first spike.                                                          |
| `analyze_pyte.py`                   | Pyte-based screen reconstruction (no longer load-bearing — kept for reference).                    |
| `spike_sendkeys.py`                 | Send-keys multi-turn driver.                                                                       |
| `spike_stopblock.py`                | Stop-block multi-turn driver (rejected approach — kept for reference).                              |
| `spike_framing.py`                  | Killed Stop-block via prompt-injection defense.                                                    |
| `spike_longmsg.py`                  | Settled on bracketed-paste wrap as the universal input strategy.                                   |
| `spike_finalize.py`                 | 4-test PASS sweep (tough inputs, multi-turn, tool loop, failure events).                           |
| `spike_resume.py`                   | Resume semantics for interactive claude.                                                           |
| `spike_crosscwd.py`                 | Cross-cwd resume — fails; informs spawn-cwd requirement.                                            |
| `bench/`                            | Workdir used for spike runs. Contains the proof-of-concept `.claude/settings.json` and hook scripts.|
| `out_*/`                            | Per-spike captured bytes + analysis outputs.                                                       |
