# Session 227 handoff (2026-05-13) — AEC3 cleaner shipped on main + 14.22 PTY+hooks refactor live-validated on its branch

Two parallel S227 sessions ran today. Both produced working code; one (AEC) landed on `main`, the other (PTY pivot) lives on a branch ready to merge after sections G/I/J/K/L follow-up work.

## Track A — AEC3 speaker-bleed cleaner (landed on main, pushed to origin)

**Two commits on `main`:**
- `bc56fad` — S227: AEC3 speaker-bleed cleaner — integrate spike into live mic pipeline. The full 7-step S225 integration plan: streaming-mode Rust binary, `pipeline/aec_cleaner.py` subprocess manager (uses `os.posix_spawn` directly — NOT subprocess.Popen), wiring in `attach_adapter.py`, deletion of the dead far-end VAD bleed gate from `pipeline/audio.py`, build + install via `install.sh` step 8.5, optional aec3 check in `pipeline/doctor.py`, source moved from `debug/14_23_aec_spike/aec3/` to tracked `src/_1_800_operator/rust/aec3/`. Also includes M↔S leg fuzzy bleed dedupe (`SequenceMatcher.ratio() ≥ 0.75`, 4s window, tunables in `config.py`). 13 files changed, +1622 / -59. Six new tests across `tests/test_aec_cleaner.py` and `tests/test_attach_audio_wiring.py`.
- `bcb672f` — S227: doctor — pre-warm mlx-whisper to surface Metal compile failure. Adds an `mlx-whisper warmup` optional doctor check that runs the same warmup operator does at slip entry. Catches `[metal::Device] Unable to build metal library from source` failures at install/diagnostic time so users don't hit the resulting MLX-internal-fork crash dialog mid-meeting.

**Live-validated** on two real Meet runs. AEC binary reports `echo_return_loss: Some(-30.0)` on both teardowns — exact match for S225's spike measurement. Mic-leg whisper had zero errors. 4/5 user utterances correctly attributed in a hard test (system audio + mic on one machine, no headphones); 1/5 was a residual-bleed phrase that the new M↔S dedupe is designed to catch but missed because the S-leg whisper hit the (separate) intermittent metal-compile error and produced no caption.

## Track B — 14.22 PTY+hooks production refactor (on `phase-14.22-pty-pivot` branch, not yet merged)

**Two commits on the branch (unpushed):**
- `daedf21` — S227: 14.22 production refactor — PTY+hooks provider replaces claude -p. Rewrites `pipeline/providers/claude_cli.py` from per-@mention `claude -p` shellouts to one long-lived interactive `claude --dangerously-skip-permissions` per meeting, driven over a PTY (pty.openpty + 40×120 winsize + `start_new_session=True`). Input is bracketed-paste wrap + CR (`\x1b[200~ <msg> \x1b[201~\r`, timings from spike_finalize T1). Output is a tail loop on `~/.operator/sessions/<uuid>/replies.jsonl` written by the operator-plugin's Stop hook. SessionStart→`ready.flag` handshake with 30 s timeout falling back to 5 s settle if the plugin isn't installed. State dir layout per DECISION.md section E. `OPERATOR_SESSION_DIR` exported into the spawn env so the plugin hook subprocesses (children of inner-claude) inherit it. ANTHROPIC_API_KEY still stripped from spawn env. Bundles A + B + C + E + F + H into one coherent commit so the result is end-to-end live-testable; G / I / J / K / L follow in their own commits.
- `a857412` — S227: PTY spawn — drop preexec_fn (double setsid), surface spawn error. Found via the first live test failing with `Exception occurred in preexec_fn.`. Root cause: `__main__.py` monkey-patches Popen to default `start_new_session=True`, which already calls setsid in the child; the provider was *also* passing `preexec_fn=os.setsid`, so setsid ran twice and the second call failed EPERM. Fix: switch to `start_new_session=True` explicitly, drop preexec_fn. Bonus: store the spawn exception on `self._spawn_exc` so the next caller's "inner-claude failed to spawn" surfaces the real cause instead of the misleading "check operator-plugin install" generic.

