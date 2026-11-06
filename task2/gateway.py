"""MCP Gateway: a reverse proxy with method-level authorization for MCP over HTTP.

Sits between an AI agent client and a downstream MCP server (streamable HTTP
transport, JSON-RPC 2.0 over POST).

    client --Bearer token--> [gateway] --plain JSON-RPC--> downstream MCP server

Rules:
  * Every request needs ``Authorization: Bearer <token>``; the token maps to a role.
    Unknown/missing token -> HTTP 401.
  * tools/call whose params.name starts with ``admin_`` needs role ``admin``.
    Otherwise the gateway answers JSON-RPC error -32001 itself and never
    contacts the downstream server.
  * Everything else (tools/list, initialize, ...) is forwarded transparently.

Run:  python gateway.py --downstream http://127.0.0.1:9000
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import unicodedata
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout, web

log = logging.getLogger("gateway")

DOWNSTREAM = web.AppKey("downstream", str)
TOKENS = web.AppKey("tokens", dict)
SESSION = web.AppKey("session", ClientSession)

# JSON-RPC 2.0 standard codes plus the one this task defines.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNAUTHORIZED_TOOL_CALL = -32001

ADMIN_PREFIX = "admin_"
ADMIN_ROLE = "admin"

# Demo tokens. Override with env GATEWAY_TOKENS="tokenA=admin,tokenB=viewer".
DEFAULT_TOKENS = {"admin-token": "admin", "viewer-token": "viewer"}

# Only these request headers reach the downstream server. Authorization is
# deliberately absent: the downstream must not see the caller's credential.
FORWARD_REQUEST_HEADERS = (
    "Content-Type",
    "Accept",
    "Mcp-Session-Id",
    "Mcp-Protocol-Version",
    "Last-Event-ID",
)
# Response headers we must not copy (hop-by-hop, or invalidated because aiohttp
# already decoded/re-frames the body).
SKIP_RESPONSE_HEADERS = {
    "connection", "keep-alive", "transfer-encoding", "upgrade", "te", "trailer",
    "proxy-authenticate", "proxy-authorization", "content-length", "content-encoding",
}


# ---------------------------------------------------------------- authentication

def parse_tokens(spec: str) -> dict[str, str]:
    """Parse "tok=role,tok2=role2" into a dict."""
    tokens: dict[str, str] = {}
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        token, sep, role = item.partition("=")
        if not sep or not token or not role:
            raise ValueError(f"bad token entry {item!r}; expected token=role")
        tokens[token.strip()] = role.strip()
    return tokens


def resolve_role(auth_header: str | None, tokens: dict[str, str]) -> str | None:
    """Return the role for an ``Authorization: Bearer <token>`` header, else None.

    This is the single place to swap in real validation (e.g. verify a JWT and
    read its ``role`` claim) without touching the rest of the gateway.
    """
    if not auth_header:
        return None
    scheme, _, token = auth_header.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    found: str | None = None
    for known, role in tokens.items():  # compare against all, in constant time per entry
        if hmac.compare_digest(known.encode(), token.encode()):
            found = role
    return found


# ---------------------------------------------------------------- JSON-RPC helpers

def rpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def is_admin_tool(name: str) -> bool:
    # NFKC + casefold so look-alikes such as "ADMIN_x" or fullwidth "ａｄｍｉｎ_x"
    # cannot slip past a downstream that is more lenient than we are.
    normalized = unicodedata.normalize("NFKC", name).strip().casefold()
    return normalized.startswith(ADMIN_PREFIX)


def authorize(msg: dict[str, Any], role: str) -> dict[str, Any] | None:
    """Return a JSON-RPC error object if the message must be blocked, else None."""
    if msg.get("method") != "tools/call":
        return None
    req_id = msg.get("id")
    params = msg.get("params")
    name = params.get("name") if isinstance(params, dict) else None
    if not isinstance(name, str):
        return rpc_error(req_id, INVALID_PARAMS, "tools/call requires params.name (string)")
    if is_admin_tool(name) and role != ADMIN_ROLE:
        return rpc_error(req_id, UNAUTHORIZED_TOOL_CALL, "Unauthorized Tool Call")
    return None


# ---------------------------------------------------------------- HTTP handler

async def handle(request: web.Request) -> web.StreamResponse:
    app = request.app
    role = resolve_role(request.headers.get("Authorization"), app[TOKENS])
    if role is None:
        return web.json_response(
            {"error": "unauthorized", "message": "Missing or invalid bearer token"},
            status=401,
            headers={"WWW-Authenticate": 'Bearer realm="mcp-gateway"'},
        )

    body: bytes | None = None
    req_id: Any = None
    if request.method == "POST":
        raw = await request.read()
        try:
            msg = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return web.json_response(rpc_error(None, PARSE_ERROR, "Parse error"))
        if isinstance(msg, list):
            # MCP (2025-06-18) dropped JSON-RPC batching; refusing is safer than
            # forwarding sub-requests we have not individually authorized.
            return web.json_response(rpc_error(None, INVALID_REQUEST, "Batch requests are not supported"))
        if not isinstance(msg, dict) or ("method" in msg and not isinstance(msg["method"], str)):
            return web.json_response(rpc_error(None, INVALID_REQUEST, "Invalid Request"))

        req_id = msg.get("id")
        denial = authorize(msg, role)
        log.info("role=%s method=%s tool=%s decision=%s", role, msg.get("method"),
                 (msg.get("params") or {}).get("name") if isinstance(msg.get("params"), dict) else None,
                 "deny" if denial else "allow")
        if denial is not None:
            if "id" not in msg:  # a notification: JSON-RPC says never reply
                return web.Response(status=202)
            return web.json_response(denial)

        # Forward what we *parsed and checked*, not the raw bytes. This closes
        # parser-differential tricks such as duplicate "name" keys, where we
        # and the downstream might pick different values.
        body = json.dumps(msg).encode()

    return await forward(request, body, req_id)


async def forward(request: web.Request, body: bytes | None, req_id: Any) -> web.StreamResponse:
    app = request.app
    url = app[DOWNSTREAM] + request.path_qs
    headers = {h: request.headers[h] for h in FORWARD_REQUEST_HEADERS if h in request.headers}
    session: ClientSession = app[SESSION]
    resp: web.StreamResponse | None = None
    try:
        async with session.request(request.method, url, data=body, headers=headers,
                                   allow_redirects=False) as upstream:
            out_headers = {k: v for k, v in upstream.headers.items()
                           if k.lower() not in SKIP_RESPONSE_HEADERS}
            resp = web.StreamResponse(status=upstream.status, headers=out_headers)
            await resp.prepare(request)
            async for chunk in upstream.content.iter_any():  # streams SSE too
                await resp.write(chunk)
            await resp.write_eof()
            return resp
    except (ClientError, TimeoutError) as exc:
        log.warning("downstream failure: %s", exc)
        if resp is not None and resp.prepared:
            raise  # headers already sent; the only honest option is to drop the connection
        return web.json_response(rpc_error(req_id, INTERNAL_ERROR, "Downstream server unavailable"),
                                 status=502)


# ---------------------------------------------------------------- app wiring

def create_app(downstream: str, tokens: dict[str, str] | None = None) -> web.Application:
    app = web.Application(client_max_size=1024 * 1024)
    app[DOWNSTREAM] = downstream.rstrip("/")
    app[TOKENS] = tokens if tokens is not None else DEFAULT_TOKENS

    async def session_ctx(app: web.Application):
        # No total timeout (SSE streams are long-lived); bound connect and idle reads.
        timeout = ClientTimeout(total=None, connect=5, sock_read=60)
        async with ClientSession(timeout=timeout, auto_decompress=True) as session:
            app[SESSION] = session
            yield

    app.cleanup_ctx.append(session_ctx)
    app.router.add_route("*", "/{tail:.*}", handle)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP Gateway (authorizing reverse proxy)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--downstream", default="http://127.0.0.1:9000",
                        help="base URL of the downstream MCP server")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    spec = os.environ.get("GATEWAY_TOKENS")
    tokens = parse_tokens(spec) if spec else DEFAULT_TOKENS
    if not spec:
        log.warning("using built-in DEMO tokens; set GATEWAY_TOKENS for anything real")
    web.run_app(create_app(args.downstream, tokens), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
