"""Empirical capture of MCP auth-failure error strings (15.7.1a).

Boots each bundled MCP server with a deliberately-bad credential and runs
one tool call to capture the exact error text. Writes results to
/tmp/mcp_auth_error_capture.txt for review. Not a test — run manually.
"""
import asyncio
import os
import sys
import traceback
from contextlib import asynccontextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

PROBES = [
    {
        "name": "github",
        "command": "./github-mcp-server",
        "args": ["stdio"],
        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_bogus_token_for_probe_000000000000000000"},
        "tool": "get_me",
        "tool_args": {},
        "cwd": REPO,
    },
    {
        "name": "figma",
        "command": "npx",
        "args": ["-y", "figma-developer-mcp@0.10.1", "--stdio"],
        "env": {"FIGMA_API_KEY": "figd_bogus_pat_for_probe_00000000"},
        "tool": "get_figma_data",
        "tool_args": {"fileKey": "aaaaaaaaaaaaaaaaaaaaaa"},
        "cwd": REPO,
    },
    {
        "name": "linear-mcp-remote",
        "command": "npx",
        "args": ["-y", "mcp-remote", "https://mcp.linear.app/sse"],
        "env": {"MCP_REMOTE_CONFIG_DIR": "/tmp/operator_probe_empty_cache"},
        "tool": "list_teams",
        "tool_args": {},
        "cwd": REPO,
    },
]


async def probe_one(spec, out):
    out.write(f"\n=== {spec['name']} ===\n")
    out.write(f"command: {spec['command']} {' '.join(spec['args'])}\n")
    env = {**os.environ, **spec["env"]}
    # Ensure no real token leaks through for servers we explicitly override.
    for k in spec["env"]:
        out.write(f"env override: {k}=<bogus>\n")

    params = StdioServerParameters(
        command=spec["command"],
        args=spec["args"],
        env=env,
        cwd=spec["cwd"],
    )

    try:
        async with asyncio.timeout(45):
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    out.write("handshake: OK\n")
                    tools = await session.list_tools()
                    out.write(f"tools discovered: {len(tools.tools)}\n")
                    try:
                        result = await session.call_tool(spec["tool"], spec["tool_args"])
                        out.write(f"isError={result.isError}\n")
                        for c in result.content:
                            text = getattr(c, "text", None) or str(c)
                            out.write(f"content: {text[:2000]}\n")
                    except Exception as e:
                        out.write(f"call_tool EXCEPTION: {type(e).__name__}: {e}\n")
                        out.write(traceback.format_exc(limit=3))
    except Exception as e:
        out.write(f"CONNECT/INIT EXCEPTION: {type(e).__name__}: {e}\n")
        out.write(traceback.format_exc(limit=3))
    out.flush()


async def main():
    outpath = "/tmp/mcp_auth_error_capture.txt"
    with open(outpath, "w") as out:
        out.write(f"MCP auth-error empirical capture\n")
        out.write(f"================================\n")
        for spec in PROBES:
            try:
                await probe_one(spec, out)
            except Exception as e:
                out.write(f"\nouter exception on {spec['name']}: {e}\n")
    print(f"wrote {outpath}")


if __name__ == "__main__":
    asyncio.run(main())
