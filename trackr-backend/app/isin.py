"""ISIN as an identifier: normalise it, and decide whether it can be trusted at all.

WHY THIS EXISTS. CLAUDE.md rule 2 makes the ISIN the identity of a security -- every
holding, lot, price and tax figure is keyed by it. Nothing in this repository has ever
checked that an incoming ISIN is *well-formed*. A tradebook column read one cell to the
left, a CAS PDF whose text extraction dropped a digit, or a hand-typed manual entry all
produce a string that looks like an ISIN, keys a brand-new security, and quietly becomes
a holding nothing will ever price. ISO 6166 puts a check digit in position 12 precisely
so that single-character damage is detectable, and this module is where that is spent.

WHAT IT DOES NOT DO. It decides nothing about *which* security an identifier names. That
is ``app.securities.lookup`` for metadata and ``app.db.security_alias`` for identity over
time, and the governing rule there -- an alias is an assertion, never an inference --
is untouched by anything here. This module answers a narrower question: is this string a
syntactically valid ISIN, and if not, what kind of damage is it?

REJECTION ONLY, in the same sense as ``security_alias._assert_same_identity``. A passing
check digit proves the string is undamaged, NOT that the security exists, NOT that it is
the one the broker meant, and NOT that it is in scope. One in ten corrupted strings
passes by chance. Treat a pass as "no detectable damage", never as verification.

WHY THE CHECK DIGIT IS NOT ENOUGH ON ITS OWN, and why this module must never be the last
word: real reference data is occasionally dirty, and a checksum failure on a string that
exactly matches a known ``security`` row is evidence about the row, not grounds to
discard a holding. The caller decides; see ``Reason.CHECKSUM`` below.

FRAMEWORK-FREE AND PURE. No database, no I/O, no framework imports, so the engine, the
parsers and the repositories may all import it without a layer violation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

#: ISO 6166 shape: 2-letter country prefix, 9 alphanumeric characters, 1 numeric check
#: digit. Indian ISINs are all ``IN``-prefixed, but this stays general -- the check digit
#: is an international standard and a US or GB ISIN reaching here is a scope error to be
#: reported, not a malformed string to be blamed on extraction.
_SHAPE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")

#: Characters that carry no meaning inside an identifier but survive PDF text extraction
#: and copy-paste out of a browser: zero-width space/non-joiner/joiner, word joiner,
#: BOM, and the non-breaking space. Stripped before anything else looks at the string,
#: because they make an otherwise-correct ISIN fail the shape check for a reason no
#: reviewer reading the value in a terminal could ever see.
_INVISIBLE = str.maketrans({c: None for c in "\u200b\u200c\u200d\u2060\ufeff\u00a0"})

#: Longest raw value stored or examined. A misaligned PDF column can hand us an entire
#: paragraph; running a regex over it and then persisting it wastes space and risks
#: capturing a name or a folio number that has no business in an identifier column.
MAX_RAW_LENGTH = 64

#: Ledger lines that legitimately carry no ISIN. These are not securities and their
#: absence of an identifier is correct, not damage. Matched on the NORMALISED label.
#:
#: This list is necessarily incomplete -- every broker words these differently -- which
#: is why an unrecognised label with no ISIN is QUARANTINED rather than assumed to be
#: one of these. Adding to the list is safe; assuming membership is not.
NON_SECURITY_LABELS = frozenset(
    {
        "TREPS",
        "CASH",
        "CASH BALANCE",
        "CLEARING MEMBER MARGIN",
        "MARGIN",
        "NET RECEIVABLES",
        "NET PAYABLES",
        "NET RECEIVABLE PAYABLE",
        "BROKERAGE",
        "STT",
        "STAMP DUTY",
        "GST",
        "SEBI TURNOVER FEES",
        "EXCHANGE TRANSACTION CHARGES",
    }
)


class Outcome(StrEnum):
    """What the caller may do with the value.

    Deliberately three-valued. A two-valued verdict would force every caller to decide
    for itself what an absent identifier means, and that decision -- cash line, or
    security whose identifier went missing -- is the one this module exists to get right.
    """

    #: Well-formed and undamaged so far as the check digit can tell. NOT "resolved".
    VALID = "VALID"
    #: Correctly has no ISIN: a cash, margin, settlement or charge line.
    NON_SECURITY = "NON_SECURITY"
    #: Do not key anything on this value. Route it to a human.
    QUARANTINE = "QUARANTINE"


class Reason(StrEnum):
    """Why a value was quarantined. Set if and only if the outcome is QUARANTINE."""

    #: Not a string at all -- ``None``, bytes, a number from a spreadsheet cell.
    NOT_A_STRING = "NOT_A_STRING"
    #: Longer than ``MAX_RAW_LENGTH`` before normalisation: a column misalignment.
    TOO_LONG = "TOO_LONG"
    #: Empty after normalisation, with no label saying it is a non-security line. The
    #: important case: a security whose identifier was lost, NOT a cash row. Treating
    #: this as NON_SECURITY is how a real holding silently disappears.
    MISSING_IDENTIFIER = "MISSING_IDENTIFIER"
    #: Does not match the ISO 6166 shape.
    SHAPE = "SHAPE"
    #: Right shape, wrong check digit -- single-character damage, or a typo.
    #:
    #: A caller holding reference data should check for an exact match before discarding
    #: the row: reference data is occasionally dirty, and a known security whose stored
    #: ISIN fails its own check digit must stay resolvable. That lookup is the caller's,
    #: because this module has no database.
    CHECKSUM = "CHECKSUM"


@dataclass(frozen=True, slots=True)
class Classification:
    """The verdict on one raw value, with the evidence a reviewer needs.

    ``raw`` is kept alongside ``normalized`` deliberately: when a value quarantines, the
    difference between the two is the extraction damage, and a reviewer who sees only the
    cleaned-up form cannot tell a broker typo from a parser bug.
    """

    outcome: Outcome
    raw: str
    normalized: str
    reason: Reason | None = None

    def __post_init__(self) -> None:
        # The invariant that makes `reason` safe to branch on.
        if (self.reason is None) == (self.outcome is Outcome.QUARANTINE):
            raise ValueError(
                f"reason must be set for QUARANTINE and absent otherwise, "
                f"got outcome={self.outcome} reason={self.reason}"
            )


def expand(isin: str) -> str:
    """ISO 6166 letter expansion: A=10 ... Z=35, digits unchanged.

    The expansion happens BEFORE the Luhn pass, and the doubling then applies to the
    expanded digit string rather than to the original characters. Doubling the characters
    first is the classic wrong implementation and it accepts damaged values.
    """
    return "".join(str(ord(c) - 55) if c.isalpha() else c for c in isin)


def _luhn_remainder(digits: str) -> int:
    """Luhn mod 10 over a digit string, doubling every second digit from the right.

    Index 0 is the rightmost digit and is not doubled, so passing a full ISIN's expansion
    (check digit included) gives 0 for a valid value.
    """
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10


def check_digit(body: str) -> int:
    """The check digit that completes ``body`` (the first 11 characters of an ISIN).

    Exposed because generating a valid identifier is the only honest way to build test
    data: an ISIN typed from memory is as likely as not to be invalid, and a fixture that
    is invalid for a reason nobody notices gets "fixed" by weakening validation.
    """
    if len(body) != 11:
        raise ValueError(f"an ISIN body is 11 characters, got {len(body)}: {body!r}")
    # Appending d shifts every existing digit one place left, flipping which ones double,
    # so the remainder is computed over the full candidate rather than over the body.
    return next(d for d in range(10) if _luhn_remainder(expand(body) + str(d)) == 0)


def has_valid_check_digit(isin: str) -> bool:
    """Whether a shape-valid ISIN's check digit is consistent with the rest of it."""
    return _luhn_remainder(expand(isin)) == 0


