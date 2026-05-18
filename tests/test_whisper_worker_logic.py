"""Unit tests for whisper_worker pure logic that moved out of attach_adapter.

When the in-process AudioProcessor path was deleted, the attribution +
bleed-dedupe + text-normalize implementations went with it (their copies
in whisper_worker.py are now the production path). This file ports the
pre-cleanup unit tests against the whisper_worker copies so regressions
get caught in CI rather than only at live-meeting time.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from _1_800_operator.pipeline.whisper_worker import (
    WhisperWorker,
    _normalize_for_dedupe,
)


def _new_worker_without_models() -> WhisperWorker:
    """Construct a WhisperWorker without triggering AudioProcessor (and
    therefore without loading the 1.5GB whisper model)."""
    w = WhisperWorker.__new__(WhisperWorker)
    w.jsonl_path = Path("/tmp/_test_whisper_worker_logic_unused.jsonl")
    w.mic_label = "user"
    from collections import deque
    import threading
    w._timeline = deque(maxlen=512)
    w._timeline_lock = threading.Lock()
    w._recent_s_captions = deque(maxlen=64)
    w._recent_s_lock = threading.Lock()
    w._shutdown_payload = None
    w._utterance_threads = []
    return w


def test_normalize_for_dedupe():
    assert _normalize_for_dedupe("Hello, World!") == "hello world"
    assert _normalize_for_dedupe("  Yes,  we  included.  ") == "yes we included"
    assert _normalize_for_dedupe("") == ""
    print("OK normalize_for_dedupe")


def test_attribute_speaker_kyle_michael_flip():
    """S235 fix: with the Kyle/Michael overlapping speaking pattern, the
    attribution must look at interval overlap with the chunk window, not
    just the most-recent speaker. Pre-S235 the live snapshot would flip
    neighboring speakers when speech 1 finalized after speech 2 started."""
    w = _new_worker_without_models()
    # Michael spoke [0, 3], Kyle spoke [3.2, 7], Michael spoke [7.2, 10]
    w._timeline.append((0.0, "Michael", "start"))
    w._timeline.append((3.0, "Michael", "stop"))
    w._timeline.append((3.2, "Kyle", "start"))
    w._timeline.append((7.0, "Kyle", "stop"))
    w._timeline.append((7.2, "Michael", "start"))
    w._timeline.append((10.0, "Michael", "stop"))
    assert w._attribute_speaker(0.0, 3.0, "other") == "Michael"
    assert w._attribute_speaker(3.2, 7.0, "other") == "Kyle"
    assert w._attribute_speaker(7.2, 10.0, "other") == "Michael"
    print("OK attribute_speaker_kyle_michael_flip")


def test_attribute_speaker_overlap_picks_dominant():
    """When two speakers overlap the chunk window, pick the one with the
    LARGEST overlap."""
    w = _new_worker_without_models()
    # A spoke [0, 2.5] (heavy overlap), B spoke [2.5, 3.5] (light overlap)
    w._timeline.append((0.0, "A", "start"))
    w._timeline.append((2.5, "A", "stop"))
    w._timeline.append((2.5, "B", "start"))
    w._timeline.append((3.5, "B", "stop"))
    assert w._attribute_speaker(0.0, 3.0, "other") == "A"
    print("OK attribute_speaker_overlap_picks_dominant")


def test_attribute_speaker_falls_back_to_default():
    """No matching interval → return the default speaker label."""
    w = _new_worker_without_models()
    # Timeline empty
    assert w._attribute_speaker(0.0, 1.0, "other") == "other"
    print("OK attribute_speaker_falls_back_to_default")


def test_attribute_speaker_falls_back_to_last_stop():
    """No overlap with chunk, but a recent speaker stopped at chunk_start
    → use that speaker (covers the Whisper-lag case)."""
    w = _new_worker_without_models()
    w._timeline.append((0.0, "A", "start"))
    w._timeline.append((1.5, "A", "stop"))
    # Chunk window [2.0, 2.5] is after A stopped — no overlap, but A is
    # the most-recent stop. Attribution returns A.
    assert w._attribute_speaker(2.0, 2.5, "other") == "A"
    print("OK attribute_speaker_falls_back_to_last_stop")


def test_bleed_dedupe_recognizes_recent_caption():
    """Recently-recorded S-leg caption fuzzy-matches against an incoming
    M-leg caption with the same content (modulo punctuation)."""
    w = _new_worker_without_models()
    w._record_s_caption("Yes, we included it in the motion.")
    # M-leg sees the same text with a trailing period swap — should match.
    assert w._is_recent_s_caption("yes we included it in the motion") is True
    print("OK bleed_dedupe_recognizes_recent_caption")


def test_bleed_dedupe_window_expires():
    """Captions older than BLEED_DEDUPE_WINDOW_SECONDS are evicted."""
    from _1_800_operator.pipeline.whisper_worker import BLEED_DEDUPE_WINDOW_SECONDS
    w = _new_worker_without_models()
    # Inject a stale entry directly (bypassing _record_s_caption's now()).
    stale_ts = time.time() - (BLEED_DEDUPE_WINDOW_SECONDS + 5)
    w._recent_s_captions.append((stale_ts, "yes we included it in the motion"))
    # Lookup should evict the stale entry and return False.
    assert w._is_recent_s_caption("yes we included it in the motion") is False
    assert len(w._recent_s_captions) == 0
    print("OK bleed_dedupe_window_expires")


def test_bleed_dedupe_distinct_text_does_not_match():
    w = _new_worker_without_models()
    w._record_s_caption("Hello everyone")
    assert w._is_recent_s_caption("This is a totally different sentence") is False
    print("OK bleed_dedupe_distinct_text_does_not_match")


if __name__ == "__main__":
    test_normalize_for_dedupe()
    test_attribute_speaker_kyle_michael_flip()
    test_attribute_speaker_overlap_picks_dominant()
    test_attribute_speaker_falls_back_to_default()
    test_attribute_speaker_falls_back_to_last_stop()
    test_bleed_dedupe_recognizes_recent_caption()
    test_bleed_dedupe_window_expires()
    test_bleed_dedupe_distinct_text_does_not_match()
    print("\nAll whisper_worker logic tests passed.")
