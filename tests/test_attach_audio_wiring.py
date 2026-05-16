"""Unit smoke test for AttachAdapter's audio frame parsing + caption dispatch.

Mocks the helper subprocess with a BytesIO stdout containing a hand-rolled
frame stream. Asserts:
  - reader splits frames on the [tag][BE u32 length][PCM] boundary correctly
  - 'S' frames → other-speaker processor, 'M' frames → user-speaker processor
  - utterance loop fires the caption callback with the right speaker label
  - tear-down stops both threads cleanly
"""
from __future__ import annotations

import io
import struct
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from _1_800_operator.connectors.attach_adapter import (
    AttachAdapter,
    _FRAME_TAG_MIC,
    _FRAME_TAG_SYSTEM,
    _SPEAKER_OTHER,
    _SPEAKER_USER_FALLBACK,
)


SAMPLE_RATE = 16000


def _frame(tag: bytes, pcm: bytes) -> bytes:
    return tag + struct.pack(">I", len(pcm)) + pcm


def _silence(seconds: float) -> bytes:
    return np.zeros(int(seconds * SAMPLE_RATE), dtype=np.float32).tobytes()


def _tone(seconds: float, rms: float) -> bytes:
    n = int(seconds * SAMPLE_RATE)
    t = np.arange(n) / SAMPLE_RATE
    arr = (rms * np.sqrt(2)) * np.sin(2 * np.pi * 200 * t).astype(np.float32)
    return arr.astype(np.float32).tobytes()


def test_reader_routes_by_tag():
    """Hand-roll three frames into a BytesIO, run reader, assert each
    processor's feed_audio got the right bytes."""
    sys_pcm = _silence(0.1)
    mic_pcm = _tone(0.1, rms=0.05)
    sys_pcm2 = _silence(0.05)
    stream = (
        _frame(_FRAME_TAG_SYSTEM, sys_pcm)
        + _frame(_FRAME_TAG_MIC, mic_pcm)
        + _frame(_FRAME_TAG_SYSTEM, sys_pcm2)
    )

    adapter = AttachAdapter()
    adapter._audio_helper_proc = MagicMock()
    adapter._audio_helper_proc.stdout = io.BytesIO(stream)

    sys_proc = MagicMock()
    mic_proc = MagicMock()
    adapter._audio_processors = {
        _FRAME_TAG_SYSTEM: sys_proc,
        _FRAME_TAG_MIC: mic_proc,
    }

    adapter._audio_reader_loop()

    sys_calls = [c.args[0] for c in sys_proc.feed_audio.call_args_list]
    mic_calls = [c.args[0] for c in mic_proc.feed_audio.call_args_list]
    assert sys_calls == [sys_pcm, sys_pcm2], (
        f"system processor got wrong frames: lengths={[len(c) for c in sys_calls]}"
    )
    assert mic_calls == [mic_pcm], (
        f"mic processor got wrong frames: lengths={[len(c) for c in mic_calls]}"
    )
    print("OK reader_routes_by_tag")


def test_utterance_loop_fires_callback_with_speaker_label():
    """Drive _audio_utterance_loop with a fake processor whose
    capture_next_utterance returns 'hello' once then stops.
    Assert the callback fires with speaker='user' and the right text.
    """
    adapter = AttachAdapter()
    fake_proc = MagicMock()
    fake_proc.capturing = True

    call_count = {"n": 0}

    def fake_capture(**_):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return "hello world", time.time()
        # Second call: signal stop, return empty
        fake_proc.capturing = False
        return "", None

    fake_proc.capture_next_utterance.side_effect = fake_capture
    adapter._audio_processors[_FRAME_TAG_MIC] = fake_proc

    callback_calls: list[tuple] = []

    def cb(speaker, text, ts):
        callback_calls.append((speaker, text, ts))

    adapter.set_caption_callback(cb)

    t = threading.Thread(
        target=adapter._audio_utterance_loop,
        args=(_FRAME_TAG_MIC, _SPEAKER_USER_FALLBACK),
        daemon=True,
    )
    t.start()
    t.join(timeout=2.0)
    assert not t.is_alive(), "utterance loop didn't exit when capturing flipped False"
    assert len(callback_calls) == 1, f"expected 1 callback, got {len(callback_calls)}"
    speaker, text, ts = callback_calls[0]
    assert speaker == _SPEAKER_USER_FALLBACK, f"wrong speaker: {speaker}"
    assert text == "hello world", f"wrong text: {text!r}"
    assert ts > 0
    print("OK utterance_loop_fires_callback_with_speaker_label")


def test_stop_audio_pipeline_idempotent():
    """Calling _stop_audio_pipeline twice must not raise."""
    adapter = AttachAdapter()
    adapter._stop_audio_pipeline()  # nothing to stop
    adapter._stop_audio_pipeline()  # still nothing to stop
    print("OK stop_audio_pipeline_idempotent")


def test_other_speaker_label():
    """Same as the user test, but verify [S] tag → 'other' speaker."""
    adapter = AttachAdapter()
    fake_proc = MagicMock()
    fake_proc.capturing = True

    seen = []

    def fake_capture(**_):
        if not seen:
            seen.append(1)
            return "remote talker", time.time()
        fake_proc.capturing = False
        return "", None

    fake_proc.capture_next_utterance.side_effect = fake_capture
    adapter._audio_processors[_FRAME_TAG_SYSTEM] = fake_proc

    callback_calls: list[tuple] = []
    adapter.set_caption_callback(lambda s, t, ts: callback_calls.append((s, t, ts)))

    t = threading.Thread(
        target=adapter._audio_utterance_loop,
        args=(_FRAME_TAG_SYSTEM, _SPEAKER_OTHER),
        daemon=True,
    )
    t.start()
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert len(callback_calls) == 1
    assert callback_calls[0][0] == _SPEAKER_OTHER
    assert callback_calls[0][1] == "remote talker"
    print("OK other_speaker_label")


