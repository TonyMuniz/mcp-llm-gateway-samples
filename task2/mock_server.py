"""Mock downstream MCP server (streamable HTTP, JSON responses only).

Implements just enough of MCP for the gateway demo: initialize, ping,
tools/list and tools/call. It records every request it receives in
``app[RECEIVED]`` so tests can prove what did (or did not) reach it.

Run:  python mock_server.py --port 9000
"""

from __future__ import annotations

import argparse
import json
import logging
from typing import Any

from aiohttp import web

log = logging.getLogger("mock")

RECEIVED = web.AppKey("received", list)

TOOLS = [
    {"name": "get_status", "description": "Return service status.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "echo", "description": "Echo back the given text.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
    {"name": "admin_reset_key", "description": "Reset an API key (admin only).",
     "inputSchema": {"type": "object", "properties": {"key_id": {"type": "string"}},
                     "required": ["key_id"]}},
    {"name": "admin_list_keys", "description": "List API keys (admin only).",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _text(req_id: Any, text: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id,
            "result": {"content": [{"type": "text", "text": text}], "isError": False}}


def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def dispatch(msg: dict[str, Any]) -> dict[str, Any] | None:
    method, req_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "mock-mcp", "version": "0.1"}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name == "get_status":
            return _text(req_id, "ok")
        if name == "echo":
            return _text(req_id, str(args.get("text", "")))
        if name == "admin_reset_key":
            return _text(req_id, f"key {args.get('key_id', '?')} reset")
        if name == "admin_list_keys":
            return _text(req_id, "key-1, key-2")
        return _error(req_id, -32602, f"Unknown tool: {name}")
    if "id" not in msg:
        return None  # notification: no response
    return _error(req_id, -32601, f"Method not found: {method}")


async def handle_post(request: web.Request) -> web.Response:
    raw = await request.read()
    msg = json.loads(raw)
    request.app[RECEIVED].append({"headers": dict(request.headers), "message": msg})
    reply = dispatch(msg)
    headers = {"Mcp-Session-Id": "mock-session-1"} if msg.get("method") == "initialize" else {}
    if reply is None:
        return web.Response(status=202, headers=headers)
    return web.json_response(reply, headers=headers)


def create_app() -> web.Application:
    app = web.Application()
    app[RECEIVED] = []
    app.router.add_post("/mcp", handle_post)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock downstream MCP server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    web.run_app(create_app(), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
