"""
Unit tests for the paragraph-flush helper used by the streaming providers.

Run:
    source venv/bin/activate
    python tests/test_streaming_paragraph_flush.py
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _1_800_operator.pipeline.providers.base import flush_paragraphs


def _collect():
    """Helper: returns (callback, posted_list)."""
    posted = []
    return posted.append, posted


def test_simple_two_paragraph_split():
    cb, posted = _collect()
    remainder = flush_paragraphs("hello\n\nworld", cb)
    assert posted == ["hello"], posted
    assert remainder == "world", remainder


def test_no_boundary_yet_keeps_buffer():
    cb, posted = _collect()
    remainder = flush_paragraphs("partial sentence with no break", cb)
    assert posted == [], posted
    assert remainder == "partial sentence with no break"


def test_force_final_flushes_remainder():
    cb, posted = _collect()
    remainder = flush_paragraphs("only one paragraph", cb, force_final=True)
    assert posted == ["only one paragraph"], posted
    assert remainder == "", remainder


def test_decoration_only_fragments_dropped():
    cb, posted = _collect()
    flush_paragraphs("real text\n\n---\n\nmore real", cb, force_final=True)
    assert posted == ["real text", "more real"], posted


def test_empty_fragments_dropped():
    cb, posted = _collect()
    flush_paragraphs("a\n\n\n\nb\n\n   \n\nc", cb, force_final=True)
    assert posted == ["a", "b", "c"], posted


def test_three_or_more_newlines_treated_as_boundary():
    cb, posted = _collect()
    flush_paragraphs("first\n\n\nsecond\n\n\n\nthird", cb, force_final=True)
    assert posted == ["first", "second", "third"], posted


def test_mixed_decorations_dropped():
    cb, posted = _collect()
    flush_paragraphs("alpha\n\n***\n\nbeta\n\n===\n\ngamma", cb, force_final=True)
    assert posted == ["alpha", "beta", "gamma"], posted


def test_partial_paragraph_not_flushed_until_force():
    cb, posted = _collect()
    # First call has no boundary in trailing chunk — keep as remainder.
    rem = flush_paragraphs("complete one\n\ngrowing", cb)
    assert posted == ["complete one"], posted
    assert rem == "growing"
    # Caller appends more text and re-flushes; still no boundary.
    rem2 = flush_paragraphs(rem + " still more", cb)
    assert posted == ["complete one"], posted
    assert rem2 == "growing still more"
    # Final flush.
    flush_paragraphs(rem2, cb, force_final=True)
    assert posted == ["complete one", "growing still more"], posted


def test_real_walkthrough_shape():
    """Mimics the test 1.1 reply that posted as one wall."""
    text = (
        "Great! I've traced the chat polling loop. Let me walk you through it step by step:\n\n"
        "---\n\n"
        "**Entry point** — `__main__.py:664-703` — The `ChatRunner` is instantiated...\n\n"
        "---\n\n"
        "**Hop 1: Core polling loop** — `pipeline/chat_runner.py:195-322` — The `_loop()` method runs every 0.5 seconds.\n\n"
        "---\n\n"
        "**Hop 2: Message fetching** — `macos_adapter.py:107-115` — The `read_chat()` method...\n\n"
        "**Questions?** Want me to drill into any specific part?"
    )
    cb, posted = _collect()
    flush_paragraphs(text, cb, force_final=True)
    # Five real chunks: intro + 3 hop blocks + closing question. No `---` line posted.
    assert len(posted) == 5, posted
    assert posted[0].startswith("Great!"), posted[0]
    assert posted[1].startswith("**Entry point**"), posted[1]
    assert posted[-1].startswith("**Questions?**"), posted[-1]
    assert all("---" not in p.split("\n")[0] or len(p.split("\n")[0]) > 5 for p in posted)


def _run():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failures = []
    for t in tests:
        try:
            t()
            print(f"ok  {t.__name__}")
        except AssertionError as e:
            failures.append((t.__name__, e))
            print(f"FAIL {t.__name__}: {e}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        sys.exit(1)
    print(f"\nall {len(tests)} tests passed")


if __name__ == "__main__":
    _run()
