# Task 4: Build a Rate-Limiting & Model Fallback Router for LLM Gateways

A gateway for chat-completion requests (Python 3.11+, developed and tested on 3.15.2; aiohttp; stdlib
`sqlite3`; managed with uv) that does three things in front of two model providers:

1. **Rate limits each tenant API key by tokens**: 50,000 tokens per sliding minute by default, state in
   an on-disk SQLite file.
2. **Fails over** from the primary provider to a backup when the primary returns 429 or does not finish
   within 3000 ms (plus other failures, see [below](#failover-routerpy)), and **stops paying that wait on every request**: a
   per-provider circuit breaker skips a dead provider, retries it periodically, and fails back to it.
3. **Never leaks internals**: every error is one standardized payload with a fixed message and a
   `request_id`; raw upstream bodies, stack traces and exception text never reach the client.

```
client --Bearer tenant key--> gateway --> primary provider
                                 |  (429 / 3 s timeout / failure)
                                 +-----> secondary provider
                    SQLite: per-key token usage (sliding window)
```

## Run

```bash
uv sync                                   # creates .venv from pyproject.toml / uv.lock
uv run python mock_provider.py --port 9101 --name primary --delay-ms 5000 &   # a primary that hangs
uv run python mock_provider.py --port 9102 --name secondary &
uv run python gateway.py --primary http://127.0.0.1:9101 --secondary http://127.0.0.1:9102 &

curl -si localhost:8080/v1/chat/completions -H 'Authorization: Bearer demo-key-1' -d '{
  "model":"m","max_tokens":100,"messages":[{"role":"user","content":"hi"}]}'
# ~3 s later: HTTP 200, X-Routed-To: secondary, X-RateLimit-Remaining-Tokens: 49985
```

Demo keys are `demo-key-1` and `demo-key-2`; set `GATEWAY_API_KEYS="k1,k2"` for anything real. Options:
`--tokens-per-minute`, `--primary-timeout` (default 3.0), `--secondary-timeout` (10.0), `--secondary-model`,
`--default-max-tokens`, `--db`, `--breaker-failures` (3), `--breaker-cooldown` (30 s),
`--breaker-max-cooldown` (300 s), `--primary-health-url` / `--secondary-health-url` (optional probes),
`--probe-interval`, `--max-inflight` (200); provider keys via `PRIMARY_API_KEY` / `SECONDARY_API_KEY`.
Tests: `uv run python -m unittest -v test_ratelimit test_gateway test_breaker` (64 tests).

## Assignment requirements

| Requirement | How it is met |
|---|---|
| Routing module for completion requests | `router.py` (`ModelRouter`), wired into `gateway.py` at `POST /v1/chat/completions` |
| Token-aware sliding window, 50,000 tokens/minute per tenant key | `ratelimit.py`: sliding window log with reserve, settle and release (see [below](#token-aware-sliding-window-rate-limiter-ratelimitpy)) |
| Fail over to a secondary on 429 or after 3000 ms | One `asyncio.timeout(3.0)` around the whole primary exchange; also fails over on connection errors and 5xx |
| Standardized error payload, no stack traces or internals | `errors.py` plus one middleware; tested with providers that return stack traces and credentials |
| On-disk SQLite | WAL-mode SQLite file (`--db`), state survives restarts (tested) |

## Token-aware sliding-window rate limiter (`ratelimit.py`)

**Sliding window log.** Each admitted request writes a row `(key_hash, ts_ms, tokens)`. A key's usage is
the sum of `tokens` over rows with `ts_ms > now - 60 s`. This is exact: no burst of 2x the limit at a
fixed-window boundary.

**Tokens are unknown before the model runs**, so each request is reserve, run, settle:

| Step | What happens |
|---|---|
| `reserve` | Estimate = prompt characters / 4 (one token per non-ASCII character) + the request's `max_tokens` (or a default cap). One transaction evicts the key's expired rows, sums the window, and either inserts the estimate or rejects with 429 and a `Retry-After`. |
| `settle` | After success, the row is rewritten to the provider's real `usage.total_tokens` (the estimate if the provider reports none). Over-estimates are refunded immediately. |
| `release` | If no provider produced an answer (or the handler is cancelled), the row is deleted: failed requests cost nothing. |

`Retry-After` is exact: the soonest moment enough old tokens expire for this request to fit. A request
bigger than the whole limit can never succeed, so it gets 413 instead of a misleading 429.

**Eviction.** Expired rows are deleted whenever their key is checked, and a background task purges idle
keys every 30 s. Correctness never depends on the purge: the sum only counts rows inside the window.

**Concurrency.** `reserve` is a single `BEGIN IMMEDIATE` transaction (evict, sum, compare, insert), so
two requests can never both read "49,000 used" and both squeeze in. SQLite work runs on one dedicated
thread, so the event loop never blocks and in-process access is serialized; `BEGIN IMMEDIATE` also makes
it safe for several gateway processes to share the file. A test fires 100 simultaneous 1,000-token
requests at a 50,000 limit and checks that exactly 50 are admitted.

**On disk.** WAL mode, so state survives restarts (tested). API keys are stored only as SHA-256 hashes
(tested: the raw key never appears in the database or WAL file).

## Failover (`router.py`)

The primary gets one deadline of **3000 ms** (`asyncio.timeout`) covering connect, headers and body, so a
server that sends headers and then stalls still fails over on time. When the deadline fires, the call is
cancelled and its connection closed; if the primary answers late, that reply is discarded and its tokens
are never counted (tested). The caller's key is never forwarded; each provider gets its own.

| Primary outcome | Action |
|---|---|
| 200 | Return it |
| 429 | Fail over |
| No answer in 3000 ms | Fail over |
| Connection error, 5xx, unparseable 200, 401/403/404 | Fail over (not the caller's fault) |
| 400 / 422 | **No failover**: the request is invalid, a second provider would reject it too |

The backup gets a 10 s deadline (`--secondary-timeout`), since there is nothing left to fail over to.
`--secondary-model` swaps the model name for the backup provider. Success responses carry
`X-Routed-To: primary|secondary` (labels only, never URLs).

## Circuit breaker and failback (`router.py`)

Without memory, a dead primary costs its full 3 s timeout on **every** request. Each provider therefore
has an in-memory circuit breaker:

```
closed --3 consecutive failures--> open --30 s cooldown--> half-open (ONE trial request)
   ^                                 ^                            |
   +---------- trial succeeded -------+--- trial failed (cooldown doubles: 60, 120, 240, 300 max)
```

- **Open** circuits are skipped, so the request goes straight to the next provider (measured below).
- **Half-open** admits exactly one real request as the trial; concurrent requests keep using the other
  provider. A trial that is cancelled (for example the client disconnects) releases the slot instead of
  wedging the circuit.
- **A 429 with `Retry-After`** uses that value (capped at 300 s) as the cooldown instead of 30 s.
- **A 400/422 never counts against a provider**: it answered correctly.
- **Failback.** If every provider is skipped, or every attempted one fails, the router makes a
  last-resort pass over the skipped providers in priority order, ignoring their circuits. So when the
  secondary dies too, the original primary is retried at once, and a success closes its circuit. The
  full scenario (primary down, secondary carries traffic, secondary down, primary retried and recovered)
  is a test.
- **Optional active probe.** Give a provider a health URL and a background task checks it every 10 s, but
  only while that provider's circuit is open. A healthy answer lets the *next real request* be the trial
  immediately instead of waiting out the cooldown. A probe can never close a circuit by itself, because a
  health endpoint cannot see throttling, and it does not override a provider's `Retry-After`.

State is per process and in memory; it resets on restart (the first requests re-learn that the primary is
down).

## Denial-of-service posture

What is in place: authentication happens before the body is read; bodies are capped at 1 MB and provider
replies at 8 MB; every upstream call has a hard deadline; each key is limited by tokens; reservations are
released on failure; health probes run only while a circuit is open, one GET per interval.

Where the circuit breaker could have multiplied traffic, and what was done:

- **Last-resort pass (fixed).** When every circuit is open, the failback pass would have sent *every*
  request to *every* provider, which is the opposite of what a breaker is for. Now at most **one forced
  attempt per provider is in flight at a time**; other requests do not queue behind it, they fail fast
  with 503 `upstream_unavailable` and a `Retry-After` (time until the next circuit may retry, 1 to 30 s).
  Upstream load during a total outage is therefore at most one extra call per provider at a time. Tested
  with 20 concurrent requests against two open circuits.
- **Global concurrency cap (added).** At most `--max-inflight` (200) requests are served at once; the rest
  get an immediate 503 `overloaded` with `Retry-After: 1`, bounding sockets, memory and provider fan-out.
  Checked after authentication, so unauthenticated traffic neither uses nor is blocked by capacity.
- **Failover doubles upstream calls** while the primary is closed but failing (before it trips), and a
  throttled primary shifts its whole load onto the secondary. Breaker thresholds bound the duration, not
  the magnitude. A secondary sized smaller than the primary can be overrun; the cap above limits how hard.

Known remaining exposure, not solved here:

- **No per-IP or per-request-count limit.** Limits are by tokens per key. A valid key sending many tiny
  requests is bounded by the token estimate (a minimum of about 1,100 tokens each at default settings,
  roughly 45 requests per minute per key), but a flood of *rejected* requests (429s) still costs a SQLite
  write transaction each. Put a connection or IP limiter (reverse proxy, WAF) in front for real exposure.
- **A key holder can fill the in-flight cap**, starving other tenants for as long as their requests run.
  Fair per-tenant concurrency limits would fix that.
- **Breaker state is per process.** Several gateways each probe a failing provider independently.
- **Slow clients and TLS** are left to the server in front.

## Errors (`errors.py`, `gateway.py`)

Every error, whatever its source, is:

```json
{"error": {"message": "...", "type": "gateway_error", "code": "...", "request_id": "..."}}
```

| Situation | Status | `code` |
|---|---|---|
| Missing or wrong API key, or a key outside the RFC 6750 token alphabet | 401 | `invalid_api_key` |
| Malformed or incomplete request (including NaN/Infinity, undecodable bytes, nesting too deep), or `stream: true` | 400 | `invalid_request`, `streaming_not_supported` |
| Body over 1 MB, or a request larger than the whole token limit | 413 | `request_too_large` |
| Key over its token limit (`Retry-After` set) | 429 | `rate_limit_exceeded` |
| Unknown path / wrong method | 404 / 405 | `not_found`, `method_not_allowed` |
| Gateway at its concurrency cap | 503 | `overloaded` |
| Every provider circuit-open and already being retried | 503 | `upstream_unavailable` |
| Both providers rate limited (`Retry-After` from the provider, capped) | 429 | `upstream_rate_limited` |
| Last provider timed out | 504 | `upstream_timeout` |
| Provider rejected the request as invalid | 400 | `invalid_request` |
| Anything else upstream | 502 | `upstream_error` |
| Unexpected bug in the gateway | 500 | `internal_error` |

How leaks are prevented: messages are fixed strings; an upstream failure is reduced to a short kind
(`timeout`, `rate_limited`, ...) the moment it is seen and the raw body is dropped; one middleware turns
*everything* (including aiohttp's own 404/405/413 pages and unexpected exceptions) into this payload.
The detail goes to the server log, tagged with the same `request_id` that is in the response and in the
`X-Request-Id` header. A test makes both providers return a stack trace containing file paths, a
database URL with a password and a GPU hostname, then checks none of it appears.

## Measured (Orange Pi 5B, arm64)

- Live run with the default settings and a primary that hangs: the request fails over at **3.013 s**.
- Live run with a hanging primary and the breaker threshold set to 2: requests 1 and 2 take 3.01 s each
  (they open the circuit); requests 3 and 4 take **8 ms** because the primary is skipped.
- Limiter throughput, ad hoc: about 1,400 full reserve-and-settle cycles per second sequentially, and
  about 7,700 concurrent reserves per second (each is a durable SQLite transaction). LLM requests
  are far slower than that, so the limiter is not the bottleneck.

## Design decisions

- **Estimate then settle**, because real usage is only known afterwards. The estimate is conservative
  (it includes the full `max_tokens`), so a client that requests `max_tokens: 4000` and uses 50 briefly
  holds 4,000 until settle. Requests without `max_tokens` assume `--default-max-tokens` (1,024).
- **Non-streaming only.** Once a streamed response has started, failing over mid-way is not possible, so
  `stream: true` is rejected with a clear error rather than half-supported.
- **API keys are validated against the RFC 6750 token alphabet** before any comparison (same rule as
  [Task 2](../task2/README.md)), and `GATEWAY_API_KEYS` entries outside it abort startup.
- **The request body is re-serialized** (`json.dumps`) before it goes to a provider, so the provider
  always receives UTF-8 JSON, whatever encoding the client used.

## Known issues and limits

- **Encoding.** Request bodies may be UTF-8 (with or without BOM), UTF-16 or UTF-32 (`json.loads`
  detects them); a `charset` parameter is ignored, so Latin-1 bytes are a 400. NaN and Infinity are
  rejected (they are not JSON, and a provider would reject the re-serialized body). Header bytes that
  are not UTF-8 in `Authorization` are a 401; before the review they caused a 500 with a traceback in
  the log on every request.
- **The token estimate is a heuristic.** About 4 ASCII characters per token, one token per non-ASCII
  character (accented, CJK and emoji text tokenizes far more finely than English). Only message text is
  counted, not tool definitions or images. It only affects admission: settle replaces it with the
  provider's reported usage. Billing-grade counting needs the provider's tokenizer.
- **Breaker state is per process and in memory.** With several gateway processes each learns separately.
  While a circuit is open, one request per cooldown (the trial) can still pay the primary's timeout, unless
  an active probe has already shown it healthy. If all providers are down, a request that wins a
  last-resort slot waits out the timeouts (3 s + 10 s); concurrent ones fail fast (see the [DoS section](#denial-of-service-posture)).
- **Wall-clock timestamps** are used (not monotonic) so usage stays meaningful across restarts; a large
  clock jump would skew windows.
- **A late completion** (a request that outlives the window) is recorded at completion time.
- **One tenant per key, one limit for all.** Per-tenant limits and a key registry are not included; keys
  come from `GATEWAY_API_KEYS`.
- **A provider's response is trusted to be JSON** of at most 8 MB; larger or malformed replies count as
  failures. The cap is enforced while reading, so it also holds for chunked replies without a
  `Content-Length` (before the review those were read whole first).
- **The provider's success body is passed through as is.** Only errors are rewritten; the content of a
  200 reply is the provider's (see [Task 3](../task3/README.md) for redacting it).

## Dependencies and security

One direct dependency, `aiohttp>=3.14.5` (the floor is the locked, audited version; the old `>=3.9`
floor allowed releases with published CVEs if someone installed without the lock file), plus the
standard library's `sqlite3`. `uv audit` on 2026-11-07: no known vulnerabilities in the 10 locked
packages. Python 3.15 is pinned in `.python-version`; the tests also pass on 3.11 and 3.13.
