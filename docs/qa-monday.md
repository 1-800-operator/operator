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

## 8. Multi-speaker cross-talk attribution — VALIDATE THE FIX (v0.1.43)

*Was a known defect (a cross-talk blob got stamped with one speaker).
**Fixed + shipped in v0.1.43**: word-level attribution maps each word to
its DOM speaker and emits one caption per speaker run. This QA is now a
**validate-the-fix** task, not observe-and-capture. Needs a real meeting
with ≥2 others talking over each other; can't be validated solo.*

**First confirm you're on the fix:** the bot self-updates at launch — check
`grep SELFUPDATE /tmp/operator.log` shows `wheel 0.1.42→0.1.43` (or
installed version ≥ 0.1.43). If it didn't swap, you're testing old code.

- During the meeting, get ≥2 participants doing fast turn-taking
(gaps <1.5s, interrupting / agreeing over each other).
- Run with **`OPERATOR_AUDIO_RAW_DUMP=1`** so the raw corpus still lands at
`~/.operator/debug/raw_<slug>/{S,M}.f32` + `meta.json` (ground truth for
grading any remaining misattribution).
- Afterward, check the captions (note the field is now **`speaker`**):
```bash
jq -c 'select(.kind=="caption")|{speaker,text}' ~/.operator/history/<slug>_<date>.jsonl
```
  **PASS:** a cross-talk stretch shows multiple caption lines with
  *different* speakers (not one blob under one name). Grade against your
  screen recording / the halo you saw live.
  **Note residual issues:** boundary words landing on the wrong speaker,
  over-fragmentation (many tiny captions), or a blob still collapsed to one
  name → capture the slug + timestamps; iterate offline with
  `debug/14_34_audio_replay/word_level_attribution.py <slug>`.
- Scope reminder: the fix is **`[S]`-leg only** (remote participants). The
  `[M]` mic leg is your own single voice — always stamped with your name,
  by design. Co-located people sharing your mic can't be split (no per-tile
  DOM signal); that's out of scope.

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

