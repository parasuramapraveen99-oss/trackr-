"""CAMS / KFintech DETAILED CAS -> a checked PREVIEW of mutual fund folios. Writes nothing.

docs/MF_ETF_PLAN.md section 6 names this module as the separate RTA adapter. This first
version stops at a preview, deliberately, and the reason is in the plan rather than in
caution for its own sake:

  * P5 says transaction import is built "from sanitized real CAMS and KFintech detailed
    transaction statements", and none exist in the repository yet.
  * "Authoritative amounts are required before transaction launch": explicit immutable
    consideration/cash columns at the persistence boundary, plus an MF import segment
    (migration 064 allows only EQ/FNO). Neither exists, and both are schema work that
    CLAUDE.md requires to go through plan mode with the owner.

So this module answers the question a preview can answer honestly -- *what is in this
statement, and does it add up?* -- and never produces a ``Transaction``. The first real
statement an owner previews through it is exactly the sample P5 is waiting for.

WHAT IT CHECKS, per folio + scheme (each a named issue, never a silent fix):

  ``NO_ISIN``             The statement printed no ISIN. casparser would normally look one
                          up by RTA code and then by fuzzy scheme name; the plan forbids
                          that ("never synthesize an ISIN or select a regular/direct plan
                          through fuzzy names"), so ``_printed_isin_only`` replaces the
                          lookup and an unprinted ISIN stays unresolved.
  ``UNITS_DO_NOT_ADD_UP`` Opening units + every movement != the closing units printed.
  ``HISTORY_GAP``         Opening units are non-zero: the units were bought before the
                          statement period, so their cost and dates are not in this file.
                          The plan: "an explicit history gap: no zero-cost opening BUY".
  ``NO_VALUATION``        No NAV (or no market value) printed for a fund that still has
                          units. casparser fills a missing NAV with 0; this module turns
                          that back into "not printed" -- a missing price is never zero
                          (CLAUDE.md rule 4) -- and the total names what it leaves out.
  ``VALUE_MISMATCH``      Closing units x NAV does not equal the printed market value.
  ``AMOUNT_MISMATCH``     A purchase's units x NAV does not equal its amount.
  ``DATE_OUTSIDE_PERIOD`` A row is dated outside the statement's own period.
  ``MISSING_BALANCE``     A row that moves units has no running balance printed, so it
                          cannot be checked against the statement's own running total.
                          These three are the statement checking itself: a misread
                          figure (or a damaged file) that still "adds up" in units is
                          caught by the valuation and allotment arithmetic instead.
  ``NEEDS_EVIDENCE``      Rows whose treatment needs evidence this statement does not
                          carry: scheme mergers (cost carry-over needs AMC documents),
                          segregated portfolios (needs the NAV ratio), gift/transfer legs
                          (needs the original lot history), rows casparser could not
                          classify -- and rows it may have MIS-classified: IDCW wording
                          not filed as a dividend, reversal wording not filed as a
                          reversal, bonus units, and a priced type with no amount.

AND FOR THE STATEMENT AS A WHOLE: ``complete`` is false whenever casparser reported a
parse warning. Its warnings are how it says a fund's header could not be read -- in which
case that fund AND ALL ITS TRANSACTIONS are absent from the result -- or that a row did not
match the printed running balance. A fund that vanished cannot flag itself, so the preview
never calls such a statement "all good". This module adds two warnings of its own: an
unreadable statement period, and a mismatch between the number of fund sections in the
PDF's raw text and the number of funds read (``_section_count``) -- which is how a fund
lost WITHOUT a casparser warning still shows up.

ONE casparser BEHAVIOUR TO KNOW. For KFin rows whose units print in parentheses against
the running balance, casparser flips the sign so ``previous balance + units`` equals the
printed ``balance`` column (``cams_detailed._apply_balance_sign_fix``). The correction is
driven by the statement's own printed balance, not a guess, so it is kept; the statement
order is kept too (``sort_transactions=False``) because the plan requires same-day
statement event order to be respected.

THE PASSWORD is request-local: it is passed to the PDF library, used once, and never
logged, stored or included in an error. Contrast ``/ingest/files``, which saves a password
that opened a broker file -- the plan rules that out for RTA statements.
"""

from __future__ import annotations

import contextlib
import io
import logging
import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)

ADAPTER_NAME = "mf_cas_pdf"

