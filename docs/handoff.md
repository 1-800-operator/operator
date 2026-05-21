# Session 254 handoff — smaller STT model (small.en), shipped v0.1.51

**What got done.** Swapped the speech-to-text model from `large-v3-turbo` (~1.5GB)
to `Systran/faster-whisper-small.en` (~464MB) to cut the install-time download (the
model fetch was the slow part of setup), and shipped it end-to-end as **v0.1.51**
(verified: manifest live, asset-sha == manifest-sha == `a8057ce7…`, 3 release assets
incl. reused aec3). The pick was data-driven: built two re-runnable benchmark
harnesses (`debug/14_34_audio_replay/model_bench.py` = attribution vs the S253
hand-labeled oracle + WER vs turbo on the 6 cross-talk windows; `full_meeting_wer.py`
= whole-meeting WER vs turbo) and swept tiny/base/small/distil .en. small.en won:
13.9% whole-meeting WER vs turbo (turbo isn't ground truth — transcripts read
near-identically), 91% per-word speaker attribution vs turbo's 94%. distil-small.en
was rejected (collapsed on long-form: 180/2208 words). Also single-sourced the model
constants — `doctor.py` now imports `_FW_MODEL_REPO`/`_FW_COMPUTE_TYPE` from `audio.py`
(resolved the duplication finding at `launch-audit-findings.md:1376`). The release was
scoped around two concurrent sessions (install.sh reorder + landing-page) by
committing whisper-only via explicit-path staging and building from a clean
`git archive HEAD`; both parallel sessions' uncommitted work was left untouched.

**Exact next step.** Nothing required — v0.1.51 is shipped and verified, rolling
forward to existing installs on next dial (one-time 464MB small.en download). To test
on a NEW machine right now, install with the ref override (the public install.sh's
default ref is still v0.1.50 until the install session ships its bump):
`curl -fsSL https://raw.githubusercontent.com/1-800-operator/operator/v0.1.51/install.sh | OPERATOR_INSTALL_REF=v0.1.51 bash`
Optional confirm: a live `operator dial` should log `SELFUPDATE wheel …→0.1.51` and
`AudioProcessor: small.en ready`.

**Open items / blockers.** None blocking. (1) The install-reorder session has
`install.sh` uncommitted (already bumped its default ref to v0.1.51) — once it ships,
the override won't be needed. (2) The landing-page session still has README/SECURITY/
CLAUDE/docs uncommitted — untouched, do not ship with a routine release. (3) Launch-
blocking roadmap item still open: enable `macos-13-large` CI so aec3 auto-attaches
(until then runbook step 7.5 manual aec3 upload is REQUIRED every release — done this
release via a byte-identical copy from v0.1.50). (4) If anyone wants attribution past
~95%, the only lever left is real acoustic diarization (pyannote on the S-leg) —
heavyweight, almost certainly not worth it.
