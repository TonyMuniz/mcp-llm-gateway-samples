"""Mock LLM provider (OpenAI-compatible chat completions) for the gateway demo.

It "generates" by echoing the last user message, which lets tests choose exactly
what text, and what chunk boundaries, the gateway sees. Test-only request fields:

  mock_chunk_size  split the text into fixed-size pieces (default: word-sized tokens)
  mock_delay_ms    pause between chunks
  mock_repeat      repeat the text N times (large-stream benchmarks)
  mock_status      reply with this HTTP error status instead
  mock_garbage     inject a non-JSON event after the first chunk
  mock_error_event send a provider error event (echoing the input) after the first chunk, then stop

Run:  uv run python mock_provider.py --port 9100
"""

from __future__ import annotations

import argparse
import asyncio
import re

from aiohttp import web


def tokenize(text: str, size: int | None) -> list[str]:
    if size:
        return [text[i:i + size] for i in range(0, len(text), size)]
    return re.findall(r"\s*\S+|\s+", text)  # " word" style tokens, like real tokenizers


def _chunk(model: str, delta: dict, finish: str | None = None) -> dict:
    return {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "created": 0, "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


async def chat(request: web.Request) -> web.StreamResponse:
    payload = await request.json()
    model = payload.get("model", "mock-1")
    text = payload["messages"][-1]["content"]
    if payload.get("mock_status"):  # like real providers, the error message echoes the input
        return web.json_response({"error": {"message": f"rejected input: {text}", "type": "provider_error"}},
                                 status=int(payload["mock_status"]), headers={"Retry-After": "7"})
    repeat = int(payload.get("mock_repeat", 1))
    if repeat > 1:
        text = (text + " ") * repeat

    if not payload.get("stream"):
        return web.json_response({
            "id": "chatcmpl-mock", "object": "chat.completion", "created": 0, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}]})

    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
    await resp.prepare(request)
    delay = float(payload.get("mock_delay_ms", 0)) / 1000

    async def send(obj: dict) -> None:
        import json
        await resp.write(b"data: " + json.dumps(obj).encode() + b"\n\n")

    await send(_chunk(model, {"role": "assistant", "content": ""}))
    if payload.get("mock_garbage"):
        await resp.write(b"data: this is not json\n\n")
    if payload.get("mock_error_event"):
        await send({"error": {"message": f"provider failed on: {text}", "type": "provider_error"}})
        return resp
    for token in tokenize(text, payload.get("mock_chunk_size")):
        await send(_chunk(model, {"content": token}))
        if delay:
            await asyncio.sleep(delay)
    await send(_chunk(model, {}, "stop"))
    await resp.write(b"data: [DONE]\n\n")
    return resp


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock LLM provider")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9100)
    args = parser.parse_args()
    web.run_app(create_app(), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
