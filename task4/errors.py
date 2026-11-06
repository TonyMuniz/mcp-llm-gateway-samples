"""Standardized gateway errors.

Every error the gateway returns has exactly this shape, whatever went wrong:

    {"error": {"message": "...", "type": "gateway_error", "code": "...", "request_id": "..."}}

Messages are fixed strings chosen by the gateway. Nothing from an upstream
response (body, headers, status text) or from a Python exception is ever copied
into a payload; the details go to the server log, keyed by ``request_id`` so an
operator can still find them.
"""

from __future__ import annotations

from typing import Any


class GatewayError(Exception):
    """An error that is safe to show to the client as is."""

    def __init__(self, status: int, code: str, message: str, retry_after: int | None = None) -> None:
        super().__init__(code)  # str(exc) is the code, never upstream text
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after  # whole seconds, for the Retry-After header


def error_payload(code: str, message: str, request_id: str) -> dict[str, Any]:
    return {"error": {"message": message, "type": "gateway_error", "code": code, "request_id": request_id}}


# Fixed client-facing messages (one place, so none can accidentally embed upstream text).
INVALID_API_KEY = GatewayError(401, "invalid_api_key", "Missing or invalid API key.")
UNSUPPORTED_STREAMING = GatewayError(400, "streaming_not_supported", "Streaming responses are not supported by this endpoint.")


def invalid_request(message: str = "The request body is not a valid completion request.") -> GatewayError:
    return GatewayError(400, "invalid_request", message)


def request_too_large() -> GatewayError:
    return GatewayError(413, "request_too_large", "The request is larger than this gateway allows.")


def rate_limit_exceeded(retry_after_s: int) -> GatewayError:
    return GatewayError(429, "rate_limit_exceeded", "Token rate limit exceeded for this API key. Retry later.",
                        retry_after=retry_after_s)


# Final outcomes when no provider could serve the request, keyed by the kind of the last failure.
_UPSTREAM_OUTCOMES = {
    "rate_limited": (429, "upstream_rate_limited", "The model providers are rate limiting requests. Retry shortly."),
    "timeout": (504, "upstream_timeout", "The model providers did not respond in time."),
    "bad_request": (400, "invalid_request", "The request was rejected as invalid."),
}
_DEFAULT_OUTCOME = (502, "upstream_error", "The model providers returned an unexpected error.")


def upstream_failure(kind: str, retry_after: int | None = None) -> GatewayError:
    status, code, message = _UPSTREAM_OUTCOMES.get(kind, _DEFAULT_OUTCOME)
    return GatewayError(status, code, message, retry_after=retry_after if status == 429 else None)


def overloaded() -> GatewayError:
    return GatewayError(503, "overloaded", "The gateway is at capacity. Retry shortly.", retry_after=1)


def upstream_unavailable(retry_after_s: int) -> GatewayError:
    return GatewayError(503, "upstream_unavailable", "The model providers are unavailable. Retry shortly.",
                        retry_after=retry_after_s)


INTERNAL_ERROR = GatewayError(500, "internal_error", "The gateway encountered an internal error.")
NOT_FOUND = GatewayError(404, "not_found", "No such endpoint.")
METHOD_NOT_ALLOWED = GatewayError(405, "method_not_allowed", "Method not allowed for this endpoint.")
