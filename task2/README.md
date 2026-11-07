# Task 2: Implement an MCP Security Gateway Proxy (Tool Filtering & Auth)

A small reverse proxy (Python 3.11+, developed and tested on 3.15.2; aiohttp; managed with uv) that sits
between an AI agent and a downstream MCP server (streamable HTTP, JSON-RPC 2.0) and enforces role-based
authorization on tool calls.

```
agent --Bearer token--> gateway :8080 --JSON-RPC--> downstream MCP server :9000
```

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
`GATEWAY_TOKENS="tokenA=admin,tokenB=viewer"` (tokens may use only the RFC 6750 characters
`A-Z a-z 0-9 - . _ ~ + /` and trailing `=`; anything else is refused at startup). Tests:
`uv run python -m unittest -v test_gateway` (22 end-to-end tests with a real gateway and mock server
on real sockets).

## Assignment requirements

| Requirement | How it is met |
|---|---|
| Lightweight HTTP/JSON-RPC reverse proxy in front of a mock MCP server | `gateway.py` (aiohttp) and `mock_server.py`; responses, including SSE, are streamed back |
| Read the role from `Authorization: Bearer <token>` | `resolve_role()`: static token table, constant-time comparison; missing or unknown token is HTTP 401 |
| `tools/list` forwarded transparently | Forwarded for every role (tested) |
| `tools/call` on an `admin_` tool needs role `admin`, else `-32001` without calling downstream | `authorize()`; the downstream's request log is checked in the tests to prove nothing reached it |
| Parse the JSON-RPC wire format correctly | Parse errors, batches, wrong `id` types, missing `params.name`, notifications (see ["Behavior and errors"](#behavior-and-errors)) |

## Behavior and errors

| Situation | Result |
|---|---|
| Missing/unknown/non-Bearer `Authorization`, or a token outside the RFC 6750 alphabet | HTTP 401 + `WWW-Authenticate: Bearer` |
| `tools/list` (and any other method) | forwarded unchanged |
| `tools/call`, `params.name` starts with `admin_`, role is `admin` | forwarded |
| same, role is not `admin` | gateway replies `-32001 Unauthorized Tool Call` (HTTP 200, request `id` echoed); downstream is never contacted |
| `tools/call` without a string `params.name` | `-32602` from the gateway |
| `tools/call` with a name outside `A-Z a-z 0-9 _ - . /` or longer than 128 characters | `-32602 Invalid tool name` from the gateway |
| Malformed JSON (including NaN/Infinity, undecodable bytes, very deep nesting) | `-32700`, `"id": null` |
| Non-object, batch array, or an `id` that is not a string, number or null | `-32600` |
| Downstream unreachable | HTTP 502 with JSON-RPC error `-32603` |

## Design decisions

- **Token to role** is a static table behind one function, `resolve_role()`. A real deployment would
  verify a JWT there and read its `role` claim; nothing else changes.
- **Authorization is by prefix** (`admin_`), case-insensitive and after Unicode NFKC normalization and
  trimming, so `ADMIN_x`, ` admin_x` or fullwidth look-alikes still get `-32001`.
- **Tool names are allow-listed** (ASCII letters, digits, `_ - . /`, 1 to 128 characters, in line with
  the MCP tool-name guidance). This is deny by default: a zero-width space, a soft hyphen or a Cyrillic
  `а` in front of `admin_` is rejected before it can reach a downstream that might match names loosely.
- **The gateway forwards its own parse, not the raw bytes.** Parsers disagree on duplicate JSON keys
  (`"name":"get_status","name":"admin_reset_key"`); re-serializing means the downstream sees exactly
  what was authorized. There is a test for this. The re-serialized body is always UTF-8 JSON.
- **Batches are rejected.** MCP dropped JSON-RPC batching in 2025-06-18, and forwarding a batch
  would mean authorizing each element.
- **Denied notifications** (no `id`) get an empty HTTP 202, since JSON-RPC forbids replying to them.
- **Authorization is not forwarded**; only Content-Type, Accept and the `Mcp-*`/`Last-Event-ID`
  headers are. Downstream responses are streamed, so SSE works.
- **`tools/list` is not filtered**, per the spec ("forward transparently"): viewers can still see
  admin tools exist but cannot call them. Filtering would be a small addition in `forward`.
- **Log lines escape caller-supplied text** (`%a`), so a method name containing a newline cannot forge
  a second "decision=allow" line (tested).

## Known issues and limits

- **Encoding.** Request bodies may be UTF-8 (with or without BOM), UTF-16 or UTF-32: Python's
  `json.loads` detects these, and the forwarded body is UTF-8. A `charset` parameter in
  `Content-Type` is ignored, so a Latin-1 body with non-ASCII bytes is answered with `-32700` rather
  than decoded. Header values with bytes that are not UTF-8 (for example in `Authorization`) are
  treated as an invalid token (401); before the review they caused an HTTP 500.
- **Look-alike names are blocked, not mapped.** The allow-list rejects `admin_` look-alikes outright;
  it does not try to decide what a downstream would make of them.
- **Every path is forwarded** (`/{tail:.*}`), not only `/mcp`; authentication applies to all of them.
  Restricting the gateway to the MCP endpoint path would be a one-line change.
- **`Mcp-Session-Id` is not bound to a role.** Authorization is decided per request from the token, so
  sharing a session id does not grant anything, but sessions are not isolated per caller either.
- **Not covered:** TLS (terminate in front), rate limiting, token expiry (needs real JWTs).

## Dependencies and security

One direct dependency, `aiohttp>=3.14.5` (the floor is the locked, audited version; the old `>=3.9`
floor allowed releases with published CVEs if someone installed without the lock file). `uv audit` on
2026-11-07: no known vulnerabilities in the 10 locked packages. Python 3.15 is pinned in
`.python-version`; the tests also pass on 3.11 and 3.13.
