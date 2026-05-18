"""Unit smoke test for AttachAdapter's audio frame parsing + dispatch.

S244 — these tests cover the post-cleanup wiring where AttachAdapter
forwards frames to the whisper_worker subprocess via stdin. The
attribution + bleed-dedupe logic itself now lives in
src/_1_800_operator/pipeline/whisper_worker.py and is covered by spike 4
(debug/14_32_shutdown_drain_spike/spike4_worker_e2e.py) and spike 5
(spike5_attach_adapter_integration.py) end-to-end. We don't unit-test
those internals here.

Asserts:
  - reader splits frames on the [tag][BE u32 length][PCM] boundary correctly
  - 'S' frames route to _send_worker_frame; 'M' frames route to AEC or
    _send_worker_frame depending on whether AEC is up
  - _stop_audio_pipeline is idempotent (and a no-op when nothing's up)
"""
from __future__ import annotations

import io
import struct
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from _1_800_operator.connectors.attach_adapter import (
    AttachAdapter,
    _FRAME_TAG_MIC,
    _FRAME_TAG_SYSTEM,
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


class _FakeHelperProc:
    def __init__(self, stream_bytes: bytes):
        self.stdout = io.BytesIO(stream_bytes)


def test_reader_routes_s_to_worker_m_to_aec():
    """S frames → _send_worker_frame; M frames → AEC.feed_capture (since AEC up)."""
    sys_pcm = _silence(0.1)
    mic_pcm = _tone(0.1, rms=0.05)
    sys_pcm2 = _silence(0.05)
    stream = (
        _frame(_FRAME_TAG_SYSTEM, sys_pcm)
        + _frame(_FRAME_TAG_MIC, mic_pcm)
        + _frame(_FRAME_TAG_SYSTEM, sys_pcm2)
    )

    adapter = AttachAdapter()
    adapter._audio_helper_proc = _FakeHelperProc(stream)
    # AEC up — captures M frames via feed_capture; S frames also feed
    # AEC's render side as the reference signal.
    fake_aec = MagicMock()
    adapter._aec_cleaner = fake_aec
    # Spy on _send_worker_frame to capture forwards to worker.
    sent = []
    adapter._send_worker_frame = lambda tag, pcm: sent.append((tag, pcm)) or True

    t = threading.Thread(target=adapter._audio_reader_loop, daemon=True)
    t.start()
    t.join(timeout=2)
    assert not t.is_alive(), "reader did not exit on EOF"

    # S frames went to worker (S leg) + AEC render
    s_sent = [pcm for (tag, pcm) in sent if tag == _FRAME_TAG_SYSTEM]
    m_sent = [pcm for (tag, pcm) in sent if tag == _FRAME_TAG_MIC]
    assert s_sent == [sys_pcm, sys_pcm2], f"S routing wrong: {[len(p) for p in s_sent]}"
    assert m_sent == [], f"M should have gone through AEC, not worker: got {len(m_sent)}"
    assert fake_aec.feed_render.call_count == 2, "S frames should feed AEC render"
    assert fake_aec.feed_capture.call_count == 1, "M frames should feed AEC capture"
    print("OK reader_routes_s_to_worker_m_to_aec")


def test_reader_no_aec_routes_m_directly_to_worker():
    """When AEC is down, M frames go straight to worker (no bleed defense)."""
    sys_pcm = _silence(0.05)
    mic_pcm = _tone(0.1, rms=0.05)
    stream = (
        _frame(_FRAME_TAG_SYSTEM, sys_pcm)
        + _frame(_FRAME_TAG_MIC, mic_pcm)
    )

    adapter = AttachAdapter()
    adapter._audio_helper_proc = _FakeHelperProc(stream)
    adapter._aec_cleaner = None  # AEC not running
    sent = []
    adapter._send_worker_frame = lambda tag, pcm: sent.append((tag, pcm)) or True

    t = threading.Thread(target=adapter._audio_reader_loop, daemon=True)
    t.start()
    t.join(timeout=2)
    assert not t.is_alive()

    s_sent = [pcm for (tag, pcm) in sent if tag == _FRAME_TAG_SYSTEM]
    m_sent = [pcm for (tag, pcm) in sent if tag == _FRAME_TAG_MIC]
    assert s_sent == [sys_pcm]
    assert m_sent == [mic_pcm], "M frames should go directly to worker when AEC is down"
    print("OK reader_no_aec_routes_m_directly_to_worker")


def test_stop_audio_pipeline_idempotent():
    """Calling _stop_audio_pipeline when nothing's up should be a no-op."""
    adapter = AttachAdapter()
    adapter._stop_audio_pipeline()  # first call
    adapter._stop_audio_pipeline()  # idempotent second call
    print("OK stop_audio_pipeline_idempotent")


def test_update_pending_shutdown_payload_buffers_latest():
    """ChatRunner pushes attended snapshots via this; _stop_audio_pipeline reads them."""
    adapter = AttachAdapter()
    assert adapter._pending_shutdown_payload is None
    adapter.update_pending_shutdown_payload(["A", "B"], ["A"], "me")
    p = adapter._pending_shutdown_payload
    assert p["type"] == "shutdown"
    assert p["attended"] == ["A", "B"]
    assert p["currently_present"] == ["A"]
    assert p["self_name"] == "me"
    # Subsequent updates overwrite
    adapter.update_pending_shutdown_payload(["C"], [], "")
    assert adapter._pending_shutdown_payload["attended"] == ["C"]
    print("OK update_pending_shutdown_payload_buffers_latest")


def test_has_audio_worker_false_without_spawn():
    """has_audio_worker is False until _spawn_audio_worker runs."""
    adapter = AttachAdapter()
    assert adapter.has_audio_worker is False
    assert adapter._audio_worker_pid is None
    print("OK has_audio_worker_false_without_spawn")


if __name__ == "__main__":
    test_reader_routes_s_to_worker_m_to_aec()
    test_reader_no_aec_routes_m_directly_to_worker()
    test_stop_audio_pipeline_idempotent()
    test_update_pending_shutdown_payload_buffers_latest()
    test_has_audio_worker_false_without_spawn()
    print("\nAll AttachAdapter audio-wiring tests passed.")
