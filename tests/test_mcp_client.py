"""
Test MCPClient against a real MCP server (a tiny Python one bundled here).

Validates: server spawn, tool discovery, OpenAI schema conversion,
tool execution, error handling, and clean shutdown.

Usage:
    python tests/test_mcp_client.py
"""
import json
import os
import sys
import textwrap
import tempfile

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("OPERATOR_BOT", "pm")

# ── Setup: write a minimal MCP server to a temp file ──────────────────
# This server exposes two tools: "echo" (returns its input) and "fail" (always errors).
_SERVER_CODE = textwrap.dedent("""\
    import asyncio
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    import mcp.types as types

    server = Server("test-server")

    @server.list_tools()
    async def list_tools():
        return [
            types.Tool(
                name="echo",
                description="Returns whatever you send it",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "The message to echo"}
                    },
                    "required": ["message"],
                },
            ),
            types.Tool(
                name="fail",
                description="Always fails with an error",
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name, arguments):
        if name == "echo":
            return [types.TextContent(type="text", text=arguments.get("message", ""))]
        elif name == "fail":
            raise Exception("Intentional test failure")
        else:
            raise Exception(f"Unknown tool: {name}")

    async def main():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(main())
""")

# Write the server to a temp file so MCPClient can spawn it
_server_file = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False)
_server_file.write(_SERVER_CODE)
_server_file.close()

# ── Patch config.MCP_SERVERS before importing MCPClient ───────────────
from _1_800_operator import config
config.MCP_SERVERS = {
    "test": {
        "command": sys.executable,
        "args": [_server_file.name],
        "env": {},
    }
}

from _1_800_operator.pipeline.mcp_client import MCPClient, MCPToolError

passed = 0
failed = 0

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  ✓ {name}")
        passed += 1
    else:
        print(f"  ✗ {name}: {detail}")
        failed += 1


# ── Test 1: Connect and discover tools ────────────────────────────────
print("\n1. Connect and discover tools")
client = MCPClient()
try:
    tool_names = client.connect_all()
    check("connect_all succeeds", True)
    check("discovers 2 tools", len(tool_names) == 2,
          f"got {len(tool_names)}: {tool_names}")
    check("tool names are namespaced", "test__echo" in tool_names and "test__fail" in tool_names,
          f"got {tool_names}")
except Exception as e:
    check("connect_all succeeds", False, str(e))
    print("Cannot continue without connection — exiting")
    os.unlink(_server_file.name)
    sys.exit(1)


# ── Test 2: OpenAI tool format ────────────────────────────────────────
print("\n2. OpenAI tool format")
tools = client.get_openai_tools()
check("returns list", isinstance(tools, list))
check("2 tools", len(tools) == 2, f"got {len(tools)}")

echo_tool = next((t for t in tools if t["function"]["name"] == "test__echo"), None)
check("echo tool found", echo_tool is not None)
if echo_tool:
    check("has type=function", echo_tool["type"] == "function")
    check("has description", echo_tool["function"]["description"] == "Returns whatever you send it")
    check("has parameters with properties",
          "message" in echo_tool["function"]["parameters"].get("properties", {}))


# ── Test 3: Execute tool (success) ────────────────────────────────────
print("\n3. Execute tool (success path)")
try:
    result = client.execute_tool("test__echo", {"message": "hello world"})
    check("execute returns string", isinstance(result, str))
    check("echo returns input", result == "hello world", f"got {result!r}")
except Exception as e:
    check("execute succeeds", False, str(e))


# ── Test 4: Execute tool (error) ──────────────────────────────────────
print("\n4. Execute tool (error path)")
try:
    client.execute_tool("test__fail", {})
    check("raises MCPToolError", False, "no exception raised")
except MCPToolError as e:
    check("raises MCPToolError", True)
    check("error message is descriptive", "fail" in str(e).lower(), str(e))
except Exception as e:
    check("raises MCPToolError (not other)", False, f"got {type(e).__name__}: {e}")


# ── Test 5: Unknown tool ──────────────────────────────────────────────
print("\n5. Unknown tool")
try:
    client.execute_tool("nonexistent__tool", {})
    check("raises MCPToolError", False, "no exception raised")
except MCPToolError as e:
    check("raises MCPToolError", True)
    check("says unknown", "unknown" in str(e).lower(), str(e))


# ── Test 6: Runtime failure backoff ───────────────────────────────────
print("\n6. Runtime failure backoff")
from _1_800_operator.pipeline.mcp_client import RUNTIME_FAILURE_THRESHOLD