#: A 10-year detailed CAS for a heavy SIP investor is a few MB; this is generous.
MAX_BYTES = 25 * 1024 * 1024

#: A complete mutual fund ISIN: INF + 8 alphanumerics + a check digit.
_FULL_MF_ISIN = re.compile(r"^INF[0-9A-Z]{8}\d$")

# Issue codes. Strings, because they cross the HTTP boundary unchanged.
NO_ISIN = "NO_ISIN"
UNITS_DO_NOT_ADD_UP = "UNITS_DO_NOT_ADD_UP"
HISTORY_GAP = "HISTORY_GAP"
NEEDS_EVIDENCE = "NEEDS_EVIDENCE"
NO_VALUATION = "NO_VALUATION"
VALUE_MISMATCH = "VALUE_MISMATCH"
AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
MISSING_BALANCE = "MISSING_BALANCE"
DATE_OUTSIDE_PERIOD = "DATE_OUTSIDE_PERIOD"

#: Allotment and redemption types: units x NAV should equal the amount.
_ALLOTMENT_TYPES = frozenset({"PURCHASE", "PURCHASE_SIP", "SWITCH_IN"})

#: casparser transaction types whose ledger treatment docs/MF_ETF_PLAN.md section 5.4
#: already decides (purchase/SIP, redemption, ordinary switch, IDCW payout/reinvestment,
#: stamp duty, STT, TDS, reversal).
DECIDED_TYPES = frozenset(
    {
        "PURCHASE",
        "PURCHASE_SIP",
        "REDEMPTION",
        "SWITCH_IN",
        "SWITCH_OUT",
        "DIVIDEND_PAYOUT",
        "DIVIDEND_REINVEST",
        "STAMP_DUTY_TAX",
        "STT_TAX",
        "TDS_TAX",
        "REVERSAL",
    }
)

#: Types whose treatment needs evidence the statement does not carry (plan section 5.4:
#: "These events stay named and pending if evidence is missing").
EVIDENCE_TYPES = frozenset(
    {
        "SWITCH_IN_MERGER",
        "SWITCH_OUT_MERGER",
        "SEGREGATION",
        "GIFT_IN",
        "GIFT_OUT",
        "MISC",
        "UNKNOWN",
    }
)


#: Wording that casparser's keyword classifier can file under the wrong type. Its
#: dividend rule needs "@ Rs." -- "Reinvestment of IDCW" without a printed rate becomes a
#: PURCHASE; its reversal rule applies only to negative units -- a cancelled redemption
#: (positive units) becomes a PURCHASE; and it has no bonus type at all.
_IDCW_WORDS = re.compile(r"\b(?:idcw|dividend|div\.)", re.I)
_REVERSAL_WORDS = re.compile(r"reversal|rejection|reversed|dishonou?red|cancel", re.I)
_BONUS_WORDS = re.compile(r"\bbonus\b", re.I)
#: Types that move units for money: one without a printed amount has no price.
_PRICED_TYPES = frozenset({"PURCHASE", "PURCHASE_SIP", "REDEMPTION", "SWITCH_IN", "SWITCH_OUT"})


def _needs_evidence(t: MfTransaction) -> bool:
    """True when this row's treatment cannot be decided from the statement alone: its
    type is one the plan leaves pending, or casparser's classification is doubtful."""
    if t.kind not in DECIDED_TYPES:
        return True
    if _IDCW_WORDS.search(t.description) and not t.kind.startswith("DIVIDEND_"):
        return True
    if _REVERSAL_WORDS.search(t.description) and t.kind != "REVERSAL":
        return True
    if _BONUS_WORDS.search(t.description):
        return True
    if t.kind in ("STAMP_DUTY_TAX", "STT_TAX", "TDS_TAX") and t.amount is None:
        return True  # a charge row with no charge
    return t.kind in _PRICED_TYPES and t.amount is None


def _value_mismatch(closing: Decimal, nav: Decimal | None, value: Decimal | None) -> bool:
    """The statement's own checksum on valuation: value = closing units x NAV. NAV prints
    to 4 places and value to 2, so allow half a unit in the last place of each."""
    if closing == 0:
        return value is not None and value != 0  # nothing held cannot be worth something
    if nav is None or value is None:
        return False
    tolerance = abs(closing) * Decimal("0.00005") + Decimal("0.01")
    return abs(closing * nav - value) > tolerance


