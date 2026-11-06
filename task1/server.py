"""Customer MCP server (stdio transport).

Tools:
  * get_customer_record(customer_id)
  * trigger_refund(customer_id, amount, reason)

Per-process settings (environment variables, read once at startup; the MCP
client sets them in its server config, callers cannot change them):

  CUSTOMER_MCP_LENIENT_IDS=1   Accept shorthand customer ids (bare 5 digits, any-case
                               "cust-" prefix, integers 0..99999 zero-padded).
                               Default off. See the WARNING below and README.md.
  CUSTOMER_MCP_AUDIT_LOG=1     Write one "audit" line to stderr per customer_id with the
                               RAW input. Default off (it adds a line to every call).

  Boolean values: 1/true/yes/on or 0/false/no/off (any case). Anything else aborts
  startup, so a typo can never silently disable auditing.

Protocol rules this file follows:
  * stdout carries ONLY JSON-RPC. Nothing here calls print(); all logging goes
    to stderr. (The SDK also diverts fd 1 to stderr while serving, so a stray
    print() from any library cannot corrupt the stream.)
  * Bad arguments -> JSON-RPC error -32602 (Invalid params), not a tool result.
  * Valid call that fails for business reasons (unknown customer, refund too
    large) -> normal result with isError=true, as the MCP spec recommends.

WARNING: CUSTOMER_MCP_LENIENT_IDS=1 makes the server guess which customer is
meant. That is convenient but can silently target the wrong customer, including
on trigger_refund, which moves money. Enable it only for trusted callers, and
preferably together with CUSTOMER_MCP_AUDIT_LOG=1.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import uuid
from typing import Annotated, Any, Union

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS
from pydantic import BaseModel, Field, ValidationError

# --------------------------------------------------------------------------
# Logging: stderr only.
# --------------------------------------------------------------------------
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("customer-mcp")

# --------------------------------------------------------------------------
# Per-process configuration
# --------------------------------------------------------------------------
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"", "0", "false", "no", "off"}


def _env_flag(name: str) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    # Fail closed: a typo like "ture" must not silently turn auditing off.
    raise SystemExit(f"customer-mcp: invalid value {os.environ[name]!r} for {name}; "
                     "use 1/true/yes/on or 0/false/no/off")


LENIENT_IDS = _env_flag("CUSTOMER_MCP_LENIENT_IDS")
AUDIT_LOG = _env_flag("CUSTOMER_MCP_AUDIT_LOG")

# --------------------------------------------------------------------------
# Input types (Pydantic). These become the tool's published JSON Schema AND
# are enforced at call time. The published schema matches the configured mode.
# --------------------------------------------------------------------------
if LENIENT_IDS:
    _CUSTOMER_ID_DESC = (
        "Customer ID. This server runs in LENIENT mode: besides the canonical CUST-XXXXX "
        "it accepts a bare 5-digit string (e.g. '00042'), the 'cust-' prefix in any case, "
        "or an integer 0..99999 which is zero-padded to 5 digits (42 -> CUST-00042). "
        "The server interprets shorthand, so verify the canonical customer_id in the result."
    )
    CustomerId = Union[
        Annotated[
            str,
            Field(
                pattern=r"^(?:[Cc][Uu][Ss][Tt]-)?[0-9]{5}$",
                min_length=5,
                max_length=10,
                strict=True,
                examples=["CUST-00042"],
            ),
        ],
        # strict=True: rejects true/false, 42.0 and "42" for the integer branch.
        Annotated[int, Field(strict=True, ge=0, le=99999)],
    ]
else:
    _CUSTOMER_ID_DESC = "Customer ID in the form CUST-XXXXX (uppercase CUST, 5 digits)."
    CustomerId = Annotated[
        str,
        Field(
            pattern=r"^CUST-[0-9]{5}$",
            min_length=10,
            max_length=10,
            strict=True,
            examples=["CUST-00042"],
        ),
    ]
CustomerIdField = Annotated[CustomerId, Field(description=_CUSTOMER_ID_DESC)]

_STRICT_ID = re.compile(r"CUST-[0-9]{5}")
_LENIENT_ID = re.compile(r"(?:cust-)?([0-9]{5})", re.IGNORECASE)


def _invalid(tool: str, field: str, reason: str, type_: str) -> MCPError:
    """Build the JSON-RPC -32602 error used for every argument problem."""
    return MCPError(INVALID_PARAMS, f"Invalid arguments for tool '{tool}'",
                    data=[{"field": field, "reason": reason, "type": type_}])


def _safe(raw: object) -> str:
    """repr() (escapes newlines, so no log injection) truncated to 80 chars."""
    s = repr(raw)
    return s if len(s) <= 80 else s[:77] + "..."


def resolve_customer_id(tool: str, raw: str | int) -> str:
    """Resolve the id; if CUSTOMER_MCP_AUDIT_LOG is on, log the RAW input (stderr).

    Audit lines: INFO = raw input was already canonical; WARNING = the server
    interpreted shorthand (a "guess"); INFO "rejected" = bad id.
    """
    try:
        canonical = _resolve_customer_id(tool, raw)
    except MCPError:
        if AUDIT_LOG:
            log.info("audit tool=%s customer_id_rejected raw=%s lenient=%s", tool, _safe(raw), LENIENT_IDS)
        raise
    if AUDIT_LOG:
        if canonical == raw:
            log.info("audit tool=%s customer_id raw=%s lenient=%s", tool, _safe(raw), LENIENT_IDS)
        else:
            log.warning("audit tool=%s customer_id_interpreted raw=%s -> %s lenient=%s",
                        tool, _safe(raw), canonical, LENIENT_IDS)
    return canonical


def _resolve_customer_id(tool: str, raw: str | int) -> str:
    """Return the canonical CUST-XXXXX id or raise -32602.

    RISK (lenient mode): shorthand input is *interpreted*, not validated as-is.
    An integer is zero-padded to 5 digits, so 123 -> CUST-00123. If a caller's
    number was truncated or mangled upstream, this silently selects a different,
    perfectly valid customer. Strict mode (default) never guesses.
    """
    if isinstance(raw, int):  # pydantic already rejected bool/float; belt and braces
        if isinstance(raw, bool) or not LENIENT_IDS:
            raise _invalid(tool, "customer_id", "integers are not accepted", "customer_id_format")
        if not 0 <= raw <= 99999:
            raise _invalid(tool, "customer_id", "integer must be between 0 and 99999", "customer_id_range")
        return f"CUST-{raw:05d}"
    if LENIENT_IDS:
        m = _LENIENT_ID.fullmatch(raw)  # fullmatch: no trailing-newline loophole
        if m:
            return f"CUST-{m.group(1)}"
        raise _invalid(tool, "customer_id", "must be 5 digits, optionally prefixed with 'CUST-' (any case)",
                       "customer_id_format")
    if _STRICT_ID.fullmatch(raw):
        return raw
    raise _invalid(tool, "customer_id", "must match CUST-XXXXX (uppercase, 5 digits)", "customer_id_format")


RefundAmount = Annotated[
    float,
    Field(
        gt=0,
        le=10_000,
        allow_inf_nan=False,  # rejects NaN / Infinity
        strict=True,  # rejects "12.5" (string) and true/false
        description="Refund amount in USD. Must be a positive number, max 10000.",
    ),
]

RefundReason = Annotated[
    str,
    Field(
        min_length=10,
        max_length=500,
        strict=True,
        description="Why the refund is being issued (10 to 500 characters).",
    ),
]


# --------------------------------------------------------------------------
# Output types
# --------------------------------------------------------------------------
class CustomerRecord(BaseModel):
    customer_id: str
    name: str
    email: str
    status: str
    lifetime_spend: float
    refunded_to_date: float


class RefundReceipt(BaseModel):
    refund_id: str
    customer_id: str
    amount: float
    reason: str
    status: str


# --------------------------------------------------------------------------
# Fake data store (replace with a real DB / API).
# --------------------------------------------------------------------------
_CUSTOMERS: dict[str, dict[str, Any]] = {
    "CUST-00001": {"name": "Ada Lovelace", "email": "ada@example.com", "status": "active",
                   "lifetime_spend": 1250.00, "refunded_to_date": 0.0},
    "CUST-00042": {"name": "Alan Turing", "email": "alan@example.com", "status": "active",
                   "lifetime_spend": 320.50, "refunded_to_date": 20.0},
    "CUST-99999": {"name": "Closed Account", "email": "closed@example.com", "status": "closed",
                   "lifetime_spend": 75.00, "refunded_to_date": 0.0},
}


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------
class StrictMCPServer(MCPServer):
    """MCPServer that reports bad arguments as JSON-RPC -32602.

    By default the SDK turns an argument validation failure into a tool
    result with isError=true. The spec's protocol-level mapping for malformed
    arguments is a JSON-RPC error, so we translate it here.
    """

    async def call_tool(self, name, arguments, *args, **kwargs):  # type: ignore[override]
        try:
            return await super().call_tool(name, arguments, *args, **kwargs)
        except ToolError as exc:
            cause = exc.__cause__
            if isinstance(cause, ValidationError):
                errors = cause.errors(include_url=False)  # includes the raw "input"
                # To the client: field + reason only; never echo the rejected value.
                problems = [
                    {"field": ".".join(str(p) for p in e["loc"]) or "(root)",
                     "reason": e["msg"], "type": e["type"]}
                    for e in errors
                ]
                log.info("Rejected %s: %s", name, [p["field"] for p in problems])
                if AUDIT_LOG:
                    # To the server log: the raw customer_id. Skip "missing" errors, whose
                    # input is the whole argument dict (it would leak amount/reason).
                    raws = {_safe(e.get("input")) for e in errors
                            if e["loc"] and e["loc"][0] == "customer_id" and e["type"] != "missing"}
                    for raw in sorted(raws):
                        log.info("audit tool=%s customer_id_rejected raw=%s lenient=%s", name, raw, LENIENT_IDS)
                raise MCPError(INVALID_PARAMS, f"Invalid arguments for tool '{name}'", data=problems) from exc
            if str(exc).startswith("Unknown tool"):
                raise MCPError(INVALID_PARAMS, f"Unknown tool: {name}") from exc
            raise


mcp = StrictMCPServer(
    name="customer-mcp",
    version="1.0.0",
    instructions="Look up customer records and issue refunds.",
)


@mcp.tool(description="Fetch a customer record by customer ID (format CUST-XXXXX).")
def get_customer_record(customer_id: CustomerIdField) -> CustomerRecord:
    customer_id = resolve_customer_id("get_customer_record", customer_id)
    record = _CUSTOMERS.get(customer_id)
    if record is None:
        raise ToolError(f"Customer {customer_id} not found.")
    log.info("get_customer_record %s", customer_id)
    return CustomerRecord(customer_id=customer_id, **record)


@mcp.tool(description="Issue a refund to a customer. Amount must be positive; reason at least 10 characters.")
def trigger_refund(customer_id: CustomerIdField, amount: RefundAmount, reason: RefundReason) -> RefundReceipt:
    customer_id = resolve_customer_id("trigger_refund", customer_id)
    # Whitespace-only reasons satisfy min_length but carry no information.
    if len(reason.strip()) < 10:
        raise MCPError(INVALID_PARAMS, "Invalid arguments for tool 'trigger_refund'",
                       data=[{"field": "reason", "reason": "must contain at least 10 non-whitespace-padded characters",
                              "type": "string_too_short"}])

    record = _CUSTOMERS.get(customer_id)
    if record is None:
        raise ToolError(f"Customer {customer_id} not found.")
    if record["status"] != "active":
        raise ToolError(f"Customer {customer_id} is {record['status']}; refunds are not allowed.")

    cents = round(amount * 100)
    if abs(amount * 100 - cents) > 1e-6:
        raise MCPError(INVALID_PARAMS, "Invalid arguments for tool 'trigger_refund'",
                       data=[{"field": "amount", "reason": "must have at most 2 decimal places",
                              "type": "decimal_places"}])
    amount = cents / 100

    refundable = round(record["lifetime_spend"] - record["refunded_to_date"], 2)
    if amount > refundable:
        raise ToolError(f"Refund {amount:.2f} exceeds refundable balance {refundable:.2f}.")

    record["refunded_to_date"] = round(record["refunded_to_date"] + amount, 2)
    receipt = RefundReceipt(refund_id=f"RF-{uuid.uuid4().hex[:8].upper()}", customer_id=customer_id,
                            amount=amount, reason=reason.strip(), status="processed")
    log.info("trigger_refund %s %.2f -> %s", customer_id, amount, receipt.refund_id)
    return receipt


def main() -> None:
    log.info("Starting customer-mcp on stdio (lenient_ids=%s audit_log=%s)", LENIENT_IDS, AUDIT_LOG)
    if LENIENT_IDS:
        log.warning("CUSTOMER_MCP_LENIENT_IDS is ON: the server will interpret shorthand customer ids"
                    "%s", "" if AUDIT_LOG else " and CUSTOMER_MCP_AUDIT_LOG is OFF, so guesses will not be logged")
    mcp.run("stdio")


if __name__ == "__main__":
    main()
