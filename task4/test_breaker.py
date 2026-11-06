"""Circuit breaker and failback tests.  Run: uv run python -m unittest -v test_breaker"""

import asyncio
import time
import unittest

import aiohttp
from aiohttp.test_utils import TestServer

import mock_provider
from errors import GatewayError
from router import (ALLOW, CLOSED, DENY, HALF_OPEN, OPEN, TRIAL, BreakerConfig, CircuitBreaker, ModelRouter,
                    ProviderConfig)

PAYLOAD = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
CFG = BreakerConfig(failure_threshold=3, base_cooldown_s=30, max_cooldown_s=300)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class BreakerUnitTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.b = CircuitBreaker("p", CFG, self.clock)

    def open_it(self):
        for _ in range(3):
            self.b.record_failure()

    def test_opens_only_after_consecutive_failures(self):
        self.b.record_failure()
        self.b.record_failure()
        self.assertEqual((self.b.state, self.b.allow()), (CLOSED, ALLOW))
        self.b.record_success()                      # a success resets the streak
        self.b.record_failure()
        self.b.record_failure()
        self.assertEqual(self.b.state, CLOSED)
        self.b.record_failure()
        self.assertEqual(self.b.state, OPEN)

    def test_denied_during_cooldown_then_exactly_one_trial(self):
        self.open_it()
        self.clock.now += 29.9
        self.assertEqual(self.b.allow(), DENY)
        self.clock.now += 0.2
        self.assertEqual(self.b.allow(), TRIAL)
        self.assertEqual(self.b.state, HALF_OPEN)
        self.assertEqual([self.b.allow() for _ in range(5)], [DENY] * 5)   # the trial is in flight

    def test_trial_success_closes(self):
        self.open_it()
        self.clock.now += 31
        self.b.allow()
        self.b.record_success()
        self.assertEqual((self.b.state, self.b.allow(), self.b.cooldown_s), (CLOSED, ALLOW, 30))

    def test_failed_trials_double_the_cooldown_up_to_the_cap(self):
        self.open_it()
        seen = []
        for _ in range(6):
            self.clock.now += self.b.cooldown_s + 1
            self.assertEqual(self.b.allow(), TRIAL)
            self.b.record_failure()
            seen.append(self.b.cooldown_s)
        self.assertEqual(seen, [60, 120, 240, 300, 300, 300])

    def test_retry_after_replaces_the_default_cooldown_and_blocks_expedite(self):
        for _ in range(3):
            self.b.record_failure(retry_after=5)
        self.assertEqual(self.b.cooldown_s, 5)
        self.b.expedite()                            # a health probe must not override Retry-After
        self.assertEqual(self.b.allow(), DENY)
        self.clock.now += 5
        self.assertEqual(self.b.allow(), TRIAL)

    def test_retry_after_is_capped(self):
        for _ in range(3):
            self.b.record_failure(retry_after=9999)
        self.assertEqual(self.b.cooldown_s, 300)

    def test_expedite_makes_the_next_request_the_trial(self):
        self.open_it()
        self.b.expedite()
        self.assertEqual(self.b.allow(), TRIAL)

    def test_abandoned_trial_can_be_retried_immediately(self):
        self.open_it()
        self.clock.now += 31
        permit = self.b.allow()
        self.b.abandon(permit)
        self.assertEqual(self.b.state, OPEN)
        self.assertEqual(self.b.allow(), TRIAL)

    def test_failures_while_open_do_not_extend_the_cooldown(self):
        self.open_it()
        until = self.b.open_until
        self.b.record_failure()                      # e.g. a forced last-resort attempt
        self.assertEqual(self.b.open_until, until)


class RouterCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = FakeClock()
        self.primary = TestServer(mock_provider.create_app("primary"))
        self.secondary = TestServer(mock_provider.create_app("secondary"))
        await self.primary.start_server()
        await self.secondary.start_server()
        self.session = aiohttp.ClientSession()
        self.router = self.make_router()

    def make_router(self, cfg=CFG, primary_timeout=0.2, health=False):
        url = lambda s: str(s.make_url("")).rstrip("/")  # noqa: E731
        return ModelRouter(self.session, [
            ProviderConfig("primary", url(self.primary), timeout_s=primary_timeout,
                           health_url=url(self.primary) + "/health" if health else ""),
            ProviderConfig("secondary", url(self.secondary), timeout_s=1.0),
        ], cfg, self.clock)

    async def asyncTearDown(self):
        await self.router.stop()
        await self.session.close()
        await self.primary.close()
        await self.secondary.close()

    @property
    def p(self):
        return self.primary.app[mock_provider.BEHAVIOR]

    @property
    def s(self):
        return self.secondary.app[mock_provider.BEHAVIOR]

    def calls(self, server):
        return len(server.app[mock_provider.CALLS])

    async def route(self):
        return (await self.router.complete(PAYLOAD)).provider

    async def open_primary(self, status=500):
        self.p["status"] = status
        for _ in range(3):
            await self.route()
        self.assertEqual(self.router.breakers["primary"].state, OPEN)


class SkipTests(RouterCase):
    async def test_dead_primary_is_skipped_once_its_circuit_opens(self):
        self.p["status"] = 500
        results = [await self.route() for _ in range(6)]
        self.assertEqual(results, ["secondary"] * 6)
        self.assertEqual(self.calls(self.primary), 3)           # only the three that opened the circuit
        self.assertEqual(self.calls(self.secondary), 6)

    async def test_the_timeout_tax_disappears(self):
        self.p["delay_ms"] = 1500                               # primary hangs; its deadline is 0.2 s
        durations = []
        for _ in range(5):
            started = time.perf_counter()
            self.assertEqual(await self.route(), "secondary")
            durations.append(time.perf_counter() - started)
        self.assertTrue(all(d >= 0.2 for d in durations[:3]), durations)   # paid the timeout while closed
        self.assertTrue(all(d < 0.1 for d in durations[3:]), durations)    # skipped once open

    async def test_a_bad_request_never_opens_the_circuit(self):
        self.p["status"] = 400
        for _ in range(5):
            with self.assertRaises(GatewayError) as cm:
                await self.route()
            self.assertEqual(cm.exception.status, 400)
        self.assertEqual(self.router.breakers["primary"].state, CLOSED)
        self.assertEqual(self.calls(self.secondary), 0)

    async def test_throttling_uses_the_providers_retry_after(self):
        await self.open_primary(429)                            # the mock sends Retry-After: 2
        self.assertEqual(self.router.breakers["primary"].cooldown_s, 2)


class FailbackTests(RouterCase):
    async def test_primary_returns_after_the_cooldown(self):
        await self.open_primary()
        self.p["status"] = 200                                  # the primary recovers
        self.clock.now += 29
        self.assertEqual(await self.route(), "secondary")       # still cooling down
        self.assertEqual(self.calls(self.primary), 3)
        self.clock.now += 2
        self.assertEqual(await self.route(), "primary")         # the trial succeeds
        self.assertEqual(self.router.breakers["primary"].state, CLOSED)
        self.assertEqual([await self.route() for _ in range(3)], ["primary"] * 3)

    async def test_failed_trial_backs_off_and_still_serves_from_the_secondary(self):
        await self.open_primary()
        self.clock.now += 31
        self.assertEqual(await self.route(), "secondary")       # trial fails, request fails over
        self.assertEqual(self.router.breakers["primary"].cooldown_s, 60)
        self.assertEqual(self.calls(self.primary), 4)
        self.assertEqual(await self.route(), "secondary")       # skipped again, no extra call
        self.assertEqual(self.calls(self.primary), 4)

    async def test_user_scenario_primary_is_retried_when_the_secondary_dies(self):
        await self.open_primary()                               # 1. primary unreachable
        for _ in range(3):                                      # 2. secondary carries the traffic
            self.assertEqual(await self.route(), "secondary")
        self.p["status"] = 200                                  # (the primary quietly recovers)
        self.s["status"] = 500                                  # 3. secondary becomes unreachable
        primary_calls = self.calls(self.primary)
        self.assertEqual(await self.route(), "primary")         # primary retried at once, no cooldown wait
        self.assertEqual(self.calls(self.primary), primary_calls + 1)
        self.assertEqual(self.router.breakers["primary"].state, CLOSED)   # and failed back
        self.assertEqual(await self.route(), "primary")

    async def test_when_every_circuit_is_open_the_primary_is_tried_first(self):
        self.p["status"] = self.s["status"] = 500
        for _ in range(3):
            with self.assertRaises(GatewayError):
                await self.route()
        self.assertEqual([b.state for b in self.router.breakers.values()], [OPEN, OPEN])
        self.p["status"] = self.s["status"] = 200
        before = (self.calls(self.primary), self.calls(self.secondary))
        self.assertEqual(await self.route(), "primary")         # forced pass, priority order
        self.assertEqual((self.calls(self.primary), self.calls(self.secondary)), (before[0] + 1, before[1]))

    async def test_all_providers_down_is_still_a_sanitized_error(self):
        self.p["status"] = self.s["status"] = 500
        for _ in range(5):
            with self.assertRaises(GatewayError) as cm:
                await self.route()
        self.assertEqual((cm.exception.status, cm.exception.code), (502, "upstream_error"))