def _amount_mismatch(t: MfTransaction) -> bool:
    """For an allotment, units = amount / NAV rounded DOWN to 3 places, so units x NAV
    sits up to 0.001 x NAV below the amount -- and never meaningfully above it. The upper
    side allows half a unit's rounding (0.0005 x NAV) for registrars that round to nearest,
    plus a paisa. Redemptions are not checked: an exit load can sit between the figures."""
    if t.kind not in _ALLOTMENT_TYPES or t.units is None or t.nav is None or t.amount is None:
        return False
    gap = abs(t.amount) - abs(t.units) * t.nav  # >= 0 for a truncated allotment
    below = t.nav * Decimal("0.001") + Decimal("0.01")
    above = t.nav * Decimal("0.0005") + Decimal("0.01")
    return gap > below or -gap > above


class MfStatementError(ValueError):
    """The file cannot be previewed. ``code`` is stable; the message is for the person."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MfTransaction:
    date: str
    kind: str
    description: str
    units: Decimal | None
    nav: Decimal | None
    amount: Decimal | None
    balance: Decimal | None


@dataclass(frozen=True)
class MfScheme:
    folio: str
    amc: str
    scheme: str
    isin: str | None
    registrar: str
    opening_units: Decimal
    closing_units: Decimal
    calculated_units: Decimal
    nav: Decimal | None
    nav_date: str | None
    value: Decimal | None
    transactions: list[MfTransaction]
    issues: list[str]
    evidence_rows: int

    @property
    def checks_passed(self) -> bool:
        return not self.issues

    @property
    def folio_label(self) -> str:
        """The folio as every other fund response shows it: masked to its last four
        characters (app/db/cas_folios.py). The full number never leaves this module."""
        return "…" + "".join(self.folio.split())[-4:]  # "1234567 / 89" -> "…7/89"


@dataclass(frozen=True)
class MfStatementPreview:
    issuer: str
    period_from: str
    period_to: str
    investor_name: str
    schemes: list[MfScheme]
    warnings: list[str] = field(default_factory=list)

    @property
    def transaction_count(self) -> int:
        return sum(len(s.transactions) for s in self.schemes)

    @property
    def folio_count(self) -> int:
        # Folio numbers are RTA-scoped: two fund houses can issue the same number.
        return len({(s.amc, s.folio) for s in self.schemes})

    @property
    def complete(self) -> bool:
        """False when the reader skipped or doubted part of the statement (see module doc)."""
        return not self.warnings

    @property
    def unvalued_count(self) -> int:
        return sum(1 for s in self.schemes if s.value is None)

    @property
    def schemes_needing_attention(self) -> int:
        return sum(1 for s in self.schemes if not s.checks_passed)

    @property
    def total_value(self) -> Decimal:
        """Sum of the values PRINTED on the statement, on the statement's own NAV dates.
        A display total, not a portfolio valuation: valuation comes from AMFI NAV
        (app/prices/routing.py), and this preview never claims otherwise. Funds with no
        printed value are left out and counted in ``unvalued_count``, never added as 0."""
        return sum((s.value for s in self.schemes if s.value is not None), Decimal(0))


# ---------------------------------------------------------------------------
# ISIN: printed on the statement, or unresolved
# ---------------------------------------------------------------------------

#: pdfium (casparser's PDF engine, via pypdfium2) is NOT thread-safe: two threads inside it
#: at once crash the whole process (found by test_parallel_parses_*; a segfault, not an
#: exception). Every pdfium call in this module -- casparser's parse and the section count
#: -- runs under this one lock, which also covers the ISIN-lookup swap.
_PDFIUM_LOCK = threading.Lock()


def _printed_isin_only(
    scheme_name: str, rta: str, rta_code: str, isin: str | None = None
) -> tuple[str | None, str | None, str | None]:
    """Replacement for casparser's ``isin_search``: the ISIN printed in the scheme header,
    if it is a complete one, and nothing else. No RTA-code lookup, no fuzzy name match,
    no AMFI code or scheme type from casparser's bundled database."""
    if isin and _FULL_MF_ISIN.match(isin.strip()):
        return isin.strip(), None, None
    return None, None, None


