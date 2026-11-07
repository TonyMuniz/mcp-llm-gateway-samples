"""Streaming redaction of direct identifiers (emails, SSNs/ITINs, cards, IBANs, ...).

The hard part of redacting a *stream* is that a secret can be split across
chunks ("bob@exa" + "mple.com"). The usual fixes are bad: buffering the whole
response kills latency and memory, and a fixed look-behind window delays every
character. This module does neither.

Algorithm, per ``feed(text)``:

1. ``buf = pending + text``.
2. Decide the *cut*: everything before it is final, everything after it is held
   back as the new ``pending``. The cut is placed at the start of the trailing
   text that could still grow into a match (``_TAIL``), then moved earlier if a
   complete match straddles it. Two rules, each covering a different case:
     * ``_TAIL`` covers matches that are *incomplete* at the end of the buffer
       (every detector says which prefixes of its matches to hold, see ``tail``).
     * The straddle rule covers matches that are *complete* but run across the
       cut, so no hand-written tail rule can leave a half-redacted secret.
3. Redact the final part and return it.

Result: streamed output is identical to redacting the whole text at once, for
any chunking (``test_redactor.py`` fuzzes this). The held-back text is the
current "word" (or the in-progress identifier), so added latency is roughly one
token interval, and memory per stream is bounded: ``pending`` never exceeds
``MAX_PENDING`` characters however long the response is.

Unicode look-alikes: model output is not always plain ASCII. Before scanning, the
text goes through two steps that keep streamed and one-shot output identical:

* Invisible format characters (zero-width spaces and joiners, soft hyphens,
  bidirectional controls, BOM) are **removed** from the output. They render as
  nothing, so they could otherwise split an identifier ("123-45<ZWSP>-6789")
  and hide it from the patterns.
* Detection runs on a *folded* copy in which each look-alike character is mapped
  to its ASCII equivalent, one character for one character: any Unicode decimal
  digit (fullwidth, Arabic-Indic, ...) to 0-9, Unicode dashes and minus signs to
  "-", Unicode spaces (no-break, narrow, figure, ...) to " ", and other single
  characters whose NFKC form is one ASCII character (fullwidth letters, "\uff20").
  Because the copy has the same length, match positions apply unchanged to the
  real text, and only the real text is ever sent.

Adding an identifier: append a ``Detector`` to ``DETECTORS``; see its docstring.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable

REDACTED = "[REDACTED]"

# Latin letters with diacritics (U+00C0 to U+024F), accepted in email local parts and
# domain labels so that "josé@example.com" is caught. Wider scripts are out of scope.
_LATIN_EXT = "\u00c0-\u024f"

# Longest tail we hold back looking for an identifier in progress. An RFC 5321
# address is at most 254 characters, so this covers any real email; it only
# bounds pathological input such as 10 KB of letters with no whitespace.
MAX_HOLD = 256
# Longest complete match any detector can produce (bounded quantifiers only).
# A straddling match can pull the cut back by at most this much.
MAX_MATCH_SPAN = 700
MAX_PENDING = MAX_HOLD + MAX_MATCH_SPAN


# ---------------------------------------------------------------- validators

def luhn_ok(text: str) -> bool:
    """Luhn checksum over the digits in ``text`` (used by Canadian SINs)."""
    total, double = 0, False
    for ch in reversed([c for c in text if c.isdigit()]):
        d = int(ch)
        if double:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        double = not double
    return total % 10 == 0


def iban_ok(text: str) -> bool:
    """ISO 13616 mod-97 check (rejects random capital-letter-and-digit strings)."""
    iban = text.replace(" ", "")
    if not 15 <= len(iban) <= 34:
        return False
    rearranged = iban[4:] + iban[:4]
    digits = "".join(str(int(c, 36)) for c in rearranged)  # A=10 ... Z=35
    return int(digits) % 97 == 1


# ---------------------------------------------------------------- detectors

@dataclass(frozen=True)
class Detector:
    """One kind of identifier.

    name         unique; becomes a named regex group
    pattern      regex for a *complete* identifier. Use bounded quantifiers only,
                 no capturing groups (except ``value_group``), and keep a match
                 under ``MAX_MATCH_SPAN`` characters
    tail         regex (anchored with ``\\Z``) matching every proper prefix of
                 anything ``pattern`` can match, i.e. "an identifier in progress".
                 Omit it if the prefixes are already covered by the generic
                 alphanumeric/digit tails below. ``test_redactor.py`` checks this
                 against the examples you give it
    validator    optional; receives the matched text, returns False to leave it alone
    value_group  optional named group inside ``pattern``; only that part is replaced
                 (so "passport number: X123456" keeps its label)
    """

    name: str
    pattern: str
    tail: str | None = None
    validator: Callable[[str], bool] | None = None
    value_group: str | None = None


DETECTORS: tuple[Detector, ...] = (
    Detector("email", (
        rf"[A-Za-z0-9_.%+\-{_LATIN_EXT}]{{1,64}}@"
        rf"[A-Za-z0-9\-{_LATIN_EXT}]{{1,63}}(?:\.[A-Za-z0-9\-{_LATIN_EXT}]{{1,63}}){{0,8}}\.[A-Za-z]{{2,24}}"
    )),
    # 13-19 digits, single space/hyphen separators. No Luhn on purpose (errs toward redaction).
    Detector("card", r"\b(?:\d[ \-]?){12,18}\d\b"),
    # Formatted SSN. US ITINs (9xx-xx-xxxx) have the same shape, so they are covered too.
    Detector("ssn", r"\b\d{3}-\d{2}-\d{4}\b"),
    # Canadian SIN: 3-3-3 digits, Luhn-checked to keep false positives down.
    Detector("sin", r"\b\d{3}[- ]\d{3}[- ]\d{3}\b", validator=luhn_ok),
    # IBAN, compact or in groups of four, mod-97 checked. Upper case only (the canonical form).
    Detector("iban", r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b",
             tail=r"(?<![A-Za-z0-9_])[A-Z]{2}\d{2}(?: ?[A-Z0-9]{1,4}){0,8} ?\Z",
             validator=iban_ok),
    # UK National Insurance number, with the HMRC rules on invalid prefixes.
    Detector("nino", (
        r"\b(?!BG|GB|NK|KN|TN|NT|ZZ)[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z](?: ?\d{2}){3} ?[A-D]\b"
    ), tail=r"(?<![A-Za-z0-9_])[A-Z]{2}(?: ?\d{1,2}){0,3}(?: ?[A-D])? ?\Z"),
    # Passport numbers are just 6-9 letters/digits, so they are only recognized right after
    # the word "passport" (context). Only the number is replaced.
    Detector("passport", (
        r"(?i:\bpassport\b)(?:[ \t]{0,3}(?i:no\.?|number|num\.?|#|is))?[ \t]{0,3}[:#=\-]?[ \t]{0,3}"
        r"(?P<pp_val>(?=[A-Za-z0-9]{0,8}\d)[A-Za-z0-9]{6,9}\b)"
    ), tail=r"(?i:\bpassport\b)[^\n]{0,32}\Z", value_group="pp_val"),
)

_BY_NAME = {d.name: d for d in DETECTORS}
# re.ASCII keeps \b and \d aligned with the ASCII character classes used in the tails.
_PATTERN = re.compile("|".join(f"(?P<{d.name}>{d.pattern})" for d in DETECTORS), re.ASCII)

# Text at the end of the buffer that might still become (part of) a match:
#   - a run of email/SSN/word characters (any word may be an email's local part)
#   - digits/spaces/hyphens from a word boundary (a card or SIN in progress)
#   - each detector's own ``tail``
# \Z (not $) so a trailing newline does not count as "end of run".
_TAIL = re.compile("|".join(
    [rf"[A-Za-z0-9_.%+@\-{_LATIN_EXT}]+\Z", r"(?<![A-Za-z0-9_])\d[\d \-]*\Z"]
    + [d.tail for d in DETECTORS if d.tail]
), re.ASCII)


# ---------------------------------------------------------------- Unicode handling

# Removed from the output: zero-width and invisible formatting characters, bidi controls, BOM.
_INVISIBLE = re.compile("[\u00ad\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")
_DASHES = frozenset("\u2010\u2011\u2012\u2013\u2014\u2015\u2043\u2212\ufe58\ufe63\uff0d")


@lru_cache(maxsize=4096)
def _fold_char(ch: str) -> str:
    """One character in, one character out: the ASCII look-alike used for detection."""
    if ch in _DASHES:
        return "-"
    if unicodedata.category(ch) == "Zs":  # every Unicode space separator, including no-break spaces
        return " "
    digit = unicodedata.decimal(ch, None)
    if digit is not None:
        return str(digit)
    folded = unicodedata.normalize("NFKC", ch)
    return folded if len(folded) == 1 and folded.isascii() else ch


def strip_invisible(text: str) -> str:
    return text if text.isascii() else _INVISIBLE.sub("", text)


def fold(text: str) -> str:
    """Same-length ASCII-folded copy of ``text``, used only for matching."""
    return text if text.isascii() else "".join(map(_fold_char, text))


# ---------------------------------------------------------------- engine

def tail_start(text: str) -> int:
    """Index where a possibly-incomplete identifier at the end of ``text`` starts
    (``len(text)`` if nothing needs holding back). Expects folded text."""
    window = text[-MAX_HOLD:]
    m = _TAIL.search(window)
    return len(text) - len(window) + m.start() if m else len(text)


def _span(match: re.Match[str]) -> tuple[int, int] | None:
    """The part of a regex match to replace, or None if its validator rejects it."""
    det = _BY_NAME[match.lastgroup]  # type: ignore[index]
    if det.validator and not det.validator(match.group()):
        return None
    return match.span(det.value_group) if det.value_group else match.span()


def _apply(buf: str, spans: list[tuple[int, int]], end: int) -> tuple[str, int]:
    """Replace every span that ends at or before ``end``; return (text, count)."""
    out, pos, n = [], 0, 0
    for s, e in spans:
        if e > end:
            break
        out.append(buf[pos:s])
        out.append(REDACTED)
        pos = e
        n += 1
    out.append(buf[pos:end])
    return "".join(out), n


def redact_text(text: str) -> str:
    """Redact a complete string in one shot (non-streaming responses)."""
    text = strip_invisible(text)
    spans = [s for m in _PATTERN.finditer(fold(text)) if (s := _span(m))]
    return _apply(text, spans, len(text))[0]


class StreamRedactor:
    """Incremental redactor. One instance per output stream (per choice index)."""

    __slots__ = ("_pending", "redactions")

    def __init__(self) -> None:
        self._pending = ""
        self.redactions = 0

    @property
    def pending_len(self) -> int:
        return len(self._pending)

    def feed(self, text: str) -> str:
        """Add a delta; return the text that is now safe to send (may be empty)."""
        buf = self._pending + strip_invisible(text)
        scan = fold(buf)  # same length as buf: positions found in scan apply to buf
        cut = tail_start(scan)
        spans = []
        for m in _PATTERN.finditer(scan):
            if m.start() < cut < m.end():  # a complete match straddles the cut: hold all of it
                cut = m.start()
            if span := _span(m):
                spans.append(span)
        self._pending = buf[cut:]
        if not cut:
            return ""
        out, n = _apply(buf, spans, cut)
        self.redactions += n
        return out

    def flush(self) -> str:
        """End of stream: release whatever is still held back."""
        buf, self._pending = self._pending, ""
        spans = [s for m in _PATTERN.finditer(fold(buf)) if (s := _span(m))]
        out, n = _apply(buf, spans, len(buf))
        self.redactions += n
        return out
