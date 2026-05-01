"""
Unit tests for Component H — Connectors (Boundary, unit depth).

Covers `connectors/terminal.py` (TerminalConnector) and the pure helpers in
`connectors/session.py` (JoinStatus, validate_auth_state, inject_cookies,
_chrome_lock_is_live).

The Playwright-touching paths (detect_page_state, save_debug, macos_adapter,
linux_adapter) stay as manual/integration testing per docs/test-plan.md.

Run:
    source venv/bin/activate
    python tests/test_connectors.py
"""
import io
import json
import os
import signal
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
os.environ.setdefault("OPERATOR_BOT", "pm")

from _1_800_operator.connectors import session as session_mod
from _1_800_operator.connectors.session import (
    JoinStatus,
    _chrome_lock_is_live,
    inject_cookies,
    validate_auth_state,
)


# ---------------------------------------------------------------------------
# TerminalConnector helpers
# ---------------------------------------------------------------------------

def _make_terminal_connector(bot_name="pm"):
    """Build a TerminalConnector without starting the real stdin thread.

    The real __init__ spawns a daemon thread that does `for line in sys.stdin`
    — in a test runner that'd either block or immediately EOF and route SIGINT
    back to our own pid (killing the test). Patching threading.Thread to a
    MagicMock makes the connector purely queue-driven for tests.
    """
    from _1_800_operator.connectors.terminal import TerminalConnector
    with patch("_1_800_operator.connectors.terminal.threading.Thread") as Thread:
        Thread.return_value = MagicMock()
        return TerminalConnector(bot_name=bot_name)


# ---------------------------------------------------------------------------
# TerminalConnector
# ---------------------------------------------------------------------------

def test_terminal_join_is_noop_and_returns_none():
    conn = _make_terminal_connector()
    assert conn.join("https://meet.google.com/xyz") is None
    assert conn.join(None) is None
    print("PASS  test_terminal_join_is_noop_and_returns_none")


def test_terminal_send_chat_prints_bot_prefix_and_message():
    conn = _make_terminal_connector(bot_name="pm")
    buf = io.StringIO()
    saved = sys.stdout
    sys.stdout = buf
    try:
        conn.send_chat("hello world")
    finally:
        sys.stdout = saved
    out = buf.getvalue()
    assert "[pm]" in out, out
    assert "hello world" in out, out
    print("PASS  test_terminal_send_chat_prints_bot_prefix_and_message")


def test_terminal_read_chat_drains_queue_with_incremental_ids():
    conn = _make_terminal_connector()
    # Empty queue — nothing to drain
    assert conn.read_chat() == []

    conn._queue.put("first")
    conn._queue.put("second")
    msgs = conn.read_chat()
    assert len(msgs) == 2
    assert msgs[0] == {"id": "term-1", "sender": "you", "text": "first"}
    assert msgs[1] == {"id": "term-2", "sender": "you", "text": "second"}

    # Ids keep incrementing across calls
    conn._queue.put("third")
    msgs2 = conn.read_chat()
    assert msgs2[0]["id"] == "term-3"
    print("PASS  test_terminal_read_chat_drains_queue_with_incremental_ids")


def test_terminal_read_chat_quit_and_exit_route_to_sigint():
    """`/quit` and `/exit` must fire SIGINT to our own pid — mirrors Ctrl+C."""
    for keyword in ("/quit", "/exit", "  /quit  "):
        conn = _make_terminal_connector()
        conn._queue.put(keyword)
        with patch("_1_800_operator.connectors.terminal.os.kill") as fake_kill:
            msgs = conn.read_chat()
        assert fake_kill.call_count == 1, keyword
        args, _ = fake_kill.call_args
        assert args == (os.getpid(), signal.SIGINT), (keyword, args)
        # /quit is not surfaced as a chat message — the loop breaks before append.
        assert msgs == [], (keyword, msgs)
    print("PASS  test_terminal_read_chat_quit_and_exit_route_to_sigint")


def test_terminal_participant_count_and_leave_flip_connection():
    conn = _make_terminal_connector()
    assert conn.get_participant_count() == 2   # pinned → 1-on-1 threshold holds
    assert conn.is_connected() is True
    conn.leave()
    assert conn.is_connected() is False
    # set_caption_callback is a no-op and must not raise
    conn.set_caption_callback(lambda *a, **k: None)
    print("PASS  test_terminal_participant_count_and_leave_flip_connection")


# ---------------------------------------------------------------------------
# JoinStatus
# ---------------------------------------------------------------------------

def test_join_status_transitions():
    js = JoinStatus()
    # Initial
    assert isinstance(js.ready, threading.Event)
    assert js.ready.is_set() is False
    assert js.success is False
    assert js.failure_reason is None
    assert js.session_recovered is False

    # Success with recovery flag
    js.signal_success(recovered=True)
    assert js.ready.is_set()
    assert js.success is True
    assert js.session_recovered is True
    assert js.failure_reason is None

    # Failure transition on a fresh status
    js2 = JoinStatus()
    js2.signal_failure("lobby timeout")
    assert js2.ready.is_set()
    assert js2.success is False
    assert js2.failure_reason == "lobby timeout"
    print("PASS  test_join_status_transitions")


