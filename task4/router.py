"""Model router: try providers in priority order, fail over, and stop hammering a dead one.

Fail over when a provider
  * answers 429 (rate limited),
  * does not finish within its deadline (3000 ms for the primary, covering connect,
    headers AND body), or
  * is otherwise unusable: connection error, 5xx, 401/403/404 (our config is wrong,
    which is not the caller's fault), or an unparseable 200.
Do NOT fail over on 400/422: the request itself is invalid, a second provider would
reject it too, and the caller should hear about it.

Circuit breaker (one per provider, in memory):

    closed --N consecutive failures--> open --cooldown elapsed--> half-open (ONE trial request)
       ^                                 ^                              |
       +--------- trial succeeded -------+---- trial failed (cooldown doubles, capped)

While a provider's circuit is open it is skipped, so a dead primary costs nothing instead of its
3 s timeout on every request. Rules that make failback work:

  * If every provider was skipped, or every attempted one failed, the router makes a last-resort
    pass over the skipped providers in priority order, ignoring their circuits. So when the
    secondary dies too, the original primary is retried immediately, and a success closes its
    circuit (failback) without waiting for the cooldown.
  * The half-open state admits exactly one trial; concurrent requests keep using the others.
  * A 429 with ``Retry-After`` uses that value as the cooldown.
  * A 400/422 never counts against a provider: it answered correctly.
  * An optional active probe (``health_url``) can shorten the wait: a healthy probe lets the next
    real request be the trial straight away. It cannot close a circuit by itself, because a
    health endpoint cannot see throttling.

Every failure is reduced to a short ``kind`` string. Raw upstream bodies, headers and
exception messages are never kept, so they cannot leak into an error payload.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Callable

from aiohttp import ClientError, ClientSession

from errors import GatewayError, upstream_failure, upstream_unavailable

log = logging.getLogger("router")

PRIMARY_TIMEOUT_S = 3.0      # the requirement: fail over after 3000 ms
SECONDARY_TIMEOUT_S = 10.0   # nothing left to fail over to, so allow more time
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class ProviderConfig:
    name: str                     # "primary" / "secondary": the only label clients ever see
    base_url: str
    api_key: str = ""
    timeout_s: float = PRIMARY_TIMEOUT_S
    model: str = ""               # if set, replaces the request's model for this provider
    health_url: str = ""          # optional cheap liveness endpoint for the active probe


@dataclass(frozen=True)
class BreakerConfig:
    failure_threshold: int = 3        # consecutive failures that open a circuit
    base_cooldown_s: float = 30.0     # first open period
    max_cooldown_s: float = 300.0     # cap when trials keep failing (cooldown doubles each time)
    probe_interval_s: float = 10.0    # active probe period (only providers with a health_url)
    probe_timeout_s: float = 2.0


@dataclass(frozen=True)
class RouteResult:
    body: dict[str, Any]
    provider: str
    total_tokens: int | None      # as reported by the provider, if it did


@dataclass(frozen=True)
class _Failure:
    kind: str                     # rate_limited | timeout | bad_request | connection | server_error | bad_response | rejected
    failover: bool
    retry_after: int | None = None


# -------------------------------------------------------------------- circuit breaker

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"
ALLOW, TRIAL, FORCED, DENY = "allow", "trial", "forced", "deny"  # what allow() tells the caller


class CircuitBreaker:
    """Single-threaded (event loop) state machine. No awaits inside, so every method is atomic."""

    def __init__(self, name: str, cfg: BreakerConfig, clock: Callable[[], float] = time.monotonic) -> None:
        self.name, self._cfg, self._clock = name, cfg, clock
        self.state = CLOSED
        self.failures = 0
        self.cooldown_s = cfg.base_cooldown_s
        self.open_until = 0.0
        self._hinted = False  # the current cooldown came from a provider's Retry-After

    def allow(self) -> str:
        """ALLOW (closed), TRIAL (you are the one half-open trial) or DENY (skip this provider)."""
        if self.state == CLOSED:
            return ALLOW
        if self.state == OPEN and self._clock() >= self.open_until:
            self.state = HALF_OPEN
            log.info("circuit provider=%s state=half_open (trial)", self.name)
            return TRIAL
        return DENY  # cooling down, or the single trial is already in flight

    def record_success(self) -> None:
        if self.state != CLOSED:
            log.info("circuit provider=%s state=closed (recovered)", self.name)
        self.state, self.failures, self.cooldown_s, self._hinted = CLOSED, 0, self._cfg.base_cooldown_s, False

    def record_failure(self, retry_after: int | None = None) -> None:
        if self.state == OPEN:
            return  # a forced last-resort attempt failed: the existing cooldown stands
        if self.state == HALF_OPEN:  # the trial failed: back off further
            self._open(retry_after or self.cooldown_s * 2, retry_after is not None)
            return
        self.failures += 1
        if self.failures >= self._cfg.failure_threshold:
            self._open(retry_after or self.cooldown_s, retry_after is not None)

    def abandon(self, permit: str) -> None:
        """The attempt was cancelled before it finished: do not leave a trial stuck in flight."""
        if permit == TRIAL and self.state == HALF_OPEN:
            self.state, self.open_until = OPEN, self._clock()  # next request may be the trial

    def expedite(self) -> None:
        """A health probe succeeded: let the next real request be the trial now (not for Retry-After holds)."""
        if self.state == OPEN and not self._hinted:
            self.open_until = self._clock()

    def _open(self, cooldown_s: float, hinted: bool) -> None:
        self.cooldown_s = min(cooldown_s, self._cfg.max_cooldown_s)
        self.open_until = self._clock() + self.cooldown_s
        self.state, self._hinted = OPEN, hinted
        log.warning("circuit provider=%s state=open cooldown=%.0fs", self.name, self.cooldown_s)


# -------------------------------------------------------------------- helpers

def _retry_after(value: str | None) -> int | None:
    try:
        return min(max(int(value or ""), 1), 120)
    except ValueError:
        return None


def _total_tokens(body: dict[str, Any]) -> int | None:
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    if total is None:
        parts = [usage.get("prompt_tokens"), usage.get("completion_tokens")]
        total = sum(p for p in parts if isinstance(p, int)) if any(isinstance(p, int) for p in parts) else None
    return total if isinstance(total, int) and not isinstance(total, bool) and total >= 0 else None


# -------------------------------------------------------------------- router

class ModelRouter:
    def __init__(self, session: ClientSession, providers: list[ProviderConfig],
                 breaker: BreakerConfig | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._session = session
        self._providers = providers
        self._cfg = breaker or BreakerConfig()
        self._clock = clock
        self._forced_busy: set[str] = set()  # providers with a last-resort attempt in flight
        self.breakers = {p.name: CircuitBreaker(p.name, self._cfg, clock) for p in providers}
        self._probes: list[asyncio.Task[None]] = []

    # -- lifecycle of the optional active probes ------------------------------------

    def start(self) -> None:
        self._probes = [asyncio.create_task(self._probe_loop(p)) for p in self._providers if p.health_url]

    async def stop(self) -> None:
        for task in self._probes:
            task.cancel()
        for task in self._probes:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._probes = []

    async def _probe_loop(self, provider: ProviderConfig) -> None:
        breaker = self.breakers[provider.name]
        while True:
            await asyncio.sleep(self._cfg.probe_interval_s)
            if breaker.state != CLOSED and await self._probe(provider):  # only probe a provider we are avoiding
                breaker.expedite()

    async def _probe(self, provider: ProviderConfig) -> bool:
        try:
            async with asyncio.timeout(self._cfg.probe_timeout_s):
                async with self._session.get(provider.health_url, allow_redirects=False) as resp:
                    return resp.status == 200
        except (TimeoutError, ClientError, OSError):
            return False

    # -- routing --------------------------------------------------------------------

    async def complete(self, payload: dict[str, Any], request_id: str = "-") -> RouteResult:
        """Return the first successful completion or raise a sanitized ``GatewayError``."""
        last: _Failure | None = None
        skipped: list[ProviderConfig] = []
        for provider in self._providers:
            permit = self.breakers[provider.name].allow()
            if permit == DENY:
                log.info("request=%s provider=%s skipped (circuit %s)", request_id, provider.name,
                         self.breakers[provider.name].state)
                skipped.append(provider)
                continue
            outcome = await self._guarded(provider, permit, payload, request_id)
            if isinstance(outcome, RouteResult):
                return outcome
            last = outcome
            if not outcome.failover:
                raise upstream_failure(outcome.kind)
        # Nothing succeeded. Last resort: the providers we skipped, in priority order (primary
        # first), ignoring their circuits. This is what makes failback work.
        # DoS guard: at most ONE forced attempt per provider at a time. Without it, an outage with every
        # circuit open sends every incoming request to every provider, so the breaker would multiply
        # load onto already-failing providers. Requests that find the slot taken do not queue behind it.
        for provider in skipped:
            if provider.name in self._forced_busy:
                continue
            self._forced_busy.add(provider.name)
            try:
                outcome = await self._guarded(provider, FORCED, payload, request_id)
            finally:
                self._forced_busy.discard(provider.name)
            if isinstance(outcome, RouteResult):
                return outcome
            last = outcome
            if not outcome.failover:
                break
        if last is None:  # every provider is circuit-open and already being probed by another request
            wait = min((b.open_until - self._clock() for b in self.breakers.values()), default=1)
            raise upstream_unavailable(max(1, min(math.ceil(wait), 30)))
        raise upstream_failure(last.kind, last.retry_after)

    async def _guarded(self, provider: ProviderConfig, permit: str, payload: dict[str, Any],
                       request_id: str) -> RouteResult | _Failure:
        """One attempt, with circuit bookkeeping."""
        breaker = self.breakers[provider.name]
        started = time.perf_counter()
        try:
            outcome = await self._attempt(provider, payload)
        except BaseException:  # cancelled (e.g. the client went away): never leave a trial in flight
            breaker.abandon(permit)
            raise
        ms = (time.perf_counter() - started) * 1000
        if isinstance(outcome, RouteResult):
            log.info("request=%s provider=%s outcome=ok permit=%s ms=%.0f", request_id, provider.name, permit, ms)
            breaker.record_success()
            return outcome
        log.warning("request=%s provider=%s outcome=%s permit=%s failover=%s ms=%.0f",
                    request_id, provider.name, outcome.kind, permit, outcome.failover, ms)
        if outcome.failover:
            breaker.record_failure(outcome.retry_after)
        else:
            breaker.record_success()  # a 400 means the provider answered correctly; it is healthy
        return outcome

    async def _attempt(self, provider: ProviderConfig, payload: dict[str, Any]) -> RouteResult | _Failure:
        body = {**payload, "model": provider.model} if provider.model else payload
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if provider.api_key:
            headers["Authorization"] = f"Bearer {provider.api_key}"  # never the caller's own key
        try:
            # One deadline around the whole exchange. If it fires first, the request is cancelled
            # (the connection is closed by the context managers) and any late reply is discarded.
            async with asyncio.timeout(provider.timeout_s):
                async with self._session.post(provider.base_url + "/v1/chat/completions",
                                              data=json.dumps(body), headers=headers,
                                              allow_redirects=False) as resp:
                    status = resp.status
                    retry_after = _retry_after(resp.headers.get("Retry-After"))
                    if (resp.content_length or 0) > MAX_RESPONSE_BYTES:
                        return _Failure("bad_response", True)
                    raw = await resp.read()
        except TimeoutError:  # asyncio.timeout; aiohttp's own timeouts subclass it too
            return _Failure("timeout", True)
        except (ClientError, OSError) as exc:
            log.warning("provider=%s transport error: %s", provider.name, type(exc).__name__)
            return _Failure("connection", True)

        if status == 200:
            try:
                parsed = json.loads(raw)
            except ValueError:
                return _Failure("bad_response", True)
            if not isinstance(parsed, dict) or len(raw) > MAX_RESPONSE_BYTES:
                return _Failure("bad_response", True)
            return RouteResult(parsed, provider.name, _total_tokens(parsed))
        if status == 429:
            return _Failure("rate_limited", True, retry_after)
        if status in (400, 422):
            return _Failure("bad_request", False)
        if status >= 500:
            return _Failure("server_error", True)
        return _Failure("rejected", True)  # 401/403/404/...: our credentials or URL, not the caller
