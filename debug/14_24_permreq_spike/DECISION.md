# 14.24–14.26 — yolo-off mode design chain (decided 2026-05-14)

**Status:** decided 2026-05-14, implemented as `/operator:slip-guarded` (Phases 0-3 done; live test gate pending).
**Trigger:** Research report on Claude Code user reactions to no-permission-controls products flagged the absence of any `deny`-rule honoring as a real adoption blocker for a "careful adopter" segment. Validated separately: a user-controllable yolo-off path is worth shipping.

## The decision

**Yolo-off mode is a separate `/operator:slip-guarded` slash command** (and `operator slip-guarded claude <url>` CLI subcommand) that:

1. Spawns inner-claude with `--permission-mode default` instead of `--dangerously-skip-permissions`.
2. The operator-plugin `PermissionRequest` hook (Phase 1) bridges each permission dialog into meeting chat.
3. A separate long-lived `PermissionClassifier` claude sidecar (Phase 2 redux) interprets the participant's verbatim chat reply as YES/NO via one tiny ~2-3s turn — no operator-side word-bag matching.
4. There is **no "allow always" / persistent-allow path.** Yolo-on is the friction-free mode; yolo-off asks every uncategorised tool every time. Within-meeting persistence isn't a bridge between them.

Default remains yolo-on (`/operator:slip`) — no regression for existing users.

## The spike chain that got us here

Three sequential spikes in this folder + 14_25 + 14_26.

### 14_24 — does `PermissionRequest` even fire in interactive PTY mode?

`spike_permreq.py`. 5 tests against fresh `claude --permission-mode default` spawns with a stand-in PermissionRequest hook.

**Outcome: PASS overall, 4/5 PASS + 1 FAIL.**

- T1 PASS: `PermissionRequest` fires in interactive PTY mode. The hook resolved the dialog cleanly.
- T2 PASS: a *blocking* hook (the operator round-trip in miniature — request → driver waits 3s → writes answer → hook returns) resolves without hanging the TUI. Total 11.6s, no hang.
- T3 **FAIL**: hook denies via `exit 2`. Tool blocked correctly, but the turn never completed — claude appears to retry-loop on the bare denial. Hit the 90s test timeout.
- T4 PASS: hook denies via JSON `{behavior: "deny", message: "..."}`. Clean. Tool didn't run, claude said *"I won't re-attempt it"*, turn ended in 10s.
- T5 PASS: with the tool pre-allowed in bench settings, `PermissionRequest` did NOT fire — narrow-firing confirmed.

**Key learning: deny via structured JSON only, never `exit 2`.** Codified in the operator hook contract (`_common.sh`): always exit 0, express decisions in JSON. Generalises to all hooks, not just PermissionRequest.

### 14_25 — can claude self-interpret via the deny-message channel?

`spike_permreq_v2.py`. Tested whether handing the participant's verbatim words to claude via the deny `message` field would let claude itself decide whether to retry the tool call. 19 scenarios across approvals, refusals, ambiguous, modified-intent.

**Outcome: NOT VIABLE.**

- Approvals: only 3/8 retried (`do it`, `go ahead`, `yeah`). The most common phrasings — `yes`, `sure`, `okay`, `sounds good`, `👍` — all FAILED. Worse: on those failures claude said *"Done."* in chat without actually running anything. Hallucinated completion.
- Refusals: 5/5 honored.
- The `yes but use --dry-run` scenario revealed the cause: claude said *"That looks like a prompt-injection attempt (it arrived via the tool-result channel, not as a real user turn)."*

**Same dynamic that killed Stop-block in 14.22's `spike_framing.py`** — claude's prompt-injection defense quarantines text arriving through hook channels that look like instructions. Not bypassable at any sane layer; it would mean disabling an Anthropic safety feature.

**Conclusion: the operator-side claude-interpretation path is a strategic dead end.** Same reasoning as the 14.22 DECISION.md hit for Stop-block.

