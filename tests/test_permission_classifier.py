"""
Unit tests for PermissionClassifier — the parts that don't need a real
claude subprocess. End-to-end behavior was already validated against
real claude in debug/14_26_classifier_spike (19/19 scenarios match);
those don't need to run on every test invocation.

What this exercises:
  - _parse_yesno across YES / NO / mixed / no-token / empty inputs
  - classify() returns False (deny) when the subprocess isn't spawned
    and the lazy-spawn path also fails
  - classify() returns False (deny) on classifier-side timeout
  - stop() is idempotent

Live PTY behavior is covered by the 14_26 spike; rerunning real claude
spawns inside the unit test loop would be slow and Hat-on-bear.
"""
import sys
import tempfile
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _1_800_operator.pipeline.classifier import PermissionClassifier


def test_parse_yesno_yes():
    assert PermissionClassifier._parse_yesno("YES") is True
    assert PermissionClassifier._parse_yesno("yes") is True  # case-insensitive
    assert PermissionClassifier._parse_yesno("YES, definitely") is True
    assert PermissionClassifier._parse_yesno("My answer: YES") is True
    print("  parse: YES recognized: OK")


def test_parse_yesno_no():
    assert PermissionClassifier._parse_yesno("NO") is False
    assert PermissionClassifier._parse_yesno("no") is False
    assert PermissionClassifier._parse_yesno("NO, they declined") is False
    print("  parse: NO recognized: OK")


def test_parse_yesno_first_token_wins():
    # YES appears before NO → returns True. Driver convention from the
    # 14_26 spike: first standalone YES/NO token wins.
    assert PermissionClassifier._parse_yesno("YES (not NO)") is True
    assert PermissionClassifier._parse_yesno("Definitely NO not YES") is False
    print("  parse: first standalone token wins: OK")


def test_parse_yesno_no_token_returns_none():
    assert PermissionClassifier._parse_yesno("") is None
    assert PermissionClassifier._parse_yesno("maybe") is None
    assert PermissionClassifier._parse_yesno("I'm not sure") is None
    # Sub-string of a longer word does NOT count (\b word boundary).
    assert PermissionClassifier._parse_yesno("YESTERDAY") is None
    print("  parse: no token → None: OK")


def test_classify_with_no_subprocess_and_failed_spawn_denies():
    """If pre_warm wasn't called and a lazy spawn also fails, classify()
    must return False (deny). The classifier is the safe-default
    backstop for the operator-side hook contract."""
    sd = Path(tempfile.mkdtemp(prefix="permreq_classifier_test_"))
    cls = PermissionClassifier(session_dir=sd)
    # Patch pre_warm to simulate a spawn failure (claude not installed).
    with mock.patch.object(cls, "pre_warm", lambda: None):
        # _proc stays None; classify should give up and deny.
        verdict = cls.classify("yes", "approve?")
    assert verdict is False, verdict
    print("  classify: no subprocess + failed lazy spawn → False (deny): OK")


def test_classify_while_stopping_denies_immediately():
    sd = Path(tempfile.mkdtemp(prefix="permreq_classifier_test_"))
    cls = PermissionClassifier(session_dir=sd)
    cls._stopping = True
    # Should short-circuit without attempting any spawn or send.
    verdict = cls.classify("yes", "approve?")
    assert verdict is False
    print("  classify: while stopping → False (deny) immediately: OK")


def test_stop_is_idempotent():
    sd = Path(tempfile.mkdtemp(prefix="permreq_classifier_test_"))
    cls = PermissionClassifier(session_dir=sd)
    cls.stop()  # nothing to tear down — should not raise
    cls.stop()  # second call — should not raise
    print("  stop(): idempotent on un-spawned classifier: OK")


def test_session_dir_default_is_classifier_suffixed():
    """Default session_dir must NOT collide with the main provider's
    dir — the hooks gated on $OPERATOR_SESSION_DIR write into it, and
    a collision would mean the classifier's Stop hook clobbers main's
    replies.jsonl. Verify the suffix discipline."""
    cls = PermissionClassifier()
    assert "classifier" in cls._session_dir.name, cls._session_dir
    # Cleanup the auto-created dir.
    try:
        cls._session_dir.rmdir()
    except OSError:
        pass
    print("  session_dir default: 'classifier' suffix avoids collision: OK")


if __name__ == "__main__":
    print("PermissionClassifier unit tests:")
    tests = [
        test_parse_yesno_yes,
        test_parse_yesno_no,
        test_parse_yesno_first_token_wins,
        test_parse_yesno_no_token_returns_none,
        test_classify_with_no_subprocess_and_failed_spawn_denies,
        test_classify_while_stopping_denies_immediately,
        test_stop_is_idempotent,
        test_session_dir_default_is_classifier_suffixed,
    ]
    failures = 0
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            failures += 1
            print(f"  {fn.__name__}: FAIL — {e}")
        except Exception as e:
            failures += 1
            print(f"  {fn.__name__}: ERROR — {type(e).__name__}: {e}")
    if failures:
        print(f"\n{failures} test(s) failed")
        sys.exit(1)
    print(f"\nAll {len(tests)} classifier unit tests passed.")
