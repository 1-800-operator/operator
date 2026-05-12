# Session 217 handoff (2026-05-11) — Finish the live-test + tackle three unresolved items

## What landed in S217

The desktop-app **moat is now load-bearing.** Tonight's live-test discovered that the moat had been silently broken on the desktop-app surface — every meeting brain was starting from a fresh context despite the `--resume-session` machinery looking correct in code. Three intertwined root causes (wrong env var name, model rewriting the skill, the original tmp-scan tier-3 fallback fundamentally couldn't fire on desktop-app spawns), all addressed by **shipping a new tier 3 that reads `cliSessionId` directly from `~/Library/Application Support/Claude/claude-code-sessions/*/*/local_*.json`**. Plus deepened operator-side resilience: three-tier resume-session resolver, singleton guard (refuses duplicate spawns from model parallel-tool-use), always-relaunch slip Chrome (drops the connect_over_cdp-then-recover dance — Chrome 121+ refuses Browser.setDownloadBehavior on reattach, so reuse never worked anyway). Validated end-to-end: 4 desktop-app spawns this session all logged `source=state-scan` with cliSessionId matching the firing conversation.

Five shipped pieces, three operator commits + one plugin commit + one marketplace bump:
- **`8cb0357`** — three-tier resume-session resolver (`--resume-session` flag → `CLAUDE_CODE_SESSION_ID` env → tmp-scan fallback) + singleton guard + always-relaunch Chrome refactor
- **`0f9f84c`** (plugin repo) — plugin **0.1.5**: simplified skill body, drops `--resume-session ${CLAUDE_CODE_SESSION_ID}` since operator resolves the session itself
- **`00b9633`** — marketplace bump to 0.1.5
- **`927836e`** — replace failed tmp-scan tier 3 with state-file scan (the actual moat fix)
- (this commit) — docs sweep + memory entries

Two new memories saved: `project_plugin_publish_two_steps.md` (the 3-step plugin publish flow we learned the hard way), `project_desktop_app_session_state_files.md` (where the desktop app stores `cliSessionId` + why mtime not `lastActivityAt` is the right signal). Three new Hard Won Knowledge entries in agent-context.md.

---

## Next session (S218): three unresolved items + finish live-testing

The moat is mechanically and behaviorally verified, but three pieces of friction remain — none blocking, all carrying user-facing impact at launch.

### Prereqs (verify, <2 min)

```bash
git rev-parse HEAD                                              # should be 927836e or the docs-sweep commit on top
gh api repos/1-800-operator/operator/commits/main --jq .sha     # should match
claude plugin list | grep operator                              # should show operator@1-800-operator v0.1.5 enabled
ls ~/.claude/plugins/cache/1-800-operator/operator/             # should include 0.1.5/
pgrep -lf "operator slip"                                       # should be empty; if pid lingers, /operator:hangup it
```

### Item 1 — 14.22.9.8: ship the desktop-app allowlist into install.sh (~30 min)

The `Bash(operator:*)` + `Bash(nohup operator:*)` patterns in `~/.claude/settings.json` `permissions.allow` are still applied manually on this machine. Every new desktop-app user hits the silent-fail wall on first `/operator:slip` until we ship this into the installer. Spec is on the roadmap (row 14.22.9.8): small Python one-liner inline in install.sh, merge-in (preserve existing user allowlist entries), idempotent (skip if both patterns present), soft-skip if file missing or invalid JSON. Live-test on a clean machine if possible; otherwise restore current settings.json after a smoke test.

### Item 2 — URL-tolerance patch in operator (~10 min)

Two-line change in `__main__.py`'s slip dispatch. If the first arg after `slip` looks like an http URL, infer `bot=claude` since v1 has only one. Absorbs the model's most common improvisation (dropping the `claude` positional) — first attempt succeeds, no retry needed, no "Background task failed" lines in the desktop-app task UI. Singleton guard already in place; this turns the model's improvisation from "two failed background tasks before a successful retry" into "one clean success."

```python
# Sketch (in __main__.py slip dispatch, after `if first == "slip":`):
if len(argv) >= 2 and argv[1].startswith(("http://", "https://")):
    # Model dropped the bot positional. v1 has only `claude`. Infer it.
    argv.insert(1, "claude")
```

### Item 3 — plugin auto-update friction (~1-2 hr depending on path)

Today, the only way for a user to get a new plugin version is:

```bash
claude plugin marketplace update 1-800-operator
claude plugin update operator@1-800-operator   # note: marketplace-qualified name required
# then relaunch the desktop app
```

There is **no in-app notification when a new version exists** — confirmed by inspecting `known_marketplaces.json` (no auto-update flag), `~/.claude/settings.json` (no plugin-update setting), and `claude plugin marketplace add --help` (no `--auto-update`). Users sit on stale versions until they manually run those commands or someone tells them to. Real product friction.

