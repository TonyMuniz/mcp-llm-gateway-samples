# Task 3: Implement an LLM Gateway Streaming Guardrail (PII Redaction)

An HTTP proxy (Python 3.11+, developed and tested on 3.15.2; aiohttp; managed with uv) that forwards chat-completion requests to an
LLM provider and streams the answer back while replacing **direct identifiers** (emails, SSNs and
ITINs, credit cards, IBANs, UK National Insurance numbers, Canadian SINs, passport numbers) with
`[REDACTED]`, in flight, without buffering the response.

```
client --POST /v1/chat/completions--> gateway :8080 --> provider
client <--SSE, redacted, token by token-- gateway <--SSE-- provider
```

It speaks the OpenAI-compatible wire format (`choices[].delta.content` over server-sent events).
`mock_provider.py` stands in for a real provider; point `--upstream` (or `UPSTREAM_BASE_URL`, plus
`UPSTREAM_API_KEY` if needed) at a real one.

> **Scope note: this is a demonstration, not a complete PII solution.** PII sanitization can be a
> monumental effort by itself. This gateway accounts for several types of direct identifier using
> patterns and checksums, but it does not cover names, addresses, dates of birth, free-text
> descriptions of a person, identifiers for most countries, or deliberately obfuscated data, and no
> pattern-based approach is ever complete or free of false positives and misses. A more comprehensive
> solution (for example a dedicated PII detection service with NER models, per-jurisdiction
> coverage, auditing, and legal review of what must be protected) should be weighed before
> implementing this outside of a non-production environment. See "Limits and trade-offs" and
> "Going further" below.

## Run

```bash
uv sync
uv run python mock_provider.py --port 9100 &
uv run python gateway.py --port 8080 --upstream http://127.0.0.1:9100 &

curl -N localhost:8080/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model":"m","stream":true,
  "messages":[{"role":"user","content":"Mail bob@example.com, SSN 123-45-6789, card 4111 1111 1111 1111 ok"}]}'
```

The mock echoes the last user message, so you see exactly what the gateway does to it.
Tests: `uv run python -m unittest -v test_redactor test_gateway` (34 tests). Benchmark: `uv run python bench.py`.

## What is detected

| Identifier | Matches | Extra check |
|---|---|---|
| Email | ASCII addresses, local part up to 64 characters | none |
| Credit card | 13-19 digits, single space or hyphen separators | none (errs toward over-redaction) |
| US SSN, US ITIN | formatted `ddd-dd-dddd` (an ITIN `9xx-xx-xxxx` has the same shape) | none |
| Canadian SIN | `ddd-ddd-ddd` or `ddd ddd ddd` | Luhn checksum |
| IBAN | compact or in groups of four, upper case | ISO 13616 mod-97 checksum |
| UK National Insurance number | `AB 12 34 56 C` or `AB123456C`, upper case | HMRC invalid-prefix rules |
| Passport number | 6-9 letters/digits **with at least one digit, right after the word "passport"** (optionally "number", "no.", "#", ":" or "is") | context keyword |

For the passport only the number is replaced, so `My passport number: X1234567` becomes
`My passport number: [REDACTED]`. A passport number is just 6-9 alphanumerics and is indistinguishable
from any other token without context, which is why it needs the keyword.

## How the streaming redaction works (`redactor.py`)

The problem: a secret can be split across chunks (`bob@exa` + `mple.com`), so you cannot redact each
chunk on its own. Buffering the whole response fixes that but destroys latency and memory; a fixed
look-behind window delays every character.

The approach, per chunk:

1. `buf = held_back + new_text`
2. Choose a **cut**. Text before it is final; text after it is held back. The cut starts at the
   beginning of the trailing text that could still grow into a match (for most identifiers, the
   current word or digit run), and is then moved earlier if any *complete* match straddles it.
3. Redact the final part with one combined regex pass and send it now.

Two rules guard the cut because they cover different failures: the tail rule handles identifiers that
are still *incomplete* at the end of the buffer; the straddle rule handles *complete* matches that run
across the cut (for example a card number whose last group runs into the next word), so a
hand-written tail rule can never leave a half-redacted secret.

Consequences:
- **Exact.** Streamed output is identical to redacting the whole text at once, for any chunking.
  `test_redactor.py` checks this for every chunk size, every two-way split, and 3,000 random texts
  with random chunking (a one-off run of 100,000 random cases found no mismatch). A separate test
  feeds every prefix of every example identifier and checks that none of it is released early.
