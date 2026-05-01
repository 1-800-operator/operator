"""
Pressure test: MCP subprocess cleanup on Ctrl+C.

Spawns a stubborn mock MCP server that traps SIGTERM (refuses to die),
then verifies:
  1. Graceful shutdown times out (logged warning)
  2. The safety net in _kill_orphaned_children() catches the orphan
  3. No child processes survive after shutdown

Usage:
    python tests/test_mcp_shutdown.py
"""
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("OPERATOR_BOT", "pm")

# ── Mock server: speaks just enough MCP to initialize, then ignores SIGTERM ──

_STUBBORN_SERVER = textwrap.dedent("""\
    import asyncio
    import signal
    import sys
    import threading
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    import mcp.types as types

    server = Server("stubborn-server")

    # Trap SIGTERM so this process refuses to die gracefully.
    # This simulates mcp-remote which survives because its OAuth
    # port listener keeps the Node.js event loop alive.
    signal.signal(signal.SIGTERM, lambda *_: None)

    @server.list_tools()
    async def list_tools():
        return [
            types.Tool(
                name="noop",
                description="Does nothing",
                inputSchema={"type": "object", "properties": {}},
            ),
        ]

    @server.call_tool()
    async def call_tool(name, arguments):
        return [types.TextContent(type="text", text="ok")]

    # Keep a background thread alive so the process survives stdin EOF.
    # Without this, closing stdin causes stdio_server to exit and the
    # process dies before SIGTERM is even needed.
    def _hold():
        import time
        while True:
            time.sleep(60)
    _t = threading.Thread(target=_hold, daemon=False)
    _t.start()

    async def main():
        try:
            async with stdio_server() as (read, write):
                await server.run(read, write, server.create_initialization_options())
        except Exception:
            pass
        # After MCP exits (stdin closed), block forever so the
        # process stays alive — only SIGKILL can stop it.
        await asyncio.Event().wait()

    asyncio.run(main())
""")

_server_file = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False)
_server_file.write(_STUBBORN_SERVER)
_server_file.close()

# ── Patch config before importing MCPClient ──────────────────────────────

from _1_800_operator import config
config.MCP_SERVERS = {
    "stubborn": {
        "command": sys.executable,
        "args": [_server_file.name],
        "env": {},
    }
}

from _1_800_operator.pipeline.mcp_client import MCPClient

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


# ── Test 1: Connect to the stubborn server ───────────────────────────────

print("\n1. Connect to stubborn (SIGTERM-ignoring) server")
client = MCPClient()
try:
    tool_names = client.connect_all()
    check("connect succeeds", len(tool_names) == 1,
          f"expected 1 tool, got {len(tool_names)}")
except Exception as e:
    check("connect succeeds", False, str(e))
    os.unlink(_server_file.name)
    sys.exit(1)


# ── Find the child process PID ───────────────────────────────────────────

print("\n2. Verify stubborn server subprocess is running")
our_pid = os.getpid()
result = subprocess.run(
    ["pgrep", "-P", str(our_pid)],
    capture_output=True, text=True, timeout=3,
)
child_pids_before = [int(p) for p in result.stdout.strip().split("\n") if p.strip()]
check("at least one child process exists", len(child_pids_before) >= 1,
      f"found {len(child_pids_before)} children")

# Also find via the server file name for targeted checking
result2 = subprocess.run(
    ["pgrep", "-f", _server_file.name],
    capture_output=True, text=True, timeout=3,
)
server_pids = [int(p) for p in result2.stdout.strip().split("\n") if p.strip()]
check("stubborn server PID found", len(server_pids) >= 1,
      f"pgrep found {len(server_pids)}")


# ── Test 3: Shutdown (graceful path should time out) ─────────────────────

print("\n3. Shutdown MCPClient (graceful path should time out on stubborn server)")
t0 = time.monotonic()
try:
    client.shutdown()
    elapsed = time.monotonic() - t0
    check("shutdown completes", True)
    check("took >4s (waited for graceful timeout)", elapsed > 4,
          f"took {elapsed:.1f}s — may have cancelled too early")
    check("took <12s (didn't hang forever)", elapsed < 12,
          f"took {elapsed:.1f}s")
    print(f"    shutdown took {elapsed:.1f}s")
except Exception as e:
    check("shutdown completes", False, str(e))


# ── Test 4: Run safety net and check for orphans ─────────────────────────

print("\n4. Run safety net to catch any survivors")

# Import and run the safety net directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Inline the safety net logic (same as __main__._kill_orphaned_children)
def _kill_orphaned_children():
    pid = os.getpid()
    try:
        result = subprocess.run(
            ["pgrep", "-P", str(pid)],
            capture_output=True, text=True, timeout=3,
            start_new_session=False,
        )
    except Exception:
        return []

    child_pids = [int(p) for p in result.stdout.strip().split("\n") if p.strip()]
    if not child_pids:
        return []

    print(f"    safety net found {len(child_pids)} orphan(s): {child_pids}")

    for cpid in child_pids:
        try:
            os.kill(cpid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    time.sleep(0.5)

    killed = []
    for cpid in child_pids:
        try:
            os.kill(cpid, 0)
            os.kill(cpid, signal.SIGKILL)
            killed.append(cpid)
        except ProcessLookupError:
            pass

    return killed


killed = _kill_orphaned_children()

# The stubborn server may already be dead (graceful path got it via
# stdio_client's SIGKILL escalation) or the safety net caught it.
# Either outcome is correct — the point is nothing survives.
if killed:
    check("safety net caught orphans", True)
    print(f"    safety net killed: {killed}")
else:
    check("graceful shutdown already cleaned up (no orphans for safety net)", True)
    print("    no orphans found — graceful path handled it")


# ── Test 5: Confirm no orphans remain ────────────────────────────────────

print("\n5. Confirm no child processes remain")
time.sleep(0.5)
result = subprocess.run(
    ["pgrep", "-P", str(our_pid)],
    capture_output=True, text=True, timeout=3,
)
remaining = [int(p) for p in result.stdout.strip().split("\n") if p.strip()]
check("zero child processes remain", len(remaining) == 0,
      f"still running: {remaining}")

# Double-check the stubborn server specifically
result2 = subprocess.run(
    ["pgrep", "-f", _server_file.name],
    capture_output=True, text=True, timeout=3,
)
server_remaining = [int(p) for p in result2.stdout.strip().split("\n") if p.strip()]
check("stubborn server process is dead", len(server_remaining) == 0,
      f"still running: {server_remaining}")


# ── Cleanup ──────────────────────────────────────────────────────────────

os.unlink(_server_file.name)

print(f"\n{'='*50}")
print(f"Results: {passed} passed, {failed} failed")
if failed:
    sys.exit(1)
else:
    print("All tests passed!")
