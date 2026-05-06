"""Throwaway 12s helper smoke — checks helper survives past the watchdog AND
both [S] / [M] streams are flowing post-codesign + Screen Recording grant.

Not a real test (won't be run in the suite). Delete after debugging."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from _1_800_operator.connectors.attach_adapter import (
    AttachAdapter,
    _FRAME_TAG_MIC,
    _FRAME_TAG_SYSTEM,
)

adapter = AttachAdapter()
adapter.set_caption_callback(lambda s, t, ts: print(f"CAPTION [{s}] {t!r}"))
adapter._start_audio_pipeline()
time.sleep(12.0)
sys_buf = len(adapter._audio_processors[_FRAME_TAG_SYSTEM]._audio_buffer)
mic_buf = len(adapter._audio_processors[_FRAME_TAG_MIC]._audio_buffer)
helper_alive = (
    adapter._audio_helper_proc is not None
    and adapter._audio_helper_proc.poll() is None
)
print(f"after 12s: helper_alive={helper_alive}, [S]_buf={sys_buf}B, [M]_buf={mic_buf}B")
adapter._stop_audio_pipeline()
