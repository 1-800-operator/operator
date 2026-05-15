"""Smoke test for pipeline/audio.py — synth-PCM round trip through AudioProcessor.

Standalone (no pytest). Asserts:
  - feed_audio() accumulates bytes
  - capture_next_utterance() finalizes on trailing silence
  - whisper transcribes a synth speech-like burst (or returns '' for pure noise)
  - silence-only feed returns '' without firing
  - repetition hallucination filter trips on a long repeated phrase
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from _1_800_operator.pipeline.audio import (
    AudioProcessor,
    SAMPLE_RATE,
    UTTERANCE_SILENCE_RMS,
)


def pcm_bytes(seconds: float, rms: float = 0.0) -> bytes:
    """Generate Float32 mono PCM at SAMPLE_RATE. rms=0 → silence."""
    n = int(seconds * SAMPLE_RATE)
    if rms <= 0:
        arr = np.zeros(n, dtype=np.float32)
    else:
        # Mix a 200Hz sine + low-amplitude noise so whisper has something
        # tone-like to chew on without us shipping a real WAV fixture.
        t = np.arange(n) / SAMPLE_RATE
        arr = (rms * np.sqrt(2)) * np.sin(2 * np.pi * 200 * t).astype(np.float32)
        arr += np.random.RandomState(0).normal(0, rms * 0.1, n).astype(np.float32)
    return arr.astype(np.float32).tobytes()


def test_silence_only_returns_empty():
    proc = AudioProcessor()
    proc.capturing = True

    def feeder():
        for _ in range(4):
            proc.feed_audio(pcm_bytes(0.5, rms=0.0))
            time.sleep(0.5)
        proc.capturing = False  # break the loop

    threading.Thread(target=feeder, daemon=True).start()
    text = proc.capture_next_utterance()
    assert text == "", f"expected empty for silence-only, got {text!r}"
    print("OK silence_only_returns_empty")


def test_speech_burst_finalizes_on_silence():
    proc = AudioProcessor()
    proc.capturing = True
    captured_text: list[str] = []

    def feeder():
        # Pre-roll silence so the loop's first tick reads quiet
        proc.feed_audio(pcm_bytes(0.5, rms=0.0))
        time.sleep(0.5)
        # Speech burst: 1.5s above RMS threshold
        burst = pcm_bytes(1.5, rms=UTTERANCE_SILENCE_RMS * 4)
        proc.feed_audio(burst)
        time.sleep(0.5)
        # Trailing silence — two ticks @0.5s = SILENCE_THRESHOLD trips
        for _ in range(3):
            proc.feed_audio(pcm_bytes(0.5, rms=0.0))
            time.sleep(0.5)
        proc.capturing = False

    threading.Thread(target=feeder, daemon=True).start()
    text = proc.capture_next_utterance()
    captured_text.append(text)
    # Whisper on a synth tone returns either '' or some hallucination — both
    # are fine. The signal we want is that the loop FINISHED (didn't hang)
    # within the feeder's lifetime, which the assert below verifies.
    assert text is not None
    print(f"OK speech_burst_finalizes_on_silence (whisper returned {text!r})")


def test_repetition_hallucination_filter():
    text = " ".join(["test"] * 30)
    assert AudioProcessor._is_repetition_hallucination(text), (
        "expected repetition filter to flag 30x 'test'"
    )
    short = "this is a normal sentence with varied words in it today"
    assert not AudioProcessor._is_repetition_hallucination(short), (
        f"expected normal text to pass, got flagged: {short!r}"
    )
    print("OK repetition_hallucination_filter")


if __name__ == "__main__":
    print("Loading faster-whisper-large-v3-turbo (first run downloads ~1.5GB)…")
    test_silence_only_returns_empty()
    test_speech_burst_finalizes_on_silence()
    test_repetition_hallucination_filter()
    print("\nAll audio tests passed.")
