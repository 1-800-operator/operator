"""
Unit tests for LinuxAdapter participant-count / participant-names plumbing.

T1.10 (session 178): Linux's `_process_chat_queue` only handled `send`/`read`.
LinuxAdapter inherited the base `get_participant_count() -> 0`, so `saw_others`
never flipped, intro never posted, alone-grace auto-leave never fired, and
1-on-1 mode never engaged. Mac's full command set is now mirrored here:
public methods queue commands, `_do_*` helpers run the DOM queries on the
browser thread, `_process_chat_queue` dispatches the new arms.

These tests exercise the queue dispatch with a fake page object — no real
Playwright browser is launched. The DOM-query implementations mirror macOS
verbatim (Meet's selectors are platform-agnostic) so we trust those by
parity rather than re-testing.

Run:
    source venv/bin/activate
    python tests/test_linux_adapter_participants.py
"""
import os
import sys
import threading
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
os.environ.setdefault("OPERATOR_BOT", "pm")

from unittest.mock import MagicMock

from _1_800_operator.connectors.linux_adapter import LinuxAdapter


def _make_adapter():
    """LinuxAdapter without invoking __init__'s side effects (config lookups,
    Playwright import). Bypassing super().__init__() so we don't need a real
    config tree set up for the test sandbox."""
    a = LinuxAdapter.__new__(LinuxAdapter)
    a._user_data_dir = "/tmp/test-profile"
    a._auth_state_file = ""
    a._leave_event = threading.Event()
    a._browser_closed = threading.Event()
    a._browser_thread = None
    a._page = None
    a._seen_message_ids = set()
    import queue as _q
    a._chat_queue = _q.Queue()
    return a


# ---------------------------------------------------------------------------
# Public methods queue the right commands
# ---------------------------------------------------------------------------

def test_get_participant_count_queues_command():
    """The public method enqueues a participant_count command and blocks
    waiting on the result queue."""
    adapter = _make_adapter()

    # Drain the queue from a worker thread, simulating what the browser
    # thread's _process_chat_queue would do.
    def fake_worker():
        cmd, args, result_q = adapter._chat_queue.get(timeout=2)
        assert cmd == "participant_count"
        assert args is None
        result_q.put(7)

    threading.Thread(target=fake_worker, daemon=True).start()

    count = adapter.get_participant_count()
    assert count == 7, f"expected 7, got {count}"
    print("PASS  test_get_participant_count_queues_command")


def test_get_participant_names_queues_command():
    adapter = _make_adapter()

    def fake_worker():
        cmd, args, result_q = adapter._chat_queue.get(timeout=2)
        assert cmd == "participant_names"
        result_q.put(["Alice", "Bob"])

    threading.Thread(target=fake_worker, daemon=True).start()

    names = adapter.get_participant_names()
    assert names == ["Alice", "Bob"]
    print("PASS  test_get_participant_names_queues_command")


def test_get_participant_count_returns_zero_on_timeout():
    """If the browser thread is dead/stuck, the public method must return
    a sensible default (0) instead of hanging or raising."""
    adapter = _make_adapter()
    # Never drain — public method should time out and return 0.
    # Patch the timeout to 0.05s so the test runs fast.
    import queue
    orig = queue.Queue.get
    def fast_get(self, *args, **kwargs):
        # Force the timeout argument to a tiny value
        kwargs["timeout"] = 0.05
        return orig(self, *args, **kwargs)
    queue.Queue.get = fast_get
    try:
        count = adapter.get_participant_count()
    finally:
        queue.Queue.get = orig
    assert count == 0, f"expected 0 on timeout, got {count}"
    print("PASS  test_get_participant_count_returns_zero_on_timeout")


def test_get_participant_names_returns_empty_on_timeout():
    adapter = _make_adapter()
    import queue
    orig = queue.Queue.get
    def fast_get(self, *args, **kwargs):
        kwargs["timeout"] = 0.05
        return orig(self, *args, **kwargs)
    queue.Queue.get = fast_get
    try:
        names = adapter.get_participant_names()
    finally:
        queue.Queue.get = orig
    assert names == [], f"expected [] on timeout, got {names}"
    print("PASS  test_get_participant_names_returns_empty_on_timeout")


# ---------------------------------------------------------------------------
# _process_chat_queue dispatches the new commands
# ---------------------------------------------------------------------------