**Plugin side (pushed to GitHub):**
- `operator-plugin@f9900f9` (version 0.1.12) — `hooks/hooks.json` + `hooks/scripts/{session_start,stop,pretool,error,_common}.sh`. Scripts source `_common.sh` which short-circuits exit 0 when `$OPERATOR_SESSION_DIR` is unset, so they no-op cleanly on the user's normal Claude Code sessions and only activate inside operator's inner-claude.
- `main@be955f0` — marketplace.json bumped to 0.1.12 (pushed to origin AND public; public was 6 commits behind, caught up).

**Live-validation results (Meet `ekg-vzgc-kiv`):** three turns, all clean.
1. `@claude hey` → 3.08 s msg→posted. Reply: "Hey Jojo! I'm in the meeting. How can I help?"
2. `@claude read mvp.md and summarize` → 6.15 s. Used Read tool. tools.jsonl shows the PreToolUse event.
3. `@claude duplicate the file` → 5.31 s. Used Write tool. Reply: "Done — mvp-copy.md has been created."

Same `session=a72bcb0f-…` resumed across all three turns (cwd-scoped `--resume` worked). `ready.flag` written by SessionStart hook within ~2 s of spawn. Bracketed-paste handled slashes, file paths, hyphens cleanly. Desktop-app session bridging worked via the `CLAUDE_CODE_SESSION_ID` env path.

## Exact next step

**Pick up Track B (PTY pivot) on the `phase-14.22-pty-pivot` branch. Five sections of DECISION.md remain:**

- **G — Operator-voice callback remap.** Wire `progress` off a `tools.jsonl` tail loop (with the existing 20s throttle); `denial` off `errors.jsonl` rows where `kind ∈ {PermissionDenied, PostToolUseFailure}`; `connection` off `errors.jsonl` rows where `kind == StopFailure` plus a PTY EOF watcher (raise `connection("dropped")` from the drain thread if it sees EOF). All four callback setters are already on the provider as no-op stubs — just fill in the implementations and start the tail loops alongside `pre_warm`.
- **I — Foreign-hook detector.** On every Stop firing, scan `transcript_path` (which the Stop hook payload carries) for any user-role message in the last turn containing `"Stop hook feedback:"`. If present, surface `[☎️ Operator] a foreign hook redirected the conversation this turn`. Also time the gap between turn-finish and Stop-fire; >5 s → surface a delay warning.
- **J — Tear-down race fix.** Today's `_terminate_inner` SIGTERMs immediately. Need to: after sending the final message, wait for the corresponding `replies.jsonl` row (with timeout, e.g. 30 s) before signaling. Otherwise the final assistant reply may be lost because the Stop hook script hadn't yet written its file when the parent SIGTERM'd inner-claude's group.
- **K — Claude Code version floor.** Pin `>= 2.1.141` in operator-plugin's `plugin.json` metadata. `doctor` refuses to launch with a clear message ("Operator requires Claude Code ≥ 2.1.141 — please update via /plugin").
- **L — Plugin install smoke test.** Verify `hooks/scripts/*.sh` exec bits survive `uv tool install --reinstall .` + desktop-app plugin sync. Falls back to interpreter-explicit invocation (`bash $CLAUDE_PLUGIN_ROOT/hooks/scripts/stop.sh`) in `hooks.json` if not — which we're already doing, so this is probably a one-line verification. Pair with rewriting `tests/test_claude_cli_provider.py` for the new architecture (the old per-shellout smoke test is obsolete).

