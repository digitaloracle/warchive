#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "mcp",
#     "cryptography",
#     "python-bidi",
#     "python-snappy",
#     "fastembed",
#     "sqlite-vec",
#     "numpy",
# ]
# ///
"""
wa_mcp.py — MCP server flavor of wa_search.py.

Exposes read-only WhatsApp Desktop search as MCP tools so Claude Desktop can
reach the running WhatsApp app from outside its skill/code sandbox. Run via uv
so all dependencies are contained in an isolated, cached environment:

    uv run --script wa_mcp.py            # stdio server (what Claude Desktop launches)

This imports wa_search.py (same directory) and reuses its full decrypt → mirror →
search pipeline. State files (.env, wa_mirror.db, _fromme_cache.json) live next to
wa_search.py. stdout is the MCP protocol channel, so this module never prints to it.
"""

import os
import sqlite3
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

import wa_search

mcp = FastMCP("whatsapp")


def _err(e: Exception) -> dict[str, Any]:
    """Render any exception as a structured tool error instead of crashing the call."""
    if isinstance(e, RuntimeError):
        msg = str(e)
        if "not found" in msg.lower():
            msg = ("WhatsApp isn't reachable and there's no cached data yet — open "
                   "WhatsApp Desktop with a chat loaded, then try again. (" + msg + ")")
        return {"error": msg}
    return {"error": f"{type(e).__name__}: {e}"}


def _direction(from_me) -> str:
    return "sent" if from_me is True else "received" if from_me is False else "unknown"


def _shape(r: dict) -> dict:
    out = {
        "timestamp": r["timestamp"],
        "direction": _direction(r.get("from_me")),
        "display":   r["display"],
        "chatId":    r["chatId"],
        "text":      r["text"],
    }
    if r.get("snippet"):
        out["snippet"] = r["snippet"]
    if "is_match" in r:            # present only when context expansion was used
        out["is_match"] = r["is_match"]
    return out


@mcp.tool()
def search_messages(query: str = "", chat: str = "", phone: str = "",
                    since: str = "", until: str = "", top_k: int = 200,
                    mode: str = "hybrid", direction: str = "any",
                    context: int = 0, recency: bool = False) -> dict[str, Any]:
    """Search WhatsApp Desktop chat history (read-only). All filters are optional
    and combine with AND.

    Args:
        query: keyword(s) / phrase / concept to find. When set, results are
               relevance-ranked; otherwise most-recent first.
        chat:  contact display name or JID fragment (case-insensitive substring).
        phone: phone number, e.g. "+15551234567".
        since: only messages on/after this date, "YYYY-MM-DD".
        until: only messages on/before this date (inclusive), "YYYY-MM-DD".
        top_k: max messages to return (default 200).
        mode:  "hybrid" (lexical + semantic, default), "lexical" (exact/keyword,
               Hebrew-aware), or "semantic" (meaning/paraphrase, incl. Hebrew↔English).
        direction: "any" (default), "me" (only messages you sent), or "them".
        context: include this many messages before/after each hit (same chat) —
               useful for reading a match in its conversational context.
        recency: blend recency into ranking to favor newer matches.

    Returns: {returned, truncated, messages:[{timestamp, direction, display,
    chatId, text, snippet}]}. `direction` is "sent" | "received" | "unknown";
    `truncated` is true if more matches may exist beyond top_k.
    """
    fm = {"me": True, "them": False}.get(direction, None)
    try:
        results = wa_search.query_messages(
            query=query or None, chat=chat or None, phone=phone or None,
            since=since or None, until=until or None, top_k=top_k,
            mode=mode, from_me=fm, context=context, recency=recency)
    except ValueError:
        return {"error": "since/until must be in YYYY-MM-DD format"}
    except Exception as e:  # noqa: BLE001 — surface as structured error, never crash
        return _err(e)

    return {
        "returned": len(results),
        "truncated": bool(top_k and len(results) >= top_k and not context),
        "messages": [_shape(r) for r in results],
    }


@mcp.tool()
def list_chats() -> dict[str, Any]:
    """List all WhatsApp chats with message counts and last-message time, busiest
    first. Use this to discover available contacts/groups and their exact names.

    Returns: {chats:[{display, chatId, messages, last}]}.
    """
    try:
        chats = wa_search.get_chats()
    except Exception as e:  # noqa: BLE001
        return _err(e)
    return {"chats": [{"display": c["display"], "chatId": c["jid"],
                       "messages": c["messages"], "last": c["last"]}
                      for c in chats]}


@mcp.tool()
def refresh_cache() -> dict[str, Any]:
    """Force-rebuild the local cache from the live WhatsApp app. Use this if a
    message you know exists isn't showing up yet (the cache normally refreshes
    itself when new messages arrive). Requires WhatsApp Desktop to be running.

    Returns: {rebuilt, messages, newest}.
    """
    try:
        session_dir = wa_search._find_session_dir()
        wa_search._ensure_mirror(session_dir, force=True)
        newest_rows = wa_search.query_messages(top_k=1)
        newest = newest_rows[0]["timestamp"] if newest_rows else None
        con = sqlite3.connect(wa_search._MIRROR_PATH)
        total = con.execute("SELECT COUNT(*) FROM message").fetchone()[0]
        con.close()
        return {"rebuilt": True, "messages": total, "newest": newest}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def wa_status() -> dict[str, Any]:
    """Diagnostics for the WhatsApp connection and local cache — does NOT rebuild.
    Use this first if other tools error, to see whether WhatsApp is reachable.

    Returns: {whatsapp_running, mirror_exists, mirror_fresh, messages, newest,
    session_dir}.
    """
    out: dict[str, Any] = {
        "whatsapp_running": wa_search._find_wa_pid() is not None,
        "mirror_exists": os.path.exists(wa_search._MIRROR_PATH),
    }
    try:
        session_dir = wa_search._find_session_dir()
        out["session_dir"] = session_dir
        out["mirror_fresh"] = wa_search._mirror_is_fresh(session_dir)
    except Exception as e:  # noqa: BLE001
        out["session_dir"] = None
        out["mirror_fresh"] = False
        out["note"] = str(e)
    if out["mirror_exists"]:
        try:
            con = sqlite3.connect(wa_search._MIRROR_PATH)
            out["messages"] = con.execute("SELECT COUNT(*) FROM message").fetchone()[0]
            mx = con.execute("SELECT MAX(ts_epoch) FROM message").fetchone()[0]
            con.close()
            out["newest"] = wa_search._ts_to_str(mx) if mx else None
        except sqlite3.Error:
            pass
    if wa_search.wa_embed:
        emb = wa_search.wa_embed
        out["embeddings_available"] = bool(getattr(emb, "EMBED_AVAILABLE", False))
        out["embedding_providers"] = getattr(emb, "_PROVIDERS", None) or ["CPUExecutionProvider"]
    return out


def main():
    # Opt-in GPU embeddings: add "--gpu" (auto: CUDA/NVIDIA or DirectML/any DX12
    # GPU) or "--directml" (force Windows DirectML) to the server's args in
    # claude_desktop_config.json, or set WA_GPU=1 / WA_DIRECTML=1. CPU is the
    # default; falls back to CPU if a GPU-enabled onnxruntime isn't installed.
    emb = wa_search.wa_embed
    if emb:
        if "--directml" in sys.argv or os.environ.get("WA_DIRECTML"):
            emb.enable_directml()
        elif "--gpu" in sys.argv or os.environ.get("WA_GPU"):
            emb.enable_gpu()
    mcp.run()   # stdio transport by default


if __name__ == "__main__":
    main()