# ---------------------------------------------------------------------------
# validate_auth_state
# ---------------------------------------------------------------------------

def test_validate_auth_state_rejection_branches():
    """None path, missing file, malformed JSON, and missing .google.com SID all → None."""
    assert validate_auth_state(None) is None
    assert validate_auth_state("") is None

    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "nope.json"
        assert validate_auth_state(str(missing)) is None

        bad = Path(tmp) / "bad.json"
        bad.write_text("{not valid json")
        assert validate_auth_state(str(bad)) is None

        # Valid JSON, but no .google.com SID cookie in the list
        no_sid = Path(tmp) / "no_sid.json"
        no_sid.write_text(json.dumps({"cookies": [
            {"name": "SID", "domain": ".example.com"},      # wrong domain
            {"name": "other", "domain": ".google.com"},     # wrong name
        ]}))
        assert validate_auth_state(str(no_sid)) is None
    print("PASS  test_validate_auth_state_rejection_branches")


def test_validate_auth_state_happy_path():
    """A file with a proper .google.com SID cookie returns the parsed dict."""
    state = {"cookies": [
        {"name": "SID", "domain": ".google.com", "value": "abc"},
        {"name": "HSID", "domain": ".google.com", "value": "def"},
    ]}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(state, f)
        path = f.name
    try:
        result = validate_auth_state(path)
        assert result == state
    finally:
        os.unlink(path)
    print("PASS  test_validate_auth_state_happy_path")


# ---------------------------------------------------------------------------
# inject_cookies
# ---------------------------------------------------------------------------

def test_inject_cookies_filters_to_google_domain_and_returns_true():
    """Only .google.com cookies are forwarded to context.add_cookies."""
    context = MagicMock()
    auth_state = {"cookies": [
        {"name": "SID", "domain": ".google.com"},
        {"name": "noise", "domain": ".example.com"},
        {"name": "HSID", "domain": "accounts.google.com"},
        {"name": "also_noise", "domain": ""},
    ]}
    ok = inject_cookies(context, auth_state)
    assert ok is True
    assert context.add_cookies.call_count == 1
    sent = context.add_cookies.call_args.args[0]
    names = sorted(c["name"] for c in sent)
    assert names == ["HSID", "SID"], names
    print("PASS  test_inject_cookies_filters_to_google_domain_and_returns_true")


def test_inject_cookies_empty_and_exception_both_return_false():
    # No .google.com cookies → False, add_cookies never called
    context = MagicMock()
    ok = inject_cookies(context, {"cookies": [{"name": "x", "domain": ".other.com"}]})
    assert ok is False
    assert context.add_cookies.call_count == 0

    # Playwright raises → False
    context2 = MagicMock()
    context2.add_cookies.side_effect = RuntimeError("playwright exploded")
    ok2 = inject_cookies(
        context2,
        {"cookies": [{"name": "SID", "domain": ".google.com"}]},
    )
    assert ok2 is False
    print("PASS  test_inject_cookies_empty_and_exception_both_return_false")


# ---------------------------------------------------------------------------
# _chrome_lock_is_live
# ---------------------------------------------------------------------------

def test_chrome_lock_is_live_three_branches():
    """Nonexistent lock → False; lock→dead pid → False; lock→live pid (self) → True."""
    with tempfile.TemporaryDirectory() as tmp:
        lock = Path(tmp) / "SingletonLock"

        # 1. readlink on a path that doesn't exist → False
        assert _chrome_lock_is_live(str(lock)) is False

        # 2. Lock → live pid (our own). The symlink target must end in "-<pid>".
        os.symlink(f"hostname-{os.getpid()}", lock)
        assert _chrome_lock_is_live(str(lock)) is True
        os.unlink(lock)

        # 3. Lock → very likely-dead pid. Use a pid we know is free on macOS
        # (ceiling of the pid space). os.kill raises OSError → caught → False.
        dead_pid = 2 ** 22 - 1
        os.symlink(f"hostname-{dead_pid}", lock)
        assert _chrome_lock_is_live(str(lock)) is False
    print("PASS  test_chrome_lock_is_live_three_branches")


# ---------------------------------------------------------------------------
# Run all
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_terminal_join_is_noop_and_returns_none,
        test_terminal_send_chat_prints_bot_prefix_and_message,
        test_terminal_read_chat_drains_queue_with_incremental_ids,
        test_terminal_read_chat_quit_and_exit_route_to_sigint,
        test_terminal_participant_count_and_leave_flip_connection,
        test_join_status_transitions,
        test_validate_auth_state_rejection_branches,
        test_validate_auth_state_happy_path,
        test_inject_cookies_filters_to_google_domain_and_returns_true,
        test_inject_cookies_empty_and_exception_both_return_false,
        test_chrome_lock_is_live_three_branches,
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
