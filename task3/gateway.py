"""LLM Gateway: streams a provider's chat completion back to the client while
redacting emails, SSNs and credit-card numbers in flight.

    client --POST /v1/chat/completions--> [gateway] --> LLM provider
    client <--SSE, redacted, token by token-- [gateway] <--SSE-- provider

Speaks the OpenAI-compatible chat completions wire format (``choices[].delta.content``
over server-sent events), which most providers and proxies offer. Supporting a
different wire format means replacing ``StreamState._redact_choice`` and the SSE
reader in ``_stream``; the redaction engine in ``redactor.py`` is format-agnostic.

Design points:
  * Each upstream line is parsed and each event forwarded as soon as it is
    redacted. Nothing waits for the end of the response.
  * Memory per request is O(1): one line of input, at most ``MAX_HOLD`` held-back
    characters per choice. ``await resp.write`` applies back-pressure to a slow client.
  * Fail closed: an event we cannot parse is never forwarded raw.

Run:  uv run python gateway.py --upstream http://127.0.0.1:9100
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from redactor import StreamRedactor, redact_text

log = logging.getLogger("llm-gateway")

UPSTREAM = web.AppKey("upstream", str)
API_KEY = web.AppKey("api_key", str)
SESSION = web.AppKey("session", ClientSession)

META_KEYS = ("id", "object", "created", "model")
DONE = b"data: [DONE]\n\n"


class UpstreamProtocolError(Exception):
    """The provider sent something we cannot safely interpret."""


def _sse_json(obj: Any) -> bytes:
    return b"data: " + json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode() + b"\n\n"


def _error_obj(message: str, code: str) -> dict[str, Any]:
    """Error shape used for everything the gateway itself raises. ``type`` says who
    raised it (providers use their own types); ``code`` says why, for programs."""
    return {"error": {"message": message, "type": "gateway_error", "code": code}}


def _error_json(status: int, message: str, code: str) -> web.Response:
    return web.json_response(_error_obj(message, code), status=status)


def _redact_strings(value: Any) -> Any:
    """Redact every string inside a parsed JSON value (used for provider error payloads)."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_redact_strings(v) for v in value]
    if isinstance(value, dict):
        return {k: _redact_strings(v) for k, v in value.items()}
    return value


class StreamState:
    """Per-request redaction state: one StreamRedactor per choice index."""

    def __init__(self) -> None:
        self.redactors: dict[int, StreamRedactor] = {}
        self.meta: dict[str, Any] = {}
        self.done = False

    @property
    def redactions(self) -> int:
        return sum(r.redactions for r in self.redactors.values())

    def process(self, data: str) -> bytes:
        """Turn one upstream SSE ``data`` payload into the bytes to send the client."""
        if data == "[DONE]":
            self.done = True
            return self.flush_all() + DONE
        try:
            obj = json.loads(data)
        except ValueError as exc:
            raise UpstreamProtocolError("upstream event is not valid JSON") from exc
        if not isinstance(obj, dict):
            raise UpstreamProtocolError("upstream event is not a JSON object")
        if "error" in obj:  # the provider reported an error mid-stream: forward it, redacted
            obj["error"] = _redact_strings(obj["error"])
        for key in META_KEYS:
            if key in obj:
                self.meta[key] = obj[key]
        choices = obj.get("choices")
        if choices is not None:
            if not isinstance(choices, list):
                raise UpstreamProtocolError("'choices' is not a list")
            for choice in choices:
                self._redact_choice(choice)
        return _sse_json(obj)

    def _redact_choice(self, choice: Any) -> None:
        if not isinstance(choice, dict):
            raise UpstreamProtocolError("choice is not an object")
        idx = choice.get("index", 0)
        redactor = self.redactors.setdefault(idx if isinstance(idx, int) else 0, StreamRedactor())
        delta = choice.get("delta")
        if delta is None:
            delta = {}
        elif not isinstance(delta, dict):
            raise UpstreamProtocolError("'delta' is not an object")
        content = delta.get("content")
        text = redactor.feed(content) if isinstance(content, str) else ""
        if choice.get("finish_reason") is not None:
            text += redactor.flush()  # release held text before the finish marker goes out
        if isinstance(content, str) or text:
            delta["content"] = text
            choice["delta"] = delta

    def flush_all(self) -> bytes:
        """Emit held-back text as synthetic chunks (used at [DONE] or an early end)."""
        out = b""
        for idx, redactor in self.redactors.items():
            text = redactor.flush()
            if text:
                chunk = {**self.meta, "choices": [{"index": idx, "delta": {"content": text},
                                                   "finish_reason": None}]}
                out += _sse_json(chunk)
        return out


