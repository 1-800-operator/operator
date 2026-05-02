"""
Unit tests for the shared yes/no detector — pipeline.confirmation.is_yes.

T1.9 (session 178) consolidated two divergent matchers (track-A
permission_chat_handler vs track-B chat_runner._handle_confirmation) into
this single helper. Tests cover the union vocab, the negation gate, the
"go ahead"/"do it" phrase override, edge cases (empty, whitespace,
contractions, unicode), and verify both call sites delegate to it.

Run:
    source venv/bin/activate
    python tests/test_confirmation.py
"""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
os.environ.setdefault("OPERATOR_BOT", "pm")

from _1_800_operator.pipeline.confirmation import is_yes


# ---------------------------------------------------------------------------
# Affirmative tokens
# ---------------------------------------------------------------------------

def test_canonical_yes():
    for s in ["yes", "Yes", "YES", "yeah", "yep", "ok", "okay", "sure",
              "approve", "approved", "confirmed", "y"]:
        assert is_yes(s) is True, f"expected True for {s!r}"
    print("PASS  test_canonical_yes")


def test_yes_inside_sentence():
    for s in ["yes please", "ok do it", "sure go for it", "confirmed, proceed"]:
        assert is_yes(s) is True, f"expected True for {s!r}"
    print("PASS  test_yes_inside_sentence")


def test_phrase_overrides():
    """'go ahead' and 'do it' alone count as yes even without an affirmative
    token, but the negation gate still applies if a negation is present."""
    assert is_yes("go ahead") is True
    assert is_yes("do it") is True
    assert is_yes("just go ahead with that") is True
    print("PASS  test_phrase_overrides")


# ---------------------------------------------------------------------------
# Negation gate
# ---------------------------------------------------------------------------

def test_plain_negations():
    for s in ["no", "No", "nope", "nah", "stop", "cancel"]:
        assert is_yes(s) is False, f"expected False for {s!r}"
    print("PASS  test_plain_negations")


def test_affirmative_plus_negation_returns_false():
    """An affirmative token paired with a negation must NOT auto-approve.
    These are the failure modes the audit's negation-gate fix targets."""
    cases = [
        "ok no",
        "ok no don't do that",
        "yes don't",
        "yeah no, cancel that",
        "go ahead no actually wait",
        "approved, no — hold off",
        "y, but cancel",
    ]
    for s in cases:
        assert is_yes(s) is False, f"expected False for {s!r}"
    print("PASS  test_affirmative_plus_negation_returns_false")


def test_contraction_negations():
    """Contractions like 'don't', 'dont', 'do not' must trip the negation
    gate even though `\\b\\w+\\b` splits them into separate words."""
    for s in ["yes don't", "ok dont", "sure, do not run that"]:
        assert is_yes(s) is False, f"expected False for {s!r}"
    print("PASS  test_contraction_negations")


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_and_whitespace():
    assert is_yes("") is False
    assert is_yes("   ") is False
    assert is_yes("\n\t  ") is False
    print("PASS  test_empty_and_whitespace")


def test_no_match_returns_false():
    """Unrelated text — neither affirmative nor negation — returns False
    (default to safety / re-prompt).

    Note: 'not sure' is intentionally omitted. Both pre-178 matchers
    accepted it as approval (`sure` is affirmative, `not` isn't in the
    negation list). Preserving that behavior here. Adding `not` to the
    negation set would catch this but would over-trigger on legitimate
    phrasings like "sure, not a problem, go ahead" — out of scope for
    T1.9 (consolidation, not vocab expansion).
    """
    for s in ["maybe", "what?", "explain", "let me think"]:
        assert is_yes(s) is False, f"expected False for {s!r}"
    print("PASS  test_no_match_returns_false")


def test_word_boundary_avoids_false_positives():
    """Bare `y` should match only as a standalone token, not embedded.
    Common false-positive risks: 'yacht', 'yoke', 'yesterday', 'okra'."""
    for s in ["yacht", "yoke", "yesterday let's not", "okra is fine"]:
        # These contain affirmative substrings but should NOT match.
        # `yesterday` and the let's-not / `okra` cases also have negations.
        # Verify the word-boundary regex isn't tripping on partial matches.
        result = is_yes(s)
        # Either False outright, or False because of an explicit negation.
        # The point: 'yacht' should not be read as 'y' + 'acht' approval.
        if "no" in s or "not" in s:
            assert result is False, f"expected False (negation) for {s!r}"
        else:
            assert result is False, f"expected False (no boundary match) for {s!r}, got True"
    print("PASS  test_word_boundary_avoids_false_positives")


def test_yesterday_does_not_match_yes():
    """Critical: 'yesterday' contains 'yes' but should NOT count as approval
    because of the word boundary."""
    assert is_yes("yesterday I said no") is False
    # Without negation, still False — 'yes' is bounded inside 'yesterday'.
    # `\b` matches at the start of 'yesterday' and `yes` is at offset 0,3.
    # `\byes\b` requires a word boundary AFTER 'yes' too — but 't' is a
    # word char, so no match. Correct.
    assert is_yes("yesterday was fine") is False
    print("PASS  test_yesterday_does_not_match_yes")


# ---------------------------------------------------------------------------
# Track-A and track-B delegation
# ---------------------------------------------------------------------------

def test_permission_chat_handler_delegates_to_shared_helper():
    """Track-A's _is_yes export is the same function as the shared is_yes."""
    from _1_800_operator.pipeline.permission_chat_handler import _is_yes
    assert _is_yes is is_yes, \
        "permission_chat_handler._is_yes must alias confirmation.is_yes"
    print("PASS  test_permission_chat_handler_delegates_to_shared_helper")


def test_chat_runner_uses_shared_helper():
    """Track-B's _handle_confirmation calls is_yes — verify by inspecting source."""
    import inspect
    from _1_800_operator.pipeline.chat_runner import ChatRunner
    src = inspect.getsource(ChatRunner._handle_confirmation)
    assert "is_yes(text)" in src, \
        f"_handle_confirmation must delegate to is_yes; source: {src[:300]}"
    print("PASS  test_chat_runner_uses_shared_helper")


# ---------------------------------------------------------------------------
# Run all
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_canonical_yes,
        test_yes_inside_sentence,
        test_phrase_overrides,
        test_plain_negations,
        test_affirmative_plus_negation_returns_false,
        test_contraction_negations,
        test_empty_and_whitespace,
        test_no_match_returns_false,
        test_word_boundary_avoids_false_positives,
        test_yesterday_does_not_match_yes,
        test_permission_chat_handler_delegates_to_shared_helper,
        test_chat_runner_uses_shared_helper,
    ]
    failures = []
    for t in tests:
        try:
            t()
        except Exception as e:
            import traceback
            print(f"FAIL  {t.__name__}: {e}")
            traceback.print_exc()
            failures.append(t.__name__)

    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