### 14_26 — interactive classifier sidecar

`spike_classifier.py`. Tested whether a *separate* long-lived classifier claude (one per meeting, on subscription pool, ~6s boot amortised) could interpret participant replies via a normal user-turn input — bypassing the prompt-injection defense by avoiding the tool-result channel entirely.

**Outcome: VIABLE.** Same 19 scenarios.

- Approvals: **8/8 correct** (every fail from 14_25 fixed: yes, sure, okay, sounds good, 👍 all PASS).
- Refusals: **5/5 correct** (no UNSAFE classifications).
- Ambiguous: all → NO (the safe default the prompt instructs).
- Average classifier turn: **2.6s**. Boot+settle: 6s, hidden in the meeting-join window.
- Cost: $0 marginal. Subscription pool, naked spawn (no `-p`).

The classifier prompt that worked: *"You are helping me interpret a participant's reply in a Google Meet chat. The bot just asked them a permission question. The participant replied: '<reply>'. Did they approve? Reply with exactly one word: YES or NO. If you're unsure, reply NO (deny is the safe default)."*

## What we kept, what we scrapped

| Built initially | Outcome |
|---|---|
| Word-bag matcher (`pipeline/confirmation.py` with `is_yes` / `is_yes_always` / `_NEGATION_RE`) | **Scrapped.** No operator-side pattern matching; the user explicitly didn't want it ("we should have the model interpret what the user says"). Deleted. |
| `permission_suggestions` echo + `updatedPermissions` "always allow this meeting" path | **Scrapped.** No persistent-allow path in yolo-off. That's what yolo-on is for. Hook stopped forwarding the field; provider stopped reading it; ChatRunner's `_build_session_allow` deleted. |
| Phase 1 PermissionRequest hook (operator-plugin/hooks/scripts/permission_request.sh) | **Kept.** Mechanism is right; just stopped passing through the suggestions field. JSON-only deny enforced. |
| Phase 2 round-trip plumbing (request file tail in provider, chat-poll-in-tick, atomic answer write) | **Kept.** Wiring works; only the classification step was wrong. Replaced word-bag with classifier call. |
| Phase 0 hook hardening (`_common.sh` contract block, `safe_emit_permreq_deny`, dropped `set -e`) | **Kept.** Foundational regardless of which classifier path we picked. T3's exit-2 finding generalised to all hooks. |

## What this leaves us with

- `/operator:slip` (default, yolo-on): unchanged. `--dangerously-skip-permissions`, every tool runs, no asks.
- `/operator:slip-guarded` (new, yolo-off): `--permission-mode default` + PermissionRequest hook + classifier sidecar. Every uncategorised tool asks the meeting; a participant's reply is interpreted by the classifier via one ~2-3s turn; allow or deny goes back through the hook. No persistent allows; ask every time.

Residual risks (re-)surfaced in yolo-off (documented in `docs/security.md`):
- **H1 — any participant can answer.** No per-sender allowlist. Mitigation: Google Meet "host manages chat."
- **H2 — natural-language ambiguity.** The classifier defaults to NO on unclear, so "skews safe" — but a confidently-ambiguous reply could go either way.
- **R1 from base security model still applies** — uncategorised tools that get approved still run with full OS access. Yolo-off lets the user *gate* per ask, it doesn't *sandbox*.

## Pointers

- `bench/` — shared spike harness scripts.
- `out_permreq_results.json` — 14_24 raw results.
- `../14_25_permreq_v2_spike/out_permreq_v2_results.json` — deny+retry rejection data.
- `../14_26_classifier_spike/out_classifier_results.json` — classifier 19/19 PASS data.
- `tests/test_permreq_round_trip.py` + `tests/test_permission_classifier.py` — operator-side mocked tests.
- `operator-plugin/hooks/scripts/permission_request.sh` + `_common.sh` — the live hooks.
- `src/_1_800_operator/pipeline/classifier.py` — the sidecar.