def normalize(raw: str) -> str:
    """Upper-case, invisible characters removed, all whitespace removed.

    Internal whitespace is REMOVED rather than collapsed. A collapse would turn
    ``"INE002A 01018"`` into ``"INE002A 01018"`` and fail the shape check; an ISIN
    contains no spaces, so any whitespace inside one is extraction damage and dropping it
    recovers the intended value. The raw form is retained on the Classification so the
    repair stays visible rather than becoming silent.
    """
    return "".join(raw.translate(_INVISIBLE).upper().split())


def _normalize_label(label: str) -> str:
    return " ".join(label.translate(_INVISIBLE).upper().split())


def classify(raw: object, label: str | None = None) -> Classification:
    """Judge one raw identifier. Never raises; every input gets a verdict.

    ``label`` is the row's description, used ONLY to recognise a line that legitimately
    has no ISIN. It is never used to guess an identifier.

    The ordering matters and is the argument of the module:

    1. Non-strings and over-long values are rejected before any pattern is applied.
    2. An empty value is a non-security ONLY if its label says so. Otherwise it is a
       missing identifier and quarantines -- the case that decides whether a security
       with no ISIN disappears or gets reviewed.
    3. Shape, then check digit, so a failure names the coarser problem first.

    Deliberately NOT here: any lookup, any suggestion of what the value might have been
    meant to be, and any decision about scope. A caller with reference data should
    consult it before acting on ``Reason.CHECKSUM``.
    """
    if not isinstance(raw, str):
        # str() rather than repr() for the record: a reviewer wants to see the value,
        # and repr of a long bytes object is noise. Truncated for the same reason as
        # TOO_LONG below -- an over-long non-string is still over-long.
        shown = str(raw)[:MAX_RAW_LENGTH]
        return Classification(Outcome.QUARANTINE, shown, "", Reason.NOT_A_STRING)

    if len(raw) > MAX_RAW_LENGTH:
        return Classification(
            Outcome.QUARANTINE, raw[:MAX_RAW_LENGTH], "", Reason.TOO_LONG
        )

    normalized = normalize(raw)

    if not normalized:
        if label is not None and _normalize_label(label) in NON_SECURITY_LABELS:
            return Classification(Outcome.NON_SECURITY, raw, "")
        return Classification(Outcome.QUARANTINE, raw, "", Reason.MISSING_IDENTIFIER)

    if not _SHAPE.match(normalized):
        return Classification(Outcome.QUARANTINE, raw, normalized, Reason.SHAPE)

    if not has_valid_check_digit(normalized):
        return Classification(Outcome.QUARANTINE, raw, normalized, Reason.CHECKSUM)

    return Classification(Outcome.VALID, raw, normalized)