# Reset counter state so prior failing tests don't pollute this one.
client._consecutive_errors.clear()
client.runtime_failures.clear()

# Below threshold: failures don't disable.
for i in range(RUNTIME_FAILURE_THRESHOLD - 1):
    tripped = client.record_tool_result("test", False)
    check(f"failure {i+1} below threshold does not trip", not tripped)
check("server not yet disabled",
      "test" not in client.runtime_failures)

# Success resets the counter.
client.record_tool_result("test", True)
check("success resets counter", client._consecutive_errors["test"] == 0)

# Now trip it deliberately.
for i in range(RUNTIME_FAILURE_THRESHOLD - 1):
    client.record_tool_result("test", False)
tripped = client.record_tool_result("test", False)
check("Nth consecutive failure trips server", tripped)
check("server in runtime_failures", "test" in client.runtime_failures)

# Re-reporting after trip does not re-announce.
tripped_again = client.record_tool_result("test", False)
check("subsequent failure does not re-trip", not tripped_again)

# Filter removes disabled-server tools from the LLM-facing list.
tools_after = client.get_openai_tools()
check("disabled server's tools hidden from get_openai_tools",
      all(t["function"]["name"].split("__", 1)[0] != "test" for t in tools_after),
      f"got {[t['function']['name'] for t in tools_after]}")

# execute_tool short-circuits with firm LLM-facing text.
try:
    client.execute_tool("test__echo", {"message": "hi"})
    check("execute_tool short-circuits on disabled server", False, "no exception")
except MCPToolError as e:
    check("execute_tool short-circuits on disabled server", True)
    msg = str(e).lower()
    check("short-circuit message steers LLM",
          "disabled" in msg and "do not retry" in msg,
          str(e))


# ── Test 6b: Brain-MCP exemption from auto-disable ────────────────────
print("\n6b. Brain-MCP exemption from auto-disable")
# When LLM_PROVIDER == "codex_mcp", the `codex` server is the agent's
# brain. Tripping it would silently kill the meeting; record_tool_result
# must bypass the auto-disable.
from _1_800_operator.pipeline import mcp_client as _mc
from _1_800_operator import config as _cfg

# Pretend we're running the codex agent for the next few calls.
_saved_provider = _cfg.LLM_PROVIDER
_cfg.LLM_PROVIDER = "codex_mcp"
client._consecutive_errors.clear()
client.runtime_failures.clear()
try:
    # Drive 5 consecutive failures — well past the threshold.
    for i in range(5):
        tripped = client.record_tool_result("codex", False, error_text="kaboom")
        check(f"codex failure {i+1} does NOT trip auto-disable", not tripped)
    check("codex NOT in runtime_failures after 5 failures",
          "codex" not in client.runtime_failures)

    # Sidekick servers under the codex agent still trip normally.
    client._consecutive_errors.clear()
    for i in range(RUNTIME_FAILURE_THRESHOLD - 1):
        client.record_tool_result("linear", False)
    tripped = client.record_tool_result("linear", False, error_text="kaboom")
    check("sidekick server (linear) still trips normally under codex agent",
          tripped)
    check("linear in runtime_failures",
          "linear" in client.runtime_failures)
finally:
    _cfg.LLM_PROVIDER = _saved_provider
    client.runtime_failures.clear()
    client._consecutive_errors.clear()

# Under the regular claude_cli / openai / anthropic agents, the `codex`
# server name has no special treatment. Auto-disable applies as usual.
_cfg.LLM_PROVIDER = "anthropic"
try:
    for i in range(RUNTIME_FAILURE_THRESHOLD - 1):
        client.record_tool_result("codex", False)
    tripped = client.record_tool_result("codex", False)
    check("codex DOES trip when not the agent's brain", tripped)
finally:
    _cfg.LLM_PROVIDER = _saved_provider
    client.runtime_failures.clear()
    client._consecutive_errors.clear()


# ── Test 7: Shutdown ──────────────────────────────────────────────────
print("\n7. Shutdown")
try:
    client.shutdown()
    check("shutdown completes", True)
    check("tools cleared", len(client._tools) == 0)
    check("servers cleared", len(client._servers) == 0)
except Exception as e:
    check("shutdown completes", False, str(e))


# ── Cleanup ───────────────────────────────────────────────────────────
os.unlink(_server_file.name)

print(f"\n{'='*40}")
print(f"Results: {passed} passed, {failed} failed")
if failed:
    sys.exit(1)
else:
    print("All tests passed!")
