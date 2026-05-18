"""
Spike 1: How long does end-of-meeting whisper drain actually take?

Question: When the user closes the Meet tab and we want to drain in-flight
audio through whisper before sealing the JSONL, what's our envelope?

Method: Use real-speech WAVs from prior meeting snapshots. Time the path
that an end-of-meeting residual utterance actually takes:
  - load+silence-pad+transcribe via the existing audio.py pipeline
  - measure wall-clock per utterance length

Worst case we care about: the longest utterance the buffer can hold —
UTTERANCE_MAX_DURATION = 10s. So we want measurements on clips up to ~12s
(with the 0.5s silence pad and 0.5s utterance buffer headroom).
"""
import glob
import os
import sys
import time
import wave

import numpy as np

sys.path.insert(
    0,
    os.path.expanduser(
        "~/.local/share/uv/tools/1-800-operator/lib/python3.14/site-packages"
    ),
)

from _1_800_operator.pipeline.audio import AudioProcessor, _get_model  # noqa: E402

WAV_GLOB = os.path.expanduser(
    "~/.operator/debug_snapshots/sqr-vyex-wob-2026-05-14-am/operator_audio_debug/S/utterance_*.wav"
)


def load_wav(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        assert w.getframerate() == 16000, f"expected 16kHz, got {w.getframerate()}"
        nframes = w.getnframes()
        raw = w.readframes(nframes)
    sampwidth = 4  # Float32 in our pipeline
    if len(raw) == nframes * sampwidth:
        return np.frombuffer(raw, dtype=np.float32)
    elif len(raw) == nframes * 2:
        # int16 → float32
        return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    else:
        raise ValueError(f"unexpected sample width for {path}")


def main():
    print("Loading whisper model (cold-load timing)...")
    t0 = time.perf_counter()
    _get_model()
    cold_load_s = time.perf_counter() - t0
    print(f"Cold load: {cold_load_s:.2f}s")

    wavs = sorted(glob.glob(WAV_GLOB), key=os.path.getsize)
    if not wavs:
        print(f"FAIL: no WAVs at {WAV_GLOB}")
        sys.exit(1)

    proc = AudioProcessor()

    # Pick a representative spread: shortest, median, longest.
    samples = [wavs[0], wavs[len(wavs) // 2], wavs[-1]]

    # Also construct a synthetic 10s + 12s clip by tiling the longest real
    # utterance to its max-duration bound (UTTERANCE_MAX_DURATION worst case).
    longest_audio = load_wav(wavs[-1])
    longest_duration_s = len(longest_audio) / 16000
    print(f"\nLongest real WAV: {longest_duration_s:.2f}s")
    n_tile = int(np.ceil(10.0 / longest_duration_s))
    audio_10s = np.tile(longest_audio, n_tile)[:160000]
    audio_12s = np.tile(longest_audio, n_tile + 1)[:192000]

    print(f"\n{'sample':<60} {'audio_s':>8} {'transcribe_s':>12} {'rt_ratio':>9}")
    print("-" * 100)
    for path in samples:
        audio = load_wav(path)
        dur = len(audio) / 16000
        t0 = time.perf_counter()
        text = proc.transcribe(audio)
        elapsed = time.perf_counter() - t0
        rt = elapsed / dur if dur else 0
        print(f"{os.path.basename(path):<60} {dur:>8.2f} {elapsed:>12.3f} {rt:>9.2f}x")

    for label, audio in [("synthetic_10s", audio_10s), ("synthetic_12s", audio_12s)]:
        dur = len(audio) / 16000
        # Warm-up run discarded; second run for stable measurement.
        _ = proc.transcribe(audio)
        t0 = time.perf_counter()
        _ = proc.transcribe(audio)
        elapsed = time.perf_counter() - t0
        rt = elapsed / dur if dur else 0
        print(f"{label:<60} {dur:>8.2f} {elapsed:>12.3f} {rt:>9.2f}x")

    # Worst-case parallel S+M scenario: serialised via _MODEL_USE_LOCK in
    # production. So two 10s utterances back-to-back = 2× single-leg cost.
    # Report this too.
    print("\nWorst-case end-of-meeting drain envelope (two legs, each 10s, serialised):")
    proc2 = AudioProcessor()
    t0 = time.perf_counter()
    _ = proc.transcribe(audio_10s)
    _ = proc2.transcribe(audio_10s)
    serial_elapsed = time.perf_counter() - t0
    print(f"  S+M back-to-back transcribe: {serial_elapsed:.3f}s wall-clock")
    print(
        f"\nConclusion: a Phase-1 drain wait of ~{int(serial_elapsed * 1.5)}s would cover\n"
        f"the realistic worst case with 50% headroom. (Today's 1.5s join is insufficient.)"
    )


if __name__ == "__main__":
    main()
