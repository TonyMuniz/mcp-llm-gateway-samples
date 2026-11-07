"""Black-box test: spawns server.py several times with different per-process
settings and speaks raw JSON-RPC over stdio.

Run:  uv run python test_stdio.py      (Windows: .venv\\Scripts\\python test_stdio.py)

Phases: default (strict, audit off) / lenient / lenient+audit / strict+audit / bad env value.
In every phase every stdout line must parse as JSON-RPC (stdout purity).
"""
import json
import os
import subprocess
import sys

failures = 0


def check(label, ok, detail=""):
    global failures
    print(("PASS " if ok else "FAIL ") + label + ("" if ok else f"  -> {detail}"))
    failures += not ok


class Session:
    """One server process."""

    def __init__(self, **env):
        e = {k: v for k, v in os.environ.items() if not k.startswith("CUSTOMER_MCP_")}
        e.update(env)
        self.proc = subprocess.Popen([sys.executable, "server.py"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, encoding="utf-8", env=e)
        self.id = 0
        self.stdout_lines: list[str] = []

    def send(self, msg):
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def call(self, method, params=None):
        self.id += 1
        self.send({"jsonrpc": "2.0", "id": self.id, "method": method, "params": params or {}})
        while True:
            line = self.proc.stdout.readline()
            assert line, "server closed stdout"
            self.stdout_lines.append(line)
            msg = json.loads(line)  # raises if stdout is polluted
            assert msg.get("jsonrpc") == "2.0", f"not JSON-RPC: {line!r}"
            if msg.get("id") == self.id:
                return msg

    def tool(self, name, args):
        return self.call("tools/call", {"name": name, "arguments": args})

    def start(self):
        init = self.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                        "clientInfo": {"name": "test", "version": "0"}})
        check("initialize ok", "result" in init, init)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.tools = {t["name"]: t["inputSchema"] for t in self.call("tools/list")["result"]["tools"]}

    def finish(self):
        """Close stdin, return stderr, and check stdout purity."""
        self.proc.stdin.close()
        self.proc.wait(timeout=10)
        err = self.proc.stderr.read()
        check("stdout had only JSON-RPC lines", all(json.loads(l) for l in self.stdout_lines))
        return err


def code(r):
    return r.get("error", {}).get("code")


def sc(r):
    return r.get("result", {}).get("structuredContent", {})


def is_err_result(r):
    return r.get("result", {}).get("isError") is True


GOOD_REFUND = {"customer_id": "CUST-00042", "amount": 10.5, "reason": "Damaged item on arrival"}

# ==========================================================================
print("\n== Phase 1: default (strict ids, audit off) ==")
s = Session()
s.start()
check("two tools listed", sorted(s.tools) == ["get_customer_record", "trigger_refund"])
check("no per-call lenient argument in either schema",
      all("lenient_customer_id" not in p["properties"] for p in s.tools.values()), s.tools)
check("customer_id schema is a plain string (no integer branch)",
      all("anyOf" not in p["properties"]["customer_id"] and p["properties"]["customer_id"].get("type") == "string"
          for p in s.tools.values()), s.tools)

r = s.tool("get_customer_record", {"customer_id": "CUST-00042"})
check("get valid -> result", "result" in r and not r["result"].get("isError"), r)
r = s.tool("get_customer_record", {"customer_id": "CUST-77777"})
check("get unknown customer -> isError result (not JSON-RPC error)", is_err_result(r), r)

for bad in ["cust-00042", "00042", "CUST-0004", "CUST-000420", "CUST-ABCDE", "CUST-00042\n", " CUST-00042",
            "", 42, 12345, 0, True, 42.0, None, ["CUST-00042"]]:
    r = s.tool("get_customer_record", {"customer_id": bad})
    check(f"strict: bad id {bad!r} -> -32602", code(r) == -32602, r)
r = s.tool("get_customer_record", {})
check("get missing id -> -32602", code(r) == -32602, r)

# A caller can no longer opt in: the old per-call argument is just an ignored extra property.
for extra in (True, "true", 1):
    r = s.tool("get_customer_record", {"customer_id": "00042", "lenient_customer_id": extra})
    check(f"caller-supplied lenient_customer_id={extra!r} does NOT enable lenient mode -> -32602",
          code(r) == -32602, r)
r = s.tool("trigger_refund", {**GOOD_REFUND, "customer_id": 42, "lenient_customer_id": True})
check("refund: caller cannot enable lenient mode either", code(r) == -32602, r)

