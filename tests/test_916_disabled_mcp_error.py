"""
test_916_disabled_mcp_error.py — Step 9.16: Granular "MCP disabled" runtime error

When the LLM invokes a tool whose namespaced prefix matches a disabled MCP server
(server present in config.yaml with `enabled: false`), MCPClient.execute_tool
raises an actionable MCPToolError naming the disabled server and remediation,
instead of the generic "Unknown tool" message.

Covers the wizard-bypass case where the user hand-edits config.yaml and the
runtime-only safety net described in the Phase 15.10 design.

Run: python tests/test_916_disabled_mcp_error.py
"""
import os
import sys

os.environ.setdefault("OPERATOR_BOT", "pm")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _1_800_operator import config
from _1_800_operator.pipeline.mcp_client import (
    MCPClient,
    MCPToolError,
    disabled_server_for_tool,
)


def test_disabled_server_for_tool_matches_prefix():
    """Tool name with __-prefix matching a disabled server resolves to that server."""
    original = dict(config.DISABLED_MCP_SERVERS)
    try:
        config.DISABLED_MCP_SERVERS["linear"] = {}
        assert disabled_server_for_tool("linear__create_issue") == "linear"
        assert disabled_server_for_tool("linear__list_issues") == "linear"
    finally:
        config.DISABLED_MCP_SERVERS.clear()
        config.DISABLED_MCP_SERVERS.update(original)
    print("✓ disabled_server_for_tool resolves matching prefix")


def test_disabled_server_for_tool_ignores_unknown_prefix():
    """Namespaced tool whose prefix isn't a disabled server returns None."""
    original = dict(config.DISABLED_MCP_SERVERS)
    try:
        config.DISABLED_MCP_SERVERS.clear()
        config.DISABLED_MCP_SERVERS["linear"] = {}
        # Prefix doesn't match the only disabled server.
        assert disabled_server_for_tool("github__create_issue") is None
        # Bare tool name (no "__") has no prefix to parse.
        assert disabled_server_for_tool("create_issue") is None
    finally:
        config.DISABLED_MCP_SERVERS.clear()
        config.DISABLED_MCP_SERVERS.update(original)
    print("✓ disabled_server_for_tool ignores non-disabled + bare names")


def test_disabled_server_for_tool_distinguishes_similar_servers():
    """Collision case: enabled 'linear' vs disabled 'linear_backup' resolve distinctly."""
    original = dict(config.DISABLED_MCP_SERVERS)
    try:
        # Only linear_backup is disabled; linear is enabled (so not in the dict).
        config.DISABLED_MCP_SERVERS["linear_backup"] = {}
        assert disabled_server_for_tool("linear_backup__create_issue") == "linear_backup"
        # The enabled server's tool should NOT be routed to the disabled error.
        assert disabled_server_for_tool("linear__create_issue") is None
    finally:
        config.DISABLED_MCP_SERVERS.clear()
        config.DISABLED_MCP_SERVERS.update(original)
    print("✓ disabled_server_for_tool distinguishes similarly-named servers")


def test_execute_tool_raises_actionable_error_for_disabled_server():
    """execute_tool() on an unknown tool whose prefix is disabled names the server."""
    original = dict(config.DISABLED_MCP_SERVERS)
    client = MCPClient()
    try:
        config.DISABLED_MCP_SERVERS["linear"] = {}
        try:
            client.execute_tool("linear__create_issue", {"title": "x"})
        except MCPToolError as e:
            msg = str(e)
            assert "linear" in msg, f"error should name the disabled server: {msg!r}"
            assert "disabled" in msg.lower(), f"error should explain state: {msg!r}"
            assert "operator build" in msg or "enabled: true" in msg, (
                f"error should carry remediation: {msg!r}"
            )
        else:
            raise AssertionError("expected MCPToolError, got none")
    finally:
        config.DISABLED_MCP_SERVERS.clear()
        config.DISABLED_MCP_SERVERS.update(original)
    print("✓ execute_tool raises actionable error for disabled-server prefix")


def test_execute_tool_preserves_generic_error_for_truly_unknown_tool():
    """Truly-unknown tool (no prefix match) still raises the generic 'Unknown tool' error."""
    original = dict(config.DISABLED_MCP_SERVERS)
    client = MCPClient()
    try:
        config.DISABLED_MCP_SERVERS.clear()
        try:
            client.execute_tool("mystery__tool", {})
        except MCPToolError as e:
            msg = str(e)
            assert "Unknown tool" in msg, f"expected generic message, got: {msg!r}"
            assert "disabled" not in msg.lower(), f"should not claim disabled: {msg!r}"
        else:
            raise AssertionError("expected MCPToolError, got none")
    finally:
        config.DISABLED_MCP_SERVERS.clear()
        config.DISABLED_MCP_SERVERS.update(original)
    print("✓ execute_tool keeps generic Unknown tool for non-disabled prefix")


def test_config_loader_populates_disabled_dict():
    """config.py surfaces every `enabled: false` server in DISABLED_MCP_SERVERS."""
    # OPERATOR_BOT=pm loaded at import time; pm ships with several disabled
    # MCPs today (github, and all the 15.7.5 scaffolded ones: slack, salesforce,
    # playwright, sentry). We don't assert a specific name list (that would
    # couple this test to config.yaml edits); we just verify the dict is
    # populated when disabled entries exist, and entries aren't in MCP_SERVERS.
    enabled_names = set(config.MCP_SERVERS.keys())
    disabled_names = set(config.DISABLED_MCP_SERVERS.keys())
    assert not (enabled_names & disabled_names), (
        f"server appears in both enabled and disabled: {enabled_names & disabled_names}"
    )
    # pm bundled config has at least one disabled entry post-15.7.5, so this
    # should be non-empty on a fresh checkout. If the bundled config ever
    # enables every MCP by default, relax to `>= 0`.
    assert len(disabled_names) > 0, (
        "expected at least one disabled MCP in pm's bundled config; "
        "if pm now enables every server by default, relax this assertion"
    )
    print(f"✓ config loader populates DISABLED_MCP_SERVERS "
          f"({len(enabled_names)} enabled, {len(disabled_names)} disabled)")


if __name__ == "__main__":
    test_disabled_server_for_tool_matches_prefix()
    test_disabled_server_for_tool_ignores_unknown_prefix()
    test_disabled_server_for_tool_distinguishes_similar_servers()
    test_execute_tool_raises_actionable_error_for_disabled_server()
    test_execute_tool_preserves_generic_error_for_truly_unknown_tool()
    test_config_loader_populates_disabled_dict()
    print("\nAll test_916 checks passed.")
