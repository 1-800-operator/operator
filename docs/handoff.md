# Session 215 handoff (2026-05-11) — Phase 14.22.8.5 shipped, ready for the user-driven Phase 14.22.9 live-test

## What landed in S215

Self-test sweep + Phase 14.22.8.5 (collapse install path) shipped. New `tests/try_terminal.py` harness exercises the full chat→LLM→reply pipeline without a real Meet — trigger gating, multi-turn memory, streaming, transcript MCP recall, tool-use turns all green. Three deployment-chain problems found and fixed: (a) the canonical public repo `1-800-operator/operator` had been at an orphan commit `145a6ca` because earlier pushes went only to the `dufis1/operator` personal mirror (force-pushed local lineage to public, now in sync); (b) the plugin repo `1-800-operator/operator-plugin` flipped from PRIVATE → PUBLIC (was scheduled for 14.22.10, moved up because the install path needs the unauthenticated clone); (c) `dufis1/operator` retired to PRIVATE per user request (stale public mirror). `install.sh` patched with step 7.5 — `claude plugin marketplace add 1-800-operator/operator` + `claude plugin install operator@1-800-operator`, both non-interactive. **User-facing install is now one `curl | sh` — no manual `/plugin install` step.** Three commits on `main`, all pushed to both `public` and `origin`: `8756d18`, `8975283`, `bd4ecce`.

---

## Next session: Phase 14.22.9 live-test runbook

The goal: validate the parts of the system that genuinely require a real Meet, real Chrome, real audio, and a real interactive Claude Code session. **Do NOT re-test what's already green from S215** (see the "skip" list below).

### Prereqs (one-time, ~5 min)

1. **Verify both repos are at the expected state.**
   ```
   gh repo view 1-800-operator/operator-plugin --json visibility
   # → "PUBLIC"
   gh repo view dufis1/operator --json visibility
   # → "PRIVATE"
   gh api repos/1-800-operator/operator/commits/main --jq .sha
   # → bd4ecce... (or whatever HEAD is when you start)
   ```

