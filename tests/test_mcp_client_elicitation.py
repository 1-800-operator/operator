"""
Test MCPClient handling of inbound MCP `elicitation/create` requests.

Drives a real `codex mcp-server` subprocess (the only MCP server we ship with
that emits elicitations today) under `approval-policy: untrusted`, registers an
elicitation handler that auto-approves, and asserts the handler was invoked
with the expected codex envelope.

Skips silently if `codex` is not on PATH or `codex login status` reports not
logged in — keeps CI green on machines without Codex CLI.

Usage:
    python tests/test_mcp_client_elicitation.py
"""
import os
import shutil
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("OPERATOR_BOT", "pm")

from _1_800_operator.pipeline.mcp_client import MCPClient


def _have_codex_cli():
    if shutil.which("codex") is None:
        return False, "codex CLI not on PATH"
    try:
        r = subprocess.run(
            ["codex", "login", "status"], capture_output=True, text=True, timeout=5
        )
    except Exception as e:
        return False, f"codex login status raised: {e}"
    # `codex login status` writes the auth banner to STDERR, not stdout.
    combined = (r.stdout or "") + (r.stderr or "")
    if "ChatGPT" not in combined and "API key" not in combined:
        return False, f"codex not logged in (output: {combined!r})"
    return True, None


def _check(label, cond, detail=""):
    mark = "✓" if cond else "✗"
    print(f"  {mark} {label}{(' — ' + detail) if detail else ''}")
    if not cond:
        raise AssertionError(label)


def test_elicitation_routes_to_handler():
    print("\n1. Inbound elicitation/create routes to registered handler")

    captured = {"params": None, "server_name": None, "count": 0}
    handler_done = threading.Event()

    def handler(server_name, params):
        captured["server_name"] = server_name
        captured["params"] = params
        captured["count"] += 1
        handler_done.set()
        return {"decision": "approved"}

    client = MCPClient()
    client.set_elicitation_handler("codex", handler)
    try:
        client._start_loop()
        client._connect_server("codex", {
            "command": "codex",
            "args": ["mcp-server"],
            "env": {"OPENAI_API_KEY": ""},
        })

        result = client.execute_tool("codex__codex", {
            "prompt": "Run `echo elicitation_test > /tmp/elicit_test.txt` and confirm.",
            "approval-policy": "untrusted",
            "sandbox": "read-only",
            "cwd": "/tmp",
        })

        _check("handler invoked at least once", captured["count"] >= 1,
               f"count={captured['count']}")
        _check("handler received correct server_name",
               captured["server_name"] == "codex")
        _check("handler received codex_command",
               isinstance(captured["params"].get("codex_command"), list))
        _check("handler received codex_cwd",
               captured["params"].get("codex_cwd") == "/tmp")
        _check("handler received codex_elicitation tag",
               captured["params"].get("codex_elicitation") == "exec-approval")
        _check("approved write actually happened",
               os.path.exists("/tmp/elicit_test.txt"))
        _check("tool/call result includes confirmation text",
               "elicitation_test" in result or "confirm" in result.lower(),
               f"result={result[:200]!r}")
    finally:
        try:
            os.remove("/tmp/elicit_test.txt")
        except FileNotFoundError:
            pass
        client.shutdown()


def test_no_handler_blocks_write():
    print("\n2. No handler registered → write is blocked (no silent approval)")

    from _1_800_operator.pipeline.mcp_client import MCPToolError

    client = MCPClient()
    # Deliberately do NOT register an elicitation handler.
    try:
        client._start_loop()
        client._connect_server("codex", {
            "command": "codex",
            "args": ["mcp-server"],
            "env": {"OPENAI_API_KEY": ""},
        })

        # Without an approver, codex cannot proceed. We expect either an
        # error result OR a tool timeout — either way, the write must
        # NOT have happened. The file existence check is the load-bearing
        # safety assertion.
        result = None
        err = None
        try:
            result = client.execute_tool("codex__codex", {
                "prompt": "Run `echo no_handler > /tmp/no_handler_test.txt` and confirm.",
                "approval-policy": "untrusted",
                "sandbox": "read-only",
                "cwd": "/tmp",
            })
        except MCPToolError as e:
            err = e

        _check("declined write did not happen",
               not os.path.exists("/tmp/no_handler_test.txt"))
        _check("call surfaced either a result or an error",
               result is not None or err is not None)
    finally:
        try:
            os.remove("/tmp/no_handler_test.txt")
        except FileNotFoundError:
            pass
        client.shutdown()


def test_invalid_handler_response_blocks_write():
    print("\n3. Handler returns invalid shape → write is blocked")

    from _1_800_operator.pipeline.mcp_client import MCPToolError

    def bad_handler(server_name, params):
        return "not a dict"  # invalid

    client = MCPClient()
    client.set_elicitation_handler("codex", bad_handler)
    try:
        client._start_loop()
        client._connect_server("codex", {
            "command": "codex",
            "args": ["mcp-server"],
            "env": {"OPENAI_API_KEY": ""},
        })

        result = None
        err = None
        try:
            result = client.execute_tool("codex__codex", {
                "prompt": "Run `echo bad_handler > /tmp/bad_handler_test.txt` and confirm.",
                "approval-policy": "untrusted",
                "sandbox": "read-only",
                "cwd": "/tmp",
            })
        except MCPToolError as e:
            err = e

        _check("invalid-shape rejection blocked the write",
               not os.path.exists("/tmp/bad_handler_test.txt"))
        _check("call surfaced either a result or an error",
               result is not None or err is not None)
    finally:
        try:
            os.remove("/tmp/bad_handler_test.txt")
        except FileNotFoundError:
            pass
        client.shutdown()


def main():
    ok, reason = _have_codex_cli()
    if not ok:
        print(f"SKIPPED — {reason}")
        return 0

    print("=" * 50)
    print("MCPClient elicitation handling")
    print("=" * 50)

    failed = 0
    for fn in [
        test_elicitation_routes_to_handler,
        test_no_handler_blocks_write,
        test_invalid_handler_response_blocks_write,
    ]:
        try:
            fn()
        except AssertionError as e:
            print(f"  FAILED: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {e}")
            failed += 1

    print("\n" + "=" * 50)
    if failed == 0:
        print("All tests passed!")
        return 0
    else:
        print(f"{failed} test(s) failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
