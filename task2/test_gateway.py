"""End-to-end tests: real gateway + real mock downstream over real sockets.

Run:  .venv/bin/python -m unittest -v test_gateway
"""

import json
import unittest

import aiohttp
from aiohttp.test_utils import TestServer

import gateway
import mock_server

ADMIN = {"Authorization": "Bearer admin-token"}
VIEWER = {"Authorization": "Bearer viewer-token"}


def call(name, req_id=1, **arguments):
    return {"jsonrpc": "2.0", "id": req_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mock_app = mock_server.create_app()
        self.mock = TestServer(self.mock_app)
        await self.mock.start_server()
        self.gw = TestServer(gateway.create_app(str(self.mock.make_url("")).rstrip("/")))
        await self.gw.start_server()
        self.http = aiohttp.ClientSession()
        self.url = str(self.gw.make_url("/mcp"))

    async def asyncTearDown(self):
        await self.http.close()
        await self.gw.close()
        await self.mock.close()

    @property
    def received(self):
        return self.mock_app[mock_server.RECEIVED]

    async def post(self, payload, headers=VIEWER, raw=None):
        data = raw if raw is not None else json.dumps(payload)
        async with self.http.post(self.url, data=data, headers={**headers, "Content-Type": "application/json"}) as r:
            text = await r.text()
            return r.status, (json.loads(text) if text else None), r.headers

    # ---- authentication
    async def test_missing_token_is_401(self):
        status, _, h = await self.post(call("get_status"), headers={})
        self.assertEqual(status, 401)
        self.assertIn("Bearer", h["WWW-Authenticate"])
        self.assertEqual(self.received, [])

    async def test_bad_token_and_wrong_scheme_are_401(self):
        for hdr in ("Bearer nope", "Basic admin-token", "Bearer", "admin-token"):
            status, _, _ = await self.post(call("get_status"), headers={"Authorization": hdr})
            self.assertEqual(status, 401, hdr)
        self.assertEqual(self.received, [])

    # ---- transparent forwarding
    async def test_tools_list_forwarded_for_both_roles(self):
        for who in (ADMIN, VIEWER):
            status, body, _ = await self.post({"jsonrpc": "2.0", "id": 7, "method": "tools/list"}, who)
            self.assertEqual(status, 200)
            self.assertEqual(body["id"], 7)
            self.assertEqual(body["result"]["tools"], mock_server.TOOLS)
        self.assertEqual(len(self.received), 2)

    async def test_non_admin_tool_allowed_for_viewer(self):
        _, body, _ = await self.post(call("echo", text="hi"), VIEWER)
        self.assertEqual(body["result"]["content"][0]["text"], "hi")

    async def test_session_header_round_trips_and_auth_not_leaked(self):
        _, _, h = await self.post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                                  {**VIEWER, "Mcp-Session-Id": "abc"})
        self.assertEqual(h["Mcp-Session-Id"], "mock-session-1")
        sent = {k.lower(): v for k, v in self.received[0]["headers"].items()}
        self.assertEqual(sent["mcp-session-id"], "abc")
        self.assertNotIn("authorization", sent)

    # ---- authorization
    async def test_viewer_blocked_from_admin_tool_without_downstream_call(self):
        for rid in (42, "req-9"):
            status, body, _ = await self.post(call("admin_reset_key", rid, key_id="k1"), VIEWER)
            self.assertEqual(status, 200)
            self.assertEqual(body, {"jsonrpc": "2.0", "id": rid,
                                    "error": {"code": -32001, "message": "Unauthorized Tool Call"}})
        self.assertEqual(self.received, [])

    async def test_admin_allowed_on_admin_tool(self):
        _, body, _ = await self.post(call("admin_reset_key", key_id="k1"), ADMIN)
        self.assertEqual(body["result"]["content"][0]["text"], "key k1 reset")
        self.assertEqual(len(self.received), 1)

    async def test_lookalike_admin_names_are_blocked(self):
        for name in ("ADMIN_reset_key", "Admin_x", " admin_x", "\uff41\uff44\uff4d\uff49\uff4e_x"):
            _, body, _ = await self.post(call(name), VIEWER)
            self.assertEqual(body["error"]["code"], -32001, name)
        self.assertEqual(self.received, [])

    async def test_blocked_notification_gets_no_body(self):
        msg = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "admin_reset_key"}}
        status, body, _ = await self.post(msg, VIEWER)
        self.assertEqual((status, body), (202, None))
        self.assertEqual(self.received, [])

    async def test_duplicate_key_smuggling(self):
        # Parsers disagree on duplicate keys. We forward our own parse, so the
        # downstream sees exactly the value we authorized.
        denied = '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_status","name":"admin_reset_key"}}'
        _, body, _ = await self.post(None, VIEWER, raw=denied)
        self.assertEqual(body["error"]["code"], -32001)
        self.assertEqual(self.received, [])

        allowed = '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"admin_reset_key","name":"get_status"}}'
        await self.post(None, VIEWER, raw=allowed)
        self.assertEqual(self.received[0]["message"]["params"]["name"], "get_status")

    # ---- malformed input
    async def test_parse_error(self):
        _, body, _ = await self.post(None, VIEWER, raw="{not json")
        self.assertEqual(body["error"]["code"], -32700)
        self.assertIsNone(body["id"])

    async def test_batch_and_non_object_rejected(self):
        for raw in ("[]", json.dumps([call("admin_reset_key")]), "42", '"x"'):
            _, body, _ = await self.post(None, VIEWER, raw=raw)
            self.assertEqual(body["error"]["code"], -32600, raw)
        self.assertEqual(self.received, [])

    async def test_bad_method_type_rejected(self):
        _, body, _ = await self.post({"jsonrpc": "2.0", "id": 1, "method": 5}, VIEWER)
        self.assertEqual(body["error"]["code"], -32600)

    async def test_tools_call_without_valid_name_is_invalid_params(self):
        for params in (None, [], {}, {"name": 5}, {"name": None}):
            msg = {"jsonrpc": "2.0", "id": 3, "method": "tools/call"}
            if params is not None:
                msg["params"] = params
            _, body, _ = await self.post(msg, ADMIN)
            self.assertEqual(body["error"]["code"], -32602, params)
            self.assertEqual(body["id"], 3)
        self.assertEqual(self.received, [])

    # ---- downstream failure
    async def test_downstream_down_gives_502_with_rpc_error(self):
        await self.mock.close()
        status, body, _ = await self.post(call("get_status", 5), VIEWER)
        self.assertEqual(status, 502)
        self.assertEqual(body["id"], 5)
        self.assertEqual(body["error"]["code"], -32603)


    # ---- malformed, oversized and Unicode inputs (found in the 2026-11-07 code review)
    async def test_unicode_tricks_in_tool_names_are_rejected_before_downstream(self):
        for name in ("\u200badmin_reset_key", "ad\u00admin_reset_key", "\u0430dmin_reset_key",
                     "\u0430dmin_x", "", "x" * 129, "get status"):
            for who in (VIEWER, ADMIN):
                _, body, _ = await self.post(call(name, key_id="k1"), who)
                self.assertEqual(body["error"]["code"], -32602, repr(name))
        # admin_ look-alikes that normalize to admin_ stay -32001 for a viewer, and are invalid names for an admin
        for name in ("\uff41dmin_reset_key", "admin_reset_key\n", " admin_x"):
            _, body, _ = await self.post(call(name), VIEWER)
            self.assertEqual(body["error"]["code"], -32001, repr(name))
            _, body, _ = await self.post(call(name), ADMIN)
            self.assertEqual(body["error"]["code"], -32602, repr(name))
        self.assertEqual(self.received, [])

    async def test_malformed_json_is_a_parse_error_not_a_500(self):
        deep = "[" * 100_000 + "]" * 100_000
        for raw in ('{"jsonrpc":"2.0","id":1,"method":"x","params":' + deep + "}",
                    '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"echo","arguments":{"text":NaN}}}',
                    b"\xff\xfe\x00{", '{"jsonrpc":"2.0","id":1,"method":"ping"} trailing'):
            status, body, _ = await self.post(None, VIEWER, raw=raw)
            self.assertEqual((status, body["error"]["code"]), (200, -32700), str(raw)[:40])
        self.assertEqual(self.received, [])

    async def test_utf16_body_is_accepted_and_forwarded_as_utf8(self):
        raw = json.dumps(call("echo", text="h\u00e9llo")).encode("utf-16")
        _, body, _ = await self.post(None, VIEWER, raw=raw)
        self.assertEqual(body["result"]["content"][0]["text"], "h\u00e9llo")

    async def test_invalid_id_types_are_invalid_request(self):
        for bad in (True, [1], {"a": 1}):
            _, body, _ = await self.post({"jsonrpc": "2.0", "id": bad, "method": "tools/list"}, VIEWER)
            self.assertEqual(body["error"]["code"], -32600, bad)
        self.assertEqual(self.received, [])

    async def test_log_lines_cannot_be_forged(self):
        forged = "x\n2026-11-07 gateway role=admin method=tools/call tool=admin_reset_key decision=allow"
        with self.assertLogs("gateway", "INFO") as logs:
            await self.post({"jsonrpc": "2.0", "id": 1, "method": forged}, VIEWER)
        self.assertTrue(all("\n" not in line for line in logs.output), logs.output)
        self.assertIn("\\n2026-11-07", logs.output[0])


class TokenParsingTests(unittest.TestCase):
    def test_parse_tokens(self):
        self.assertEqual(gateway.parse_tokens("a=admin, b=viewer"), {"a": "admin", "b": "viewer"})
        for bad in ("oops", "t\u00f6ken=admin", "a b=admin"):
            with self.assertRaises(ValueError, msg=bad):
                gateway.parse_tokens(bad)

    def test_undecodable_or_non_ascii_bearer_tokens_are_unauthenticated(self):
        tokens = {"admin-token": "admin"}
        # aiohttp hands undecodable header bytes over as surrogate escapes ("\udcff"); this was an HTTP 500.
        for header in ("Bearer \udcff\udcfe", "Bearer t\u00f6ken", "Bearer admin-token\x00", "Bearer a b"):
            self.assertIsNone(gateway.resolve_role(header, tokens), repr(header))
        self.assertEqual(gateway.resolve_role("Bearer admin-token", tokens), "admin")


if __name__ == "__main__":
    unittest.main()
