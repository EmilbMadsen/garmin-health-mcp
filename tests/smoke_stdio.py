"""Manual end-to-end check: start the server over stdio, like an MCP client does, and list tools.

    uv run python tests/smoke_stdio.py          # list tools only, no API key needed
    uv run python tests/smoke_stdio.py --live   # also call two tools (uses the keyring key)

Not part of the pytest suite: it spawns a subprocess, and --live talks to Garmin. The live part
prints only the shape of each result, never values.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def main(live: bool) -> int:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "garmin_health_mcp.server"], env=dict(os.environ)
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        print(f"connected to {init.server_info.name} v{init.server_info.version}")

        tools = (await session.list_tools()).tools
        print(f"\n{len(tools)} tools:")
        for tool in sorted(tools, key=lambda t: t.name):
            read_only = bool(tool.annotations and tool.annotations.read_only_hint)
            summary = (tool.description or "").strip().splitlines()[0]
            print(f"  [{'read ' if read_only else 'WRITE'}] {tool.name:34} {summary[:56]}")

        if live:
            print()
            calls: list[tuple[str, dict[str, Any]]] = [
                ("get_profile_settings", {}),
                ("get_steps", {}),
            ]
            for name, arguments in calls:
                result = await session.call_tool(name, arguments)
                content = result.structured_content or {}
                if result.is_error:
                    print(f"{name} -> error: {result.content}")
                    return 1
                shape = {k: type(v).__name__ for k, v in content.items()}
                print(f"{name} -> ok, {shape}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--live" in sys.argv)))
