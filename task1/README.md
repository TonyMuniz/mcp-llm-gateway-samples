# customer-mcp

A Python MCP server (stdio transport, `mcp` SDK 2.3) exposing two tools:

| Tool | Arguments |
|---|---|
| `get_customer_record` | `customer_id` |
| `trigger_refund` | `customer_id`, `amount` (> 0, max 10000, 2 decimals), `reason` (10 to 500 chars) |

## Setup and test (Debian / Linux)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python test_stdio.py      # black-box JSON-RPC test over stdio (runs the server in 5 configurations)
.venv/bin/python server.py          # run the server (speaks JSON-RPC on stdin/stdout)
```

stdout carries only JSON-RPC; all logs go to stderr.

## Per-process settings

Both settings are environment variables, read **once at startup**. They apply for the life of the
server process and **callers cannot change them** (they are not tool arguments). With stdio the MCP
client launches the server, so the operator sets them in the client's server config, for example:

```json
{
  "mcpServers": {
    "customer": {
      "command": "/path/to/mcp-llm-gateway-samples/task1/.venv/bin/python",
      "args": ["/path/to/mcp-llm-gateway-samples/task1/server.py"],
      "env": { "CUSTOMER_MCP_LENIENT_IDS": "1", "CUSTOMER_MCP_AUDIT_LOG": "1" }
    }
  }
}
```

| Variable | Default | Effect |
|---|---|---|
| `CUSTOMER_MCP_LENIENT_IDS` | off | Accept shorthand customer ids (see below). |
| `CUSTOMER_MCP_AUDIT_LOG` | off | Write an `audit` line to stderr per `customer_id` with the raw input (see below). |

Boolean values: `1/true/yes/on` or `0/false/no/off` (any case; unset or empty means off). **Any other
value aborts startup** with a message on stderr, so a typo such as `ture` cannot silently disable
auditing. The server logs its effective settings on startup:
`Starting customer-mcp on stdio (lenient_ids=False audit_log=False)`.

## Error model

- Bad arguments, unknown tool: JSON-RPC error `-32602` (Invalid params), with a `data` list of
  `{field, reason, type}`. Rejected values are never echoed back to the client.
- Unknown method: `-32601`.
- Valid call that fails for business reasons (customer not found, closed account, refund above the
  refundable balance): normal result with `isError: true`.

## customer_id formats

By default `customer_id` must be exactly the string `CUST-XXXXX` (uppercase prefix, 5 digits).
Anything else, including integers, is `-32602`. The published tool schema matches the configured mode.

## `CUSTOMER_MCP_LENIENT_IDS=1`: read this before enabling

When on, the server **interprets** the ID instead of validating it as given, and normalises it to
`CUST-XXXXX`:

| Input | Becomes |
|---|---|
| `"00042"` (5-digit string) | `CUST-00042` |
| `"cust-00042"`, `"Cust-00042"` (any-case prefix) | `CUST-00042` |
| `42` (integer, 0 to 99999) | `CUST-00042` (zero-padded) |
| `"42"` (unpadded string), `42.0`, `true`, `-1`, `100000` | rejected, `-32602` |

In this mode `customer_id` is published as `anyOf: [string, integer]`, and its description tells
clients the server interprets shorthand.

### Risks and implications

1. **Guessing, not validating.** Strict mode can only select the customer the caller literally named.
   Lenient mode selects a customer the server inferred. If the number was truncated, re-typed,
   or mangled by an upstream tool, the result is a different but **perfectly valid** customer, and
   no error is raised. Example: an upstream system drops a leading digit, `10042` becomes `42`,
   and the call silently hits `CUST-00042`.
2. **Integers hide lost zeros.** The integer path assumes the caller's leading zeros were lost to
   number parsing. It cannot tell that apart from a genuinely different number.
3. **`trigger_refund` moves money.** A wrong guess on `get_customer_record` is a wrong read; on
   `trigger_refund` it is a refund to the wrong customer. The setting applies to both tools.
   Treat it as a convenience for trusted, known callers only.
4. **Server-wide, not per caller.** The setting applies to every call the process serves. Callers
   (including LLMs) cannot turn it on or off. If you need different policies for different callers,
   run separate server processes with different environments.
5. **Audit trail is optional.** Without `CUSTOMER_MCP_AUDIT_LOG=1`, guesses leave no record of the
   raw input. The server warns at startup when lenient mode is on and auditing is off.
6. **Schema compatibility.** Some tool-calling clients handle `anyOf` poorly.

### Mitigations built in

- Default is off; strict mode never guesses.
- Both tools return the canonical ID (`CUST-XXXXX`) in the result, including the refund receipt.
  **Callers should compare it with the customer they intended** and treat a mismatch as an error.
- Strict typing: booleans, floats, unpadded numeric strings, negative and out-of-range integers are rejected.
- Optional audit logging (below).

### Recommendations

- Prefer clients that send the exact `CUST-XXXXX` string, and leave lenient mode off.
- If you enable it, enable `CUSTOMER_MCP_AUDIT_LOG=1` as well and alert on `customer_id_interpreted`
  for `trigger_refund`.
- For refunds, verify the returned `customer_id` against the intended customer (ideally confirm the
  customer first with `get_customer_record`).

## `CUSTOMER_MCP_AUDIT_LOG=1`

Off by default because it adds a line to stderr for every tool call. Customers without audit
requirements can leave it off and turn it on only while debugging. When on, every `customer_id`
produces one line (stderr only, never sent to the client). The raw value is `repr`-escaped and
truncated to 80 chars, so newlines cannot forge extra log lines:

| Level | Line | Meaning |
|---|---|---|
| INFO | `audit tool=… customer_id raw='CUST-00042' lenient=False` | already canonical, nothing interpreted |
| WARNING | `audit tool=… customer_id_interpreted raw=42 -> CUST-00042 lenient=True` | the server guessed |
| INFO | `audit tool=… customer_id_rejected raw='00042' lenient=False` | invalid id (includes schema-level rejections) |

Notes:
- When the flag is off, **no** audit lines are written, including `customer_id_interpreted`.
- A call with `customer_id` missing logs no raw value, because the offending "input" would be the whole
  argument object (amount, reason).
- Other, non-audit INFO lines (startup, per-call summary, `Rejected …`) are unaffected by this flag.