@contextlib.contextmanager
def _strict_isin() -> Iterator[None]:
    """Swap casparser's ISIN lookup for ``_printed_isin_only`` for one parse.

    ``cams_detailed`` calls its module-level ``isin_search`` by name, so replacing that
    name is the narrowest hook available. Callers hold ``_PDFIUM_LOCK``, so two
    concurrent parses can neither interleave restores nor enter pdfium together.
    """
    from casparser.parsers import cams_detailed

    original = cams_detailed.isin_search
    cams_detailed.isin_search = _printed_isin_only
    try:
        yield
    finally:
        cams_detailed.isin_search = original


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _read(content: bytes, password: str) -> Any:
    """casparser's typed result, or a ``MfStatementError`` that never echoes the password."""
    import casparser
    from casparser.exceptions import CASParseError, IncorrectPasswordError

    try:
        with _PDFIUM_LOCK, _strict_isin():
            return casparser.read_cas_pdf(
                io.BytesIO(content), password, output="dict", sort_transactions=False
            )
    except IncorrectPasswordError as exc:
        raise MfStatementError(
            "wrong_password",
            "That password did not open the statement. Use the password you chose on the "
            "CAMS website when you requested it.",
        ) from exc
    except CASParseError as exc:
        # casparser's own messages name the issuer problem; they never contain the password.
        logger.info("mf statement not readable: %s", type(exc).__name__)
        raise MfStatementError(
            "not_a_statement",
            "This does not look like a CAMS or KFintech statement. Upload the original PDF "
            "from the CAMS email, not a printed or re-saved copy.",
        ) from exc
    except Exception as exc:  # noqa: BLE001 - pdfium/pydantic failures on odd files
        logger.warning("mf statement parse failed: %s", type(exc).__name__)
        raise MfStatementError(
            "unreadable",
            "This statement could not be read. Upload the original PDF from the CAMS email.",
        ) from exc


def _period(start: str, end: str) -> tuple[str, str] | None:
    """The statement period as ISO dates, or None if it was not printed/readable."""
    from dateutil import parser as dateparse

    try:
        return dateparse.parse(start).date().isoformat(), dateparse.parse(end).date().isoformat()
    except (ValueError, OverflowError, TypeError):
        return None


#: The same anchor casparser builds a fund on (cams_detailed.OPEN_BAL_RE): the label AND
#: its figure, so explanatory text mentioning the phrase is not counted.
_SECTION_RE = re.compile(r"Opening\s+Unit\s+Balance\s*:?\s*[\d,.]*\d", re.I)
#: ...and the anchor that closes it. casparser defaults a missing closing line to 0 units,
#: which "adds up" against a section whose rows were lost too -- so both are counted.
_SECTION_END_RE = re.compile(r"Closing\s+Unit\s+Balance\s*:?\s*[\d,.]*\d", re.I)


def _section_count(content: bytes, password: str) -> tuple[int, int] | None:
    """How many fund sections the PDF's raw text contains, counted independently of
    casparser's parse. A fund that casparser lost without a warning (its fund-house or
    folio line unreadable) still leaves its 'Opening Unit Balance' behind. Returns
    (opening anchors, closing anchors)."""
    import pypdfium2 as pdfium

    with _PDFIUM_LOCK:
        try:
            doc = pdfium.PdfDocument(content, password=password)
        except Exception as exc:  # noqa: BLE001 - opened once already; surfaced as a warning
            logger.warning(
                "mf statement: second open for section count failed: %s", type(exc).__name__
            )
            return None
        try:
            opened = closed = 0
            for page in doc:
                text = page.get_textpage().get_text_bounded()
                opened += len(_SECTION_RE.findall(text))
                closed += len(_SECTION_END_RE.findall(text))
            return opened, closed
        finally:
            doc.close()


def _iso(value: Any) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _kind(value: Any) -> str:
    return str(getattr(value, "value", value))


