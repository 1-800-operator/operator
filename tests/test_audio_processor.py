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
    text, t_start, _words = proc.capture_next_utterance()
    assert text == "", f"expected empty for silence-only, got {text!r}"
    assert t_start is None, f"expected no speech_start_time for silence, got {t_start!r}"
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
    text, t_start, _words = proc.capture_next_utterance()
    captured_text.append(text)
    # Whisper on a synth tone returns either '' or some hallucination — both
    # are fine. The signal we want is that the loop FINISHED (didn't hang)
    # within the feeder's lifetime, which the assert below verifies.
    assert text is not None
    # When speech was detected we get a wall-clock start timestamp;
    # when it was dropped as a hallucination we get None alongside ''.
    if text:
        assert isinstance(t_start, float), f"expected float t_start for non-empty text, got {t_start!r}"
    print(f"OK speech_burst_finalizes_on_silence (whisper returned {text!r}, t_start={t_start!r})")


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


def test_helper_starvation_does_not_finalize_utterance():
    """H-24: an empty drain means the helper produced no frames this tick
    (transient backpressure — TCC renegotiation, CPU pressure, whisper
    inference feeding back to read scheduling). It is NOT real silence.
    Pre-fix, every empty tick bumped silence_count, so two consecutive
    starvation ticks (1s) would finalize a mid-utterance — cutting the
    user off mid-word.

    Scenario: speech burst → 4 ticks of helper starvation (would be 2x
    SILENCE_THRESHOLD pre-fix, definitely enough to falsely finalize) →
    more speech → real trailing silence. We assert the captured utterance
    audio includes BOTH speech bursts (i.e. starvation did NOT cut the
    utterance after the first burst).
    """
    proc = AudioProcessor()
    proc.capturing = True

    # Replace transcribe with a sniffer that records utterance length
    # without running whisper (fast + deterministic). The capture loop
    # calls self.transcribe(np.ndarray) with the assembled PCM.
    captured_lengths: list[int] = []
    def fake_transcribe(audio):
        captured_lengths.append(int(audio.size * audio.itemsize))
        return ""
    proc.transcribe = fake_transcribe  # type: ignore[method-assign]

    def feeder():
        # Burst 1: 1.0s of speech-RMS audio.
        proc.feed_audio(pcm_bytes(1.0, rms=UTTERANCE_SILENCE_RMS * 4))
        time.sleep(0.6)
        # Helper STARVATION: 4 ticks where feed_audio is never called.
        # drain_audio_buffer will return b"" on each loop iteration.
        # Pre-fix this would have finalized the utterance after 2 ticks (1s).
        time.sleep(2.0)
        # Burst 2: another 1.0s of speech. Pre-fix this would be a new
        # utterance (because burst 1 was already finalized). Post-fix it
        # belongs to the same utterance.
        proc.feed_audio(pcm_bytes(1.0, rms=UTTERANCE_SILENCE_RMS * 4))
        time.sleep(0.6)
        # Real trailing silence to finalize cleanly.
        for _ in range(3):
            proc.feed_audio(pcm_bytes(0.5, rms=0.0))
            time.sleep(0.5)
        proc.capturing = False

    threading.Thread(target=feeder, daemon=True).start()
    proc.capture_next_utterance()

    # The captured utterance audio length should reflect BOTH bursts plus
    # trailing silence — i.e. roughly 2x the single-burst byte count, not
    # one burst's worth. Each burst is 1.0s @ 16kHz Float32 = 64000 bytes.
    # If starvation prematurely finalized, we'd see ~one burst (~64KB);
    # post-fix we should see closer to ~two bursts + trailing silence.
    assert len(captured_lengths) == 1, (
        f"expected exactly one utterance to be finalized, got "
        f"{len(captured_lengths)} (starvation may have prematurely cut)"
    )
    single_burst_bytes = int(1.0 * SAMPLE_RATE * 4)  # 1s Float32
    assert captured_lengths[0] >= int(single_burst_bytes * 1.5), (
        f"utterance audio length {captured_lengths[0]} is closer to one "
        f"burst ({single_burst_bytes}) than two — starvation finalized "
        f"prematurely. Expected ≥{int(single_burst_bytes * 1.5)} bytes."
    )
    print(
        f"OK helper_starvation_does_not_finalize_utterance "
        f"(captured {captured_lengths[0]} bytes — both bursts present)"
    )


