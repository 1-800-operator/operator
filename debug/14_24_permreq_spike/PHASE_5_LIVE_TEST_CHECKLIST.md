# Phase 5 — yolo-off live test checklist

**Goal:** validate `/operator:slip-guarded` end-to-end against a real Google Meet before declaring the work shippable.

**Required:** real Meet with at least one human participant (you) who can type yes/no replies in chat. Approx. 10–15 minutes including setup.

The work this validates lives in two repos: `operator/` (CLI + classifier + chat round-trip plumbing) and `operator-plugin/` (PermissionRequest hook + new slash command). Prior validation: 14_24 / 14_25 / 14_26 spikes (mechanism), Phase 0–4 unit tests (15 files, all green).

---

## Prerequisites

- [ ] **operator CLI reinstalled from working tree:**
  `cd /Users/jojo/Desktop/operator && uv tool install --reinstall .`
  (Per `project_uv_tool_reinstall` — the CLI runs from the uv-tool install, not the working tree.)
- [ ] **operator-plugin changes are visible to Claude Code.** Two paths:
  - **(Recommended)** Bump `operator-plugin/plugin.json` to `0.1.17`, bump `operator/marketplace.json` to match, push both, `git pull` the local marketplace cache, then `claude plugin install operator@1-800-operator --reinstall`.
  - **(Quick-and-dirty for spot-testing)** Copy the live working-tree files (`operator-plugin/hooks/scripts/*.sh`, `operator-plugin/hooks/hooks.json`, `operator-plugin/skills/slip-guarded/SKILL.md`) into the installed plugin's location under `~/.claude/plugins/` directly. Restart Claude Code to pick up the new SKILL.
- [ ] `operator doctor` reports green.
- [ ] `claude auth status --json` confirms subscription auth is live.
- [ ] You have a fresh test Meet URL handy.

---

## A. Smoke (does it boot?)

- [ ] In Claude Code, run `/operator:slip-guarded <meet-url>` (or terminal: `operator slip-guarded claude <meet-url>`).
- [ ] Synchronous response is the expected "operator: joining …" line — no errors.
- [ ] `tail -f /tmp/operator.log | grep -i "guarded\|classifier"` shows:
  - [ ] `slip: guarded mode — classifier sidecar spawning in parallel`
  - [ ] `PermissionClassifier spawning sidecar claude` (within ~1s of the slip dispatch)
  - [ ] `PermissionClassifier: sidecar ready` (within ~10s)
- [ ] Slip Chrome window opens, joins the meeting, opens the chat panel.
- [ ] `operator status` reports `in meeting <url>`.

## B. Allow happy path

In meeting chat, post: `@claude please run a simple Bash command — list files in /tmp.`

- [ ] The bot self-narrates intent in chat ("let me list /tmp now" or similar).
- [ ] Within ~5s, the bot posts a permission question that includes the tool name (`Bash`) and the truncated command. Wording is "reply *yes* or *no*." (no "yes always" wording).
- [ ] Reply `yes` (or `sure`, `okay`, `do it` — any phrasing).
- [ ] Within ~3-5s, the bot posts the result of the command.
- [ ] `tail /tmp/operator.log` shows: `permreq <id> got chat reply from sender=…` → `TIMING classifier_turn=… verdict=allow` → answer file written.
- [ ] No spurious "Permission denied" or hook-error notices in the Claude Code transcript.

## C. Deny happy path

In meeting chat, post: `@claude please run another Bash command — touch /tmp/spike-canary.`

- [ ] Bot posts a fresh permission question.
- [ ] Reply `no` (or `nah`, `skip it`).
- [ ] Within ~3-5s, the bot acknowledges the denial in chat (something like "got it, won't run that").
- [ ] `/tmp/spike-canary` does NOT exist on disk (deny was honored).
- [ ] `tail /tmp/operator.log` shows: `TIMING classifier_turn=… verdict=deny`.

## D. UX checks

- [ ] Latency from your reply → bot's next chat post is roughly **2-5s**, not 10s+.
- [ ] Question wording is clear without scrolling — tool name + a snippet of the input fit on one chat line.
- [ ] If the bot's tool input is long (e.g. a Write with a big body), the question shows it head-only with `...` (no 2KB JSON dump in chat).
- [ ] `[🤖 Claude]` reply prefix appears on every bot post (consistent with yolo-on).

