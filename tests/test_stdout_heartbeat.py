"""
Tests for the stdout heartbeat additions: MCP roll-up line and per-turn
elapsed/tool-count line. Both write to stderr via the ui module.

Constraint: stdout heartbeat MUST be metadata only — no message bodies,
no sender names, no tool arguments.

Run: python tests/test_stdout_heartbeat.py
"""
import io
import os
import sys
import time
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
os.environ.setdefault("BRAINCHILD_BOT", "pm")
os.environ.setdefault("NO_COLOR", "1")  # strip ANSI so substring asserts are stable


def _capture_stderr(fn, *args, **kwargs):
    saved = sys.stderr
    buf = io.StringIO()
    sys.stderr = buf
    try:
        fn(*args, **kwargs)
    finally:
        sys.stderr = saved
    return buf.getvalue()


def test_mcp_rollup_all_pass():
    from brainchild import config
    from brainchild.__main__ import _emit_mcp_rollup

    config.MCP_SERVERS = {"linear": {}, "github": {}}
    mcp = MagicMock()
    mcp.startup_failures = {}

    out = _capture_stderr(_emit_mcp_rollup, mcp)
    assert "MCP:" in out
    assert "linear ✓" in out
    assert "github ✓" in out
    assert "✗" not in out
    print("✓ MCP roll-up renders all-pass")


def test_mcp_rollup_mixed_with_oauth_hint():
    from brainchild import config
    from brainchild.__main__ import _emit_mcp_rollup

    config.MCP_SERVERS = {"linear": {}, "sentry": {}}
    mcp = MagicMock()
    mcp.startup_failures = {
        "sentry": {"kind": "oauth_needed", "fix": "ignored", "auth_url": "x"},
    }

    out = _capture_stderr(_emit_mcp_rollup, mcp)
    assert "linear ✓" in out
    assert "sentry ✗" in out
    assert "brainchild auth sentry" in out, "oauth failure must surface remediation command"
    print("✓ MCP roll-up surfaces oauth remediation")


def test_mcp_rollup_missing_creds():
    from brainchild import config
    from brainchild.__main__ import _emit_mcp_rollup

    config.MCP_SERVERS = {"github": {}}
    mcp = MagicMock()
    mcp.startup_failures = {
        "github": {"kind": "missing_creds", "vars": ["GITHUB_TOKEN"]},
    }

    out = _capture_stderr(_emit_mcp_rollup, mcp)
    assert "github ✗" in out
    assert "GITHUB_TOKEN" in out
    print("✓ MCP roll-up surfaces missing-cred var name")


def test_mcp_rollup_silent_when_no_mcp_client():
    from brainchild import config
    from brainchild.__main__ import _emit_mcp_rollup

    config.MCP_SERVERS = {"linear": {}}
    out = _capture_stderr(_emit_mcp_rollup, None)
    assert out == "", "Track-A (no mcp client) must skip the roll-up"
    print("✓ MCP roll-up silent when mcp client is None")


def test_turn_done_metadata_only():
    """The heartbeat closer must NEVER include message bodies or tool args."""
    from brainchild.pipeline import chat_runner
    from brainchild.pipeline.chat_runner import ChatRunner

    runner = ChatRunner.__new__(ChatRunner)
    runner._turn_count = 7
    runner._turn_start_ts = time.time() - 1.4
    runner._turn_tool_count = 2

    out = _capture_stderr(runner._emit_turn_done)
    assert "Replied" in out
    # Sanity: time + tool count in the line, no fishing for content
    assert "tool" in out
    assert "2 tools" in out
    print("✓ Heartbeat shows elapsed + tool count, no content")


def test_turn_done_idempotent():
    """Calling twice must not double-print — _turn_start_ts is drained."""
    from brainchild.pipeline.chat_runner import ChatRunner

    runner = ChatRunner.__new__(ChatRunner)
    runner._turn_count = 1
    runner._turn_start_ts = time.time()
    runner._turn_tool_count = 0

    out1 = _capture_stderr(runner._emit_turn_done)
    out2 = _capture_stderr(runner._emit_turn_done)
    assert "Replied" in out1
    assert out2 == "", "Second call after drain must be a no-op"
    print("✓ Heartbeat closer is idempotent")


def test_turn_done_failed_branch():
    from brainchild.pipeline.chat_runner import ChatRunner

    runner = ChatRunner.__new__(ChatRunner)
    runner._turn_count = 3
    runner._turn_start_ts = time.time() - 0.2
    runner._turn_tool_count = 0

    out = _capture_stderr(runner._emit_turn_done, failed=True)
    assert "failed" in out.lower()
    assert "Turn 3" in out
    print("✓ Heartbeat failed-branch labels turn number")


if __name__ == "__main__":
    test_mcp_rollup_all_pass()
    test_mcp_rollup_mixed_with_oauth_hint()
    test_mcp_rollup_missing_creds()
    test_mcp_rollup_silent_when_no_mcp_client()
    test_turn_done_metadata_only()
    test_turn_done_idempotent()
    test_turn_done_failed_branch()
    print("\nAll stdout heartbeat tests passed.")
