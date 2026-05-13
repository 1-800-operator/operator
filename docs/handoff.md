# Session 224 handoff (2026-05-13) — [S] speaker attribution fix + corrected reading of the audio bug

## Corrected reading of the S223 "Jojo in [S]" observation

S223 closed with the hypothesis that the runner's voice was leaking into the `[S]` system-audio leg. Today we verified that's **not** what's happening.

Listening to `/tmp/operator_audio_debug/S/` (from a fresh `OPERATOR_AUDIO_DEBUG=1` run) confirmed that the runner's voice is **never** in `[S]`. The architecture is fine: slip Chrome's system audio only contains remote participants, just as designed.

What was actually going wrong: the JSONL caption attribution. `sqr-vyex-wob.jsonl` contains 6 `Jojo Shapiro`-tagged captions whose **text** is from remote participants (e.g. the meeting farewell at `1778692437.6` — "Appreciate the time everyone. Talk to you on the next calls. All right. Thank you." — was actually Michael+Matthew+Kyle in quick succession, not Jojo).

Root cause: `_audio_utterance_loop`'s `[S]` branch resolves the speaker via the DOM speaking-indicator (BlxGDf class). Meet drives that indicator off mic activity. The runner's mic is *constantly* hot because room audio from the speakers leaks into it, so the runner's local tile is *frequently* "speaking" — and the `[S]` utterance (genuinely from a remote participant) gets tagged with the runner's name.

## What landed this session

**Fix: exclude the local tile from `[S]` speaker attribution.** Two layers:

1. `INSTALL_SPEAKING_OBSERVER_JS` now identifies the local tile (same predicate as `GET_SELF_NAME_JS`: presence of `button[data-idom-class]` + non-empty `span.notranslate`) and skips installing a MutationObserver on it. Return shape changed from bare `count` to `{count, local_pid}`.
2. `_drain_speaking_queue` defensively filters events whose `participant_id` matches the cached `_local_participant_id`, in case the local tile DOM re-renders and emits a stale event.

Net effect: `_speaking_participants` and `_last_s_speaker` can never name the runner. The `[S]` leg attributes only to remote participants.

**Side cleanup:** fixed three stale test assertions in `tests/test_transcript_mcp.py` left over from S223's truncation-notice wording change (commit `8b4260e`).

## State of the repo

All commits pushed to `origin` and `public`. `uv tool install --reinstalled` on this machine.

The `README.md` is still dirty (user-owned billing-protection wording) — not committed by convention.

## Open questions remaining

### 1. Can we send to the chat textarea while the panel is hidden?

S223 disabled chat-panel auto-reopen pending validation. The two console scripts from the S223 handoff are still ready to run in a live meeting. Results pending.

### 2. The `[M]` mic is full of bleed.

Independent from the `[S]` attribution bug. With speakers (not headphones), the runner's `[M]` leg captures the remote participants playing back through the room and Whisper transcribes it labeled as the runner. Examples in `sqr-vyex-wob.jsonl`:

- Kyle: "Monday or whatever." → user: "Monday, day or whatever."
- Kyle: "The end game is to get an ultra wide monitor" → user: "The end game is to did an ultra-wide monitor."

The bleed-suppressor (`far_end`-aware VAD on the mic leg) isn't catching these. Candidates:

- Bleed-suppressor's far-end-activity window is too narrow / VAD threshold too lenient.
- The mic Whisper finalizes faster than the bleed-suppressor's far-end snapshot.
- With AirPods/headphones the leak goes away — confirming room acoustics are the proximate cause but not the only mitigation.

### 3. Is `[M]` redundant?

Resolved: **no.** `[S]` only contains remote participants (architecture confirmed). `[M]` is the only path for the runner's own voice. Keep both pipelines. The bleed problem (Q2 above) is what makes `[M]` look noisy; with clean acoustics it would be silent except for the runner.

## How to verify the fix in a live meeting

1. `uv tool install --reinstall` after pulling.
2. Run `OPERATOR_AUDIO_DEBUG=1 operator slip claude <multi-party-meet-url>`.
3. Talk over remote participants briefly so the local tile would be "speaking" while a remote is also talking.
4. After the meeting, inspect the new `~/.operator/history/<slug>.jsonl`. No caption should have `sender` matching the runner's display name *and* text that is clearly a remote participant's speech.
5. Cross-check `/tmp/operator_audio_debug/S/*.wav` to confirm the audio you expect.
6. In `/tmp/operator.log`, look for the install line: `speaking observer installed on N remote tile(s); local_pid='…'` — confirms the local tile was identified and skipped.
