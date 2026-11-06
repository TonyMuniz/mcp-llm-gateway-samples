"""End-to-end tests: gateway + two mock providers over real sockets.

Run:  uv run python -m unittest -v test_gateway
"""

import asyncio
import io
import json
import os
import tempfile
import time
import unittest
from unittest import mock

import aiohttp
from aiohttp.test_utils import TestServer

import gateway
import mock_provider
from ratelimit import hash_key
from router import PRIMARY_TIMEOUT_S, ModelRouter, ProviderConfig

PAYLOAD = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 100}  # estimate: 105
ERROR_KEYS = {"message", "type", "code", "request_id"}
LEAKS = ("Traceback", "/srv/", "hunter2", "postgres", "gpu-node", "server.py", "CUDA", "says no")


class GatewayCase(unittest.IsolatedAsyncioTestCase):
    LIMIT = 50_000
    PRIMARY_TIMEOUT = 0.3
    SECONDARY_TIMEOUT = 1.5
    MAX_INFLIGHT = 200

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.primary = TestServer(mock_provider.create_app("primary"))
        self.secondary = TestServer(mock_provider.create_app("secondary"))
        await self.primary.start_server()
        await self.secondary.start_server()
        config = gateway.Config(
            providers=[
                ProviderConfig("primary", self.url(self.primary), "pk-primary", self.PRIMARY_TIMEOUT),
                ProviderConfig("secondary", self.url(self.secondary), "pk-secondary", self.SECONDARY_TIMEOUT,
                               model="backup-model"),
            ],
            db_path=os.path.join(self.tmp.name, "usage.sqlite3"), api_keys=("k1", "k2"),
            tokens_per_window=self.LIMIT, max_inflight=self.MAX_INFLIGHT)
        self.gw = TestServer(gateway.create_app(config))
        await self.gw.start_server()
        self.http = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.http.close()
        await self.gw.close()
        await self.primary.close()
        await self.secondary.close()
        self.tmp.cleanup()

    @staticmethod
    def url(server):
        return str(server.make_url("")).rstrip("/")

    @property
    def p_app(self):
        return self.primary.app

    @property
    def s_app(self):
        return self.secondary.app

    async def post(self, payload=PAYLOAD, key="k1", raw=None, method="POST", path="/v1/chat/completions"):
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        data = raw if raw is not None else json.dumps(payload)
        async with self.http.request(method, str(self.gw.make_url(path)), data=data, headers=headers) as r:
            text = await r.text()
            return r.status, (json.loads(text) if text else None), r.headers

    def assertStandardError(self, status, body, headers, want_status, want_code):
        self.assertEqual(status, want_status)
        self.assertEqual(set(body), {"error"})
        self.assertEqual(set(body["error"]), ERROR_KEYS)
        self.assertEqual((body["error"]["type"], body["error"]["code"]), ("gateway_error", want_code))
        self.assertEqual(headers["X-Request-Id"], body["error"]["request_id"])
        dump = json.dumps(body)
        for leak in LEAKS:
            self.assertNotIn(leak, dump)

    async def usage(self, key="k1"):
        limiter = self.gw.app[gateway.LIMITER]
        return await limiter.store.usage(hash_key(key), limiter.window_ms, limiter.clock())