def test_bleed_dedupe_drops_matching_mic_caption():
    """An M-leg caption that fuzzy-matches a recent S-leg caption is dropped."""
    adapter = AttachAdapter()

    # Seed an S-leg caption into the rolling buffer.
    adapter._record_s_caption("Yes, we included in the motion.")

    # An M-leg caption with the same content (punct/case drift) should match.
    assert adapter._is_recent_s_caption("yes we included in the motion") is True
    # A near-miss (one letter different word) still matches at threshold 0.75.
    assert adapter._is_recent_s_caption("Yes, we included in the emotion.") is True
    # A completely different caption does NOT match.
    assert adapter._is_recent_s_caption("Don't worry about that.") is False
    # An empty caption does NOT match (would otherwise compare against an
    # empty normalized form and ratio against short strings can spike).
    assert adapter._is_recent_s_caption("") is False
    print("OK bleed_dedupe_drops_matching_mic_caption")


def test_attribute_s_leg_kyle_michael_flip():
    """Exact reproduction of the dko-pgom-bfe.jsonl flip pattern.

    Michael speaks 0..3, Kyle 3.2..7, Michael 7.2..10. Whisper would
    finalize each segment ~500ms after speech_stop — by which point
    the next speaker has already started. The old logic attributed
    by "who is speaking now"; the new one attributes by chunk window.
    """
    adapter = AttachAdapter()
    history = [
        (0.0,  "Michael", "start"),
        (3.0,  "Michael", "stop"),
        (3.2,  "Kyle",    "start"),
        (7.0,  "Kyle",    "stop"),
        (7.2,  "Michael", "start"),
        (10.0, "Michael", "stop"),
    ]
    for ev in history:
        adapter._speaking_history.append(ev)

    # Three finalizations, each timed AFTER the next speaker started.
    assert adapter._attribute_s_leg(0.0, 3.0, "other") == "Michael"
    assert adapter._attribute_s_leg(3.2, 7.0, "other") == "Kyle"
    assert adapter._attribute_s_leg(7.2, 10.0, "other") == "Michael"
    print("OK attribute_s_leg_kyle_michael_flip")


def test_attribute_s_leg_overlap_picks_dominant():
    """When two speakers overlap, the one with larger overlap wins."""
    adapter = AttachAdapter()
    for ev in [
        (0.0, "A", "start"),
        (2.5, "B", "start"),   # B cuts in
        (3.0, "A", "stop"),
        (3.5, "B", "stop"),
    ]:
        adapter._speaking_history.append(ev)
    # Chunk covers A's full 3s window; B only overlaps 0.5s of it.
    assert adapter._attribute_s_leg(0.0, 3.0, "other") == "A"
    print("OK attribute_s_leg_overlap_picks_dominant")


def test_attribute_s_leg_falls_back_to_default():
    """Empty history → returns the caller-provided default."""
    adapter = AttachAdapter()
    assert adapter._attribute_s_leg(0.0, 1.0, "other") == "other"
    print("OK attribute_s_leg_falls_back_to_default")


def test_attribute_s_leg_falls_back_to_last_stop():
    """Chunk lands in silence after a known speaker stopped → that speaker."""
    adapter = AttachAdapter()
    for ev in [
        (0.0, "A", "start"),
        (1.0, "A", "stop"),
    ]:
        adapter._speaking_history.append(ev)
    # Chunk window 2.0-2.5 has no overlap with any interval; nearest
    # prior stop is A at t=1.0.
    assert adapter._attribute_s_leg(2.0, 2.5, "other") == "A"
    print("OK attribute_s_leg_falls_back_to_last_stop")


def test_bleed_dedupe_window_expires():
    """S-leg captions older than the configured window stop matching."""
    import time as _time
    from _1_800_operator import config as _config

    adapter = AttachAdapter()
    # Manually backfill an old entry past the window.
    stale_ts = _time.time() - (_config.BLEED_DEDUPE_WINDOW_SECONDS + 1.0)
    adapter._recent_s_captions.append((stale_ts, "yes we included in the motion"))

    # A current M-leg caption with matching text should NOT dedupe because
    # the only candidate is stale and gets evicted on lookup.
    assert adapter._is_recent_s_caption("Yes, we included in the motion.") is False
    # And the eviction should have actually removed the stale entry.
    assert len(adapter._recent_s_captions) == 0
    print("OK bleed_dedupe_window_expires")


if __name__ == "__main__":
    test_reader_routes_by_tag()
    test_utterance_loop_fires_callback_with_speaker_label()
    test_other_speaker_label()
    test_stop_audio_pipeline_idempotent()
    test_bleed_dedupe_drops_matching_mic_caption()
    test_bleed_dedupe_window_expires()
    test_attribute_s_leg_kyle_michael_flip()
    test_attribute_s_leg_overlap_picks_dominant()
    test_attribute_s_leg_falls_back_to_default()
    test_attribute_s_leg_falls_back_to_last_stop()
    print("\nAll AttachAdapter audio-wiring tests passed.")
