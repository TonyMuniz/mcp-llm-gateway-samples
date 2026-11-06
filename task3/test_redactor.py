"""Unit tests for the streaming redactor.  Run: uv run python -m unittest -v test_redactor"""

import random
import unittest

from redactor import (DETECTORS, MAX_HOLD, MAX_PENDING, REDACTED, StreamRedactor, iban_ok, luhn_ok,
                      redact_text)

SAMPLE = ("Contact alice.smith@example.com or bob@mail.example.co.uk. SSN 123-45-6789, "
          "cards 4111 1111 1111 1111 and 5500-0000-0000-0004, plain 4111111111111111 done.")

# One realistic, valid example per identifier form. Used by the prefix-hold test.
EXAMPLES = [
    "alice.smith@example.com", "123-45-6789", "912-70-1234",
    "4111 1111 1111 1111", "5500-0000-0000-0004",
    "046 454 286",
    "GB82 WEST 1234 5698 7654 32", "GB82WEST12345698765432", "DE89 3704 0044 0532 0130 00",
    "AB 12 34 56 C", "AB123456C",
    "passport number: X1234567", "Passport No. 123456789", "passport is k9876543",
]


def stream(text: str, sizes) -> str:
    """Feed text in pieces of the given sizes (cycled) and return the full output."""
    r, out, i, k = StreamRedactor(), [], 0, 0
    while i < len(text):
        n = sizes[k % len(sizes)]
        out.append(r.feed(text[i:i + n]))
        i += n
        k += 1
    out.append(r.flush())
    return "".join(out)


class RedactionTests(unittest.TestCase):
    def test_one_shot(self):
        out = redact_text(SAMPLE)
        for secret in ("alice.smith@example.com", "bob@mail.example.co.uk", "123-45-6789",
                       "4111 1111 1111 1111", "5500-0000-0000-0004", "4111111111111111"):
            self.assertNotIn(secret, out)
        self.assertEqual(out.count(REDACTED), 6)
        self.assertIn("Contact [REDACTED] or [REDACTED].", out)  # trailing period preserved

    def test_every_chunk_size_matches_one_shot(self):
        expected = redact_text(SAMPLE)
        for size in range(1, len(SAMPLE) + 1):
            self.assertEqual(stream(SAMPLE, [size]), expected, f"chunk size {size}")

    def test_every_two_way_split(self):
        expected = redact_text(SAMPLE)
        for i in range(len(SAMPLE) + 1):
            r = StreamRedactor()
            out = r.feed(SAMPLE[:i]) + r.feed(SAMPLE[i:]) + r.flush()
            self.assertEqual(out, expected, f"split at {i}")

    def test_fuzz_random_text_and_chunking(self):
        rng = random.Random(1234)
        pieces = ["hello", "world", "a.b", "x1", "2024-01-05", "12:30", "(", ")", ",", ".", "-",
                  "bob@example.com", "carol_9@sub.domain.org", "123-45-6789", "078-05-1120",
                  "4111111111111111", "4111 1111 1111 1111", "1234-5678-9012-3456", "12345",
                  "@", "user@", "@host.com", "v1.2.3", "123-45-6789world",
                  "9 9 9 9 9 9 9 9 9 9 9 9 9 9x", "100%", "+1", "\n", "é", "日本",
                  # newer identifiers, valid and invalid forms, plus look-alikes and partial context
                  "GB82 WEST 1234 5698 7654 32", "GB83 WEST 1234 5698 7654 32", "GB82WEST12345698765432",
                  "DE89 3704 0044 0532 0130 00", "GB82", "AB", "OK", "US",
                  "AB 12 34 56 C", "AB123456C", "BG 12 34 56 C", "AB 12 34", "046 454 286", "046 454 287",
                  "123 456 789", "passport", "Passport", "number:", "No.", "#", "is", "passport number: X1234567",
                  "passport is k9876543", "passport office", "passport AB123456.suffix"]
        seps = [" ", " ", " ", "\n", ", ", ""]
        for _ in range(3000):
            text = "".join(rng.choice(pieces) + rng.choice(seps) for _ in range(rng.randint(1, 25)))
            sizes = [rng.randint(1, 9) for _ in range(rng.randint(1, 5))]
            self.assertEqual(stream(text, sizes), redact_text(text), f"{text!r} sizes={sizes}")

    def test_no_false_positives_on_ordinary_text(self):
        text = ("Version 1.2.3 shipped on 2024-01-05 at 10:30. Call 555-1234 (ext 12). "
                "Use user@host locally; ratio 3/4; id 123-45-678; pi is 3.14159265. "
                "The US passport office is open; AB 12 is a code; order 046 454 287.")
        self.assertEqual(stream(text, [3]), text)
        self.assertEqual(redact_text(text), text)


