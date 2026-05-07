"""30s mic pipeline test with stdout logging.

Run this, speak a sentence within the first 25s, leave at least 1s of
silence at the end, and watch for AudioProcessor lifecycle lines +
CAPTION output.
"""
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(name)s %(message)s",
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from _1_800_operator.connectors.attach_adapter import (
    AttachAdapter,
    _FRAME_TAG_MIC,
    _FRAME_TAG_SYSTEM,
)

adapter = AttachAdapter()
adapter.set_caption_callback(
    lambda s, t, ts: print(f"\n>>> CAPTION [{s}] {t!r}\n", flush=True)
)
print(">>> starting pipeline; speak a sentence within 25s, then stay silent")
adapter._start_audio_pipeline()
time.sleep(30.0)
print(">>> stopping pipeline")
adapter._stop_audio_pipeline()
print(">>> done")
