# Session 219 handoff (2026-05-12) — Root-caused the /slip double-Bash + lockfile guard shipped

## What landed in S219

S219 closed the singleton-guard fragility that S218 had left flagged AND root-caused the desktop-app "operator slip is already running" error pattern that had been intermittent across S217/S218 live tests. Two complementary fixes shipped:

- **Operator `6b67e1f`** (origin + public) — replaces the pgrep cmdline-grep singleton guard with a `~/.operator/slip.pid` lockfile. O_CREAT|O_EXCL atomic acquire; holder-PID liveness via `kill(pid,0)` + identity check via `ps -o command=` argv match (handles PID reuse); stale-lock auto-reclaim; ownership-aware release (only the recorded PID deletes the file — so the daemonize parent's release is a no-op and only the daemon child reaps it on graceful shutdown); `_run_hangup` rewritten to read the lockfile instead of pgrep. Smoke-tested across three reclaim scenarios + verified end-to-end in a real meeting.
- **Plugin `caf1cfd` (0.1.10)** on `1-800-operator/operator-plugin` — rewrites SKILL.md to remove the "**Action — run this immediately.** Invoke the Bash tool with the command in the `!` block below…" prose directive. New SKILL.md leads with the `!` block and follows with prose framing the output as already-produced and explicitly forbidding a second invocation. Marketplace bumped to 0.1.10.

Plus a docs sweep at session end (this commit): roadmap status line + 14.22.9.12 row, agent-context Current Status converted to Prior + fresh S219 entry, two new Hard Won Knowledge entries (JSONL forensic methodology + `!`-block pre-execution mechanics), and this handoff file.

The root-cause discovery is captured in the second Hard Won Knowledge entry: the Claude Code desktop-app harness pre-executes ` ```! ``` ` blocks at SKILL-load time and **inlines the command's stdout into the SKILL.md text replacing the original command** before the model ever sees it. Prior SKILL.md prose also instructed the model to invoke Bash, causing a redundant second Bash call where the model reconstructed argv from context (dropping `claude`). Proven by JSONL transcript inspection at `~/.claude/projects/-Users-jojo/<session-id>.jsonl` — first lesson learned: **read the transcript before guessing.**

A separate hypothesis from earlier in the session — that Playwright page handles silently re-target during Meet's green-room → in-call transition — was DISPROVED by live-test instrumentation. No prophylactic fix was needed; DIAG instrumentation was added and then stripped before commit.

---

## Next session (S220): three carryover items

### Prereqs (verify, <2 min)

```bash
git rev-parse HEAD                                              # should be the docs-sweep commit
gh api repos/1-800-operator/operator/commits/main --jq .sha     # should match
claude plugin list | grep operator                              # may still show 0.1.9 — desktop app doesn't auto-update
ls ~/.claude/plugins/cache/1-800-operator/operator/             # should include 0.1.10/ after `claude plugin update operator@1-800-operator`
cat ~/.operator/slip.pid 2>&1                                   # should print "No such file" (clean state)
pgrep -lf "operator slip" || echo "clean"                       # should print clean
```

If the desktop app is still showing 0.1.9 in `claude plugin list`, run `claude plugin marketplace update 1-800-operator && claude plugin update operator@1-800-operator` and restart Claude Code. This is itself the plugin-auto-update friction item from S218 carryover — still unresolved.

### Item 1 — 14.22.9.8: ship the desktop-app allowlist into install.sh (~30 min)

The most-carried item across S217 → S218 → S219. Every new desktop-app user hits the silent-fail wall on first `/operator:slip` until we ship this. Spec unchanged: small Python one-liner inline in install.sh that merges `Bash(operator:*)` + `Bash(nohup operator:*)` into `~/.claude/settings.json` `permissions.allow`. Merge-in (preserve existing user entries), idempotent (skip if both already present), soft-skip if file missing or invalid JSON. Live-test on a clean machine if possible; otherwise restore current settings.json after a smoke test.

### Item 2 — Plugin auto-update friction (~1-2 hr depending on path)

Also a multi-session carryover. The desktop app doesn't notify users when a new plugin version exists; they sit on stale versions. Two options from the S218 framing:

- **Ship an `/operator:update` skill.** Bundles the two CLI commands (`claude plugin marketplace update 1-800-operator` + `claude plugin update operator@1-800-operator`) + a "restart Claude Code" note. ~30 min. Solves execution, not discovery.
- **Build version-stale detection into operator.** On every slip start, operator fetches the marketplace `marketplace.json` from GitHub, compares with `~/.claude/plugins/installed_plugins.json`, and if newer posts a one-time `[☎️ Operator] A new operator version (X.Y.Z) is available — run /operator:update to upgrade.` chat line. ~1-2 hr. Solves both.

Best paired: option 1 makes option 2's chat line actionable. **Recommend shipping both this session if time allows.**

### Item 3 — Optional: deeper live-test coverage

S218 handoff's "Item 4" deferred items still untested: audio + transcript MCP recall on real speech, prompt-cache hit on second @mention, terminal-direct path (`operator slip claude <url>` without env bridge). If S220 has time after Items 1 + 2, this is the cleanup pass.

---

## Open questions / blockers

- **None blocking.** S219 closed the model-improvisation feedback loop that S218 had narrowed; the remaining items are scope-clear and well-bounded.
- **The same-URL absorption I proposed mid-session as a "fix" for the double-Bash error is now correctly deferred** — the SKILL.md rewrite addresses the actual root cause. If the desktop-app model ever regresses (or a different surface duplicates the call for unrelated reasons), the absorption is a known-good belt-and-suspenders we can return to.
- **The harness-pre-executes-`!`-blocks behavior is uncertain in scope.** We proved it for `/operator:slip` specifically. Whether it applies to all SKILL types (skill-creator-built, command, etc.) or all desktop-app surfaces is not established. If we author future SKILL.md files with `!` blocks, follow the new author rule (don't double-instruct) until/unless we have more data.

---

## Gotchas / don't forget

- **Zero unpushed commits** at handoff time (after the docs-sweep commit). Operator: `6b67e1f` + docs-sweep. Plugin: `caf1cfd`. All pushed.
- **Three-step plugin publish flow still load-bearing.** Future plugin bumps: (1) bump operator-plugin's `plugin.json` + push; (2) bump operator's `marketplace.json` + push to both `origin` and `public`; (3) `cd ~/.claude/plugins/marketplaces/1-800-operator && git pull --ff-only origin main` to refresh the local cache. See `memory/project_plugin_publish_two_steps.md`.
- **README.md is dirty** in operator repo with pre-existing user work on billing-protection wording. NOT mine — left untouched. User to commit when ready.
- **DIAG instrumentation was added then stripped** within this session. The page-handle re-resolution hypothesis was disproved; no code change shipped from that hypothesis. The invocations.log diagnostic in `__main__.py` was useful forensically but not shipped — it was stripped before commit.
- **`/tmp/operator.log` was rotated to 5GB** at session start; rotated to `/tmp/operator.log.s218` and `/tmp/operator.log.preS219test3` before tests. Still no logrotate / size cap on the live log; worth a follow-up pass eventually.
- **Plugin auto-update friction is the second-most-aged carryover** behind 14.22.9.8 — flag for fresh action in S220.
- **The S219 architectural lesson:** when investigating a model's behavior, JSONL transcripts at `~/.claude/projects/<cwd-slug>/<session-id>.jsonl` are forensic ground truth — read them BEFORE forming hypotheses. The cost of speculation is rounds; the cost of reading the actual transcript is minutes.
