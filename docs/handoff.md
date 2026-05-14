# Session 225 handoff (2026-05-13) — AEC bleed-mitigation spike complete; integration not yet started

## What landed this session

**No production commits.** Work was entirely in `debug/14_23_aec_spike/` (untracked). The session converted S224's open question #1 (`[M]` bleed unreliable) from "we don't know what to do" to "we have a working algorithm + concrete parameters."

### Spike artifacts (in `debug/14_23_aec_spike/`)

- `record_test_session.sh` + `record_continuous.py` — guided 90-second test recorder that walks the user through seven labeled segments (silent baseline / bleed-only / user-only / two double-talk types / quick alternation / bookend) and dumps continuous S+M WAVs.
- Two real test recordings: archived v1 (full speaker volume) in `archive_v1_fullvol/`, live v2 (realistic volume) at `session_{S,M}.wav` + `session_log.txt`.
- `aec3/` Rust binary using `tonarino/webrtc-audio-processing` 2.1.0 with `bundled` + `experimental-aec3-config` features. Binary at `aec3/target/release/aec3_spike`. CLI: `--mic <wav> --ref <wav> --out <wav>`.
- `analyze_session.py` / `diagnose.py` / `aligned_rerun.py` / `run_aec3_v2.py` / `aec3_shift_sweep.py` — measurement scripts.
- Listenable A/B output at `out_aec3/listen_aec3_shifted.wav` — user verified by ear that the cleaned audio matches the numbers.

### Empirical findings

1. **WebRTC AEC3 + correct alignment gives -30 dB bleed cancellation, -0.1 dB user-voice damage, -8 dB double-talk reduction.** Default config; no tuning required.
2. **speexdsp not viable** — only -17 dB max even on aligned data.
3. **SCStream has a 63 ms anti-causal timing skew** (mic LEADS ref). This is the bug that made S224's coincidence-VAD bleed gate ineffective. Documented as new Hard Won Knowledge entry in `docs/agent-context.md`.
4. **AEC3's causal delay-search window: 63-500 ms.** Over-shifting is safe within that range.
5. **Production pick: 150 ms hardcoded pre-shift.** Saved as memory `project_aec_design_findings`.

### New memories saved

- `feedback_no_rinky_dink_deps.md` — user rejected the small-but-clean DTLN-aec CoreML library mid-session ("rinky dink repo"). Don't propose load-bearing deps on bus-factor-of-one repos.
- `project_aec_design_findings.md` — empirical findings + 150 ms shift + algorithm decision for the future integration session.

## Exact next step

**Start integration with step 1 of the 7-step plan in `docs/roadmap.md`'s Current Status section.** Step 1 is contained (~1 hour, only touches `debug/14_23_aec_spike/aec3/src/main.rs`):

> Extend the Rust binary from batch mode (one-WAV in, one-WAV out) to streaming mode: read framed PCM on stdin using the same `[1-byte tag][4-byte BE length][N bytes Float32 LE]` protocol the audio helper outputs, internally maintain a 150 ms M-frame delay buffer, output framed cleaned-M frames on stdout. Long-running process; clean shutdown on stdin EOF.

Other steps in order: `pipeline/aec_cleaner.py` (subprocess manager + delay buffer + frame pairing); wire into `connectors/attach_adapter.py`; delete the dead `far_end` coupling from `pipeline/audio.py`; add build/install for the Rust binary; tests; live validation. Each step is small enough to commit independently.

## Open follow-ups (carried)

- **Claude reply-delivery feedback loop** (carried from S224) — when `send_chat` fails, the claude subprocess gets no signal. Not addressed this session.
- **Post-MVP: gate `operator slip` behind the plugin** (carried from S224).

## State of the repo

- `main` is at `f1add9d` (last S224 commit), unchanged this session.
- Tracked-but-modified files: `README.md` (user-owned billing wording, convention is not to commit), `docs/agent-context.md` + `docs/handoff.md` + `docs/roadmap.md` (this session's updates — should commit). The agent-context + roadmap + handoff updates are the only changes worth committing this session.
- Untracked: `debug/14_23_aec_spike/` (spike artifacts — generally we leave debug/ uncommitted, follow existing convention).

## How to verify the spike findings in a fresh session

```bash
cd debug/14_23_aec_spike
source venv/bin/activate
python aec3_shift_sweep.py
# Expect: shifts -63 to -500 ms all give ~-30 dB bleed drop; past -500 falls off
afplay out_aec3/listen_aec3_shifted.wav
# A/B confirms the numbers by ear
```