r = s.tool("trigger_refund", GOOD_REFUND)
check("refund valid -> result", "result" in r and not r["result"].get("isError"), r)
bad_args = {
    "amount 0": {**GOOD_REFUND, "amount": 0},
    "amount negative": {**GOOD_REFUND, "amount": -5},
    "amount string": {**GOOD_REFUND, "amount": "10"},
    "amount bool": {**GOOD_REFUND, "amount": True},
    "amount too big": {**GOOD_REFUND, "amount": 1e9},
    "amount 3 decimals": {**GOOD_REFUND, "amount": 1.234},
    "reason short": {**GOOD_REFUND, "reason": "too short"},
    "reason spaces": {**GOOD_REFUND, "reason": "   a        "},
    "reason missing": {k: v for k, v in GOOD_REFUND.items() if k != "reason"},
    "bad customer": {**GOOD_REFUND, "customer_id": "nope"},
}
for label, args in bad_args.items():
    r = s.tool("trigger_refund", args)
    check(f"refund {label} -> -32602", code(r) == -32602, r)
check("refund over balance -> isError result", is_err_result(s.tool("trigger_refund", {**GOOD_REFUND, "amount": 5000})))
check("refund closed account -> isError result",
      is_err_result(s.tool("trigger_refund", {**GOOD_REFUND, "customer_id": "CUST-99999"})))
check("refund unknown customer -> isError result",
      is_err_result(s.tool("trigger_refund", {**GOOD_REFUND, "customer_id": "CUST-12345"})))
check("unknown tool -> -32602", code(s.tool("no_such_tool", {})) == -32602)
check("unknown method -> -32601", code(s.call("no/such/method")) == -32601)

err = s.finish()
check("startup line shows lenient_ids=False audit_log=False", "lenient_ids=False audit_log=False" in err, err[:300])
check("audit off: no audit lines at all", "audit tool=" not in err, err[-400:])

# ==========================================================================
print("\n== Phase 2: CUSTOMER_MCP_LENIENT_IDS=1 (audit off) ==")
s = Session(CUSTOMER_MCP_LENIENT_IDS="1")
s.start()
check("no per-call lenient argument in either schema",
      all("lenient_customer_id" not in p["properties"] for p in s.tools.values()), s.tools)
check("customer_id schema now allows string or integer",
      all("anyOf" in p["properties"]["customer_id"] for p in s.tools.values()), s.tools)

for ok_id in ["00042", "CUST-00042", "cust-00042", "Cust-00042", 42]:
    r = s.tool("get_customer_record", {"customer_id": ok_id})
    check(f"lenient accepts {ok_id!r} -> CUST-00042", sc(r).get("customer_id") == "CUST-00042", r)
check("lenient int 1 -> CUST-00001", sc(s.tool("get_customer_record", {"customer_id": 1})).get("customer_id") == "CUST-00001")
check("lenient int 99999 -> CUST-99999 (upper edge)",
      sc(s.tool("get_customer_record", {"customer_id": 99999})).get("customer_id") == "CUST-99999")
check("lenient int 0 in range, no such customer -> isError (not -32602)",
      is_err_result(s.tool("get_customer_record", {"customer_id": 0})))
for bad in ["0004", "000420", "cust_00042", "00042\n", "abcde", "42", "CUST-", 100000, -1, True, False, 42.0, 42.5,
            None, [42]]:
    r = s.tool("get_customer_record", {"customer_id": bad})
    check(f"lenient rejects {bad!r} -> -32602", code(r) == -32602, r)
    data = r.get("error", {}).get("data", [])
    check(f"lenient rejection of {bad!r}: ONE plain customer_id entry, no union-branch names",
          [d.get("field") for d in data] == ["customer_id"] and "constrained" not in json.dumps(data), r)
r = s.tool("trigger_refund", {**GOOD_REFUND, "customer_id": "42", "amount": "x", "reason": "short"})
check("lenient: customer_id entry is collapsed while other fields keep their own entries",
      sorted(d.get("field") for d in r.get("error", {}).get("data", [])) == ["amount", "customer_id", "reason"], r)
r = s.tool("trigger_refund", {**GOOD_REFUND, "customer_id": 42, "amount": 1.0})
check("refund with int id -> receipt echoes CUST-00042", sc(r).get("customer_id") == "CUST-00042", r)
r = s.tool("trigger_refund", {**GOOD_REFUND, "customer_id": "00042", "amount": 1.0})
check("refund with bare-digit id -> receipt echoes CUST-00042", sc(r).get("customer_id") == "CUST-00042", r)
r = s.tool("trigger_refund", {**GOOD_REFUND, "customer_id": 42, "amount": 5000.0})
check("lenient: over-balance refusal names the canonical id CUST-00042",
      is_err_result(r) and "CUST-00042" in json.dumps(r), r)
