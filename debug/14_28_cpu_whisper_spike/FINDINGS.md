# 14.28 — CPU whisper backend benchmark

## TL;DR

**Switching from mlx-whisper (Metal) to faster-whisper (CPU) is viable
and kills the entire Metal/MLX crash family** (S227 + S233 HWK entries).
The trade is a 5× slower p50 (650ms → 3.4s) for **identical accuracy**
and a **lower worst-case latency** than MLX. For operator's use case
(transcript MCP, captions don't need to be sub-second), the latency
trade is invisible to the user.

## Method

Re-ran S231's benchmark — same 12 hand-corrected ground-truth WAVs at
`~/.operator/debug_snapshots/gte-dmiw-spw-2026-05-14-pm/`, same
silence-padding, same WER metric — with `faster-whisper-large-v3-turbo`
(CTranslate2 conversion at `deepdml/faster-whisper-large-v3-turbo-ct2`)
on CPU instead of `mlx-whisper-large-v3-turbo` on Metal.

Two compute types: `int8` (production CPU pick) and `float32` (max
accuracy floor).

## Results

| Backend | p50 ms | max ms | longest rt | WER | CPU% |
|---------|--------|--------|------------|-----|------|
| **mlx/metal** (S231 baseline) | 650 | 6200 | 0.57× | 13.0% | n/a |
| **CPU int8** | 3469 | 3772 | **0.35×** | 14.4% | 388% (~4 cores) |
| **CPU float32** | 3729 | 4626 | **0.43×** | 13.7% | 244% (~2.5 cores) |

Cold-load (cached model): 1.6s, similar to MLX.
First-ever download of the CTranslate2 model: ~100s (one-time, ~1.5GB).

## What this means

**Accuracy is a wash.** 14.4% (int8) vs 13.0% (MLX) is within noise for
an STT system; float32 closes that to 13.7%. The +0.7 to +1.4 point WER
hit is invisible at the meeting-transcript granularity operator uses.

**Worst-case latency is actually better on CPU.** Counterintuitive but
real: max latency on CPU int8 was 3.77s on a 10.8s utterance (0.35×
realtime); MLX baseline was 6.2s on a 10s utterance (0.57× realtime).
faster-whisper's beam-search is more predictable than MLX's runtime —
no shader-compile spikes, no XPC variance.

**p50 latency is 5× slower on CPU** (3469ms vs 650ms). This is the only
real trade. **It doesn't matter for operator's use case** because:

- Transcripts are queried via `mcp__transcript__search_captions` —
  asynchronous, not real-time. A user asking "what did X say about Y?"
  sees the answer regardless of whether the caption landed 0.5s or 3.5s
  after the utterance ended.
- The audio pipeline is segmented per-utterance. Each utterance is
  transcribed independently after a silence gap. 3.4s of compute on a
  3.5s utterance gap is fine; the system clears each utterance well
  before the next one finishes being captured.
- Real-time captions aren't a product surface. There's no "live closed
  captioning" feature operator ships.

**CPU load is bursty, not constant.** 4 cores pegged during transcribe,
0 cores between utterances. On an M-series with 8+ performance cores,
this is noticeable but not crushing — the user wouldn't peg the
machine. Worth measuring on a long real meeting to confirm fan / heat
behavior, but no red flag from these numbers.

**The big win: the entire Metal/MLX crash family disappears.** Two
crashes today (and the documented S227 family) all trace to MLX's
`mlx::core::gpu::check_error` throwing from a Metal completion-handler
dispatch thread when the Metal compiler XPC service is unhappy. Drop
MLX, drop Metal, drop the whole class of bug.

## Recommended config

```python
from faster_whisper import WhisperModel
model = WhisperModel(
    "deepdml/faster-whisper-large-v3-turbo-ct2",
    device="cpu",
    compute_type="int8",   # production CPU pick
    cpu_threads=0,          # 0 = use all available
)
```

`int8` over `float32` because:
- Faster wall-clock by ~7% (3469ms vs 3729ms p50)
- WER hit vs float32 is +0.7 points — still 1+ point better than the noise
  floor on a 12-utterance sample
- Higher CPU% (388% vs 244%) is fine in a bursty workload

## Implementation scope (NOT done in this spike)

This spike is measurement-only. The code change to swap backends is:

1. `src/_1_800_operator/pipeline/audio.py`:
   - Replace `import mlx_whisper` + `mlx_whisper.transcribe(…)` with
     `from faster_whisper import WhisperModel` + `model.transcribe(…)`.
   - Constructor builds the model once, instance method calls
     `model.transcribe(audio, language="en", beam_size=5, vad_filter=False)`.
   - Result shape differs: faster-whisper returns `(segments, info)`
     where segments is a generator of segment objects; concatenate
     `.text` to match the existing string return contract.
2. `pyproject.toml`:
   - Add `faster-whisper>=1.2.0` to deps.
   - Remove `mlx-whisper` from deps.
3. `src/_1_800_operator/pipeline/doctor.py`:
   - Replace `_check_mlx_whisper_warm` (the S227-era doctor check) with
     a `_check_faster_whisper_warm` equivalent — model is cached at
     `~/.cache/huggingface/`, first-time download is ~100s, warmup ~3s.
4. `docs/agent-context.md`:
   - Update the S227/S233 HWK entries to note the migration superseded
     them. (Or leave as historical.)
5. `pipeline/aec_cleaner.py` + the `os.posix_spawn` workaround:
   - **Can this be reverted to subprocess.Popen?** Probably yes — the
     S227 fork-after-mlx hazard goes away when MLX isn't loaded. But
     the posix_spawn wrapper isn't broken either; only worth reverting
     if it materially simplifies maintenance. Probably leave it.

Estimate: 1-2 hours for the code swap, 30 min for doctor + docs, plus
live-meeting validation.

## Open follow-ups

- **Long-meeting CPU heat / fan behavior** — should be benched on a real
  1-hour meeting to confirm no thermal throttling. The 388% CPU figure
  is per-utterance burst, not sustained. Not blocking, but worth
  measuring before publishing the change.
- ~~**`beam_size` tuning** — faster-whisper default is 5, can lower to 1
  (greedy) for ~20-30% latency improvement at a small WER cost. If
  p50 latency is too high in practice, drop beam_size before increasing
  hardware demands.~~ **Resolved S240 (2026-05-17)** — re-benched at
  beam_size 1/3/5 on the same 12-utterance set (see `bench_beam_size.py`
  in this dir). Projected 20-30% latency win did not materialise:
  p50 is flat (~3450ms at every beam_size) because the turbo decoder is
  fast enough that the CPU-int8 encoder dominates wall-clock. WER is
  best at beam_size=5 (14.4% vs 15.1% at 1 and 16.4% at 3). Kept at 5,
  now sourced from `WHISPER_BEAM_SIZE` constant in `pipeline/audio.py`.
- **`vad_filter`** — faster-whisper's built-in VAD is solid; might let
  us drop operator's existing silence-based segmentation in audio.py.
  Separate spike, not part of the swap.