def test_process_chat_queue_dispatches_participant_count():
    """A queued participant_count command must be routed to
    _do_get_participant_count and the result placed on the result queue."""
    adapter = _make_adapter()
    page = MagicMock()
    # Replace the DOM-query helper with a stub return.
    adapter._do_get_participant_count = lambda p: 4

    import queue
    result_q = queue.Queue()
    adapter._chat_queue.put(("participant_count", None, result_q))

    adapter._process_chat_queue(page)

    assert result_q.get(timeout=1) == 4
    print("PASS  test_process_chat_queue_dispatches_participant_count")


def test_process_chat_queue_dispatches_participant_names():
    adapter = _make_adapter()
    page = MagicMock()
    adapter._do_get_participant_names = lambda p: ["Alice", "Bob", "Carol"]

    import queue
    result_q = queue.Queue()
    adapter._chat_queue.put(("participant_names", None, result_q))

    adapter._process_chat_queue(page)

    assert result_q.get(timeout=1) == ["Alice", "Bob", "Carol"]
    print("PASS  test_process_chat_queue_dispatches_participant_names")


def test_process_chat_queue_handles_mixed_command_batch():
    """Multiple queued commands of different types each route correctly."""
    adapter = _make_adapter()
    page = MagicMock()
    adapter._do_send_chat = lambda p, m: None
    adapter._do_read_chat = lambda p: [{"id": "m1", "sender": "x", "text": "hi"}]
    adapter._do_get_participant_count = lambda p: 3
    adapter._do_get_participant_names = lambda p: ["A", "B", "C"]

    import queue
    rqs = [queue.Queue() for _ in range(4)]
    adapter._chat_queue.put(("send", "hello", rqs[0]))
    adapter._chat_queue.put(("read", None, rqs[1]))
    adapter._chat_queue.put(("participant_count", None, rqs[2]))
    adapter._chat_queue.put(("participant_names", None, rqs[3]))

    adapter._process_chat_queue(page)

    assert rqs[0].get(timeout=1) is None
    assert rqs[1].get(timeout=1) == [{"id": "m1", "sender": "x", "text": "hi"}]
    assert rqs[2].get(timeout=1) == 3
    assert rqs[3].get(timeout=1) == ["A", "B", "C"]
    print("PASS  test_process_chat_queue_handles_mixed_command_batch")


# ---------------------------------------------------------------------------
# _do_get_participant_count reads from the DOM
# ---------------------------------------------------------------------------

def test_do_get_participant_count_reads_locator_count():
    """The DOM helper must use the participant-tile selector and return the
    locator's count value."""
    adapter = _make_adapter()
    page = MagicMock()
    locator = MagicMock()
    locator.count.return_value = 5
    page.locator.return_value = locator

    n = adapter._do_get_participant_count(page)

    page.locator.assert_called_once_with('[data-requested-participant-id]')
    assert n == 5
    print("PASS  test_do_get_participant_count_reads_locator_count")


def test_do_get_participant_count_returns_zero_on_exception():
    """Any selector / page error must degrade gracefully to 0, not raise.
    Otherwise the participant-watchdog loop would crash the browser thread."""
    adapter = _make_adapter()
    page = MagicMock()
    page.locator.side_effect = RuntimeError("page closed")

    n = adapter._do_get_participant_count(page)
    assert n == 0
    print("PASS  test_do_get_participant_count_returns_zero_on_exception")


def test_do_get_participant_names_returns_empty_on_exception():
    adapter = _make_adapter()
    page = MagicMock()
    page.evaluate.side_effect = RuntimeError("page closed")

    names = adapter._do_get_participant_names(page)
    assert names == []
    print("PASS  test_do_get_participant_names_returns_empty_on_exception")


# ---------------------------------------------------------------------------
# Run all
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_get_participant_count_queues_command,
        test_get_participant_names_queues_command,
        test_get_participant_count_returns_zero_on_timeout,
        test_get_participant_names_returns_empty_on_timeout,
        test_process_chat_queue_dispatches_participant_count,
        test_process_chat_queue_dispatches_participant_names,
        test_process_chat_queue_handles_mixed_command_batch,
        test_do_get_participant_count_reads_locator_count,
        test_do_get_participant_count_returns_zero_on_exception,
        test_do_get_participant_names_returns_empty_on_exception,
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