## E. Pre-allowed tool path (no question fires)

If your `~/.claude/settings.json` has `permissions.allow: ["Bash(echo:*)"]` or similar:

- [ ] In chat, post: `@claude run \`echo hi\``.
- [ ] No permission question — bot runs the command directly and posts the result.
- [ ] `tail /tmp/operator.log` shows the tool ran but no permreq fired (`grep "permreq" /tmp/operator.log` shows nothing for this turn).

## F. Pre-denied tool path (no question fires)

If you can add a temporary `permissions.deny: ["Bash(rm:*)"]` to your settings.json:

- [ ] In chat, post: `@claude run \`rm /tmp/spike-canary\``.
- [ ] No permission question — Claude is told the call was denied natively, the bot narrates the refusal.
- [ ] `tail /tmp/operator.log` shows no permreq for this turn.

(Remove the temporary deny rule after testing.)

## G. Edge cases

- [ ] **Ambiguous reply.** Trigger a permission ask, reply `what would that do?`. Expected: classifier defaults to NO (the "if unsure, NO" instruction). Bot acknowledges deny.
- [ ] **Non-English approval.** Trigger an ask, reply `sí, adelante` (or another language you read). Expected: classified as YES, tool runs.
- [ ] **Emoji reply.** Trigger an ask, reply `👍`. Expected: classified as YES, tool runs.
- [ ] **No reply (round-trip timeout).** Trigger an ask, do NOT answer. After ~120s, the hook self-denies. Bot narrates that the request timed out / wasn't answered. Meeting recovers — you can mention `@claude` again.
- [ ] **Multi-tool turn (if you can elicit one).** Ask `@claude do A and then B` where each requires a tool. Verify each ask is posted serially (one at a time, not all at once), each gets its own answer, both tools resolve.

## H. Failure paths

- [ ] **Hangup mid-permreq.** Trigger an ask, do NOT answer, instead run `/operator:hangup`. Verify: bot leaves cleanly within a few seconds. No lingering `claude` processes (`pgrep -fl claude` is clean for the meeting + classifier sidecars). No crash dumps in operator.log.
- [ ] **Classifier crash recovery (best-effort).** This is harder to induce intentionally. If you have time: while a permreq is pending, kill the classifier subprocess (`pkill -f "claude.*classifier"`-equivalent — find the right pid via the operator.log `pid=` line). The next ask should attempt a respawn; if respawn fails, the answer should default to deny (operator hook contract — safe default).

## I. Verify the docs match reality

- [ ] `docs/security.md` "Yolo-off mode" section accurately describes what you just observed. Flag anything wrong.
- [ ] `README.md` "Permissions & safety" "Alternative: yolo off" section matches the actual UX. Flag anything wrong.
- [ ] `/operator:slip-guarded`'s post-spawn message in Claude Code reads sensibly.

---

## Acceptance criteria

**Pass:**
- A, B, C all pass cleanly.
- Latency per ask in the 2-5s window.
- Ambiguous and non-English/emoji replies behave per the spike data (NO and YES respectively).
- No crashes or hangs.

**Defer:** any single non-blocking polish item gets logged as a follow-up — not a launch blocker if A/B/C/E/F all pass.

**Fail:** any of the following blocks ship:
- Approvals routinely misclassified as deny (the v2 prompt-injection failure mode resurfacing — would mean the classifier prompt isn't doing what the spike showed).
- Refusals classified as allow (UNSAFE — the "skews safe" property must hold).
- Hangs at any step (the hook contract should make hangs impossible by construction; if one happens, find the path).
- Plugin not visible in Claude Code (means the install path didn't pick up new files).

If a fail happens: capture `tail -200 /tmp/operator.log`, the chat transcript, and the failing step number, and send it back. The fix is likely in `pipeline/classifier.py` or `pipeline/chat_runner.py` `_check_permreq_chat_for_answer`.

---

## After the test

- [ ] If pass: bump `operator-plugin/plugin.json` to `0.1.17` (if not already done in prerequisites), push both repos, mark Phase 5 complete in your task tracker.
- [ ] Update `docs/security.md` and `README.md` if anything in the live UX surprised you and the docs read wrong against reality.
- [ ] Consider what to add to `CLAUDE.md` about the two-mode surface (the architecture-level note that's currently missing — the file mentions "no permission layer" as if it's still unconditional).