class DetectorTests(unittest.TestCase):
    def test_every_detector_has_unique_name(self):
        names = [d.name for d in DETECTORS]
        self.assertEqual(len(names), len(set(names)))

    def test_itin_is_covered_by_the_ssn_pattern(self):
        self.assertEqual(redact_text("ITIN 912-70-1234 ok"), "ITIN [REDACTED] ok")

    def test_iban_requires_valid_checksum(self):
        self.assertTrue(iban_ok("GB82 WEST 1234 5698 7654 32"))
        self.assertFalse(iban_ok("GB83 WEST 1234 5698 7654 32"))
        self.assertEqual(redact_text("pay GB82 WEST 1234 5698 7654 32 now"), "pay [REDACTED] now")
        self.assertEqual(redact_text("pay GB82WEST12345698765432 now"), "pay [REDACTED] now")
        self.assertEqual(redact_text("pay DE89 3704 0044 0532 0130 00 now"), "pay [REDACTED] now")
        self.assertEqual(redact_text("pay GB83 WEST 1234 5698 7654 32 now"),
                         "pay GB83 WEST 1234 5698 7654 32 now")  # bad checksum: left alone

    def test_uk_nino(self):
        for good in ("AB 12 34 56 C", "AB123456C", "ab 12 34 56 c".upper()):
            self.assertEqual(redact_text(f"NI {good}."), "NI [REDACTED].", good)
        for bad in ("BG 12 34 56 C", "DA 12 34 56 C", "AB 12 34 56 E", "AB 12 34 56"):
            self.assertEqual(redact_text(f"NI {bad}."), f"NI {bad}.", bad)  # invalid prefix/suffix

    def test_canadian_sin_requires_luhn(self):
        self.assertTrue(luhn_ok("046 454 286"))
        self.assertFalse(luhn_ok("046 454 287"))
        self.assertEqual(redact_text("SIN 046-454-286."), "SIN [REDACTED].")
        self.assertEqual(redact_text("SIN 046 454 287."), "SIN 046 454 287.")

    def test_passport_needs_context_and_keeps_the_label(self):
        self.assertEqual(redact_text("My passport number: X1234567."), "My passport number: [REDACTED].")
        self.assertEqual(redact_text("Passport No. 123456789"), "Passport No. [REDACTED]")
        self.assertEqual(redact_text("my passport is k9876543!"), "my passport is [REDACTED]!")
        self.assertEqual(redact_text("passport # AB1234567"), "passport # [REDACTED]")
        # the same characters without the keyword are not touched, and neither are letters-only values
        self.assertEqual(redact_text("ticket X1234567 booked"), "ticket X1234567 booked")
        self.assertEqual(redact_text("passport number: ABCDEFGH"), "passport number: ABCDEFGH")
        self.assertEqual(redact_text("passport office hours"), "passport office hours")

    def test_nothing_of_an_identifier_leaks_before_it_is_complete(self):
        """Feeding any prefix of any example must release only the text before it."""
        for example in EXAMPLES:
            for i in range(1, len(example) + 1):
                r = StreamRedactor()
                self.assertEqual(r.feed("ctx " + example[:i]), "ctx ", f"{example!r}[:{i}]")

    def test_examples_are_redacted_when_streamed_in_every_chunk_size(self):
        for example in EXAMPLES:
            text = f"before {example} after"
            expected = redact_text(text)
            self.assertIn(REDACTED, expected, example)
            for size in range(1, 9):
                self.assertEqual(stream(text, [size]), expected, f"{example!r} size {size}")

    def test_split_between_keyword_and_value(self):
        r = StreamRedactor()
        out = r.feed("my passport number: ") + r.feed("X1234") + r.feed("567 thanks") + r.flush()
        self.assertEqual(out, "my passport number: [REDACTED] thanks")


class LatencyAndMemoryTests(unittest.TestCase):
    def test_text_ending_in_space_is_released_immediately(self):
        r = StreamRedactor()
        self.assertEqual(r.feed("Hello world. "), "Hello world. ")
        self.assertEqual(r.pending_len, 0)

    def test_only_the_current_word_is_held(self):
        r = StreamRedactor()
        self.assertEqual(r.feed("Hello wor"), "Hello ")
        self.assertEqual(r.pending_len, 3)
        self.assertEqual(r.feed("ld! "), "world! ")

    def test_two_capital_letters_then_space_costs_one_extra_token(self):
        # "AB " could be the start of a UK National Insurance number, so it is held
        # until the next token shows it is not. A known, small cost of that detector.
        r = StreamRedactor()
        self.assertEqual(r.feed("in the US "), "in the ")
        self.assertEqual(r.feed("today"), "US ")

    def test_email_split_across_chunks_is_held_then_redacted(self):
        r = StreamRedactor()
        self.assertEqual(r.feed("mail bob@exa"), "mail ")
        self.assertEqual(r.feed("mple.com now"), "[REDACTED] ")
        self.assertEqual(r.flush(), "now")

    def test_pending_is_bounded_on_pathological_input(self):
        r = StreamRedactor()
        emitted = 0
        for _ in range(2000):  # 1 MB of letters with no whitespace
            emitted += len(r.feed("a" * 512))
            self.assertLessEqual(r.pending_len, MAX_HOLD)
        self.assertGreater(emitted, 900_000)

    def test_pending_is_bounded_on_digit_runs(self):
        r = StreamRedactor()
        for _ in range(500):
            r.feed("1 2 3 4 5 6 7 8 9 0 " * 5)
            self.assertLessEqual(r.pending_len, MAX_PENDING)

    def test_pending_is_bounded_on_identifier_heavy_input(self):
        rng = random.Random(7)
        r = StreamRedactor()
        words = EXAMPLES + ["passport", "GB82", "AB", "x@", "1234", "-", " "]
        for _ in range(5000):
            r.feed(rng.choice(words) + rng.choice([" ", "", "x", "\n"]))
            self.assertLessEqual(r.pending_len, MAX_PENDING)

    def test_redaction_counter(self):
        r = StreamRedactor()
        r.feed(SAMPLE)
        r.flush()
        self.assertEqual(r.redactions, 6)


if __name__ == "__main__":
    unittest.main()