Two real options:

- **Ship an `/operator:update` skill.** Bundles the two CLI commands + tells the user to restart. Cheapest possible mitigation; solves execution but not discovery. Maybe 30 min.
- **Build version-stale detection into operator.** On every slip start, operator fetches the marketplace `marketplace.json` from GitHub, compares with `~/.claude/plugins/installed_plugins.json`, and if there's a newer version posts a one-time `[☎️ Operator] A new operator version (X.Y.Z) is available — run /operator:update to upgrade.` chat line. Discoverability + execution. Recommend this; ~1-2 hours.

Both options pair best with shipping option 1 (the `/operator:update` skill) so option 2's chat line can suggest a concrete user action. If we ship both, the version-stale check can recommend the skill.

### Item 4 — finish the live-testing carried over from S216/S217

The moat is now verified. Three pieces of the original S216 live-test runbook are still untested behaviorally:

1. **Audio + transcript MCP recall on real speech.** Speak out loud for ~10s in a slip meeting, then `@claude what did I just say out loud?` — verify Whisper transcription quality + that the `search_captions` MCP tool returns relevant content.
2. **Prompt-cache hit on 2nd @mention.** After a first `@claude ...` round-trip, fire a second one in the same meeting. `grep "TIMING claude_cli_turn" /tmp/operator.log | tail -3` — second-and-later @mention should show `cache_read_input_tokens > 0`, proving `--resume <id>` hit the prompt cache.
3. **Terminal-direct path** (`operator slip claude <url>` with NO `--resume-session`). Confirm fresh-session-on-first-@mention (log line `resume=none`), then `--resume <newly-spawned-id>` on the 2nd. This validates the path-not-bridged-to-your-Claude-Code-session for users who just want a meeting bot.

Also worth a quick behavioral retest of `/operator:recap` from the desktop app since plugin 0.1.5 didn't touch that skill — should still work.

---

## Open questions / blockers

- **None blocking.** All three unresolved items are scope-clear and well-bounded.
- **The "model rewrites everything" pattern is now load-bearing.** Operator-side resilience (singleton guard, three-tier resolver, state-scan fallback, URL-tolerance once shipped) is the contract. Trying to "make the model behave" via skill prose has been unproductive — every iteration this session has confirmed the model treats SKILL.md as a hint, not a literal command.

---

## Gotchas / don't forget

- **Zero unpushed commits.** Operator: `8cb0357`, `00b9633`, `927836e`, plus this docs-sweep commit. All pushed to both `origin` and `public`. Plugin: `0f9f84c` (0.1.5). `git push origin main && git push public main` after every commit going forward.
- **Three-step plugin publish flow.** Any future plugin bump needs: (1) bump operator-plugin's `plugin.json` + push; (2) bump operator's `marketplace.json` + push to both `origin` and `public`; (3) `cd ~/.claude/plugins/marketplaces/1-800-operator && git pull --ff-only origin main` to refresh the local cache. The desktop app does NOT auto-pull on relaunch. See `memory/project_plugin_publish_two_steps.md`.
- **Operator pid may still be running.** Verify `pgrep -lf "operator slip"` at S218 start; clear with `operator hangup` if anything stale lingers.
- **Allowlist edit still manual on this machine** (`~/.claude/settings.json` has `Bash(operator:*)` + `Bash(nohup operator:*)` from S216). Until 14.22.9.8 ships, fresh-machine installs need this applied manually OR will hit the silent-fail wall.
- **Untracked working-tree items carried forward** (NOT touched in S217): `debug/14_20_audio_spike/{0, DECISION.md, STT_COMPARISON.md, USER_NOTE.md, decode_frames.py, spike_capture, spike_capture.swift}`, `debug/14_21_mic_capture_spike/spike_mic_via_sckit`, `debug/resume_spike/`, `docs/landing-page.md`, `mvp.md`, `public/`, `operator-architecture-handoff.md` at repo root. End-session work, not S218 blocking.
- **The moat has a sharp UX edge.** Validated end-to-end tonight: an `@claude hello?` in a meeting whose bridged Claude Code session had been doing operator-debugging replied with "Operator is already running in a session (pid X)…" — because the inner-claude carries the bridged session's context. For users in the wild, `/operator:slip` from a fresh conversation produces a clean meeting brain; from a context-loaded conversation produces a context-aware meeting brain (the moat); from an operator-debugging conversation produces a meta-operator-debugger. Worth a one-line user-facing doc: "For a clean, neutral meeting assistant, start `/operator:slip` from a fresh Claude Code conversation."
