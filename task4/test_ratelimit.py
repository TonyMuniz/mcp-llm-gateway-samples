"""Rate limiter tests: real SQLite file, fake clock.  Run: uv run python -m unittest -v test_ratelimit"""

import asyncio
import os
import tempfile
import unittest

from errors import GatewayError
from ratelimit import TokenRateLimiter, UsageStore, estimate_tokens, hash_key


class FakeClock:
    def __init__(self, now: int = 1_000_000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


class LimiterCase(unittest.IsolatedAsyncioTestCase):
    LIMIT, WINDOW = 1000, 60_000

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "usage.sqlite3")
        self.clock = FakeClock()
        self.store = UsageStore(self.path)
        await self.store.open()
        self.limiter = TokenRateLimiter(self.store, self.LIMIT, self.WINDOW, self.clock)

    async def asyncTearDown(self):
        await self.store.close()
        self.tmp.cleanup()

    async def denied(self, key, tokens):
        with self.assertRaises(GatewayError) as cm:
            await self.limiter.reserve(key, tokens)
        return cm.exception


class WindowTests(LimiterCase):
    async def test_admits_up_to_the_limit_then_rejects(self):
        await self.limiter.reserve("k", 600)
        await self.limiter.reserve("k", 400)
        err = await self.denied("k", 1)
        self.assertEqual((err.status, err.code), (429, "rate_limit_exceeded"))

    async def test_window_slides_instead_of_resetting(self):
        await self.limiter.reserve("k", 600)            # t = 0
        self.clock.now += 30_000
        await self.limiter.reserve("k", 400)            # t = 30 s, window now full
        self.clock.now += 29_999                        # t = 59.999 s: first row still counts
        self.assertEqual((await self.denied("k", 1)).retry_after, 1)
        self.clock.now += 1                             # t = 60 s: first row (ts <= cutoff) expires
        await self.limiter.reserve("k", 600)
        err = await self.denied("k", 1)                 # the 400 from t=30 s is still inside
        self.assertEqual(err.status, 429)

    async def test_retry_after_is_when_enough_tokens_expire(self):
        for _ in range(3):
            await self.limiter.reserve("k", 300)
            self.clock.now += 10_000                    # rows at t = 0, 10, 20 s; now t = 30 s
        self.clock.now -= 5_000                         # t = 25 s, used 900
        err = await self.denied("k", 500)               # must free 400: needs the first two rows
        self.assertEqual(err.retry_after, 45)           # second row expires at t = 70 s

    async def test_keys_are_independent(self):
        await self.limiter.reserve("a", 1000)
        await self.limiter.reserve("b", 1000)
        self.assertEqual((await self.denied("a", 1)).status, 429)

    async def test_request_that_can_never_fit_is_413_not_429(self):
        err = await self.denied("k", self.LIMIT + 1)
        self.assertEqual((err.status, err.code), (413, "request_too_large"))


class SettleReleaseTests(LimiterCase):
    async def test_settle_replaces_the_estimate_with_actual_usage(self):
        r = await self.limiter.reserve("k", 900)
        self.assertEqual(await self.limiter.settle(r, 100), 900)      # 900 tokens left
        await self.limiter.reserve("k", 900)                           # fits again

    async def test_settle_can_exceed_the_estimate(self):
        r = await self.limiter.reserve("k", 100)
        self.assertEqual(await self.limiter.settle(r, 1500), 0)       # remaining floors at 0
        self.assertEqual((await self.denied("k", 1)).status, 429)

    async def test_release_returns_the_tokens(self):
        r = await self.limiter.reserve("k", 1000)
        await self.limiter.release(r)
        await self.limiter.reserve("k", 1000)

    async def test_settle_after_the_row_aged_out_still_counts_the_usage(self):
        r = await self.limiter.reserve("k", 100)
        self.clock.now += 61_000
        await self.limiter.purge()
        self.assertEqual(await self.store.row_count(), 0)
        self.assertEqual(await self.limiter.settle(r, 250), 750)       # recorded at completion time


