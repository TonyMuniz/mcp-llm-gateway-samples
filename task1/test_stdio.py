"""Black-box test: spawns server.py several times with different per-process
settings and speaks raw JSON-RPC over stdio.

Run:  uv run python -m unittest -v      (or: uv run python test_stdio.py)
Each phase is one test; every check inside it is a subTest, so a failing check is reported
by name and the remaining checks of that phase still run.

Phases: default (strict, audit off) / lenient / lenient+audit / strict+audit / bad env value.
In every phase every stdout line must parse as JSON-RPC (stdout purity).
"""
import json
import os
import subprocess
import sys
import unittest

checks = 0
_case: unittest.TestCase | None = None   # the running test, set in setUp
_sessions: list = []                     # server processes started by the running test


def check(label, ok, detail=""):
    """Soft assertion: record a pass or failure under its label and keep going."""
    global checks
    checks += 1
    with _case.subTest(label):
        _case.assertTrue(ok, f"{label}  -> {detail}")


class Session:
    """One server process."""

    def __init__(self, **env):
        e = {k: v for k, v in os.environ.items() if not k.startswith("CUSTOMER_MCP_")}
        e.update(env)
        self.proc = subprocess.Popen([sys.executable, "server.py"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, encoding="utf-8", env=e)
        self.id = 0
        self.stdout_lines: list[str] = []
        _sessions.append(self)

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

    def raw(self, line: bytes):
        """Send raw bytes as one line; return the next reply with "id": null (a parse-level error)."""
        self.proc.stdin.flush()
        self.proc.stdin.buffer.write(line + b"\n")
        self.proc.stdin.buffer.flush()
        while True:
            out = self.proc.stdout.readline()
            assert out, "server closed stdout"
            self.stdout_lines.append(out)
            msg = json.loads(out)
            if "id" in msg and msg["id"] is None:
                return msg

    def raw_tool(self, line: bytes):
        """Send a hand-built tools/call line (it must use the next id) and return its reply."""
        self.id += 1
        self.proc.stdin.flush()
        self.proc.stdin.buffer.write(line.replace(b'"id": 0', f'"id": {self.id}'.encode()) + b"\n")
        self.proc.stdin.buffer.flush()
        while True:
            out = self.proc.stdout.readline()
            assert out, "server closed stdout"
            self.stdout_lines.append(out)
            msg = json.loads(out)
            if msg.get("id") == self.id:
                return msg

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


class StdioTests(unittest.TestCase):
    """Spawns server.py per phase and speaks raw JSON-RPC over stdio."""

    def setUp(self):
        global _case
        _case = self
        _sessions.clear()

    def tearDown(self):
        """Kill a server left running when a phase stops early (an exception, not a failed check)."""
        for sess in _sessions:
            if sess.proc.poll() is None:
                sess.proc.kill()
                sess.proc.wait(timeout=10)
            for f in (sess.proc.stdin, sess.proc.stdout, sess.proc.stderr):
                try:
                    f.close()
                except (OSError, ValueError):
                    pass

    @classmethod
    def tearDownClass(cls):
        print(f"\n{checks} checks", file=sys.stderr)

    def test_phase1_default_strict_ids_audit_off(self):
        """Phase 1: default (strict ids, audit off)"""
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

        # Encoding and character-level edge cases (added in the 2026-11-07 code review).
        good_line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
        unparseable = {
            "garbage": b"this is not json",
            "truncated JSON": b'{"jsonrpc":"2.0","id":5,"method":',
            "UTF-8 BOM before the message": b"\xef\xbb\xbf" + good_line,
            "UTF-16 encoded line": good_line.decode().encode("utf-16-le"),
            "lone surrogate escape": b'{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"x\\ud800"}}',
            "nesting 5000 deep": b'{"jsonrpc":"2.0","id":5,"method":"ping","params":{"a":' + b"[" * 5000 + b"]" * 5000 + b"}}",
        }
        for label, line in unparseable.items():
            r = s.raw(line)
            check(f"{label} -> -32700 with id null (used to get no reply at all)", code(r) == -32700, r)
        r = s.raw(b'{"hello": "world"}')
        check("valid JSON that is not JSON-RPC -> -32600 with id null", code(r) == -32600, r)
        check("server still answers after unparseable lines", "result" in s.call("ping"))

        bad_reasons = {
            "NUL": "ok\u0000 reason text here",
            "ESC": "ok\u001b[31m reason text",
            "zero-width spaces only": "\u200b" * 12,
            "RLO bidi override": "refund \u202eesrever text",
            "line separator U+2028": "ok\u2028 reason text here",
            "private-use character": "ok\ue000 reason text here",
            "U+FFFD replacement char": "ok \ufffd reason text here",
        }
        for label, reason in bad_reasons.items():
            r = s.tool("trigger_refund", {**GOOD_REFUND, "amount": 1.0, "reason": reason})
            check(f"refund reason with {label} -> -32602", code(r) == -32602, r)
        r = s.raw_tool(b'{"jsonrpc": "2.0", "id": 0, "method": "tools/call", "params": {"name": "trigger_refund", "arguments": '
                       b'{"customer_id": "CUST-00042", "amount": 1.0, "reason": "bad \xff\xfe bytes in here"}}}')
        check("refund reason with invalid UTF-8 bytes -> -32602 invalid_encoding (was silently stored as U+FFFD)",
              code(r) == -32602 and r["error"]["data"][0]["type"] == "invalid_encoding", r)
        for reason in ("Caf\u00e9 order arrived damaged", "Late delivery \U0001F4E6 again",
                       "Two lines:\n\tbox was crushed"):
            r = s.tool("trigger_refund", {**GOOD_REFUND, "amount": 1.0, "reason": reason})
            check(f"refund reason {reason!r} (accents, emoji, tab/newline) is accepted",
                  sc(r).get("reason") == reason.strip(), r)
        for cid in ("CUST-\uff10\uff10\uff10\uff14\uff12", "CUST-\u0660\u0660\u0660\u0664\u0662", "CU\u017fT-00042"):
            check(f"strict: non-ASCII look-alike id {cid!r} -> -32602",
                  code(s.tool("get_customer_record", {"customer_id": cid})) == -32602)

        err = s.finish()
        check("startup line shows lenient_ids=False audit_log=False", "lenient_ids=False audit_log=False" in err, err[:300])
        check("audit off: no audit lines at all", "audit tool=" not in err, err[-400:])

    def test_phase2_customer_mcp_lenient_ids_1_audit_off(self):
        """Phase 2: CUSTOMER_MCP_LENIENT_IDS=1 (audit off)"""
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

    def test_phase3_lenient_customer_mcp_audit_log_1(self):
        """Phase 3: lenient + CUSTOMER_MCP_AUDIT_LOG=1"""
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

    def test_phase4_strict_customer_mcp_audit_log_1(self):
        """Phase 4: strict + CUSTOMER_MCP_AUDIT_LOG=1"""
        s = Session(CUSTOMER_MCP_AUDIT_LOG="1")
        s.start()
        s.tool("get_customer_record", {"customer_id": "CUST-00042"})
        s.tool("get_customer_record", {"customer_id": "00042"})
        s.tool("get_customer_record", {"customer_id": "CU\u017fT-00042"})  # long s: looks like CUST-00042
        err = s.finish()
        check("strict+audit: non-ASCII look-alike is logged escaped, so it cannot pass for CUST-00042",
              "customer_id_rejected raw='CU\\u017fT-00042'" in err and "\u017f" not in err, err[-600:])
        check("strict+audit: canonical id logged", "audit tool=get_customer_record customer_id raw='CUST-00042' lenient=False" in err)
        check("strict+audit: rejected shorthand logged with raw input",
              "customer_id_rejected raw='00042' lenient=False" in err)

    def test_phase5_invalid_flag_value_aborts_startup(self):
        """Phase 5: invalid flag value aborts startup"""
        for name, val in [("CUSTOMER_MCP_AUDIT_LOG", "maybe"), ("CUSTOMER_MCP_LENIENT_IDS", "ture")]:
            e = {k: v for k, v in os.environ.items() if not k.startswith("CUSTOMER_MCP_")}
            e[name] = val
            p = subprocess.run([sys.executable, "server.py"], input="", capture_output=True, text=True, env=e, timeout=30)
            check(f"{name}={val!r}: exits non-zero", p.returncode != 0, p.returncode)
            check(f"{name}={val!r}: message on stderr, nothing on stdout", name in p.stderr and p.stdout == "", (p.stdout, p.stderr))


if __name__ == "__main__":
    unittest.main(verbosity=2)
