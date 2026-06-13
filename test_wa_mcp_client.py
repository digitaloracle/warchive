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
            print("search returned:", sc.get("returned"), file=sys.stderr)
            assert sc.get("returned", 0) <= 2 and "messages" in sc

            # Semantic query loads the embedding model server-side; if its warnings
            # leaked to stdout they'd corrupt the MCP stream and this would fail.
            res2 = await session.call_tool(
                "search_messages", {"query": "pizza", "mode": "semantic", "top_k": 3})
            sc2 = res2.structuredContent
            print("semantic returned:", sc2.get("returned"), file=sys.stderr)
            assert "messages" in sc2 and "error" not in sc2
            print("E2E stdio handshake (incl. semantic): PASS", file=sys.stderr)
            return 0


sys.exit(asyncio.run(main()))
