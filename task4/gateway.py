"""LLM Gateway: authentication, token rate limiting, and primary/secondary routing.

    client --POST /v1/chat/completions (Bearer tenant key)--> gateway
        1. authenticate the key
        2. reserve estimated tokens in SQLite (429 if the key is over its limit)
        3. route: primary, then secondary on 429 / 3000 ms timeout / failure
        4. settle the reservation to the provider's real usage (or release it on failure)

Non-streaming completions only. Every error is the standardized payload from errors.py.

Run:  uv run python gateway.py --primary http://127.0.0.1:9101 --secondary http://127.0.0.1:9102
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from aiohttp import ClientSession, ClientTimeout, web

import errors
from errors import GatewayError, error_payload
from ratelimit import (DEFAULT_LIMIT, DEFAULT_WINDOW_MS, TokenRateLimiter, UsageStore, estimate_tokens,
                       wall_clock_ms)
from router import PRIMARY_TIMEOUT_S, SECONDARY_TIMEOUT_S, BreakerConfig, ModelRouter, ProviderConfig

log = logging.getLogger("gateway")

DEMO_KEYS = ("demo-key-1", "demo-key-2")  # replaced by env GATEWAY_API_KEYS="k1,k2"
PURGE_INTERVAL_S = 30
BEARER_TOKEN = re.compile(r"[A-Za-z0-9\-._~+/]+=*")  # RFC 6750 b64token (same rule as Task 2)


@dataclass
class Config:
    providers: list[ProviderConfig]
    db_path: str = "usage.sqlite3"
    api_keys: tuple[str, ...] = DEMO_KEYS
    tokens_per_window: int = DEFAULT_LIMIT
    window_ms: int = DEFAULT_WINDOW_MS
    default_max_tokens: int = 1024  # assumed completion size when the request sets no max_tokens
    clock: Callable[[], int] = field(default=wall_clock_ms)
    max_inflight: int = 200  # requests being served at once; beyond this, fail fast with 503
    breaker: BreakerConfig = field(default_factory=BreakerConfig)
    breaker_clock: Callable[[], float] = field(default=time.monotonic)


LIMITER = web.AppKey("limiter", TokenRateLimiter)
ROUTER = web.AppKey("router", ModelRouter)
CONFIG = web.AppKey("config", Config)
INFLIGHT = web.AppKey("inflight", list)  # [count]; the event loop is single threaded
REQUEST_ID = web.RequestKey("request_id", str)


def _error_response(err: GatewayError, request_id: str) -> web.Response:
    headers = {"Retry-After": str(err.retry_after)} if err.retry_after else {}
    return web.json_response(error_payload(err.code, err.message, request_id), status=err.status, headers=headers)


@web.middleware
async def error_middleware(request: web.Request, handler):
    """Single choke point: whatever happens, the client gets the standard payload and nothing else."""
    request_id = uuid.uuid4().hex[:16]
    request[REQUEST_ID] = request_id
    try:
        response = await handler(request)
    except GatewayError as exc:
        response = _error_response(exc, request_id)
    except web.HTTPException as exc:  # aiohttp's own 404/405/413/... pages
        known = {404: errors.NOT_FOUND, 405: errors.METHOD_NOT_ALLOWED, 413: errors.request_too_large()}
        response = _error_response(known.get(exc.status, errors.invalid_request("The request could not be processed.")),
                                   request_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        # Full detail goes to the log only, tagged with the id the client receives.
        log.exception("unhandled error request=%s", request_id)
        response = _error_response(errors.INTERNAL_ERROR, request_id)
    response.headers["X-Request-Id"] = request_id
    return response


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def _authenticate(request: web.Request, allowed: tuple[str, ...]) -> str:
    scheme, _, token = request.headers.get("Authorization", "").strip().partition(" ")
    token = token.strip()
    # RFC 6750 token characters only: undecodable header bytes used to reach .encode() and give a 500.
    if scheme.lower() == "bearer" and BEARER_TOKEN.fullmatch(token):
        match = False
        for key in allowed:  # compare against all keys, in constant time per key
            match |= hmac.compare_digest(key.encode(), token.encode())
        if match:
            return token
    raise errors.INVALID_API_KEY


def _parse_payload(raw: bytes) -> dict[str, Any]:
    try:
        # NaN/Infinity are not JSON (and would be re-sent to the provider as invalid JSON);
        # RecursionError is very deep nesting, which used to surface as a 500.
        payload = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise errors.invalid_request() from None
    if not isinstance(payload, dict) or not isinstance(payload.get("model"), str) \
            or not isinstance(payload.get("messages"), list) or not payload["messages"]:
        raise errors.invalid_request("Request must include a 'model' and a non-empty 'messages' list.")
    if payload.get("stream"):
        raise errors.UNSUPPORTED_STREAMING
    return payload


async def chat_completions(request: web.Request) -> web.Response:
    app = request.app
    cfg: Config = app[CONFIG]
    api_key = _authenticate(request, cfg.api_keys)  # before reading the body: strangers cost nothing
    inflight = app[INFLIGHT]
    if inflight[0] >= cfg.max_inflight:  # bulkhead: bound sockets, memory and upstream fan-out
        raise errors.overloaded()
    inflight[0] += 1
    try:
        return await _serve(request, api_key)
    finally:
        inflight[0] -= 1


async def _serve(request: web.Request, api_key: str) -> web.Response:
    app = request.app
    cfg: Config = app[CONFIG]
    payload = _parse_payload(await request.read())

    limiter, request_id = app[LIMITER], request[REQUEST_ID]
    reservation = await limiter.reserve(api_key, estimate_tokens(payload, cfg.default_max_tokens))
    settled = False
    try:
        result = await app[ROUTER].complete(payload, request_id)
        actual = result.total_tokens if result.total_tokens is not None else reservation.tokens
        remaining = await limiter.settle(reservation, actual)
        settled = True
        return web.json_response(result.body, headers={
            "X-Routed-To": result.provider,
            "X-RateLimit-Limit-Tokens": str(limiter.limit),
            "X-RateLimit-Remaining-Tokens": str(remaining),
        })
    finally:
        if not settled:  # failed, timed out, or the client went away: give the tokens back
            await asyncio.shield(limiter.release(reservation))


def create_app(config: Config) -> web.Application:
    app = web.Application(middlewares=[error_middleware], client_max_size=1024 * 1024)
    app[CONFIG] = config
    app[INFLIGHT] = [0]

    async def lifecycle(app: web.Application):
        store = UsageStore(config.db_path)
        await store.open()
        limiter = TokenRateLimiter(store, config.tokens_per_window, config.window_ms, config.clock)
        # No total timeout on the shared session: each provider call has its own asyncio deadline.
        async with ClientSession(timeout=ClientTimeout(total=None, connect=5)) as session:
            app[LIMITER] = limiter
            router = ModelRouter(session, config.providers, config.breaker, config.breaker_clock)
            app[ROUTER] = router
            router.start()  # optional active health probes

            async def purge_loop() -> None:  # evict expired rows of idle keys
                while True:
                    await asyncio.sleep(PURGE_INTERVAL_S)
                    try:
                        removed = await limiter.purge()
                        if removed:
                            log.info("purged %d expired usage rows", removed)
                    except Exception:
                        log.exception("usage purge failed")

            task = asyncio.create_task(purge_loop())
            yield
            await router.stop()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await store.close()

    app.cleanup_ctx.append(lifecycle)
    app.router.add_post("/v1/chat/completions", chat_completions)
    return app


def main() -> None:
    p = argparse.ArgumentParser(description="LLM gateway with token rate limiting and model fallback")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--primary", required=True, help="primary provider base URL (env PRIMARY_API_KEY for its key)")
    p.add_argument("--secondary", required=True, help="backup provider base URL (env SECONDARY_API_KEY)")
    p.add_argument("--secondary-model", default="", help="model name to send to the backup provider")
    p.add_argument("--primary-timeout", type=float, default=PRIMARY_TIMEOUT_S)
    p.add_argument("--secondary-timeout", type=float, default=SECONDARY_TIMEOUT_S)
    p.add_argument("--tokens-per-minute", type=int, default=DEFAULT_LIMIT)
    p.add_argument("--default-max-tokens", type=int, default=1024)
    p.add_argument("--max-inflight", type=int, default=200, help="concurrent requests before returning 503")
    p.add_argument("--db", default="usage.sqlite3", help="SQLite file for rate-limiter state")
    p.add_argument("--breaker-failures", type=int, default=BreakerConfig.failure_threshold,
                   help="consecutive failures that open a provider's circuit")
    p.add_argument("--breaker-cooldown", type=float, default=BreakerConfig.base_cooldown_s,
                   help="seconds a circuit stays open before one trial request")
    p.add_argument("--breaker-max-cooldown", type=float, default=BreakerConfig.max_cooldown_s)
    p.add_argument("--primary-health-url", default="", help="optional liveness URL probed while the primary's circuit is open")
    p.add_argument("--secondary-health-url", default="")
    p.add_argument("--probe-interval", type=float, default=BreakerConfig.probe_interval_s)
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    env_keys = os.environ.get("GATEWAY_API_KEYS")
    keys = tuple(k.strip() for k in env_keys.split(",") if k.strip()) if env_keys else DEMO_KEYS
    if bad := [k for k in keys if not BEARER_TOKEN.fullmatch(k)]:
        raise SystemExit(f"GATEWAY_API_KEYS: {len(bad)} key(s) use characters a Bearer token cannot carry "
                         "(allowed: A-Z a-z 0-9 - . _ ~ + / and trailing =)")
    if not env_keys:
        log.warning("using built-in DEMO api keys; set GATEWAY_API_KEYS for anything real")
    config = Config(
        providers=[
            ProviderConfig("primary", args.primary.rstrip("/"), os.environ.get("PRIMARY_API_KEY", ""),
                           args.primary_timeout, health_url=args.primary_health_url),
            ProviderConfig("secondary", args.secondary.rstrip("/"), os.environ.get("SECONDARY_API_KEY", ""),
                           args.secondary_timeout, args.secondary_model, args.secondary_health_url),
        ],
        db_path=args.db, api_keys=keys, tokens_per_window=args.tokens_per_minute,
        default_max_tokens=args.default_max_tokens, max_inflight=args.max_inflight,
        breaker=BreakerConfig(args.breaker_failures, args.breaker_cooldown, args.breaker_max_cooldown,
                              args.probe_interval),
    )
    web.run_app(create_app(config), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
