# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp"]
# ///
"""End-to-end MCP test: launch wa_mcp.py as a real stdio server (via uv, exactly
as Claude Desktop will) and drive it as an MCP client.
Run with: uv run --script test_wa_mcp_client.py
"""
import asyncio
import os
import shutil
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

UV = shutil.which("uv") or "uv"   # resolve uv from PATH
HERE = os.path.dirname(os.path.abspath(__file__))


async def main() -> int:
    params = StdioServerParameters(
        command=UV,
        args=["run", "--script", os.path.join(HERE, "wa_mcp.py")],
        cwd=HERE,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print("tools:", names, file=sys.stderr)
            assert names == ["list_chats", "refresh_cache", "search_messages", "wa_status"], names

            status = await session.call_tool("wa_status", {})
            print("wa_status ->", status.structuredContent, file=sys.stderr)

            res = await session.call_tool("search_messages", {"top_k": 2})
            sc = res.structuredContent
            print("search returned:", sc.get("returned"), "of", sc.get("total_matched"),
                  file=sys.stderr)
            assert sc.get("returned", 0) <= 2 and sc.get("total_matched", 0) > 0
            print("E2E stdio handshake: PASS", file=sys.stderr)
            return 0


sys.exit(asyncio.run(main()))
