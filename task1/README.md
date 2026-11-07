# Task 1: Build a Custom MCP Server with Strict Validation & Transport Handling

A Python MCP server (stdio transport, `mcp` SDK 2.3) exposing two tools:

| Tool | Arguments |
|---|---|
| `get_customer_record` | `customer_id` |
| `trigger_refund` | `customer_id`, `amount` (> 0, max 10000, 2 decimals), `reason` (10 to 500 chars) |

## Setup and test (Debian / Linux)

```bash
uv sync                          # creates .venv from pyproject.toml / uv.lock
uv run python test_stdio.py      # black-box JSON-RPC test over stdio (runs the server in 5 configurations)
uv run python server.py          # run the server (speaks JSON-RPC on stdin/stdout)
```

stdout carries only JSON-RPC; all logs go to stderr.

## Built-in test data

The server has no database. It ships with three fake in-memory customers so every code path can be
exercised from any MCP client. Any other well-formed id, such as `CUST-12345`, returns a normal
"not found" result (`isError: true`).

| Customer ID | Account | Status | Lifetime spend | Refunded so far | Refundable |
|---|---|---|---|---|---|
| `CUST-00001` | Ada Lovelace | active | 1250.00 | 0.00 | 1250.00 |
| `CUST-00042` | Alan Turing | active | 320.50 | 20.00 | 300.50 |
| `CUST-99999` | Closed Account | closed | 75.00 | 0.00 | refunds rejected |

Refund totals are held in memory and reset whenever the server restarts. Nothing is deduplicated, so
repeating a refund processes it again. The data lives in `_CUSTOMERS` in `server.py`.

## Testing with the MCP Inspector

[`@modelcontextprotocol/inspector`](https://github.com/modelcontextprotocol/inspector) needs Node.js.
Run these from the repository root (so the relative paths below resolve), and **not from inside a
clone of the Inspector repository**: `npx` would then try to run that unbuilt checkout and fail with
`inspector: not found`.

Interactive (terminal UI; needs a real terminal, so use `ssh -t` when connecting remotely):

```bash
npx @modelcontextprotocol/inspector --tui .venv/bin/python server.py
# with the per-process settings:
npx @modelcontextprotocol/inspector --tui .venv/bin/python server.py \
  -e CUSTOMER_MCP_LENIENT_IDS=1 -e CUSTOMER_MCP_AUDIT_LOG=1
```

Keys: `c` connect, `t` tools, `Enter` run the selected tool, `p` raw JSON-RPC history, `o` server
stderr (audit lines appear here), `?` help.

One request per run (not interactive; prints the result and exits):

```bash
npx @modelcontextprotocol/inspector --cli .venv/bin/python server.py \
  --method tools/call --tool-name trigger_refund \
  --tool-args-json '{"customer_id":"CUST-00042","amount":25.00,"reason":"Damaged item on arrival"}'
```

`--method tools/list` lists the tools. Add `-e NAME=value` before `--method` to set the server's
environment variables.

Caveat for negative tests: the Inspector converts argument values to the types in the tool's schema
before sending, even with `--tool-args-json`. A string amount such as `"10"` therefore reaches the
server as the number `10` and is accepted. To confirm what was really sent, check the Protocol tab
(`p`) in the TUI. To test type rejection exactly, use `uv run python test_stdio.py`, which sends raw
JSON-RPC.

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

Replace `/path/to/mcp-llm-gateway-samples/task1` with the absolute path of this repository. Use absolute paths:
the client starts the server itself and the JSON is not run through a shell. In testing with the MCP
Inspector, a literal `~/...` in `command` failed with `spawn ~/.../python ENOENT`, while the same
config with absolute paths connected. Do not rely on `~` or `$HOME` in these fields.

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
  `{field, reason, type}`. Rejected values are never echoed back to the client. Every problem found
  is listed, not just the first. In lenient mode a bad `customer_id` is reported as a single plain
  entry (`field: "customer_id"`, `type: "customer_id_format"`) rather than one entry per union
  branch, so clients never see Pydantic's internal branch names.
- Unknown method: `-32601`.
- Valid call that fails for business reasons (customer not found, closed account, refund above the
  refundable balance): normal result with `isError: true`.

## customer_id formats

By default `customer_id` must be exactly the string `CUST-XXXXX` (uppercase prefix, 5 digits).
Anything else, including integers, is `-32602`. The published tool schema matches the configured mode.

### customer_id: numeric or alphanumeric?

**Assumption: the five `X` characters are digits only (`0` to `9`).** The task text gives the format
as `CUST-XXXXX` without saying what `X` may be; this server reads it as five digits (`CUST-00042`),
which is also the only form the built-in test data and `test_stdio.py` use.

If your real ids can contain letters (for example `CUST-A1B2C`), the server needs a one-line change.
In `server.py`, find `_ID_CHARS` and swap which of the two lines is commented out:

```python
_ID_CHARS = "[0-9]"          # numeric XXXXX (default)
# _ID_CHARS = "[0-9A-Z]"     # alphanumeric XXXXX (digits + uppercase letters)
```

Everything else (both validation patterns, error messages, and the published schema) is derived from
that line. Notes on the alphanumeric setting:

- Letters must be uppercase in the canonical form. With `CUSTOMER_MCP_LENIENT_IDS=1`, lowercase
  letters are accepted and upper-cased (`cust-a1b2c` becomes `CUST-A1B2C`).
- Integers in lenient mode still map to all-digit ids (`42` becomes `CUST-00042`), so they cannot
  reach an id that contains a letter. The "integers hide lost zeros" risk below is unchanged.
- A bare 5-character string in lenient mode is more ambiguous with letters allowed; keep that in mind
  before enabling both.
- `test_stdio.py` asserts the numeric rules, so its "letters are rejected" checks are expected to
  fail after the switch. Update them if you keep the alphanumeric setting.
- The fake test accounts all have numeric ids; add an alphanumeric one to `_CUSTOMERS` to try it.

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
6. **Schema compatibility.** Some tool-calling clients handle `anyOf` poorly. Observed with the MCP
   Inspector TUI (v2.14.0): it renders the `anyOf: [string, integer]` field as a text box and sends
   what you type as a **string**, so typing `42` sends `"42"`, which lenient mode rejects (the
   unpadded string rule above). The TUI therefore cannot send the integer form at all. Use a
   zero-padded string such as `"00042"` there, or send the integer with the Inspector `--cli`
   (`--tool-args-json '{"customer_id":42}'`) or any client that honors the integer branch.

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
