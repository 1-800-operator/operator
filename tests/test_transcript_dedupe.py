"""
TranscriptFinalizer per-speaker prefix dedupe.

Meet keeps a rolling window of recent caption text per speaker visible in
the same DOM region, so each finalize re-emits whatever Meet hasn't scrolled
off yet. Without dedupe, a same-speaker continuation gets persisted as
"prior + new" instead of just "new". Meet also auto-corrects casing and
punctuation on prior text between finalizes ("here," -> "Here."), so the
prefix check has to be tolerant of that.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _1_800_operator.pipeline.meeting_record import MeetingRecord
from _1_800_operator.pipeline.transcript import TranscriptFinalizer, _strip_prior_prefix


def test_strip_exact_prefix():
    out = _strip_prior_prefix("hello world how are you", "hello world")
    assert out == "how are you", repr(out)
    print("PASS: exact prefix stripped")


def test_strip_prefix_with_punct_and_case_drift():
    # Meet rewrote "here," -> "Here." between finalizes
    prior = "here, this is a test for"
    new = "Here. This is a test for saying something out loud."
    out = _strip_prior_prefix(new, prior)
    assert out == "saying something out loud.", repr(out)
    print("PASS: prefix dedupe tolerant of casing + punctuation drift")


def test_no_prefix_means_no_strip():
    # Meet rolled the window — prior is no longer at the start
    out = _strip_prior_prefix("completely different text", "earlier sentence")
    assert out == "completely different text", repr(out)
    print("PASS: non-matching prior leaves text unchanged")


def test_identical_text_returns_empty():
    out = _strip_prior_prefix("Hello, from the other side.", "hello from the other side")
    assert out == "", repr(out)
    print("PASS: identical text after normalization returns empty")


def test_empty_prior_passthrough():
    out = _strip_prior_prefix("first utterance", "")
    assert out == "first utterance", repr(out)
    print("PASS: empty prior returns text unchanged")


def test_finalizer_dedupes_same_speaker_continuation():
    with tempfile.TemporaryDirectory() as tmp:
        rec = MeetingRecord(slug="t", root=__import__('pathlib').Path(tmp))
        tf = TranscriptFinalizer(rec, silence_seconds=99.0)  # silence loop won't fire
        tf.on_caption_update("Alice", "hello world", 1000.0)
        tf.stop()  # flushes via reason=stop
        # Re-arm and feed a continuation
        tf2 = TranscriptFinalizer(rec, silence_seconds=99.0)
        # Re-use same dedupe state by reading in same TF — instead, simulate two
        # finalizes on the same TF via speaker changes:
        tf2._last_window_per_speaker["Alice"] = "hello world"
        tf2.on_caption_update("Alice", "Hello, world. how are you", 1001.0)
        tf2.stop()

    rows = [r for r in rec.tail(10) if r.get("kind") == "caption"]
    assert len(rows) == 2, [r["text"] for r in rows]
    assert rows[0]["text"] == "hello world", rows[0]
    assert rows[1]["text"] == "how are you", rows[1]
    print("PASS: same-speaker continuation persists only the new suffix")


def test_finalizer_resets_per_speaker():
    with tempfile.TemporaryDirectory() as tmp:
        rec = MeetingRecord(slug="t2", root=__import__('pathlib').Path(tmp))
        tf = TranscriptFinalizer(rec, silence_seconds=99.0)
        tf._last_window_per_speaker["Alice"] = "alice said this"
        # Bob's first utterance must NOT be deduped against Alice's window
        tf.on_caption_update("Bob", "alice said this is what bob also says", 2000.0)
        tf.stop()

    rows = [r for r in rec.tail(10) if r.get("kind") == "caption"]
    assert len(rows) == 1
    assert rows[0]["sender"] == "Bob"
    assert rows[0]["text"] == "alice said this is what bob also says", rows[0]
    print("PASS: dedupe state is per-speaker, not shared across speakers")


if __name__ == "__main__":
    test_strip_exact_prefix()
    test_strip_prefix_with_punct_and_case_drift()
    test_no_prefix_means_no_strip()
    test_identical_text_returns_empty()
    test_empty_prior_passthrough()
    test_finalizer_dedupes_same_speaker_continuation()
    test_finalizer_resets_per_speaker()
    print("\nAll transcript dedupe tests passed.")