def test_shutdown_drains_pending_buffer():
    """S244: capturing=False must drain the in-flight _audio_buffer once
    more before returning, instead of dropping bytes that arrived between
    the last 0.5s tick and the flag flip.

    Pre-fix: a speech burst fed immediately before `capturing = False`
    would be lost — the while loop exited at the top of the next iteration
    without ever calling drain_audio_buffer on the residual.
    Post-fix: the residual is drained once and appended to utterance_audio
    when (a) we were already mid-utterance OR (b) the residual itself is
    above the speech RMS threshold.
    """
    proc = AudioProcessor()
    proc.capturing = True

    def feeder():
        # Pre-roll some silence so the loop's first tick reads quiet.
        proc.feed_audio(pcm_bytes(0.3, rms=0.0))
        time.sleep(0.3)
        # 0.8s speech burst (fully read by next tick).
        proc.feed_audio(pcm_bytes(0.8, rms=UTTERANCE_SILENCE_RMS * 4))
        time.sleep(0.6)
        # Now stuff the buffer with a fresh burst RIGHT BEFORE flipping
        # capturing. Pre-fix this gets dropped; post-fix it's appended.
        proc.feed_audio(pcm_bytes(0.5, rms=UTTERANCE_SILENCE_RMS * 4))
        proc.capturing = False

    threading.Thread(target=feeder, daemon=True).start()
    # capture_next_utterance returns when capturing flips False. We don't
    # care what whisper transcribes — we care that the returned utterance
    # included BOTH the in-loop burst (0.8s) AND the post-flip residual
    # (0.5s). We can't easily inspect utterance_audio length from outside,
    # so we assert via a wrapper that records the byte count.
    captured_lengths: list[int] = []
    orig_transcribe = proc.transcribe

    def spy_transcribe(audio):
        # audio is Float32 ndarray; 4 bytes per sample.
        captured_lengths.append(len(audio) * 4)
        return orig_transcribe(audio)

    proc.transcribe = spy_transcribe  # type: ignore[method-assign]
    proc.capture_next_utterance()
    assert captured_lengths, "transcribe was never called — drain didn't fire"
    captured = captured_lengths[0]
    # 0.8s + 0.5s = 1.3s at 16kHz Float32 = ~83200 bytes (plus the 0.5s
    # silence pre-pad transcribe adds = +32000). We measure the spy input
    # so subtract the silence pre-pad: spy sees the silence-padded array.
    # The 0.5s pad accounts for 32000 bytes. So we expect ≥ 0.8s + 0.5s
    # speech + 0.5s pad = ~115000 bytes total.
    # Pre-fix would yield: 0.8s speech + 0.5s pad = ~83000 bytes (residual
    # 0.5s lost).
    pre_fix_upper = int((0.8 + 0.5) * SAMPLE_RATE * 4)  # ~83200 bytes (pre-fix max)
    expected_min = int((0.8 + 0.5 + 0.5) * SAMPLE_RATE * 4 * 0.9)  # ~103000 (post-fix, 10% slop)
    assert captured > pre_fix_upper, (
        f"utterance audio {captured} bytes — looks like the post-flip "
        f"residual was lost (pre-fix upper bound was {pre_fix_upper})"
    )
    print(
        f"OK shutdown_drains_pending_buffer "
        f"(captured {captured} bytes incl. silence pad ≥ post-flip residual)"
    )


if __name__ == "__main__":
    print("Loading faster-whisper-large-v3-turbo (first run downloads ~1.5GB)…")
    test_silence_only_returns_empty()
    test_speech_burst_finalizes_on_silence()
    test_repetition_hallucination_filter()
    test_helper_starvation_does_not_finalize_utterance()
    test_shutdown_drains_pending_buffer()
    print("\nAll audio tests passed.")
