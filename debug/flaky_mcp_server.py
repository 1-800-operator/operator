"""
Flaky MCP server for live-testing step 10.6 runtime failure backoff.

Exposes a single tool, `check_weather`, that always raises. Use it to watch
Operator trip the server after 3 failures, announce once in chat, and hide
the tool from the LLM for the rest of the session.

Add this to the active bot's agents/<name>/config.yaml under mcp_servers:

  flaky:
    command: "python"
    args: ["debug/flaky_mcp_server.py"]
    hints: |
      Use check_weather when the user asks about the weather.
"""
import asyncio

from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

server = Server("flaky")


@server.list_tools()
async def list_tools():
    return [
        types.Tool(
            name="check_weather",
            description="Get the current weather for a city. Call this whenever the user asks about weather.",
            inputSchema={
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City name"},
                },
                "required": ["city"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name, arguments):
    raise RuntimeError("weather service unreachable (simulated flaky failure)")


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
