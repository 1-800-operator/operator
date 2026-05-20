# QA checklist — Monday meeting (2026-05-18)

Covers user-facing changes from S234–S237 that haven't been validated in
a real meeting yet. Group by what you'd naturally exercise during the
flow of a meeting.

## 1. Install / TCC permissions (S237)

*Only relevant if you reinstall on a fresh machine before Monday —
otherwise skip.*

- `./install.sh` triggers Mic + Screen Recording prompts attributed
to **operator-audio-capture.app** (not to Terminal/IDE). Click
Allow on both.
- Re-run `./install.sh`: prints "Audio permissions already granted"
and doesn't re-prompt.
- (If perms drift mid-life) running `/operator:dial` re-warms TCC  
via the preflight before "operator: joining…" — synchronous,  
~10s on first run.

## 3. Trigger + sticky conversation window (S234)

- First `@claude <question>` from you → bot answers.
- **Follow-up from you within 90s** without `@claude` → bot still
answers (sticky window).
- **A different participant** speaks without `@claude` during your
window → bot ignores (window is sender-scoped).
- After ~90s of silence, follow-up without `@claude` → bot ignores
again.
- Rapid-fire corrections from you (two messages in <2s) → bot only  
responds to the latest (debounce).

## 6. Recap / list_meeting_record (S236)

*Best tested at end of meeting or after.*

- In meeting: `@claude give me a recap so far` on a long meeting  
(45+ min). Claude should produce a coherent recap **without**  
saying "only the tail was captured" or "I only have partial  
transcripts."

## 7. Audio drain on shutdown (S244)

*The whisper_worker subprocess drains residual audio after main
exits, but there's also an in-process `AudioProcessor.drain` flush
that has only been unit-tested. Validate the trailing utterance
lands in the JSONL across the three shutdown paths.*

- **Leave button** mid-sentence → wait for the worker to seal →
`jq '.kind == "caption"' ~/.operator/history/<slug>_<date>.jsonl`
shows the trailing utterance.
- **Tab close** mid-sentence → same expectation.
- **`/operator:hangup`** mid-sentence → same expectation.

## 8. Multi-speaker single-winner attribution (S250 — known open defect)

*Needs a real meeting with ≥2 other people who talk over each other
to reproduce; can't be validated solo. Carried here from the S250
handoff. The defect: a single whisper utterance that spans rapid
back-and-forth between speakers gets stamped with ONE speaker name
(the one with the most total overlap), not split per-turn. Observed
S250: a ~14s blob spanning 4 Matthew↔Michael turns attributed
entirely to Michael.*

**This is an observe-and-capture task, not a fix-and-verify task** —
the fix has to be iterated offline against a real recording.

- During the meeting, get ≥2 participants doing fast turn-taking
(gaps <1.5s, e.g. interrupting / agreeing over each other).
- Run with **`OPERATOR_AUDIO_RAW_DUMP=1`** set so the raw float32 PCM
corpus lands at `~/.operator/debug/raw_<slug>/{S,M}.f32` + `meta.json`.
- Afterward, check the captions in
`~/.operator/history/<slug>_<date>.jsonl`: does a single caption
line carry words from two different people under one `speaker`?
Note the timestamps of any misattributed blob.
- The replay corpus + `debug/14_34_audio_replay/load_corpus.py` are
the offline iteration harness for the actual fix. Two known
structural causes to address there: (1) the S249 hybrid VAD ends
utterances on Silero-silence only, which stays hot across rapid
turn-taking, so the blob never splits; (2) `_attribute_speaker`
picks one winner for the whole window. Minor related bias:
`chunk_end=time.time()` is stamped ~3.5s after audio end (post-
transcribe), stretching the attribution window past the audio.

## 9. AEC pre-shift — validate it's even an issue post-migration (H-23)

*The audit (H-8/H-23) flagged that AEC3's 150ms pre-shift, baked into
the Rust `aec3` binary, could mangle clean mic input on the recommended
config. BUT that finding's premise was SCStream's 63ms output-buffer
skew — and SCStream was removed in v0.1.35 (Core Audio Tap migration).
The actual skew on the new audio path was never measured, so we don't
know if the 150ms is now harmless, off-by-a-little, or off-by-a-lot.
This is a "is this even a real problem for us?" validation, not a fix.*

The bug, if real, only manifests on **built-in speakers** (system
audio leaks into the mic → AEC tries to cancel it → misaligned
pre-shift subtracts the wrong thing and garbles your own speech). On
AirPods/headphones there's no echo path, so this can't show.

- **Baseline (AirPods):** in a meeting, say 2-3 known sentences. Note
the M-leg caption quality in `~/.operator/history/<slug>_<date>.jsonl`.
- **Test (built-in speakers):** switch the Mac to built-in speakers,
have a remote participant talk (so real audio plays out the speakers
during your speech), say the **same** sentences. AEC now has a real
echo path to cancel.
- **Compare:** if the built-in-speaker captions of *your own* speech
are noticeably more garbled than the AirPods baseline, the pre-shift is
misaligned and H-23 is real. If they're comparable, the migration
neutralized it and H-23 can be closed.
- Optional: set **`OPERATOR_AUDIO_DEBUG=1`** to dump per-utterance M-leg
WAVs to `~/.operator/debug/audio_<ts>/M/` — listen to the cleaned mic
directly to hear residual-subtraction artifacts.

