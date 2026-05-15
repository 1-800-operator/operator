"""Live smoke for AttachAdapter._start_audio_pipeline against the real helper.

Spawns operator-audio-capture, lets it stream for ~3s (under the 10s no-callback
watchdog), counts frames received per stream, then stops cleanly. NOT a unit
test — requires Screen Recording + Microphone TCC granted to the parent
terminal, otherwise the helper exits with code 4/5 and the smoke fails fast.

Run: python tests/test_helper_spawn_smoke.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from _1_800_operator.connectors.attach_adapter import (
    AttachAdapter,
    _FRAME_TAG_MIC,
    _FRAME_TAG_SYSTEM,
    _resolve_audio_helper,
)


def main() -> int:
    helper = _resolve_audio_helper()
    if helper is None:
        print("FAIL: operator-audio-capture not found. Build with `swiftc src/_1_800_operator/swift/operator-audio-capture.swift -O -o src/_1_800_operator/swift/operator-audio-capture`")
        return 1
    print(f"helper: {helper}")

    adapter = AttachAdapter()
    captured: list[tuple] = []
    adapter.set_caption_callback(lambda s, t, ts: captured.append((s, t, ts)))

    print("warming AudioProcessor + spawning helper (this loads faster-whisper-large-v3-turbo)…")
    t0 = time.monotonic()
    adapter._start_audio_pipeline()
    warm_ms = (time.monotonic() - t0) * 1000
    print(f"pipeline up in {warm_ms:.0f}ms")

    if adapter._audio_helper_proc is None:
        print("FAIL: helper subprocess wasn't spawned (check log warnings above)")
        return 1
    if not adapter._audio_processors:
        print("FAIL: AudioProcessors didn't init")
        return 1

    SAMPLE_SECONDS = 3.0
    print(f"streaming for {SAMPLE_SECONDS}s (speak into the mic if you want to see a transcription)…")
    time.sleep(SAMPLE_SECONDS)

    sys_buf_len = len(adapter._audio_processors[_FRAME_TAG_SYSTEM]._audio_buffer)
    mic_buf_len = len(adapter._audio_processors[_FRAME_TAG_MIC]._audio_buffer)
    print(f"buffers at stop: [S]={sys_buf_len}B [M]={mic_buf_len}B")

    adapter._stop_audio_pipeline()
    print(f"captioned utterances: {len(captured)}")
    for s, t, ts in captured:
        print(f"  [{s}] {t!r}")

    if sys_buf_len == 0 and mic_buf_len == 0:
        print("FAIL: no frames flowed from either stream — TCC silent failure?")
        print("Check System Settings → Privacy & Security → {Screen Recording, Microphone}")
        print("Operator's parent terminal must be in the granted list. Quit + relaunch the terminal after granting.")
        return 1

    print("OK: helper streamed real frames into AttachAdapter")
    return 0


if __name__ == "__main__":
    sys.exit(main())
