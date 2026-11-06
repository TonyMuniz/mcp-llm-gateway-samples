"""Mock LLM provider with a configurable personality, for demos and tests.

  --status 429        answer every request with this HTTP status
  --delay-ms 5000     wait this long before answering (use > 3000 to trigger a timeout failover)
  --leak              make error bodies look like a leaky stack trace (to prove the gateway hides it)
  --tokens 120        usage.total_tokens to report

Tests change ``app[BEHAVIOR]`` directly and read ``app[CALLS]``.

Run:  uv run python mock_provider.py --port 9101 --name primary --status 429
"""

from __future__ import annotations

import argparse
import asyncio

from aiohttp import web

BEHAVIOR = web.AppKey("behavior", dict)
CALLS = web.AppKey("calls", list)

LEAKY_BODY = (
    'Traceback (most recent call last):\n'
    '  File "/srv/inference/server.py", line 88, in handle\n'
    'RuntimeError: CUDA out of memory on gpu-node-17 (db=postgres://admin:hunter2@10.1.2.3/models)\n'
)


def create_app(name: str = "mock", status: int = 200, delay_ms: int = 0, leak: bool = False,
               tokens: int = 15) -> web.Application:
    app = web.Application()
    app[BEHAVIOR] = {"status": status, "delay_ms": delay_ms, "leak": leak, "tokens": tokens}
    app[CALLS] = []

    async def chat(request: web.Request) -> web.Response:
        payload = await request.json()
        b = app[BEHAVIOR]
        app[CALLS].append({"payload": payload, "authorization": request.headers.get("Authorization")})
        if b["delay_ms"]:
            await asyncio.sleep(b["delay_ms"] / 1000)
        if b["status"] != 200:
            headers = {"Retry-After": "2"} if b["status"] == 429 else {}
            if b["leak"]:
                return web.Response(text=LEAKY_BODY, status=b["status"], headers=headers)
            return web.json_response({"error": {"message": f"{name} says no"}}, status=b["status"], headers=headers)
        return web.json_response({
            "id": f"chatcmpl-{name}", "object": "chat.completion", "model": payload.get("model"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": f"Hello from {name}"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": max(b["tokens"] - 10, 0),
                      "total_tokens": b["tokens"]},
        })

    async def health(request: web.Request) -> web.Response:  # healthy exactly when it would answer 200
        return web.Response(status=200 if app[BEHAVIOR]["status"] == 200 else 503)

    app.router.add_post("/v1/chat/completions", chat)
    app.router.add_get("/health", health)
    return app


def main() -> None:
    p = argparse.ArgumentParser(description="Mock LLM provider")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9101)
    p.add_argument("--name", default="mock")
    p.add_argument("--status", type=int, default=200)
    p.add_argument("--delay-ms", type=int, default=0)
    p.add_argument("--leak", action="store_true")
    p.add_argument("--tokens", type=int, default=15)
    a = p.parse_args()
    web.run_app(create_app(a.name, a.status, a.delay_ms, a.leak, a.tokens), host=a.host, port=a.port, print=None)


if __name__ == "__main__":
    main()