Estimate: 1 session for G alone (it's three tail-loop wirings plus the EOF watcher), 1 session for I/J/K/L bundled.

**After all sections land:** merge `phase-14.22-pty-pivot` into `main`, then run the integration test pass numbered 20–25 in DECISION.md (long-meeting compaction, hook latency on hot path, foreign-hook interference, tear-down race, resume from desktop-app session, --fresh mode).

## Open follow-ups (carried)

- **Anthropic's classification past June 15.** Interactive PTY-driven claude — counts as subscription usage (planned) or reclassified as programmatic (bad)? Untestable until June 15. Watch Claude Code release notes for new flags/env vars that signal Anthropic is patterning against this. If they reclassify, BYO-API-key (DECISION.md section M, ~10 lines) becomes the only option.
- **Claude reply-delivery feedback loop** (S224 carryover). When `send_chat` fails, the claude subprocess gets no signal — it confidently says "I replied" after every reply got dropped. Wiring failure back into the claude session as a tool-result error would close the loop. Independent of the PTY refactor; could land any time.
- **Post-MVP: gate `operator slip` behind the plugin** (deprecate terminal entry).
- **Python 3.14 + MLX shader-compile flakiness.** Mitigated via doctor pre-warm; root cause is upstream and out of operator's control. Consider pinning `requires-python` to `>=3.10,<3.14` if it recurs.

## State of the repos

**operator (main):** at `be955f0`. Pushed to `origin` AND `public`. Pending: this end-session doc commit will land on top.

**operator (phase-14.22-pty-pivot branch):** 2 commits ahead of main (`daedf21` + `a857412`). Unpushed; locally only. Ready to merge into main once sections G/I/J/K/L land.

**operator-plugin (main):** at `f9900f9` (version 0.1.12). Pushed to `origin`.

**Tracked-but-modified on operator main (do not commit):** `README.md` (user-owned billing-protection wording). Untracked: same set of debug/ and stale-doc artifacts as before (`debug/14_20_*`, `debug/14_21_*`, `debug/14_23_aec_spike/`, `debug/claude_idle_overnight/`, `debug/resume_spike/`, `docs/landing-page.md`, `hooks.md`, `mvp.md`, `mvp-copy.md`, `operator-architecture-handoff.md`, `public/`). The `mvp-copy.md` was created by inner-claude's Write tool during the PTY live test; safe to delete.

## Hard Won Knowledge captured

Three new entries appended to `docs/agent-context.md`:
1. (Track A) `subprocess.Popen` is unsafe for spawning long-running subprocesses from an mlx-whisper-loaded process on Python 3.14 — fall back to `os.posix_spawn` directly.
2. (Track A) MLX's first-time Metal shader compile is flaky on Python 3.14; the resulting crash dialog is from MLX's internal fork-helper, not operator's main process. Doctor pre-warm surfaces it at install time.
3. (Track B) `__main__.py` monkey-patches `subprocess.Popen` to default `start_new_session=True`; passing `preexec_fn=os.setsid` *also* runs setsid twice in the child and the second call fails EPERM. The surfaced error is the opaque single-line `Exception occurred in preexec_fn.` with no other context. Fix: drop preexec_fn, pass `start_new_session=True` explicitly. Lesson: process-wide Popen patches are a load-bearing surface that's easy to forget about — document them or use an explicit pass-through assertion.

## How to verify both tracks in a fresh session

```bash
cd /Users/jojo/Desktop/operator

# Track A — AEC unit tests + binary build sanity:
source venv/bin/activate
for f in tests/test_*.py; do echo "=== $f ==="; python "$f" 2>&1 | tail -3; done
~/.cargo/bin/cargo build --release --manifest-path src/_1_800_operator/rust/aec3/Cargo.toml
operator doctor

# Track B — PTY refactor live retest:
git switch phase-14.22-pty-pivot
uv tool install --reinstall .
# Then in a fresh Claude Code window:
#   /operator:slip <meet-url>
#   @claude hello
#   @claude read some file and summarize
# Expect: ~/.operator/sessions/<uuid>/{ready.flag,replies.jsonl,tools.jsonl} populated;
# all turns post within 3-7s.
```