class EvictionTests(LimiterCase):
    async def test_expired_rows_are_deleted_when_the_key_is_checked(self):
        await self.limiter.reserve("k", 10)
        await self.limiter.reserve("k", 10)
        self.clock.now += 61_000
        await self.limiter.reserve("k", 10)
        self.assertEqual(await self.store.row_count(), 1)

    async def test_purge_evicts_idle_keys(self):
        await self.limiter.reserve("idle", 10)
        self.clock.now += 30_000
        await self.limiter.reserve("busy", 10)
        self.clock.now += 31_000                        # idle is 61 s old, busy is 31 s old
        self.assertEqual(await self.limiter.purge(), 1)
        self.assertEqual(await self.store.row_count(), 1)
        self.assertEqual(await self.store.usage(hash_key("busy"), self.WINDOW, self.clock.now), 10)

    async def test_expired_rows_never_count_even_if_not_purged(self):
        await self.store.reserve(hash_key("k"), 1000, self.LIMIT, self.WINDOW, self.clock.now)
        self.clock.now += 60_000                        # row is exactly one window old
        self.assertEqual(await self.store.usage(hash_key("k"), self.WINDOW, self.clock.now), 0)


class DiskTests(LimiterCase):
    async def test_state_survives_a_restart(self):
        await self.limiter.reserve("k", 900)
        await self.store.close()
        self.store = UsageStore(self.path)
        await self.store.open()
        self.limiter = TokenRateLimiter(self.store, self.LIMIT, self.WINDOW, self.clock)
        self.assertEqual((await self.denied("k", 200)).status, 429)

    async def test_raw_api_key_is_never_written_to_disk(self):
        await self.limiter.reserve("sk-super-secret-key", 10)
        await self.store.close()
        blobs = b""
        for name in os.listdir(self.tmp.name):
            with open(os.path.join(self.tmp.name, name), "rb") as f:
                blobs += f.read()
        self.assertNotIn(b"sk-super-secret-key", blobs)
        self.assertIn(hash_key("sk-super-secret-key").encode(), blobs)
        self.store = UsageStore(self.path)               # so asyncTearDown can close it
        await self.store.open()


class ConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_reservations_never_exceed_the_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageStore(os.path.join(tmp, "u.sqlite3"))
            await store.open()
            limiter = TokenRateLimiter(store, 50_000, 60_000)   # real clock
            results = await asyncio.gather(*[limiter.reserve("k", 1000) for _ in range(100)],
                                           return_exceptions=True)
            admitted = [r for r in results if not isinstance(r, Exception)]
            rejected = [r for r in results if isinstance(r, GatewayError) and r.status == 429]
            self.assertEqual((len(admitted), len(rejected)), (50, 50))
            self.assertEqual(await store.usage(hash_key("k"), 60_000, limiter.clock()), 50_000)
            await store.close()


class EstimateTests(unittest.TestCase):
    def test_estimate(self):
        payload = {"messages": [{"role": "user", "content": "x" * 40}], "max_tokens": 100}
        self.assertEqual(estimate_tokens(payload, 1024), 14 + 100)   # (40 + 16 chars) / 4, plus the cap

    def test_default_cap_when_none_or_invalid(self):
        for cap in (None, 0, -5, "100", True):
            payload = {"messages": [{"role": "user", "content": ""}], "max_tokens": cap}
            self.assertEqual(estimate_tokens(payload, 777), 4 + 777, cap)

    def test_multipart_content_and_prefers_max_completion_tokens(self):
        payload = {"messages": [{"role": "user", "content": [{"type": "text", "text": "x" * 48}, {"type": "image"}]}],
                   "max_completion_tokens": 50, "max_tokens": 999}
        self.assertEqual(estimate_tokens(payload, 1024), 16 + 50)


if __name__ == "__main__":
    unittest.main()
