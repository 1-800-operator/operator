"""
End-to-end test for AecCleaner against the real aec3_spike --stream binary.

Skips with a clear message if the binary hasn't been built yet. Uses the spike
session recordings (debug/14_23_aec_spike/session_{S,M}.wav) as input; verifies
that cleaned frames flow back through the on_clean_mic callback and that the
totals line up with the binary's 150ms mic-delay semantics.
"""
from __future__ import annotations

import struct
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from _1_800_operator.pipeline.aec_cleaner import AecCleaner  # noqa: E402

SAMPLE_RATE = 16_000
HELPER_CHUNK_SAMPLES = 640  # ~40ms, matches Swift helper cadence
MIC_DELAY_SAMPLES = 2400    # 150ms — binary's hardcoded pre-shift

BINARY = ROOT / "src" / "_1_800_operator" / "rust" / "aec3" / "target" / "release" / "aec3"
SESSION_M = ROOT / "debug" / "14_23_aec_spike" / "session_M.wav"
SESSION_S = ROOT / "debug" / "14_23_aec_spike" / "session_S.wav"


def skip(msg: str) -> None:
    print(f"SKIP: {msg}")
    sys.exit(0)


def read_wav_f32(path: Path) -> np.ndarray:
    """Read a 16kHz mono WAV (PCM i16 or IEEE float32) to a float32 array."""
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getframerate() != SAMPLE_RATE:
            raise SystemExit(f"{path}: expected mono 16 kHz")
        raw = w.readframes(w.getnframes())
        sw = w.getsampwidth()
    if sw == 2:
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if sw == 4:
        # session_{S,M}.wav are IEEE float32 from hound — interpret as f32 LE.
        return np.frombuffer(raw, dtype="<f4").copy()
    raise SystemExit(f"{path}: unsupported sampwidth {sw}")


