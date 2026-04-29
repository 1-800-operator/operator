"""
Test that MCP startup failures get surfaced in meeting chat (not just logs).

The user has been losing visibility into degraded MCP state — silent
failures during connect leave them confused why a tool doesn't work.
This validates that _emit_mcp_rollup posts a one-liner to the
connector when failures exist, and stays silent when they don't.

Usage:
    python tests/test_mcp_failure_chat.py
"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BRAINCHILD_BOT", "pm")


class _FakeMCP:
    def __init__(self, startup_failures=None):
        self.startup_failures = startup_failures or {}


class _FakeConnector:
    def __init__(self):
        self.sent: list[str] = []

    def send_chat(self, msg):
        self.sent.append(msg)


def test_no_failures_no_chat():
    from brainchild.__main__ import _emit_mcp_rollup
    connector = _FakeConnector()
    mcp = _FakeMCP(startup_failures={})
    with patch("brainchild.config.MCP_SERVERS", {"sentry": {}, "linear": {}}):
        _emit_mcp_rollup(mcp, connector=connector)
    assert connector.sent == [], f"chat sent on success-only: {connector.sent}"
    print("✓ silent on success — no chat clutter when MCPs are fine")


def test_one_failure_sends_chat():
    from brainchild.__main__ import _emit_mcp_rollup
    connector = _FakeConnector()
    mcp = _FakeMCP(startup_failures={
        "sentry": {"kind": "missing_creds", "vars": ["SENTRY_TOKEN"]},
    })
    with patch("brainchild.config.MCP_SERVERS", {"sentry": {}, "linear": {}}):
        _emit_mcp_rollup(mcp, connector=connector)
    assert len(connector.sent) == 1, connector.sent
    msg = connector.sent[0]
    assert "1 MCP server failed" in msg, msg
    assert "sentry" in msg, msg
    print("✓ one failure → one chat message")


def test_multiple_failures_one_message():
    from brainchild.__main__ import _emit_mcp_rollup
    connector = _FakeConnector()
    mcp = _FakeMCP(startup_failures={
        "sentry": {"kind": "missing_creds", "vars": ["SENTRY_TOKEN"]},
        "linear": {"kind": "oauth_needed"},
    })
    with patch("brainchild.config.MCP_SERVERS", {"sentry": {}, "linear": {}}):
        _emit_mcp_rollup(mcp, connector=connector)
    assert len(connector.sent) == 1, "multiple failures should batch into one chat msg"
    msg = connector.sent[0]
    assert "2 MCP servers failed" in msg, msg
    assert "sentry" in msg and "linear" in msg, msg
    print("✓ multiple failures batched into one chat message")


def test_no_connector_does_not_break():
    """The Track-A path doesn't have an MCP rollup, but if some other
    caller invokes without a connector, it must not crash."""
    from brainchild.__main__ import _emit_mcp_rollup
    mcp = _FakeMCP(startup_failures={"sentry": {"kind": "missing_creds"}})
    with patch("brainchild.config.MCP_SERVERS", {"sentry": {}}):
        # Should print to terminal via ui.say, not raise.
        _emit_mcp_rollup(mcp, connector=None)
    print("✓ connector=None path still functions")


def test_send_chat_exception_doesnt_propagate():
    """If send_chat raises (e.g. connector not yet ready), the rollup
    should log and continue — not crash startup."""
    from brainchild.__main__ import _emit_mcp_rollup

    class _BrokenConnector:
        def send_chat(self, msg):
            raise RuntimeError("simulated chat send failure")

    mcp = _FakeMCP(startup_failures={"sentry": {"kind": "missing_creds"}})
    with patch("brainchild.config.MCP_SERVERS", {"sentry": {}}):
        _emit_mcp_rollup(mcp, connector=_BrokenConnector())
    print("✓ send_chat exceptions are swallowed")


if __name__ == "__main__":
    test_no_failures_no_chat()
    test_one_failure_sends_chat()
    test_multiple_failures_one_message()
    test_no_connector_does_not_break()
    test_send_chat_exception_doesnt_propagate()
    print("\nAll MCP failure-chat tests passed.")
