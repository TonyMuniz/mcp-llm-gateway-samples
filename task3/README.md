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
uv sync                                   # creates .venv from pyproject.toml / uv.lock
uv run python mock_provider.py --port 9100 &
uv run python gateway.py --port 8080 --upstream http://127.0.0.1:9100 &

curl -N localhost:8080/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model":"m","stream":true,
  "messages":[{"role":"user","content":"Mail bob@example.com, SSN 123-45-6789, card 4111 1111 1111 1111 ok"}]}'
```

The mock echoes the last user message, so you see exactly what the gateway does to it.
Tests: `uv run python -m unittest -v test_redactor test_gateway` (39 tests). Benchmark: `uv run python bench.py`.

## Assignment requirements

| Requirement | How it is met |
|---|---|
| Proxy endpoint that routes generation requests to a provider and streams the reply | `POST /v1/chat/completions` (OpenAI-compatible SSE), `--upstream` / `UPSTREAM_BASE_URL` |
| Intercept the chunk stream in real time | Each SSE event is parsed, redacted and written as it arrives (see "How the streaming redaction works") |
| Detect and redact emails, SSNs, card numbers as `[REDACTED]` | Plus ITINs, IBANs, UK NINOs, Canadian SINs and passport numbers (see "What is detected"), also when written with Unicode look-alike characters |
| No full-response buffering; low TTFT and latency | Only the current word is held; memory is flat at about 42 MB; TTFT adds about one token interval (see "Measured") |

## What is detected

| Identifier | Matches | Extra check |
|---|---|---|
| Email | Local part up to 64 characters; ASCII plus Latin letters with accents (`josé@example.com`) | none |
| Credit card | 13-19 digits, single space or hyphen separators | none (errs toward over-redaction) |
| US SSN, US ITIN | formatted `ddd-dd-dddd` (an ITIN `9xx-xx-xxxx` has the same shape) | none |
| Canadian SIN | `ddd-ddd-ddd` or `ddd ddd ddd` | Luhn checksum |
| IBAN | compact or in groups of four, upper case | ISO 13616 mod-97 checksum |
| UK National Insurance number | `AB 12 34 56 C` or `AB123456C`, upper case | HMRC invalid-prefix rules |
| Passport number | 6-9 letters/digits **with at least one digit, right after the word "passport"** (optionally "number", "no.", "#", ":" or "is") | context keyword |

For the passport only the number is replaced, so `My passport number: X1234567` becomes
`My passport number: [REDACTED]`. A passport number is just 6-9 alphanumerics and is indistinguishable
from any other token without context, which is why it needs the keyword.

**Unicode look-alikes are covered.** Models do not always write plain ASCII: an SSN may come out with
non-breaking hyphens (`123‑45‑6789`), a card number with no-break spaces, and a prompt can ask for
fullwidth digits (`１２３-４５-６７８９`) or a zero-width space inside a number precisely to get past
a filter. Before the review all nine such variants that were tried streamed through unredacted. Now:

- Invisible format characters (zero-width spaces and joiners, soft hyphens, bidi controls, BOM) are
  **removed** from the output.
- Matching runs on a folded copy of the text in which every Unicode decimal digit becomes `0-9`, every
  Unicode dash or minus sign becomes `-`, every Unicode space becomes ` `, and other characters whose
  NFKC form is a single ASCII character (fullwidth letters, `＠`) become that character. The copy is
  the same length as the original, so match positions carry over and only the original text is sent,
  with the matches replaced.
- Plain ASCII text skips both steps (`str.isascii()` fast path).

## How the streaming redaction works (`redactor.py`)

The problem: a secret can be split across chunks (`bob@exa` + `mple.com`), so you cannot redact each
chunk on its own. Buffering the whole response fixes that but destroys latency and memory; a fixed
look-behind window delays every character.

The approach, per chunk:

1. `buf = held_back + new_text` (with invisible characters removed), and a same-length folded copy
   for matching (see "Unicode look-alikes" above)
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
  `test_redactor.py` checks this for every chunk size, every two-way split, 3,000 random texts
  with random chunking (a one-off run of 100,000 random cases found no mismatch), and another 3,000
  random texts built from Unicode look-alike pieces. A separate test
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
| Bad request body (not JSON, undecodable bytes, nesting too deep) | HTTP 400, `{"error": {"message", "type": "gateway_error", "code": "invalid_request"}}` |
| Provider unreachable | HTTP 502, `code: "upstream_unavailable"` |
| Provider sends an unparseable 200 reply | HTTP 502, `code: "invalid_upstream_response"` |
| Provider's non-streamed reply is over 8 MB | HTTP 502, `code: "upstream_response_too_large"` |
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
| Throughput (unpaced, CPU-bound on JSON and regex) | n/a | about 21k events/s |

Real providers emit tens of tokens per second per stream, so throughput is not the constraint. The
Unicode handling and the wider field coverage added in the review cost about 4% throughput (22.3k to
21.4k events/s at 128k events, measured back to back); TTFT and memory were unchanged.

## Design decisions

- **Per-event pipeline, back-pressure included.** Upstream lines are read as they arrive, each event
  is redacted and written with `await resp.write(...)`. A slow client slows the read from upstream
  instead of growing a queue. If the client disconnects, the upstream request is closed.
- **Fail closed.** An upstream event that is not valid JSON is never forwarded raw. The gateway sends
  an error event, drops any held-back text, and ends the stream.
- **Errors are structured, never mixed into the text.** Model text only appears inside `choices`; an
  error has a top-level `error` and no `choices`.
- **Every text field is redacted, not just `content`.** Each string in a `delta` except `role`
  (`content`, `refusal`, `reasoning_content`, or a field a provider adds later) and every tool-call
  `arguments` string gets its own redactor, keyed by choice index and field. `logprobs` is replaced by
  `null`, because it repeats each generated token as plain text and would bypass redaction entirely.
  Redacting inside tool-call arguments keeps them valid JSON (`[REDACTED]` contains no quotes).
- **Multiple choices (`n > 1`)** each get their own redactors, keyed by `choice.index`.
- **Non-streaming requests** are redacted too (every text field of `message`, tool-call arguments,
  `logprobs` dropped), since the full body is small; it is read up to 8 MB.
- **Provider-agnostic core.** `redactor.py` knows nothing about JSON or HTTP. Supporting another wire
  format (for example Anthropic's `content_block_delta`) means changing `StreamState._redact_choice`
  and the SSE field handling in `_stream`.
- **No PII library dependency.** Checksums (Luhn, IBAN mod-97) are a few lines each, which keeps the
  dependency surface, and the CVE surface, to aiohttp alone.

## Known issues and limits

Encoding and Unicode:

- **Upstream bytes must be UTF-8.** Each SSE line is decoded as UTF-8; an undecodable line ends the
  stream with an `upstream_stream_error` event (fail closed). A `charset` other than UTF-8 in the
  provider's `Content-Type` is not honored. Non-streamed replies go through `json.loads`, which also
  accepts UTF-16 and UTF-32.
- **SSE lines must end in `\n` or `\r\n`.** The SSE spec also allows a bare `\r`; a provider using that
  would arrive as one long line and hit the line-length limit (fail closed, not a leak).
- **Invisible characters are removed from the output.** A side effect: emoji built with zero-width
  joiners (family or profession emoji) show as their separate parts, and right-to-left text loses its
  explicit bidi controls. For English output this is acceptable; a multilingual deployment would want a
  narrower rule.
- **Non-Latin scripts.** Emails with non-Latin letters (Cyrillic, CJK, ...) in the address and
  internationalized top-level domains are not matched. Digits in any script are folded, so numbers
  written in Arabic-Indic or fullwidth digits are caught.
- **Look-alikes outside the folding rules** (for example a letter `O` written for a zero, or a homoglyph
  that has no single-character NFKC form) are not normalized.

Detection scope:

- **Cards: any 13-19 digit run** (single space/hyphen separators) is redacted, with no Luhn check. This
  errs toward over-redaction, so a 13-digit millisecond timestamp is also redacted.
- **SSNs/ITINs: only the formatted `ddd-dd-dddd`.** Bare 9-digit numbers are too common to redact safely.
- **Canadian SINs and IBANs are checksum-validated**, so a mistyped one (or a random look-alike) is
  left alone. That is a miss for a typo'd real identifier.
- **Passport numbers need the word "passport" nearby.** A bare passport number, a letters-only one, or
  one introduced in another language or after a longer gap is not caught.
- **Coverage is a handful of identifiers.** There are hundreds of national ID and tax ID formats; this
  covers a few. Names, addresses, dates of birth, phone numbers and free-text descriptions of a
  person are not covered.
- **Obfuscated secrets** ("bob at example dot com", digits spelled out) are out of scope; that needs a
  model, not a regex.
- **Text nested deeper than a `delta` field or tool-call arguments** (for example an audio
  transcript object) is not scanned. `logprobs` is dropped rather than redacted.
- **A single run longer than 256 characters** with no break (for example a base64 blob) streams through
  with only its last 256 characters held; a secret embedded in such a blob is not guaranteed to be caught.
- **Prompts (request side) are not redacted**, and the request body is forwarded as received.
- **No client authentication.** Anyone who can reach the gateway uses it with the gateway's own
  `UPSTREAM_API_KEY`. It binds to `127.0.0.1` by default; put it behind the auth you already use
  (Tasks 2 and 4 show the Bearer-token pattern).

## Going further

For anything beyond a demonstration, weigh a purpose-built approach first. Common options in Python:
Microsoft Presidio (pattern recognizers plus NER models for names and locations), `python-stdnum`
(validators for many countries' national and tax IDs), or a managed service (Google Cloud DLP, AWS
Comprehend, Azure AI Language). NER-based detection needs sentence-level context, so in a stream it
trades time to first token for coverage; it is best run as a second, slower layer or on the
non-latency-sensitive path. This gateway's `Detector` registry is the place to plug in additional
fast, high-precision patterns alongside such a layer.

## Dependencies and security

One direct dependency, `aiohttp>=3.14.5` (the floor is the locked, audited version; the old `>=3.9`
floor allowed releases with published CVEs if someone installed without the lock file). The Unicode
handling uses only the standard library (`unicodedata`). `uv audit` on 2026-11-07: no known
vulnerabilities in the 10 locked packages. Python 3.15 is pinned in `.python-version`; the tests also
pass on 3.11 and 3.13. The Unicode tables come from the Python version in use, so folding of newly
added digit scripts can differ slightly between Python versions.
