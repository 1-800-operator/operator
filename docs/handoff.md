# Session 221 handoff (2026-05-12) — Slip latency cliff fix + desktop-app error visibility

## What landed in this S221 arc

One operator commit `38700a0` on `main`, pushed to both `origin` and `public`. Eight discrete fixes bundled together because they all chase the same user-facing failure mode ("the bot missed my first message after I admitted it"). The headline measurement: **`TIMING listening_ready ms_since_meeting_entry` dropped from 7764ms to ~395ms** across five live runs at session end. First @-mention after admission no longer silently dropped.

The earlier S221 commit `1d2e61f` (PyPI publish pipeline groundwork — GitHub Actions workflow only, no runtime changes) is already on `main` and pushed; this S221-continued arc is independent and additive.

The fixes inside `38700a0`:

1. **Inner-claude pre_warm moved upstream** — fires from `_run_slip` immediately after `build_provider()`, before `connector.join()`. The warm subprocess gets the full ~30s join sequence to complete Node boot + MCP attach + `--resume` JSONL parse silently. `cli_init` on a warm turn is now consistently 11-21ms.
2. **All pre-daemonize CLI errors flipped to `stdout/exit-0`** — the Claude Code desktop-app harness wraps any non-zero-exit `!`-block in a DO-NOT-RESPOND caveat, so the natural CLI shape (stderr + exit 2) was invisible to users. SKILL.md "Error" branch matches by output shape, not exit code.
3. **Singleton-guard message names the active meeting URL** — read from `~/.operator/.current_meeting` via new `_read_current_meeting_url()` helper.
4. **Slip Chrome closes on `leave()`** — `_evict_other_chrome_on_cdp_port()` called from `AttachAdapter.leave()` after Playwright teardown. Prevents leftover slip Chrome from poisoning the next slip's pre-join state (one S221 21:41 run paid 78s of lobby wait because of it).
5. **Whisper model warms async on a daemon thread from `join()` start** — populates `self._audio_processors` in the background. `_start_audio_pipeline` joins the warm thread with timeout 30s; sync warm remains as fallback.
6. **Chat panel open + observer install moved upstream of audio in `_browser_session`** — runs immediately after `_wait_for_meeting_entry()` returns true. Closes the seed-loop drop window from 7.76s to ~300ms.
7. **`js.signal_success()` fires the moment the observer is watching** — not after audio. ChatRunner unblocks immediately.
8. **`_start_audio_pipeline` spawns on a daemon thread** — browser thread immediately enters `_process_chat_queue` instead of blocking on whisper-warm join. Without this, the chat queue piled up even though ChatRunner had unblocked (S221 22:05 run showed `poll_lag_ms=6573` on turn 1).

**New TIMING instrumentation:** `TIMING listening_ready ms_since_meeting_entry=… ms_since_slip_start=…` at observer-install. **This line is the regression canary** — if `ms_since_meeting_entry` creeps over 1000ms, something has snuck into the critical path between meeting-entry-detected and observer-install.

**Two new project memories saved** (indexed in MEMORY.md):
- `project_desktop_app_silences_nonzero_exit.md` — `!`-block + DO-NOT-RESPOND caveat finding.
- `project_chat_observer_seed_loop_drops_pre_install_messages.md` — keep `ms_since_meeting_entry < 1s`.

**Three new Hard Won Knowledge entries** appended to `docs/agent-context.md`.

---

## Next session (S222): three carryover items

### Prereqs (verify, <2 min)

```bash
git rev-parse HEAD                                              # docs sweep commit
gh api repos/1-800-operator/operator/commits/main --jq .sha     # should match
gh api repos/dufis1/operator/commits/main --jq .sha             # should match
grep '"version"' ~/.claude/plugins/marketplaces/1-800-operator/.claude-plugin/marketplace.json  # 0.1.11 still
```

### Item 1 — Read the overnight idle-timeout probe results

S220 carryover. The user said at sign-off they'd run it overnight. First thing to check:

```bash
tail -50 /tmp/claude_idle_overnight/probe_*.log
```

What we'd love to see: `verification result=ok` after multiple hours idle, confirming the warm-at-join path survives even very long lobby waits. If `DEATH: claude exited at elapsed=Xs` shows up, document the timeout — pre-warm already handles this gracefully via `proc.poll()` fallback, but knowing the actual threshold helps reason about how often warm slots get refreshed.

### Item 2 — Live-test of the `/operator:update` chat hint

S220 carryover, not exercised in S221. The `pipeline/update_check.py` logic is unit-verified (compare returns hint string correctly; end-to-end against live remote returns None when same version). The unfunded slice is the daemon thread `_post_update_hint_if_newer` at `chat_runner.py:326` actually firing during a real meeting join with a stale local marketplace cache. Low priority; the chat-post path is the same `send_chat_raw` used elsewhere.

To force-test it: temporarily roll back `~/.claude/plugins/marketplaces/1-800-operator/.claude-plugin/marketplace.json` to 0.1.10 (one-number edit, no commit needed), restart Claude Code, `/operator:slip <meet-url>` — should see `[🤖 Claude] A newer operator version (0.1.11) is available — type /operator:update in Claude Code to upgrade.` in chat. Then `git checkout` the marketplace.json to restore.

### Item 3 — Optional: PyPI publish dashboard wiring (only if launching)

S221 PyPI groundwork shipped the workflow but the OIDC trusted publisher needs one-time dashboard config before first use: PyPI Manage → Publishing → add pending publisher: owner `1-800-operator`, repo `operator`, workflow `publish.yml`, environment `pypi`. ~2 min. Only do this if S222 is the launch session.

---

## Open questions / blockers

- **None blocking.** Both S221 commits pushed to origin + public. All unit tests green. Live-tested across five real meeting sessions.
- **README.md still dirty** in operator repo with the user's billing-protection wording. NOT mine, left untouched (per S219/S220 carryover convention). User to commit on their own timeline.
- **`debug/claude_idle_overnight/`** is untracked — script for the overnight probe. Transient diagnostic, intentionally not committed.

---

## Gotchas / don't forget

- **The new `TIMING listening_ready` line is the regression canary.** Four invariants to preserve: (a) `_install_chat_observer` runs on the browser thread immediately after `_wait_for_meeting_entry()` returns true; (b) `js.signal_success()` fires right after observer install; (c) `_start_audio_pipeline` runs on a daemon thread, not inline on browser thread; (d) whisper warm is async on its own daemon thread from `join()` start.
- **Don't try to make the observer's seed-loop "smart."** Timestamp-aware filtering won't work because Meet's DOM timestamps are minute-resolution. The right fix is collapsing the install window, which we already did.
- **Desktop-app `!`-block exit code convention is inverted.** Any new operator CLI subcommand that should surface user-facing output through the desktop app must `print(msg)` (stdout, no `file=` arg) and `return 0` — even for "this is an error" cases. The SKILL.md branches by output shape. Post-daemonize errors can keep `return 2` because the child's stdio is `/dev/null` anyway.
- **The plugin publish 3-step flow still load-bearing** for any future plugin version: bump `operator-plugin/plugin.json` → bump `operator/.claude-plugin/marketplace.json` → `cd ~/.claude/plugins/marketplaces/1-800-operator && git pull --ff-only origin main`. S221 didn't ship a new plugin version (just operator-side changes).
- **PyPI version pinning** — when launching via `1d2e61f`'s workflow, pick a clean version (likely `0.1.0` or `1.0.0`) and update `pyproject.toml` `version` + tag `v<version>`; don't carry forward the dev iteration version. Already noted in `docs/roadmap.md` Packaging & Community section.