class ConcurrencyTests(RouterCase):
    async def test_only_one_trial_request_reaches_a_recovering_primary(self):
        await self.open_primary()
        self.p.update(status=200, delay_ms=150)                 # recovered, but slow enough to overlap
        self.router = self.make_router(primary_timeout=1.0)
        self.router.breakers["primary"].state = OPEN
        self.router.breakers["primary"].open_until = self.clock.now - 1
        before = self.calls(self.primary)
        results = await asyncio.gather(*[self.route() for _ in range(10)])
        self.assertEqual(self.calls(self.primary) - before, 1)  # exactly one trial
        self.assertEqual(results.count("primary"), 1)
        self.assertEqual(results.count("secondary"), 9)
        self.assertEqual(self.router.breakers["primary"].state, CLOSED)

    async def test_a_cancelled_trial_does_not_wedge_the_circuit(self):
        await self.open_primary()
        self.p.update(status=200, delay_ms=600)
        self.router = self.make_router(primary_timeout=2.0)
        breaker = self.router.breakers["primary"]
        breaker.state, breaker.open_until = OPEN, self.clock.now - 1
        task = asyncio.create_task(self.route())
        await asyncio.sleep(0.1)
        self.assertEqual(breaker.state, HALF_OPEN)              # the trial is in flight
        task.cancel()                                           # e.g. the client disconnected
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(breaker.state, OPEN)
        self.assertEqual(breaker.allow(), TRIAL)                # the next request may try again


class AmplificationTests(RouterCase):
    async def test_an_outage_does_not_send_every_request_to_every_provider(self):
        await self.open_primary()
        self.router.breakers["secondary"].state = OPEN           # both circuits open: all requests are "forced"
        self.router.breakers["secondary"].open_until = self.clock.now + 100
        self.p["delay_ms"] = self.s["delay_ms"] = 300
        before = (self.calls(self.primary), self.calls(self.secondary))
        results = await asyncio.gather(*[self.route() for _ in range(20)], return_exceptions=True)
        self.assertLessEqual(self.calls(self.primary) - before[0], 1)    # one forced attempt per provider
        self.assertLessEqual(self.calls(self.secondary) - before[1], 1)
        shed = [x for x in results if isinstance(x, GatewayError) and x.code == "upstream_unavailable"]
        self.assertGreaterEqual(len(shed), 18)                    # the rest fail fast
        self.assertTrue(all(x.status == 503 and x.retry_after >= 1 for x in shed))


class ActiveProbeTests(RouterCase):
    async def test_healthy_probe_shortens_the_wait_but_does_not_close_the_circuit(self):
        self.router = self.make_router(BreakerConfig(3, 30, 300, probe_interval_s=0.05, probe_timeout_s=0.5),
                                       health=True)
        await self.open_primary()
        self.router.start()
        await asyncio.sleep(0.25)                               # probes run, primary still unhealthy
        self.assertEqual(await self.route(), "secondary")
        self.assertEqual(self.calls(self.primary), 3)           # no request wasted on it
        self.p["status"] = 200                                  # primary recovers; the clock never advances
        await asyncio.sleep(0.25)                               # a probe sees /health succeed
        breaker = self.router.breakers["primary"]
        self.assertEqual(breaker.state, OPEN)                   # a probe alone never closes it
        self.assertEqual(await self.route(), "primary")         # but the next real request is the trial
        self.assertEqual(breaker.state, CLOSED)


if __name__ == "__main__":
    unittest.main()