async def chat_completions(request: web.Request) -> web.StreamResponse:
    app = request.app
    body = await request.read()
    try:
        payload = json.loads(body)
    except ValueError:
        return _error_json(400, "request body is not valid JSON", "invalid_request")
    if not isinstance(payload, dict):
        return _error_json(400, "request body must be a JSON object", "invalid_request")

    headers = {"Content-Type": "application/json"}
    headers["Accept"] = "text/event-stream" if payload.get("stream") else "application/json"
    if app[API_KEY]:
        headers["Authorization"] = f"Bearer {app[API_KEY]}"

    started = time.perf_counter()
    try:
        async with app[SESSION].post(app[UPSTREAM] + "/v1/chat/completions",
                                     data=body, headers=headers) as upstream:
            ctype = upstream.headers.get("Content-Type", "")
            if upstream.status == 200 and ctype.startswith("text/event-stream"):
                return await _stream(request, upstream, started)
            return await _buffered(upstream)
    except (ClientError, TimeoutError) as exc:
        log.warning("upstream failure: %s", exc)
        return _error_json(502, "upstream provider unavailable", "upstream_unavailable")


async def _buffered(upstream) -> web.Response:
    """Non-streaming reply (or an upstream error): small, so read it whole."""
    raw = await upstream.read()
    ctype = upstream.headers.get("Content-Type", "application/json")
    if upstream.status != 200:
        # The provider's own error: keep its status and shape, but redact every string in it
        # (error messages often echo the offending input).
        keep = {k: upstream.headers[k] for k in ("Retry-After",) if k in upstream.headers}
        try:
            return web.json_response(_redact_strings(json.loads(raw)), status=upstream.status, headers=keep)
        except (ValueError, RecursionError):
            return web.Response(status=upstream.status, headers=keep, content_type="text/plain",
                                text=redact_text(raw.decode("utf-8", "replace")))
    try:
        obj = json.loads(raw)
        for choice in obj.get("choices") or []:
            message = choice.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                message["content"] = redact_text(message["content"])
    except (ValueError, AttributeError, TypeError, RecursionError):
        return _error_json(502, "upstream returned an unparseable response", "invalid_upstream_response")
    return web.json_response(obj)


async def _stream(request: web.Request, upstream, started: float) -> web.StreamResponse:
    resp = web.StreamResponse(status=200, headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",  # tell reverse proxies not to buffer the stream
    })
    await resp.prepare(request)
    state = StreamState()
    data_lines: list[str] = []
    events = 0
    try:
        async for raw in upstream.content:  # one SSE line at a time, as it arrives
            line = raw.decode("utf-8").rstrip("\r\n")
            if line.startswith("data:"):
                data_lines.append(line[6:] if line.startswith("data: ") else line[5:])
            elif line == "":
                if data_lines:  # blank line terminates an event
                    await resp.write(state.process("\n".join(data_lines)))
                    data_lines.clear()
                    events += 1
            elif line.startswith(":"):
                await resp.write(b":\n\n")  # pass keep-alive comments through
            # other SSE fields (event:, id:, retry:) are not used by this wire format
        if data_lines:  # upstream closed without the final blank line
            await resp.write(state.process("\n".join(data_lines)))
            events += 1
        if not state.done:  # upstream ended early: still release held-back text
            await resp.write(state.flush_all())
    except ConnectionResetError:
        log.info("client disconnected after %d events", events)
        return resp
    except (UpstreamProtocolError, ValueError, RecursionError, ClientError, TimeoutError) as exc:
        # ValueError covers undecodable bytes and over-long lines. Held-back text is
        # dropped and nothing unparsed is forwarded: fail closed.
        log.warning("stream aborted: %s", exc)
        code = "invalid_upstream_event" if isinstance(exc, UpstreamProtocolError) else "upstream_stream_error"
        try:
            await resp.write(_sse_json(_error_obj("stream interrupted by gateway", code)))
        except ConnectionResetError:
            return resp
    try:
        await resp.write_eof()
    except ConnectionResetError:
        pass
    log.info("stream done events=%d redactions=%d duration_ms=%.0f",
             events, state.redactions, (time.perf_counter() - started) * 1000)
    return resp


def create_app(upstream: str, api_key: str = "") -> web.Application:
    app = web.Application(client_max_size=10 * 1024 * 1024)
    app[UPSTREAM] = upstream.rstrip("/")
    app[API_KEY] = api_key

    async def session_ctx(app: web.Application):
        # No total timeout (generation can be long); bound connect and idle reads.
        timeout = ClientTimeout(total=None, connect=5, sock_read=120)
        async with ClientSession(timeout=timeout, auto_decompress=True) as session:
            app[SESSION] = session
            yield

    app.cleanup_ctx.append(session_ctx)
    app.router.add_post("/v1/chat/completions", chat_completions)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM Gateway with streaming PII redaction")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--upstream", default=os.environ.get("UPSTREAM_BASE_URL", "http://127.0.0.1:9100"),
                        help="provider base URL (env UPSTREAM_BASE_URL)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    web.run_app(create_app(args.upstream, os.environ.get("UPSTREAM_API_KEY", "")),
                host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