r = s.tool("trigger_refund", {**GOOD_REFUND, "customer_id": "99999", "amount": 1.0})
check("lenient: closed-account refusal names the canonical id CUST-99999",
      is_err_result(r) and "CUST-99999" in json.dumps(r), r)

err = s.finish()
check("startup line shows lenient_ids=True audit_log=False", "lenient_ids=True audit_log=False" in err, err[:300])
check("lenient on + audit off: warns at startup that guesses are not logged",
      "guesses will not be logged" in err, err[:600])
check("audit off: no audit lines even though guesses happened", "audit tool=" not in err, err[-400:])

# ==========================================================================
print("\n== Phase 3: lenient + CUSTOMER_MCP_AUDIT_LOG=1 ==")
s = Session(CUSTOMER_MCP_LENIENT_IDS="true", CUSTOMER_MCP_AUDIT_LOG="YES")  # also checks value parsing
s.start()
s.tool("get_customer_record", {"customer_id": 42})
s.tool("get_customer_record", {"customer_id": "00042"})
s.tool("get_customer_record", {"customer_id": "CUST-00042"})
s.tool("get_customer_record", {"customer_id": "cust_00042"})            # rejected by schema
s.tool("get_customer_record", {"customer_id": "abc\nFAKE LOG LINE"})    # newline must be escaped
s.tool("get_customer_record", {"customer_id": True})                    # rejected by schema (type)
s.tool("get_customer_record", {})                                       # missing: must not leak args
s.tool("trigger_refund", {"customer_id": 42, "amount": 1.0, "reason": "SECRET-REASON-TEXT-123"})
s.tool("trigger_refund", {"amount": 1.0, "reason": "SECRET-REASON-TEXT-456"})  # missing id
err = s.finish()
check("audit: int logged as interpreted with raw input",
      "audit tool=get_customer_record customer_id_interpreted raw=42 -> CUST-00042 lenient=True" in err, err[-900:])
check("audit: bare digits logged as interpreted",
      "customer_id_interpreted raw='00042' -> CUST-00042" in err)
check("audit: canonical id logged plain, at INFO",
      " INFO customer-mcp: audit tool=get_customer_record customer_id raw='CUST-00042' lenient=True" in err)
check("audit: interpreted lines are WARNING level", " WARNING customer-mcp: audit tool=" in err)
check("audit: refund interpretation logged", "audit tool=trigger_refund customer_id_interpreted raw=42" in err)
check("audit: rejected id logged", "customer_id_rejected raw='cust_00042'" in err)
check("audit: rejected bool logged", "customer_id_rejected raw=True" in err)
check("audit: newline escaped, cannot forge a log line",
      "raw='abc\\nFAKE LOG LINE'" in err and "\nFAKE LOG LINE" not in err)
check("audit: missing id does not leak other arguments", "SECRET-REASON-TEXT" not in err)

# ==========================================================================
print("\n== Phase 4: strict + CUSTOMER_MCP_AUDIT_LOG=1 ==")
s = Session(CUSTOMER_MCP_AUDIT_LOG="1")
s.start()
s.tool("get_customer_record", {"customer_id": "CUST-00042"})
s.tool("get_customer_record", {"customer_id": "00042"})
err = s.finish()
check("strict+audit: canonical id logged", "audit tool=get_customer_record customer_id raw='CUST-00042' lenient=False" in err)
check("strict+audit: rejected shorthand logged with raw input",
      "customer_id_rejected raw='00042' lenient=False" in err)

# ==========================================================================
print("\n== Phase 5: invalid flag value aborts startup ==")
for name, val in [("CUSTOMER_MCP_AUDIT_LOG", "maybe"), ("CUSTOMER_MCP_LENIENT_IDS", "ture")]:
    e = {k: v for k, v in os.environ.items() if not k.startswith("CUSTOMER_MCP_")}
    e[name] = val
    p = subprocess.run([sys.executable, "server.py"], input="", capture_output=True, text=True, env=e, timeout=30)
    check(f"{name}={val!r}: exits non-zero", p.returncode != 0, p.returncode)
    check(f"{name}={val!r}: message on stderr, nothing on stdout", name in p.stderr and p.stdout == "", (p.stdout, p.stderr))

print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