def parse_statement(content: bytes, password: str) -> MfStatementPreview:
    """Unlock, identify and check a CAMS/KFintech detailed statement.

    Raises ``MfStatementError`` for anything that is not a readable detailed RTA
    statement. Returns a preview; never a ``Transaction``, never a write.
    """
    from casparser.enums import CASFileType, FileType
    from casparser.types import CASData

    if not content.startswith(b"%PDF-"):
        raise MfStatementError("not_pdf", "Upload the statement as a PDF.")
    if len(content) > MAX_BYTES:
        raise MfStatementError("too_large", "This file is too large to be a CAS statement.")

    data = _read(content, password)

    # CASData is declared with use_enum_values, so these arrive as their string values;
    # _kind() compares by value either way.
    if not isinstance(data, CASData) or _kind(data.file_type) not in (
        FileType.CAMS.value,
        FileType.KFINTECH.value,
    ):
        raise MfStatementError(
            "depository_statement",
            "This is a depository (CDSL or NSDL) statement. It shows balances, not your "
            "mutual fund transactions. Upload it under Import instead, and request the "
            "Detailed statement from CAMS for this step.",
        )
    if _kind(data.cas_type) != CASFileType.DETAILED.value:
        raise MfStatementError(
            "summary_statement",
            "This is a summary statement, which has balances but no transactions. Request "
            "the Detailed statement from CAMS.",
        )

    period = _period(data.statement_period.from_, data.statement_period.to)
    schemes: list[MfScheme] = []
    for folio in data.folios:
        for s in folio.schemes:
            transactions = [
                MfTransaction(
                    date=_iso(t.date),
                    kind=_kind(t.type),
                    description=t.description,
                    units=_dec(t.units),
                    nav=_dec(t.nav),
                    amount=_dec(t.amount),
                    balance=_dec(t.balance),
                )
                for t in s.transactions
            ]
            # Required fields on casparser's Scheme model, so never None here.
            opening = Decimal(str(s.open))
            closing = Decimal(str(s.close))
            calculated = Decimal(str(s.close_calculated))
            evidence_rows = sum(1 for t in transactions if _needs_evidence(t))

            issues: list[str] = []
            if s.isin is None:
                issues.append(NO_ISIN)
            if calculated != closing:
                issues.append(UNITS_DO_NOT_ADD_UP)
            if opening != 0:
                issues.append(HISTORY_GAP)
            if evidence_rows:
                issues.append(NEEDS_EVIDENCE)

            # casparser defaults a missing NAV / Market Value line to 0. A NAV of 0 is never
            # real, and a value of 0 is real only when no units are held.
            valuation = s.valuation
            nav = _dec(valuation.nav) if valuation else None
            value = _dec(valuation.value) if valuation else None
            if nav is None or nav == 0:
                nav = None
            if value is None or (value == 0 and closing != 0):
                value = None
            if nav is None and value is None:
                nav_date = None
            else:
                nav_date = _iso(valuation.date)
            # Every fund line ends with its NAV, zero-unit ones included: no NAV means the
            # valuation is missing, uncheckable, or the fund's block was not read whole.
            if nav is None or (closing != 0 and value is None):
                issues.append(NO_VALUATION)
            if _value_mismatch(closing, nav, value):
                issues.append(VALUE_MISMATCH)
            if any(_amount_mismatch(t) for t in transactions):
                issues.append(AMOUNT_MISMATCH)
            if any(t.units is not None and t.balance is None for t in transactions):
                issues.append(MISSING_BALANCE)
            if period is not None and any(
                not (period[0] <= t.date <= period[1]) for t in transactions
            ):
                issues.append(DATE_OUTSIDE_PERIOD)

            schemes.append(
                MfScheme(
                    folio=folio.folio,
                    amc=folio.amc,
                    scheme=s.scheme,
                    isin=s.isin,
                    registrar=s.rta,
                    opening_units=opening,
                    closing_units=closing,
                    calculated_units=calculated,
                    nav=nav,
                    nav_date=nav_date,
                    value=value,
                    transactions=transactions,
                    issues=issues,
                    evidence_rows=evidence_rows,
                )
            )

    if not schemes and not data.parse_warnings:
        raise MfStatementError(
            "no_funds",
            "This statement lists no mutual funds. Check that you used the email address "
            "linked to your mutual funds when you requested it from CAMS.",
        )

    warnings = list(data.parse_warnings)
    if period is None:
        warnings.append("The statement period could not be read.")
    sections = _section_count(content, password)
    if sections is None:
        warnings.append("The number of funds in the file could not be cross-checked.")
    elif sections != (len(schemes), len(schemes)):
        opened, closed = sections
        warnings.append(
            f"The file has {opened} fund section{'s' if opened != 1 else ''} "
            f"({closed} with a closing balance) but {len(schemes)} "
            f"{'was' if len(schemes) == 1 else 'were'} read; a fund or its rows may be "
            "missing from this preview."
        )

    return MfStatementPreview(
        issuer=_kind(data.file_type),
        period_from=data.statement_period.from_,
        period_to=data.statement_period.to,
        investor_name=data.investor_info.name,
        schemes=schemes,
        warnings=warnings,
    )