class RoutingTests(GatewayCase):
    async def test_happy_path_uses_primary_with_the_providers_own_key(self):
        status, body, h = await self.post()
        self.assertEqual(status, 200)
        self.assertEqual(body["choices"][0]["message"]["content"], "Hello from primary")
        self.assertEqual(h["X-Routed-To"], "primary")
        self.assertEqual(h["X-RateLimit-Remaining-Tokens"], str(self.LIMIT - 15))  # settled to real usage
        self.assertEqual(self.p_app[mock_provider.CALLS][0]["authorization"], "Bearer pk-primary")  # not the tenant key
        self.assertEqual(self.s_app[mock_provider.CALLS], [])

    async def test_failover_on_429(self):
        self.p_app[mock_provider.BEHAVIOR]["status"] = 429
        status, body, h = await self.post()
        self.assertEqual((status, h["X-Routed-To"]), (200, "secondary"))
        self.assertEqual(body["choices"][0]["message"]["content"], "Hello from secondary")
        self.assertEqual((len(self.p_app[mock_provider.CALLS]), len(self.s_app[mock_provider.CALLS])), (1, 1))
        self.assertEqual(self.s_app[mock_provider.CALLS][0]["payload"]["model"], "backup-model")  # per-provider model
        self.assertEqual(self.p_app[mock_provider.CALLS][0]["payload"]["model"], "m")

    async def test_failover_on_timeout_happens_at_the_deadline_not_when_the_primary_answers(self):
        self.p_app[mock_provider.BEHAVIOR]["delay_ms"] = 1200
        started = time.perf_counter()
        status, _, h = await self.post()
        elapsed = time.perf_counter() - started
        self.assertEqual((status, h["X-Routed-To"]), (200, "secondary"))
        self.assertGreaterEqual(elapsed, self.PRIMARY_TIMEOUT)
        self.assertLess(elapsed, 0.9)

    async def test_late_primary_reply_is_ignored_and_not_double_counted(self):
        self.p_app[mock_provider.BEHAVIOR]["delay_ms"] = 500        # answers after the 300 ms deadline
        status, _, h = await self.post()
        self.assertEqual((status, h["X-Routed-To"]), (200, "secondary"))
        await asyncio.sleep(0.7)                        # let the abandoned primary call finish
        self.assertEqual(await self.usage(), 15)        # only the secondary's tokens
        self.assertEqual(len(self.s_app[mock_provider.CALLS]), 1)

    async def test_failover_on_server_error_and_connection_failure(self):
        self.p_app[mock_provider.BEHAVIOR]["status"] = 503
        self.assertEqual((await self.post())[2]["X-Routed-To"], "secondary")
        self.p_app[mock_provider.BEHAVIOR]["status"] = 200
        await self.primary.close()                      # connection refused
        self.assertEqual((await self.post())[2]["X-Routed-To"], "secondary")

    async def test_no_failover_for_a_request_the_provider_calls_invalid(self):
        self.p_app[mock_provider.BEHAVIOR]["status"] = 400
        status, body, h = await self.post()
        self.assertStandardError(status, body, h, 400, "invalid_request")
        self.assertEqual(self.s_app[mock_provider.CALLS], [])

    async def test_primary_credentials_failure_fails_over(self):
        self.p_app[mock_provider.BEHAVIOR]["status"] = 401
        self.assertEqual((await self.post())[2]["X-Routed-To"], "secondary")


class OverloadTests(GatewayCase):
    MAX_INFLIGHT = 2

    async def test_excess_concurrency_fails_fast_with_503(self):
        self.p_app[mock_provider.BEHAVIOR]["delay_ms"] = 200     # keeps the first two in flight
        results = await asyncio.gather(*[self.post() for _ in range(6)])
        ok = [r for r in results if r[0] == 200]
        shed = [r for r in results if r[0] == 503]
        self.assertEqual((len(ok), len(shed)), (2, 4))
        for status, body, h in shed:
            self.assertStandardError(status, body, h, 503, "overloaded")
            self.assertEqual(h["Retry-After"], "1")
        self.assertEqual(len(self.p_app[mock_provider.CALLS]), 2)   # shed requests never reached a provider
        self.assertEqual((await self.post())[0], 200)             # the counter was released

    async def test_unauthenticated_requests_do_not_consume_capacity(self):
        self.p_app[mock_provider.BEHAVIOR]["delay_ms"] = 200
        slow = [asyncio.create_task(self.post()) for _ in range(2)]
        await asyncio.sleep(0.05)
        self.assertStandardError(*await self.post(key="wrong"), 401, "invalid_api_key")
        await asyncio.gather(*slow)


