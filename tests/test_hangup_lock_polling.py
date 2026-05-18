"""H-26 regression: /operator:hangup now polls on slip.pid being
released rather than on the daemon's pid exiting.

The daemon's _shutdown releases the lockfile early (~500ms after
SIGTERM, intentional design — keeps /operator:slip retry-able mid-
teardown). Pre-fix, hangup polled on pid liveness, which kept it
blocked through the full 5-12s of teardown (PTY drain, connector.leave,
audio helper exit). Hangup would then either bail at its 3s deadline
(returning "hung up" prematurely while the daemon was still alive — the
direct contradiction the H-26 finding flagged) or wait the full
teardown.

Post-fix: hangup polls on lockfile gone (the faster, truthful signal).
Hangup returns in <1s in the common case; the daemon's background
teardown continues without blocking the user.
"""

import io
import os
import signal
import sys
import tempfile
import threading
import time
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _1_800_operator import __main__ as op_main


def _setup_fake_daemon(lock_path: Path, release_after: float):
    """Write lockfile pointing at THIS pid (alive). Schedule a thread
    that unlinks the lockfile after `release_after` seconds to simulate
    the daemon's early lock-release in _shutdown.
    """
    lock_path.write_text(f"{os.getpid()}\n", encoding="utf-8")

    def _release_later():
        time.sleep(release_after)
        try:
            lock_path.unlink()
        except OSError:
            pass

    t = threading.Thread(target=_release_later, daemon=True)
    t.start()
    return t


def test_hangup_returns_when_lock_released():
    """Common-path: daemon releases lock after ~300ms; hangup returns
    shortly after. Pre-fix this would have waited the full 3s."""
    tmp = Path(tempfile.mkdtemp(prefix="op_hangup_test_"))
    fake_lock = tmp / "slip.pid"
    original_lock = op_main._SLIP_LOCK_PATH
    op_main._SLIP_LOCK_PATH = fake_lock

    # Don't actually SIGTERM ourselves.
    original_kill = os.kill
    def _safe_kill(pid, sig):
        if sig == 0:
            # Liveness probe — preserve real behaviour (we ARE alive).
            return original_kill(pid, 0)
        # SIGTERM/SIGKILL → swallow.
        return None
    op_main.os.kill = _safe_kill

    # _pid_is_operator probes via ps; force True so the test pid is
    # treated as the daemon.
    original_pid_check = op_main._pid_is_operator
    op_main._pid_is_operator = lambda pid: True

    try:
        _setup_fake_daemon(fake_lock, release_after=0.3)
        buf = io.StringIO()
        t0 = time.monotonic()
        with redirect_stdout(buf):
            rc = op_main._run_hangup()
        elapsed = time.monotonic() - t0
        out = buf.getvalue()
        assert rc == 0
        assert "hung up" in out, out
        # Should return shortly after the 0.3s lock release, well under
        # the 3s ceiling.
        assert 0.25 < elapsed < 1.0, (
            f"expected ~0.3s return on early lock release, got {elapsed:.3f}s"
        )
        print(f"  hangup returns ~{elapsed:.2f}s after early lock release: OK")
    finally:
        op_main._SLIP_LOCK_PATH = original_lock
        op_main.os.kill = original_kill
        op_main._pid_is_operator = original_pid_check
        try:
            fake_lock.unlink()
        except OSError:
            pass
        tmp.rmdir()


def test_hangup_no_daemon_running():
    """No lockfile → hangup says 'not in a meeting' immediately."""
    tmp = Path(tempfile.mkdtemp(prefix="op_hangup_test_"))
    fake_lock = tmp / "slip.pid"
    original_lock = op_main._SLIP_LOCK_PATH
    op_main._SLIP_LOCK_PATH = fake_lock
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = op_main._run_hangup()
        out = buf.getvalue()
        assert rc == 0
        assert "not in a meeting" in out, out
        print("  hangup: 'not in a meeting' when no lockfile: OK")
    finally:
        op_main._SLIP_LOCK_PATH = original_lock
        tmp.rmdir()


def test_hangup_releases_lock_if_daemon_dies_without_releasing():
    """Pathological: daemon crashed mid-_shutdown without releasing the
    lock. Hangup detects pid is dead and unlinks the lock itself, then
    returns. Without this, the lock would persist (broken singleton)
    until the next operator slip's stale-lock reclaim path."""
    tmp = Path(tempfile.mkdtemp(prefix="op_hangup_test_"))
    fake_lock = tmp / "slip.pid"
    original_lock = op_main._SLIP_LOCK_PATH
    op_main._SLIP_LOCK_PATH = fake_lock

    # Pid that is almost certainly not in use → os.kill(pid, 0) raises
    # ProcessLookupError. We use a giant pid to be safe.
    DEAD_PID = 9999999

    # Write lockfile with the dead pid, and make _pid_is_operator
    # return True so the cleanup-stale path is bypassed (we want to
    # exercise the in-loop dead-pid branch).
    fake_lock.write_text(f"{DEAD_PID}\n", encoding="utf-8")
    original_pid_check = op_main._pid_is_operator
    op_main._pid_is_operator = lambda pid: True

    # Pin os.kill to never actually do anything — both SIGTERM and the
    # signal-0 liveness probe should behave the way the real kernel
    # would for a dead pid: SIGTERM → ProcessLookupError too, probe →
    # ProcessLookupError.
    original_kill = os.kill
    def _dead_kill(pid, sig):
        if pid == DEAD_PID:
            raise ProcessLookupError
        return original_kill(pid, sig)
    op_main.os.kill = _dead_kill

    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = op_main._run_hangup()
        out = buf.getvalue()
        assert rc == 0
        # Either path ("not in a meeting" via ProcessLookupError on the
        # SIGTERM, or "hung up (1 session)" via the in-loop probe) is
        # acceptable — both unlink the lock.
        assert not fake_lock.exists(), (
            "lock should be released after hangup detected dead daemon"
        )
        print(f"  hangup self-releases lock when daemon is dead: OK ({out.strip()!r})")
    finally:
        op_main._SLIP_LOCK_PATH = original_lock
        op_main.os.kill = original_kill
        op_main._pid_is_operator = original_pid_check
        try:
            fake_lock.unlink()
        except OSError:
            pass
        tmp.rmdir()


if __name__ == "__main__":
    tests = [
        test_hangup_returns_when_lock_released,
        test_hangup_no_daemon_running,
        test_hangup_releases_lock_if_daemon_dies_without_releasing,
    ]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} H-26 hangup-lock-polling tests passed.")
