"""End-to-end tests: real gateway + real mock provider over real sockets.

Run:  uv run python -m unittest -v test_gateway
"""

import json
import time
import unittest

import aiohttp
from aiohttp.test_utils import TestServer

import gateway
import mock_provider
from redactor import redact_text

TEXT = ("Reach alice.smith@example.com or bob@mail.example.co.uk. SSN 123-45-6789, "
        "card 4111 1111 1111 1111 and 5500-0000-0000-0004. Thanks for asking!")
SECRETS = ("alice.smith@example.com", "bob@mail.example.co.uk", "123-45-6789",
           "4111 1111 1111 1111", "5500-0000-0000-0004")


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.provider = TestServer(mock_provider.create_app())
        await self.provider.start_server()
        self.gw = TestServer(gateway.create_app(str(self.provider.make_url("")).rstrip("/")))
        await self.gw.start_server()
        self.http = aiohttp.ClientSession()
        self.url = str(self.gw.make_url("/v1/chat/completions"))

    async def asyncTearDown(self):
        await self.http.close()
        await self.gw.close()
        await self.provider.close()

    async def stream(self, content=TEXT, **extra):
        """Return [(seconds_since_request, event)] where event is a dict or '[DONE]'."""
        payload = {"model": "m", "stream": True, "messages": [{"role": "user", "content": content}], **extra}
        events, t0 = [], time.perf_counter()
        async with self.http.post(self.url, json=payload) as r:
            self.assertEqual(r.status, 200)
            self.assertTrue(r.headers["Content-Type"].startswith("text/event-stream"))
            async for raw in r.content:
                line = raw.decode().strip()
                if line.startswith("data:"):
                    data = line[5:].strip()
                    events.append((time.perf_counter() - t0, data if data == "[DONE]" else json.loads(data)))
        return events

    @staticmethod
    def text_of(events):
        return "".join(e["choices"][0]["delta"].get("content", "")
                       for _, e in events if e != "[DONE]" and e.get("choices"))

    async def test_secrets_never_reach_client_for_any_chunk_size(self):
        expected = redact_text(TEXT)
        for size in (1, 2, 3, 4, 5, 7, 11, 64, None):
            extra = {"mock_chunk_size": size} if size else {}
            events = await self.stream(**extra)
            out = self.text_of(events)
            self.assertEqual(out, expected, f"chunk size {size}")
            for secret in SECRETS:
                self.assertNotIn(secret, json.dumps([e for _, e in events]))

    async def test_additional_identifiers_across_chunk_sizes(self):
        text = ("Pay GB82 WEST 1234 5698 7654 32 today. Passport number: X1234567, "
                "NI AB 12 34 56 C, SIN 046 454 286, ITIN 912-70-1234. Thanks!")
        expected = redact_text(text)
        self.assertEqual(expected.count("[REDACTED]"), 5)
        self.assertIn("Passport number: [REDACTED],", expected)  # label kept, number replaced
        for size in (1, 2, 3, 5, 8, 13, None):
            extra = {"mock_chunk_size": size} if size else {}
            events = await self.stream(text, **extra)
            self.assertEqual(self.text_of(events), expected, f"chunk size {size}")

    async def test_sse_structure_is_preserved(self):
        events = await self.stream(mock_chunk_size=5)
        self.assertEqual(events[-1][1], "[DONE]")
        objs = [e for _, e in events[:-1]]
        self.assertEqual(objs[0]["choices"][0]["delta"].get("role"), "assistant")
        self.assertEqual(objs[-1]["choices"][0]["finish_reason"], "stop")
        self.assertTrue(all(o["id"] == "chatcmpl-mock" for o in objs))
        # held-back text was released before the finish marker, not after it
        self.assertEqual(sum(1 for o in objs if o["choices"][0]["finish_reason"]), 1)

    async def test_stream_is_incremental_not_buffered(self):
        # 40 chunks, 50 ms apart: ~2 s total. A buffering gateway would show
        # its first content only after the end.
        text = "word " * 40
        events = await self.stream(text, mock_chunk_size=5, mock_delay_ms=50)
        total = events[-1][0]
        first_content = next(t for t, e in events if e != "[DONE]" and e["choices"][0]["delta"].get("content"))
        self.assertGreater(total, 1.5)
        self.assertLess(first_content, 0.3)
        self.assertEqual(self.text_of(events), text)

    async def test_non_streaming_reply_is_redacted(self):
        payload = {"model": "m", "messages": [{"role": "user", "content": TEXT}]}
        async with self.http.post(self.url, json=payload) as r:
            body = await r.json()
        self.assertEqual(body["choices"][0]["message"]["content"], redact_text(TEXT))

    async def test_provider_error_keeps_status_and_is_redacted(self):
        payload = {"model": "m", "stream": True, "mock_status": 429,
                   "messages": [{"role": "user", "content": "mail bob@example.com"}]}
        async with self.http.post(self.url, json=payload) as r:
            self.assertEqual(r.status, 429)
            self.assertEqual(r.headers["Retry-After"], "7")
            body = await r.json()
        self.assertEqual(body["error"]["type"], "provider_error")  # provider's own shape, not ours
        self.assertEqual(body["error"]["message"], "rejected input: mail [REDACTED]")

    async def test_provider_error_event_mid_stream_is_forwarded_redacted(self):
        events = await self.stream("mail bob@example.com", mock_error_event=True)
        err = events[-1][1]
        self.assertEqual(err["error"]["type"], "provider_error")
        self.assertEqual(err["error"]["message"], "provider failed on: mail [REDACTED]")
        self.assertNotIn("[DONE]", [e for _, e in events])

    async def test_garbage_event_fails_closed_with_code(self):
        events = await self.stream("my email is bob@example.com", mock_garbage=True)
        err = events[-1][1]["error"]
        self.assertEqual((err["type"], err["code"]), ("gateway_error", "invalid_upstream_event"))
        self.assertNotIn("[DONE]", [e for _, e in events])  # abnormal end: no [DONE]
        self.assertNotIn("not json", json.dumps([e for _, e in events]))
        self.assertNotIn("bob@example.com", json.dumps([e for _, e in events]))

    async def test_bad_request_body(self):
        async with self.http.post(self.url, data="{nope", headers={"Content-Type": "application/json"}) as r:
            self.assertEqual(r.status, 400)
            err = (await r.json())["error"]
        self.assertEqual((err["type"], err["code"]), ("gateway_error", "invalid_request"))

    async def test_provider_down_gives_502_with_code(self):
        await self.provider.close()
        payload = {"model": "m", "stream": True, "messages": [{"role": "user", "content": "x"}]}
        async with self.http.post(self.url, json=payload) as r:
            self.assertEqual(r.status, 502)
            err = (await r.json())["error"]
        self.assertEqual((err["type"], err["code"]), ("gateway_error", "upstream_unavailable"))


class StreamStateTests(unittest.TestCase):
    def test_multiple_choices_are_independent(self):
        s = gateway.StreamState()

        def ev(idx, content, finish=None):
            return json.dumps({"id": "x", "choices": [{"index": idx, "delta": {"content": content},
                                                       "finish_reason": finish}]})
        out = b"".join([s.process(ev(0, "a@b.c")), s.process(ev(1, "123-45-")),
                        s.process(ev(0, "om", "stop")), s.process(ev(1, "6789", "stop"))])
        self.assertNotIn(b"a@b.com", out)
        self.assertNotIn(b"123-45-6789", out)
        self.assertEqual(out.count(b"[REDACTED]"), 2)

    def test_non_object_event_is_a_protocol_error(self):
        for bad in ("[1]", '"x"', "nope"):
            with self.assertRaises(gateway.UpstreamProtocolError):
                gateway.StreamState().process(bad)


if __name__ == "__main__":
    unittest.main()
