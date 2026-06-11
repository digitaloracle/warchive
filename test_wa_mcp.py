# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp", "cryptography", "python-bidi", "python-snappy"]
# ///
"""Smoke test for wa_mcp.py — run with: uv run --script test_wa_mcp.py"""
import asyncio
import io
import re
import sys
from contextlib import redirect_stdout

failures = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}",
          file=sys.stderr)
    if not cond:
        failures.append(name)


# 1) stdout discipline: importing wa_mcp + calling tools must emit nothing to stdout
buf = io.StringIO()
with redirect_stdout(buf):
    import wa_mcp
    res = wa_mcp.search_messages(top_k=3)   # most recent 3 across all chats
    chats = wa_mcp.list_chats()
    status = wa_mcp.wa_status()
stdout_text = buf.getvalue()
check("stdout stays clean during import + tool calls", stdout_text == "",
      f"captured {len(stdout_text)} chars")

# 2) tool registration: exactly the 4 designed tools
tools = asyncio.run(wa_mcp.mcp.list_tools())
names = sorted(t.name for t in tools)
check("4 tools registered", names == ["list_chats", "refresh_cache", "search_messages", "wa_status"],
      str(names))

# 3) search_messages shape + cap
check("search_messages returns capped messages", res.get("returned", 0) <= 3 and "total_matched" in res,
      f"returned={res.get('returned')} total={res.get('total_matched')}")
if res.get("messages"):
    m = res["messages"][0]
    check("message has expected keys",
          set(m) == {"timestamp", "direction", "display", "chatId", "text"}, str(set(m)))
    check("direction is valid", m["direction"] in ("sent", "received", "unknown"), m["direction"])

# 4) list_chats shape
check("list_chats returns chats", isinstance(chats.get("chats"), list) and len(chats["chats"]) > 0,
      f"{len(chats.get('chats', []))} chats")
if chats.get("chats"):
    c = chats["chats"][0]
    check("chat has expected keys", set(c) == {"display", "chatId", "messages", "last"}, str(set(c)))

# 5) wa_status diagnostics
check("wa_status has core fields",
      {"whatsapp_running", "mirror_exists", "mirror_fresh"} <= set(status), str(sorted(status)))
check("wa_status reports messages", status.get("messages", 0) > 0, f"messages={status.get('messages')}")

# 6) timestamps are well-formed (proves the data path resolves through the MCP layer)
ts = res["messages"][0]["timestamp"] if res.get("messages") else ""
check("messages have well-formed timestamps",
      bool(re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", ts)), f"sample={ts}")

print(f"\n{'ALL PASS' if not failures else 'FAILURES: ' + ', '.join(failures)}", file=sys.stderr)
sys.exit(1 if failures else 0)