- **Low latency.** Text ending in whitespace or punctuation is released immediately; otherwise only
  the current word is held (any word could be an email's local part). The added delay is about
  **one token interval**. Two costs of the newer detectors: a two-capital-letter word followed by a
  space (such as "US ") is held for one more token because it could begin a National Insurance number,
  and the word "passport" holds the next ~30 characters until it is clear whether a number follows.
- **Bounded memory.** Held-back text never exceeds `MAX_PENDING` (956 characters) per choice, however
  long the response is.
- **Fast.** One precompiled regex over the buffer. Quantifiers are bounded, so there is no
  catastrophic backtracking.

At `finish_reason` or `[DONE]` the held-back text is flushed (redacted) *before* the finish marker
goes out, so nothing is lost and the event order stays valid.

## Adding an identifier

Append a `Detector` to `DETECTORS` in `redactor.py`:

```python
Detector("sin", r"\b\d{3}[- ]\d{3}[- ]\d{3}\b", validator=luhn_ok)
```

- `pattern`: regex for a complete identifier (bounded quantifiers, no capturing groups).
- `tail`: regex (ending in `\Z`) matching every *prefix* of a match, so the streaming code knows to
  hold it. Optional when the generic word and digit-run tails already cover the prefixes.
- `validator`: optional checksum or rule function, to cut false positives.
- `value_group`: optional named group, to replace only part of the match (keep a label).

Then add an example to `EXAMPLES` in `test_redactor.py`. The prefix test will fail if the `tail` is
wrong, and the fuzz test will fail if streaming and one-shot output ever differ.

## Errors

| When | What the client sees |
|---|---|
| Bad request body | HTTP 400, `{"error": {"message", "type": "gateway_error", "code": "invalid_request"}}` |
| Provider unreachable | HTTP 502, `code: "upstream_unavailable"` |
| Provider sends an unparseable 200 reply | HTTP 502, `code: "invalid_upstream_response"` |
| Provider returns its own error (429, 500, ...) | Same status and `Retry-After`, the provider's own JSON shape, **every string redacted** |
| Provider sends an `{"error": ...}` event mid-stream | Forwarded as is, strings redacted |
| Gateway aborts a stream (HTTP 200 already sent) | One SSE event `{"error": {..., "type": "gateway_error", "code": "invalid_upstream_event" or "upstream_stream_error"}}`, then the stream ends **without `[DONE]`** |

`type == "gateway_error"` means the gateway raised it; `code` says why. A stream that ends without
`[DONE]` ended abnormally. Held-back text is dropped on abort, never sent unredacted.

## Measured (Orange Pi 5B, arm64, mock provider on the same box)

| | direct | via gateway |
|---|---|---|
| TTFT, 20 ms between tokens | 1.2 ms | 23.4 ms (about one token interval) |
| Total time for that stream | 327 ms | 331 ms |
| Peak RSS as the response grows 16x (8k to 128k events) | n/a | 41.7 MB, flat |
| Throughput (unpaced, CPU-bound on JSON and regex) | n/a | about 22k events/s |

Real providers emit tens of tokens per second per stream, so throughput is not the constraint.

## Design decisions

- **Per-event pipeline, back-pressure included.** Upstream lines are read as they arrive, each event
  is redacted and written with `await resp.write(...)`. A slow client slows the read from upstream
  instead of growing a queue. If the client disconnects, the upstream request is closed.
- **Fail closed.** An upstream event that is not valid JSON is never forwarded raw. The gateway sends
  an error event, drops any held-back text, and ends the stream.
- **Errors are structured, never mixed into the text.** Model text only ever appears in
  `choices[].delta.content`; an error has a top-level `error` and no `choices`.
- **Multiple choices (`n > 1`)** each get their own redactor, keyed by `choice.index`.
- **Non-streaming requests** are redacted too (`message.content`), since the full body is small.
- **Provider-agnostic core.** `redactor.py` knows nothing about JSON or HTTP. Supporting another wire
  format (for example Anthropic's `content_block_delta`) means changing `StreamState._redact_choice`
  and the SSE field handling in `_stream`.
- **No PII library dependency.** Checksums (Luhn, IBAN mod-97) are a few lines each, which keeps the
  dependency surface, and the CVE surface, to aiohttp alone.

## Limits and trade-offs (deliberate, and where to change them)

- **Cards: any 13-19 digit run** (single space/hyphen separators) is redacted, with no Luhn check. This
  errs toward over-redaction, so a 13-digit millisecond timestamp is also redacted.
- **SSNs/ITINs: only the formatted `ddd-dd-dddd`.** Bare 9-digit numbers are too common to redact safely.
- **Canadian SINs and IBANs are checksum-validated**, so a mistyped one (or a random look-alike) is
  left alone. That is a miss for a typo'd real identifier.
- **Passport numbers need the word "passport" nearby.** A bare passport number, a letters-only one, or
  one introduced in another language or after a longer gap is not caught.
- **Emails:** ASCII addresses only. Internationalized addresses are not matched.
- **Coverage is a handful of identifiers.** There are hundreds of national ID and tax ID formats; this
  covers a few. Names, addresses, dates of birth, phone numbers and free-text descriptions of a
  person are not covered.
- **Obfuscated secrets** ("bob at example dot com", digits spelled out) are out of scope; that needs a
  model, not a regex.
- **Only `delta.content` is scanned.** Tool-call arguments, `reasoning_content` and other fields pass through.
- **A single run longer than 256 characters** with no break (for example a base64 blob) streams through
  with only its last 256 characters held; a secret embedded in such a blob is not guaranteed to be caught.
- **Prompts (request side) are not redacted**, and the gateway does no client authentication. Put it
  behind the auth you already use.

## Going further

For anything beyond a demonstration, weigh a purpose-built approach first. Common options in Python:
Microsoft Presidio (pattern recognizers plus NER models for names and locations), `python-stdnum`
(validators for many countries' national and tax IDs), or a managed service (Google Cloud DLP, AWS
Comprehend, Azure AI Language). NER-based detection needs sentence-level context, so in a stream it
trades time to first token for coverage; it is best run as a second, slower layer or on the
non-latency-sensitive path. This gateway's `Detector` registry is the place to plug in additional
fast, high-precision patterns alongside such a layer.