2. **Pick a test machine.** Two options:
   - **Recommended: a "fresh-ish" state on this Mac.** Operator is currently installed AND the plugin is installed at user-scope (left there from the S215 smoke-test — that IS the post-install state, so it's fine to leave). To get a closer-to-fresh validation: `rm -rf ~/.operator/slip_profile/` to clear the slip Chrome profile, and optionally `claude plugin uninstall operator && claude plugin marketplace remove 1-800-operator` to test the install.sh path from scratch.
   - **Strictest: a clean Mac.** Run the curl install on a Mac that's never had operator. Best test of the install path but requires another machine.

### Step 1 — Install path (validates 14.22.8.5)

If testing from a clean state:
```bash
curl -fsSL https://raw.githubusercontent.com/1-800-operator/operator/main/install.sh | bash
```
(Note: the `1-800-operator.com/install.sh` URL the plugin README mentions is not actually serving yet — use the GitHub raw URL.)

After install.sh completes you should see:
- `Installed signed helper: …/operator-audio-capture.app` (audio helper)
- `Registered transcript MCP (user-scope).`
- `Installed operator plugin (user-scope). Slash commands /operator:* are now available in Claude Code.` ← this is the new 14.22.8.5 line
- Sendoff suggesting `/operator:slip <meet-url>` first, with `operator slip claude <meet-url>` as the terminal-direct fallback.

Verify the plugin is registered: `claude plugin list` should show `operator@1-800-operator` enabled.

### Step 2 — The plugin → meeting flow (validates items 1, 2, 4, 5, 7 from roadmap row 14.22.9)

1. **Open Claude Code** in an interactive session. Load some real pre-meeting context — paste a Linear ticket you've been reading, or draft a short idea, anything where you'd genuinely care if the meeting brain didn't know about it.

2. **Start a Google Meet** in another window. Get the meet URL.

3. **Trigger the slip skill**: type `/operator:slip <meet-url>` in your Claude Code session. The skill should expand `${CLAUDE_SESSION_ID}` to your live session ID at dispatch time and spawn `operator slip claude --resume-session <real-id> <meet-url>` in the background.

4. **Watch what happens:**
   - Slip Chrome should launch (`~/.operator/slip_profile/`). First time: Google sign-in flow happens once; cookies persist.
   - Operator should attach via CDP, navigate to the meet URL, request to join. You admit it from the host side.
   - Chat panel should open. Operator's MutationObserver should be listening.
   - The Swift audio helper should spawn — both `[S]` (system audio) and `[M]` (mic) legs should show frames in `/tmp/operator.log`.

5. **First @mention** — type into Meet chat: `@claude what's that ticket I was reading earlier?` (or your equivalent pre-meeting context probe). The reply should reference the context — that proves `--resume-session ${CLAUDE_SESSION_ID}` substituted correctly and inner-claude inherited your Claude Code session.

6. **Transcript MCP probe** — say something out loud in the meeting (so it lands in captions). Wait ~10s for the Whisper transcription. Then `@claude what did we just discuss?` — the reply should reference your spoken content, proving the audio pipeline + transcript MCP round-trip works on real speech.

7. **Hang up** — type `/operator:hangup` in Claude Code (or `operator hangup` in a terminal). Slip Chrome should detach but stay open with you still in the call.

8. **Post-meeting check** — back in your Claude Code session, the meeting's @-mention exchanges should be visible in conversation history. Verify by scrolling up or asking Claude Code about what was said in the meeting — proves the meeting interactions joined your real Claude Code session via `--resume`.

### Step 3 — Terminal-direct path (validates item 7)

After hanging up step 2's session:
```bash
operator slip claude <meet-url>
```
(No `--resume-session` flag.) Confirms fresh-session-on-first-@mention. After the first @mention, watch `/tmp/operator.log` for a `TIMING claude_cli_turn=...s ... ttft=...s` line with `cache_read_input_tokens > 0` on the SECOND @mention — proves `--resume` is hitting the prompt cache.

### What to look for / what to capture

- `/tmp/operator.log` is the source of truth for what happened. `grep "TIMING\|ERROR\|WARN" /tmp/operator.log` after each test phase for a quick sanity check.
- If something fails: which **earlier phase** owns the surface? CLI = 14.22.3-onwards; plugin = 14.22.7; install.sh = 14.22.6 / 14.22.8.5; marketplace = 14.22.8 / 14.22.8.5. Loop back there before moving on to 14.22.10.

---

## Skip list — already self-test-green in S215, do NOT redo

- All 11 unit tests in `tests/test_*.py`
- The full chat→LLM→reply pipeline (proven via `tests/try_terminal.py` harness)
- Trigger phrase gating (`@claude` triggers; bare messages don't)
- Multi-turn memory bridging across per-@mention spawns
- Paragraph streaming with `STREAM_PARAGRAPH_MIN_INTERVAL` pacing
- Transcript MCP search/recall (with seeded captions)
- Tool-use turns with operator-voice `[☎️ Operator] running <tool>` narration
- `claude plugin validate` against the marketplace.json
- `operator doctor` (all 5 checks)
- Plugin manifest + 4 slash commands resolve via `--plugin-dir`
- `/operator:doctor` / `:status` / `:hangup` dispatch end-to-end via the plugin path
- `claude plugin install operator@1-800-operator` from the canonical public marketplace + fresh `claude` session sees the slash commands

---

## Open questions / blockers

- **None blocking.** 14.22.9 is "go run a real meeting and watch what happens."

---

## Gotchas (don't panic)

- **`Listening for @@claude` in the slip stdout banner** is a cosmetic double-`@` (filed as roadmap row 14.22.9.5 — one-character fix in `chat_runner.py:286`). Not a regression. Bundle the fix into the same session if you want to clean it up while you're in there.
- **`https://1-800-operator.com/install.sh` is not yet hosted** — the curl-install README references it but the domain isn't serving. Use the GitHub raw URL (`https://raw.githubusercontent.com/1-800-operator/operator/main/install.sh`) for the live-test. Domain hosting is a 14.22.10 launch task.
- **Plugin currently installed user-scope on this machine** (from S215 smoke-test). That IS the expected post-install state. Uninstall + reinstall via install.sh if you want a strict test of the install path.
- **Operator skill body's `${CLAUDE_SESSION_ID}` substitution is the single most important thing to verify in step 2 above** — it's the load-bearing handoff between Claude Code and the operator subprocess. Without it, the meeting brain doesn't inherit your pre-meeting context, which is operator's whole moat. S215 confirmed substitution can't be probed via `claude -p` (the model paraphrases the skill body instead of running the literal `!` block) — so this is the genuinely-live-test-only item.

---

## Don't forget

- Three S215 commits, all pushed to both `public` (`1-800-operator/operator`) and `origin` (`dufis1/operator`, now private): `8756d18` (harness), `8975283` (14.22.8.5 collapse install), `bd4ecce` (end-session docs).
- **Both remotes need pushes going forward.** `git push origin main && git push public main` after every commit — `origin` is now a private backup mirror and `public` is the canonical install URL. Plain `git push` without an explicit remote will only push to whichever one is upstream-tracked.
- Plugin repo is PUBLIC; `dufis1/operator` is PRIVATE; `1-800-operator/operator-plugin` is PUBLIC; `1-800-operator/operator` is PUBLIC.
- Untracked working-tree carry-overs (not touched in S215): `debug/14_20_audio_spike/{0, DECISION.md, STT_COMPARISON.md, USER_NOTE.md, decode_frames.py, spike_capture, spike_capture.swift}`, `debug/14_21_mic_capture_spike/spike_mic_via_sckit`, `debug/resume_spike/`, `docs/landing-page.md`, `mvp.md`, `public/`, `operator-architecture-handoff.md` at repo root.
