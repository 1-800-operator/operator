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
            return "hello world"
        # Second call: signal stop, return empty
        fake_proc.capturing = False
        return ""

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
            return "remote talker"
        fake_proc.capturing = False
        return ""

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


if __name__ == "__main__":
    test_reader_routes_by_tag()
    test_utterance_loop_fires_callback_with_speaker_label()
    test_other_speaker_label()
    test_stop_audio_pipeline_idempotent()
    print("\nAll AttachAdapter audio-wiring tests passed.")
