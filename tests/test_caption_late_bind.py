"""
Caption late-bind behavior — set_caption_callback works before OR after join().

Validates the late-bind design: the JS bridge is exposed at browser startup
whenever CAPTIONS_ENABLED is true, so a callback registered after the browser
is already running still receives captions cleanly. Useful when the meeting
slug is only known post-navigation (e.g. opening `meet.new`), so the
finalizer cannot be registered up front. Tests exercise the Python-side
bridge stub directly (no Playwright launch) because that's the only piece
the late-bind fix changes — the JS observer hasn't moved.
"""
import os
import sys

# config.py hard-exits at import time if OPERATOR_BOT is unset; the adapter
# import below pulls it in transitively. Pin a bundled agent so the test is
# self-contained instead of requiring the CLI wrapper.
os.environ.setdefault("OPERATOR_BOT", "pm")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _1_800_operator.connectors.macos_adapter import MacOSAdapter


def _make_adapter():
    return MacOSAdapter(auth_state_file="/tmp/_unused_auth_state.json")


def test_caption_dropped_when_no_callback():
    adapter = _make_adapter()
    # Bridge stub must tolerate captions arriving before any callback is set —
    # matches the window between browser-up and sink-registration when the
    # meeting slug is only known post-navigation.
    adapter._on_caption_from_js("Alice", "hello world", 1000.0)
    print("PASS: no callback -> caption dropped silently")


def test_callback_receives_caption():
    adapter = _make_adapter()
    received = []
    adapter.set_caption_callback(lambda s, t, ts: received.append((s, t)))
    adapter._on_caption_from_js("Alice", "hello world", 1000.0)
    assert received == [("Alice", "hello world")], received
    print("PASS: registered callback receives caption")


def test_late_bind_after_first_caption():
    """Core late-bind invariant: registering a callback AFTER captions have
    already started flowing through the bridge still routes new captions."""
    adapter = _make_adapter()
    adapter._on_caption_from_js("Alice", "ignored utterance", 1000.0)

    received = []
    adapter.set_caption_callback(lambda s, t, ts: received.append((s, t)))
    adapter._on_caption_from_js("Bob", "now I am listening", 2000.0)

    assert received == [("Bob", "now I am listening")], received
    print("PASS: late-bound callback receives subsequent captions")


def test_swap_callback_routes_to_new_one():
    """Replacing the finalizer mid-session (e.g. end-of-meeting → next-meeting):
    future captions must route to the new sink, not the old one."""
    adapter = _make_adapter()
    first, second = [], []

    adapter.set_caption_callback(lambda s, t, ts: first.append((s, t)))
    adapter._on_caption_from_js("Alice", "meeting one", 1000.0)

    adapter.set_caption_callback(lambda s, t, ts: second.append((s, t)))
    adapter._on_caption_from_js("Bob", "meeting two", 2000.0)

    assert first == [("Alice", "meeting one")], first
    assert second == [("Bob", "meeting two")], second
    print("PASS: callback swap routes future captions to the new sink")


def test_unregister_with_none():
    adapter = _make_adapter()
    received = []
    adapter.set_caption_callback(lambda s, t, ts: received.append((s, t)))
    adapter._on_caption_from_js("Alice", "first", 1000.0)
    adapter.set_caption_callback(None)
    adapter._on_caption_from_js("Bob", "ignored", 2000.0)
    assert received == [("Alice", "first")], received
    print("PASS: set_caption_callback(None) unregisters cleanly")


def test_callback_exception_does_not_break_bridge():
    adapter = _make_adapter()

    def boom(_s, _t, _ts):
        raise RuntimeError("callback exploded")

    adapter.set_caption_callback(boom)
    # Bridge swallows the exception — the browser thread must not die.
    adapter._on_caption_from_js("Alice", "hello", 1000.0)

    # Bridge still alive: register a real callback and confirm flow resumes.
    received = []
    adapter.set_caption_callback(lambda s, t, ts: received.append((s, t)))
    adapter._on_caption_from_js("Bob", "still here", 2000.0)
    assert received == [("Bob", "still here")], received
    print("PASS: callback exception swallowed -> bridge stays alive")


def test_filtered_caption_never_calls_callback():
    """filter_caption drops system phrases like 'You left the meeting'.
    The callback must not see those even when registered."""
    adapter = _make_adapter()
    received = []
    adapter.set_caption_callback(lambda s, t, ts: received.append((s, t)))
    adapter._on_caption_from_js("System", "You left the meeting", 1000.0)
    assert received == [], received
    print("PASS: filter_caption-rejected text never reaches the callback")


if __name__ == "__main__":
    test_caption_dropped_when_no_callback()
    test_callback_receives_caption()
    test_late_bind_after_first_caption()
    test_swap_callback_routes_to_new_one()
    test_unregister_with_none()
    test_callback_exception_does_not_break_bridge()
    test_filtered_caption_never_calls_callback()
    print("\nAll caption late-bind tests passed.")
