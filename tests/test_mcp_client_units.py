"""
Unit gap-fill tests for Component F — MCPClient.

Complements tests/test_mcp_client.py (real subprocess MCP) and
tests/test_mcp_shutdown.py (orphan cleanup). These cover the pure-function
and lightweight branches not exercised by the subprocess-level tests:

  1. _classify_startup_failure — FileNotFoundError → "command not found"
  2. _classify_startup_failure — "process exited" pattern
  3. _classify_startup_failure — unwraps BaseExceptionGroup (anyio wrapping)
  4. server_for_tool + tool_timeout_for — per-server override beats global default
  5. execute_tool — strips unprompted `limit` from Linear calls before MCP invoke
  6. execute_tool — blocks get_file_contents on non-text extensions (pre-execution)

Run:
    source venv/bin/activate
    python tests/test_mcp_client_units.py
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
os.environ.setdefault("OPERATOR_BOT", "pm")

from unittest.mock import MagicMock

from _1_800_operator import config
from _1_800_operator.pipeline.mcp_client import MCPClient, MCPToolError, _classify_startup_failure


# ---------------------------------------------------------------------------
# Test 1: FileNotFoundError → "command not found" message
# ---------------------------------------------------------------------------

def test_classify_file_not_found():
    """FileNotFoundError from stdio_client → kind=binary_missing with fix hint."""
    exc = FileNotFoundError(2, "No such file or directory", "my-mcp-binary")
    info = _classify_startup_failure(exc, {"command": "my-mcp-binary"})
    assert info["kind"] == "binary_missing", info
    assert "was not found" in info["fix"]
    assert "my-mcp-binary" in info["fix"]
    assert "config.yaml" in info["fix"] or "PATH" in info["fix"], \
        f"Expected actionable hint, got: {info}"
    print("PASS  test_classify_file_not_found")


# ---------------------------------------------------------------------------
# Test 2: "process exited" pattern → "exited before handshake"
# ---------------------------------------------------------------------------

def test_classify_process_exited_early():
    """An exception whose message mentions 'process exited' → kind=handshake_crash."""
    exc = RuntimeError("subprocess process exited with code 1")
    info = _classify_startup_failure(exc, {"command": "/bin/echo"})
    assert info["kind"] == "handshake_crash", info
    assert "exited before" in info["fix"].lower()
    assert "/bin/echo" in info["fix"]
    assert "manually" in info["fix"].lower(), \
        f"Expected actionable debugging hint, got: {info}"
    print("PASS  test_classify_process_exited_early")


# ---------------------------------------------------------------------------
# Test 3: BaseExceptionGroup unwrap — reach the real cause
# ---------------------------------------------------------------------------

def test_classify_unwraps_exception_group():
    """anyio TaskGroup wraps failures in BaseExceptionGroup — we must unwrap to the inner cause."""
    inner = FileNotFoundError(2, "No such file or directory", "wrapped-cmd")
    group = BaseExceptionGroup("task group failed", [inner])
    info = _classify_startup_failure(group, {"command": "wrapped-cmd"})
    assert info["kind"] == "binary_missing", info
    assert "was not found" in info["fix"]
    assert "wrapped-cmd" in info["fix"]
    # Must NOT leak the group type name in the user-facing hint
    assert "BaseExceptionGroup" not in info["fix"] and "ExceptionGroup" not in info["fix"]
    print("PASS  test_classify_unwraps_exception_group")


# ---------------------------------------------------------------------------
# Test 4: server_for_tool + tool_timeout_for — override precedence
# ---------------------------------------------------------------------------

def test_tool_timeout_resolution():
    """Per-server override wins; unknown tools return None; servers without override return None."""
    client = MCPClient()
    # Populate _tools directly — avoid spawning real servers
    client._tools = {
        "slow__run_task": {"server_name": "slow", "mcp_tool": MagicMock()},
        "fast__ping": {"server_name": "fast", "mcp_tool": MagicMock()},
    }

    original = dict(config.MCP_SERVERS)
    try:
        config.MCP_SERVERS = {
            "slow": {"command": "x", "args": [], "env": {}, "tool_timeout_seconds": 300},
            "fast": {"command": "x", "args": [], "env": {}},  # no override
        }

        # server_for_tool
        assert client.server_for_tool("slow__run_task") == "slow"
        assert client.server_for_tool("fast__ping") == "fast"
        assert client.server_for_tool("unknown__missing") is None

        # tool_timeout_for — reports explicit override only (None when absent)
        assert client.tool_timeout_for("slow__run_task") == 300
        assert client.tool_timeout_for("fast__ping") is None
        assert client.tool_timeout_for("unknown__missing") is None
    finally:
        config.MCP_SERVERS = original
    print("PASS  test_tool_timeout_resolution")


def test_effective_timeout_precedence():
    """Effective timeout walks: per-server override → DEFAULT_TOOL_TIMEOUTS → global fallback."""
    client = MCPClient()
    client._tools = {
        "override__x":  {"server_name": "override",  "mcp_tool": MagicMock()},
        "defaulted__y": {"server_name": "claude-code", "mcp_tool": MagicMock()},
        "fallback__z":  {"server_name": "novel",    "mcp_tool": MagicMock()},
    }

    orig_servers = dict(config.MCP_SERVERS)
    orig_defaults = dict(config.DEFAULT_TOOL_TIMEOUTS)
    orig_global = config.TOOL_TIMEOUT_SECONDS
    try:
        config.MCP_SERVERS = {
            "override":    {"command": "x", "args": [], "env": {}, "tool_timeout_seconds": 999},
            "claude-code": {"command": "x", "args": [], "env": {}},  # no override → ship default
            "novel":       {"command": "x", "args": [], "env": {}},  # not in default map → global
        }
        config.DEFAULT_TOOL_TIMEOUTS = {"claude-code": 600}
        config.TOOL_TIMEOUT_SECONDS = 60

        assert client._effective_timeout_for("override__x") == 999   # per-server override wins
        assert client._effective_timeout_for("defaulted__y") == 600  # ship default applies
        assert client._effective_timeout_for("fallback__z") == 60    # global fallback
    finally:
        config.MCP_SERVERS = orig_servers
        config.DEFAULT_TOOL_TIMEOUTS = orig_defaults
        config.TOOL_TIMEOUT_SECONDS = orig_global
    print("PASS  test_effective_timeout_precedence")


# ---------------------------------------------------------------------------
# Test 5: Linear `limit` strip — remove unprompted limit before MCP call
# ---------------------------------------------------------------------------

def test_execute_strips_linear_limit():
    """execute_tool strips the 'limit' arg the LLM keeps injecting on Linear list calls."""
    client = MCPClient()
    handle = MagicMock()
    handle.call_tool.return_value = "ok"
    client._tools = {
        "linear__list_issues": {"server_name": "linear", "mcp_tool": MagicMock()},
    }
    client._servers = {"linear": handle}

    result = client.execute_tool("linear__list_issues", {"team": "ENG", "limit": 100})

    assert result == "ok"
    handle.call_tool.assert_called_once()
    tool_name_arg, args_arg = handle.call_tool.call_args.args
    assert tool_name_arg == "list_issues"  # namespace stripped
    assert "limit" not in args_arg, f"Expected 'limit' stripped, got args={args_arg}"
    assert args_arg == {"team": "ENG"}

    # Non-linear servers keep `limit` as-is
    handle.reset_mock()
    client._tools["other__search"] = {"server_name": "other", "mcp_tool": MagicMock()}
    client._servers["other"] = handle
    client.execute_tool("other__search", {"q": "x", "limit": 5})
    _, args_arg2 = handle.call_tool.call_args.args
    assert args_arg2 == {"q": "x", "limit": 5}, \
        f"Expected limit preserved for non-linear server, got: {args_arg2}"
    print("PASS  test_execute_strips_linear_limit")


# ---------------------------------------------------------------------------
# Test 6: Binary-file block — get_file_contents with non-text path
# ---------------------------------------------------------------------------

def test_execute_blocks_binary_file_reads():
    """execute_tool rejects non-text paths for get_file_contents before dispatching to MCP."""
    client = MCPClient()
    handle = MagicMock()
    # Namespaced under any server — the binary check fires on original_name == get_file_contents
    client._tools = {
        "github__get_file_contents": {"server_name": "github", "mcp_tool": MagicMock()},
    }
    client._servers = {"github": handle}

    # Binary extension → blocked, MCP call never fires
    raised = False
    try:
        client.execute_tool("github__get_file_contents", {"path": "assets/logo.png"})
    except MCPToolError as e:
        raised = True
        assert "non-text" in str(e).lower() or "blocked" in str(e).lower()
    assert raised, "Expected MCPToolError on .png path"
    handle.call_tool.assert_not_called()

    # Text extension → proceeds to MCP
    handle.call_tool.return_value = "file body"
    result = client.execute_tool("github__get_file_contents", {"path": "src/main.py"})
    assert result == "file body"
    handle.call_tool.assert_called_once()
    print("PASS  test_execute_blocks_binary_file_reads")


# ---------------------------------------------------------------------------
# Run all
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_classify_file_not_found,
        test_classify_process_exited_early,
        test_classify_unwraps_exception_group,
        test_tool_timeout_resolution,
        test_effective_timeout_precedence,
        test_execute_strips_linear_limit,
        test_execute_blocks_binary_file_reads,
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
