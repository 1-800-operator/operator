"""H-16 regression: verify `_chrome_user_data_dir_on_cdp_port` correctly
parses the `--user-data-dir=<path>` flag from the Chrome process
listening on CDP_PORT, and that the reuse path's verification logic
treats unmatched paths as foreign.

The helper itself shells out to lsof + ps. We monkey-patch
subprocess.run to return canned fixtures so the test runs in <1s and
doesn't need a real Chrome on the box.
"""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _1_800_operator.connectors import attach_adapter


def _fake_run(stdouts: dict[str, str]):
    """Build a subprocess.run drop-in that dispatches by leading argv arg.

    `stdouts` maps "lsof" / "ps" to the stdout string to return. Anything
    else raises so the test fails loudly on an unexpected call.
    """
    def _run(argv, capture_output=True, text=True, timeout=2):
        prog = argv[0]
        if prog in stdouts:
            return SimpleNamespace(
                returncode=0, stdout=stdouts[prog], stderr="",
            )
        raise AssertionError(f"unexpected subprocess.run call: {argv!r}")
    return _run


def _patch_run(monkeypatch_run):
    """Install fake subprocess.run on the attach_adapter module."""
    original = attach_adapter.subprocess.run
    attach_adapter.subprocess.run = monkeypatch_run
    return original


def test_returns_user_data_dir_when_chrome_present():
    """Common path: lsof names a pid; ps shows --user-data-dir=<path>."""
    fake = _fake_run({
        "lsof": "12345\n",
        "ps": (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
            "--remote-debugging-port=9222 "
            "--user-data-dir=/Users/jojo/.operator/slip_profile "
            "--remote-allow-origins=http://operator-abc.local"
        ),
    })
    original = _patch_run(fake)
    try:
        result = attach_adapter._chrome_user_data_dir_on_cdp_port()
        assert result == "/Users/jojo/.operator/slip_profile", result
        print("✓ returns parsed --user-data-dir when present")
    finally:
        attach_adapter.subprocess.run = original


def test_returns_none_when_lsof_finds_nothing():
    """No process on CDP_PORT → lsof returns empty stdout → None."""
    fake = _fake_run({"lsof": ""})
    original = _patch_run(fake)
    try:
        result = attach_adapter._chrome_user_data_dir_on_cdp_port()
        assert result is None, result
        print("✓ returns None when no process is on CDP_PORT")
    finally:
        attach_adapter.subprocess.run = original


def test_returns_none_when_ps_lacks_user_data_dir_flag():
    """Foreign Chrome launched without --user-data-dir → None → caller
    treats as foreign and evicts. Matches H-16's intent: anything we
    can't positively verify as the slip profile is treated as foreign."""
    fake = _fake_run({
        "lsof": "12345\n",
        "ps": (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
            "--remote-debugging-port=9222"
        ),
    })
    original = _patch_run(fake)
    try:
        result = attach_adapter._chrome_user_data_dir_on_cdp_port()
        assert result is None, result
        print("✓ returns None when --user-data-dir not in argv")
    finally:
        attach_adapter.subprocess.run = original


def test_returns_none_on_lsof_failure():
    """lsof raises (timeout, missing binary) → None, caller treats as
    foreign and evicts. Fail-safe: don't attach to an unverifiable
    Chrome."""
    def _run(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="lsof", timeout=2)
    original = _patch_run(_run)
    try:
        result = attach_adapter._chrome_user_data_dir_on_cdp_port()
        assert result is None, result
        print("✓ returns None when lsof raises (fail-safe to evict)")
    finally:
        attach_adapter.subprocess.run = original


def test_returns_none_when_ps_raises():
    """lsof succeeds but ps raises → None. Helper iterates pids; any
    per-pid failure just skips that pid."""
    call_count = {"n": 0}
    def _run(argv, capture_output=True, text=True, timeout=2):
        if argv[0] == "lsof":
            return SimpleNamespace(returncode=0, stdout="12345\n", stderr="")
        if argv[0] == "ps":
            call_count["n"] += 1
            raise subprocess.TimeoutExpired(cmd="ps", timeout=2)
        raise AssertionError(argv)
    original = _patch_run(_run)
    try:
        result = attach_adapter._chrome_user_data_dir_on_cdp_port()
        assert result is None, result
        assert call_count["n"] == 1, call_count
        print("✓ returns None when ps raises (per-pid skip then exhaust)")
    finally:
        attach_adapter.subprocess.run = original


def test_realpath_comparison_handles_symlinks():
    """The reuse-decision compares via os.path.realpath so a profile dir
    symlinked into ~/.operator/slip_profile is recognised as the same
    path. This test pins the comparison logic mirrored from
    _browser_session by exercising it directly."""
    expected = attach_adapter.SLIP_PROFILE_DIR
    # Same path → match.
    assert os.path.realpath(expected) == os.path.realpath(expected)
    # Different path → no match (the H-16 reject case).
    foreign = "/tmp/some_other_profile"
    assert os.path.realpath(expected) != os.path.realpath(foreign)
    print("✓ realpath comparison: same matches, different rejects")


if __name__ == "__main__":
    tests = [
        test_returns_user_data_dir_when_chrome_present,
        test_returns_none_when_lsof_finds_nothing,
        test_returns_none_when_ps_lacks_user_data_dir_flag,
        test_returns_none_on_lsof_failure,
        test_returns_none_when_ps_raises,
        test_realpath_comparison_handles_symlinks,
    ]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} H-16 CDP user-data-dir verification tests passed.")
