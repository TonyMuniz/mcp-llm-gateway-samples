# Task 2: Implement an MCP Security Gateway Proxy (Tool Filtering & Auth)

A small reverse proxy (Python 3.11+, developed and tested on 3.15.2; aiohttp; managed with uv) that sits between an AI agent and a
downstream MCP server (streamable HTTP, JSON-RPC 2.0) and enforces role-based
authorization on tool calls.

```
agent --Bearer token--> gateway :8080 --JSON-RPC--> downstream MCP server :9000
```

## Behavior

| Situation | Result |
|---|---|
| Missing/unknown/non-Bearer `Authorization` | HTTP 401 + `WWW-Authenticate: Bearer` |
| `tools/list` (and any other method) | forwarded unchanged |
| `tools/call`, `params.name` starts with `admin_`, role is `admin` | forwarded |
| same, role is not `admin` | gateway replies `-32001 Unauthorized Tool Call` (HTTP 200, request `id` echoed); downstream is never contacted |
| `tools/call` without a string `params.name` | `-32602` from the gateway |
| Malformed JSON / non-object / batch array | `-32700` / `-32600` / `-32600` |
| Downstream unreachable | HTTP 502 with JSON-RPC error `-32603` |

## Run

```bash
uv sync                                   # creates .venv from pyproject.toml / uv.lock
uv run python mock_server.py --port 9000 &
uv run python gateway.py --port 8080 --downstream http://127.0.0.1:9000 &

curl -s localhost:8080/mcp -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer viewer-token' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{"key_id":"k1"}}}'
# {"jsonrpc": "2.0", "id": 1, "error": {"code": -32001, "message": "Unauthorized Tool Call"}}
```

Tokens: demo defaults are `admin-token` (admin) and `viewer-token` (viewer). Override with
`GATEWAY_TOKENS="tokenA=admin,tokenB=viewer"`. Tests: `uv run python -m unittest -v test_gateway`
(16 end-to-end tests with a real gateway and mock server on real sockets).

## Design decisions

- **Token to role** is a static table behind one function, `resolve_role()`. A real deployment would
  verify a JWT there and read its `role` claim; nothing else changes.
- **Authorization is by prefix** (`admin_`), after Unicode NFKC normalization, trimming and case
  folding, so `ADMIN_x` or fullwidth look-alikes cannot bypass the check on a lenient downstream.
- **The gateway forwards its own parse, not the raw bytes.** Parsers disagree on duplicate JSON keys
  (`"name":"get_status","name":"admin_reset_key"`); re-serializing means the downstream sees exactly
  what was authorized. There is a test for this.
- **Batches are rejected.** MCP dropped JSON-RPC batching in 2025-06-18, and forwarding a batch
  would mean authorizing each element.
- **Denied notifications** (no `id`) get an empty HTTP 202, since JSON-RPC forbids replying to them.
- **Authorization is not forwarded**; only Content-Type, Accept and the `Mcp-*`/`Last-Event-ID`
  headers are. Downstream responses are streamed, so SSE works.
- **`tools/list` is not filtered**, per the spec ("forward transparently"): viewers can still see
  admin tools exist but cannot call them. Filtering would be a small addition in `forward`.
- Not covered: TLS (terminate in front), rate limiting, token expiry (needs real JWTs).