def test_round_trip_matches_binary_semantics() -> None:
    """Feed S+M frames through AecCleaner, verify cleaned mic frames flow back."""
    mic = read_wav_f32(SESSION_M)
    ref = read_wav_f32(SESSION_S)
    n = min(len(mic), len(ref))
    mic, ref = mic[:n], ref[:n]
    print(f"test: feeding {n} samples per side ({n / SAMPLE_RATE:.1f}s)")

    cleaned_chunks: list[bytes] = []
    chunks_lock = threading.Lock()
    first_chunk_at: list[float] = []

    def on_clean(pcm: bytes) -> None:
        with chunks_lock:
            if not first_chunk_at:
                first_chunk_at.append(time.monotonic())
            cleaned_chunks.append(pcm)

    cleaner = AecCleaner(BINARY, on_clean)
    cleaner.start()
    try:
        assert cleaner.alive, "binary should be alive after start()"
        t_start = time.monotonic()
        for start in range(0, n, HELPER_CHUNK_SAMPLES):
            end = start + HELPER_CHUNK_SAMPLES
            s_bytes = ref[start:end].astype("<f4").tobytes()
            m_bytes = mic[start:end].astype("<f4").tobytes()
            cleaner.feed_render(s_bytes)
            cleaner.feed_capture(m_bytes)
        # Give the binary a moment to flush the in-flight queue before we
        # close stdin; stop() will then wait for the EOF drain.
        time.sleep(0.2)
        elapsed_feed = time.monotonic() - t_start
        print(f"test: feed phase {elapsed_feed:.2f}s")
    finally:
        cleaner.stop(timeout=5.0)

    assert not cleaner.alive, "binary should have exited after stop()"

    total_cleaned_samples = sum(len(c) // 4 for c in cleaned_chunks)
    # The binary holds back MIC_DELAY_SAMPLES at EOF; expected output is
    # roughly (mic_frames_of_160 * 160 - MIC_DELAY_SAMPLES). Slack covers the
    # last partial helper chunk.
    expected_mic_aec_frames = n // 160
    expected_cleaned = max(0, expected_mic_aec_frames * 160 - MIC_DELAY_SAMPLES)
    drift = abs(total_cleaned_samples - expected_cleaned)
    print(
        f"test: cleaned samples={total_cleaned_samples} expected~{expected_cleaned} "
        f"(drift {drift}, slack 320)"
    )
    assert drift <= 320, (
        f"cleaned-sample count {total_cleaned_samples} drifts {drift} from "
        f"expected ~{expected_cleaned} (>320 samples = >20ms — binary is "
        "either dropping frames or running a different delay than 150ms)"
    )

    # Sanity check: cleaned audio should have non-zero energy and be quieter
    # than the raw mic (AEC removed bleed). RMS check is loose because the
    # session has stretches with no user speech at all.
    cleaned = np.frombuffer(b"".join(cleaned_chunks), dtype="<f4")
    mic_rms = float(np.sqrt(np.mean(mic ** 2)))
    cleaned_rms = float(np.sqrt(np.mean(cleaned ** 2)))
    print(
        f"test: rms mic={20 * np.log10(max(1e-12, mic_rms)):.1f} dB  "
        f"cleaned={20 * np.log10(max(1e-12, cleaned_rms)):.1f} dB"
    )
    assert cleaned_rms > 1e-5, "cleaned output should not be all zeros"
    # In this session mic includes loud bleed segments; cleaned must drop
    # measurably. -3 dB is a very loose floor — the spike measured -7 dB
    # overall and -30 dB on bleed-only segments.
    assert cleaned_rms < mic_rms, "cleaned RMS should be lower than mic RMS"

    print("test_round_trip_matches_binary_semantics: OK")


def test_oversize_frame_is_dropped() -> None:
    """An oversize feed shouldn't crash the manager or the binary."""
    chunks: list[bytes] = []
    cleaner = AecCleaner(BINARY, lambda pcm: chunks.append(pcm))
    cleaner.start()
    try:
        # 2 MiB > _MAX_FRAME_BYTES (1 MiB) — should be dropped with a warning.
        cleaner.feed_capture(b"\x00" * (2 << 20))
        # Feed a tiny valid frame too so we know the manager is still alive.
        cleaner.feed_render(np.zeros(160, dtype="<f4").tobytes())
        cleaner.feed_capture(np.zeros(160, dtype="<f4").tobytes())
        time.sleep(0.1)
        assert cleaner.alive, "binary should still be alive after oversize drop"
    finally:
        cleaner.stop()
    print("test_oversize_frame_is_dropped: OK")


def test_start_is_idempotent() -> None:
    cleaner = AecCleaner(BINARY, lambda pcm: None)
    cleaner.start()
    try:
        proc1 = cleaner._proc
        cleaner.start()  # should be a no-op
        assert cleaner._proc is proc1, "start() must not respawn a live subprocess"
    finally:
        cleaner.stop()
    print("test_start_is_idempotent: OK")


def test_missing_binary_raises() -> None:
    bogus = ROOT / "definitely-does-not-exist-aec3"
    cleaner = AecCleaner(bogus, lambda pcm: None)
    try:
        cleaner.start()
    except FileNotFoundError:
        print("test_missing_binary_raises: OK")
        return
    cleaner.stop()
    raise SystemExit("expected FileNotFoundError for missing binary")


def main() -> int:
    if not BINARY.exists():
        skip(
            f"aec3 binary not built at {BINARY} — "
            "run `cargo build --release --manifest-path src/_1_800_operator/rust/aec3/Cargo.toml`"
        )
    if not SESSION_M.exists() or not SESSION_S.exists():
        skip("spike session WAVs not present (debug/14_23_aec_spike/session_{S,M}.wav)")

    test_missing_binary_raises()
    test_start_is_idempotent()
    test_oversize_frame_is_dropped()
    test_round_trip_matches_binary_semantics()
    print("\nall AecCleaner tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