class FinalErrorTests(GatewayCase):
    async def test_both_rate_limited(self):
        self.p_app[mock_provider.BEHAVIOR]["status"] = self.s_app[mock_provider.BEHAVIOR]["status"] = 429
        status, body, h = await self.post()
        self.assertStandardError(status, body, h, 429, "upstream_rate_limited")
        self.assertEqual(h["Retry-After"], "2")

    async def test_both_time_out(self):
        self.p_app[mock_provider.BEHAVIOR]["delay_ms"] = self.s_app[mock_provider.BEHAVIOR]["delay_ms"] = 3000
        self.gw.app[gateway.ROUTER]._providers = [  # shorten both deadlines to keep the test fast
            ProviderConfig("primary", self.url(self.primary), timeout_s=0.2),
            ProviderConfig("secondary", self.url(self.secondary), timeout_s=0.2)]
        status, body, h = await self.post()
        self.assertStandardError(status, body, h, 504, "upstream_timeout")

    async def test_both_fail_with_server_errors_without_leaking_anything(self):
        for app in (self.p_app, self.s_app):
            app[mock_provider.BEHAVIOR].update(status=500, leak=True)
        status, body, h = await self.post()
        self.assertStandardError(status, body, h, 502, "upstream_error")
        self.assertNotIn("Traceback", json.dumps(dict(h)))

    async def test_failed_requests_give_their_reserved_tokens_back(self):
        self.p_app[mock_provider.BEHAVIOR]["status"] = self.s_app[mock_provider.BEHAVIOR]["status"] = 500
        for _ in range(3):
            await self.post()
        self.assertEqual(await self.usage(), 0)

    async def test_unexpected_exception_becomes_a_generic_500(self):
        with mock.patch.object(ModelRouter, "complete", side_effect=RuntimeError("secret at /etc/passwd")):
            status, body, h = await self.post()
        self.assertStandardError(status, body, h, 500, "internal_error")
        self.assertNotIn("passwd", json.dumps(body))
        self.assertEqual(await self.usage(), 0)

    async def test_every_client_side_error_is_the_standard_payload(self):
        cases = [
            (await self.post(key=None), 401, "invalid_api_key"),
            (await self.post(key="wrong"), 401, "invalid_api_key"),
            (await self.post(raw="{nope"), 400, "invalid_request"),
            (await self.post(payload={"model": "m"}), 400, "invalid_request"),
            (await self.post(payload={"model": "m", "messages": []}), 400, "invalid_request"),
            (await self.post(payload={**PAYLOAD, "stream": True}), 400, "streaming_not_supported"),
            (await self.post(path="/nope"), 404, "not_found"),
            (await self.post(method="GET"), 405, "method_not_allowed"),
            (await self.post(raw=io.BytesIO(b"x" * (1024 * 1024 + 10))), 413, "request_too_large"),
        ]
        for (status, body, h), want_status, want_code in cases:
            self.assertStandardError(status, body, h, want_status, want_code)
        self.assertEqual(self.p_app[mock_provider.CALLS], [])       # none of them reached a provider


class RateLimitTests(GatewayCase):
    LIMIT = 250

    async def test_over_limit_is_rejected_before_any_provider_is_called(self):
        self.p_app[mock_provider.BEHAVIOR]["tokens"] = 200           # first request really uses 200 of 250
        self.assertEqual((await self.post())[0], 200)
        status, body, h = await self.post()              # needs 105, only 50 left
        self.assertStandardError(status, body, h, 429, "rate_limit_exceeded")
        self.assertIn(h["Retry-After"], {"59", "60"})
        self.assertEqual(len(self.p_app[mock_provider.CALLS]), 1)

    async def test_other_tenants_are_unaffected(self):
        self.p_app[mock_provider.BEHAVIOR]["tokens"] = 250
        await self.post(key="k1")
        self.assertEqual((await self.post(key="k1"))[0], 429)
        self.assertEqual((await self.post(key="k2"))[0], 200)

    async def test_request_larger_than_the_whole_limit_is_413(self):
        status, body, h = await self.post({**PAYLOAD, "max_tokens": 5000})
        self.assertStandardError(status, body, h, 413, "request_too_large")


class ConcurrencyTests(GatewayCase):
    LIMIT = 1050                                         # room for exactly ten 105-token requests

    async def test_burst_of_requests_admits_exactly_what_fits(self):
        self.p_app[mock_provider.BEHAVIOR]["tokens"] = 105           # real usage equals the estimate
        results = await asyncio.gather(*[self.post() for _ in range(25)])
        codes = sorted(r[0] for r in results)
        self.assertEqual(codes, [200] * 10 + [429] * 15)
        self.assertEqual(len(self.p_app[mock_provider.CALLS]), 10)
        self.assertEqual(await self.usage(), 1050)


class DefaultsTests(unittest.TestCase):
    def test_primary_deadline_is_three_seconds(self):
        self.assertEqual(PRIMARY_TIMEOUT_S, 3.0)
        self.assertEqual(ProviderConfig("p", "http://x").timeout_s, 3.0)


if __name__ == "__main__":
    unittest.main()
