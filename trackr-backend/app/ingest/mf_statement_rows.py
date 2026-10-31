"""A checked CAMS/KFintech preview -> the ledger rows each fund section would become.

Pure: no database, no network, no write. ``app.db.imports.stage_mf_statement`` stages what
this returns; promotion is the separate, attributed step it always is (CLAUDE.md rule 5).

ONE FUND SECTION IMPORTS WHOLE OR NOT AT ALL. A section's units only roll forward when
every row in it is accepted, so a single row this module cannot place blocks the whole
section with a named reason -- never a partial history that reconciles by accident.

AMOUNTS ARE THE STATEMENT'S, NOT UNITS x NAV (docs/MF_ETF_PLAN.md sections 5.4 and 6).
Measured on the first real detailed CAS (26-Oct-2026):

    purchase    printed amount = rupees allotted to units; stamp duty is its own row.
                Cash paid = printed amount + stamp duty. units x NAV misses the printed
                amount by a fraction of a paisa because units are rounded to 3 decimals.
    redemption  printed amount = rupees credited, already net of STT (its own row).
                It equals units x NAV - STT exactly.

So ``cash_amount`` carries the printed figure and units x NAV is only the check, within
the rounding the printed precision allows -- never a silent ₹1 tolerance.

Charge rows (stamp duty, STT, TDS) attach to the priced row immediately before them on the
same date, which is how both registrars print them. Anything else is ambiguous and blocks
the section.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal

from app.engine.types import ZERO, TxnType
from app.ingest.mf_cas_pdf import MfScheme, MfStatementPreview, MfTransaction, _period

#: Bumped whenever the mapping below changes meaning, so a staged batch records which
#: rules produced it.
MAPPING_VERSION = "mf-rows.2"  # .2: canonical numbers and the NAV in ``source_ref``

_BUY_KINDS = frozenset({"PURCHASE", "PURCHASE_SIP", "SWITCH_IN"})
_SELL_KINDS = frozenset({"REDEMPTION", "SWITCH_OUT"})
_CHARGE_KINDS = {"STAMP_DUTY_TAX": "stamp_duty", "STT_TAX": "stt", "TDS_TAX": "tds"}

#: Plain-English reason for each check in app/ingest/mf_cas_pdf.py that blocks a section.
ISSUE_REASON = {
    "NO_ISIN": "The statement does not print this fund's ISIN, so the exact plan cannot be "
    "identified.",
    "UNITS_DO_NOT_ADD_UP": "The units in the rows do not add up to the closing balance.",
    "HISTORY_GAP": "You held units before this statement starts, and when or at what price "
    "they were bought is not in it. Request a statement from the earliest date "
    "(\"since inception\") to import this fund.",
    "NEEDS_EVIDENCE": "It has a merger, segregation, gift or unrecognised row that needs "
    "documents Trackr+ does not have yet.",
    "AMOUNT_MISMATCH": "A purchase's amount does not match its units and NAV.",
    "MISSING_BALANCE": "Some rows have no running unit balance to check against.",
    "DATE_OUTSIDE_PERIOD": "Some rows are dated outside the statement period.",
    "VALUE_MISMATCH": "The closing units times the NAV do not match the printed value.",
    "NO_VALUATION": "The statement prints no NAV for this fund.",
}


@dataclass(frozen=True)
class MfLedgerRow:
    """One ledger row and the statement facts behind it."""

    txn_type: TxnType
    txn_date: date
    quantity: Decimal
    price: Decimal
    charges: Decimal
    stt: Decimal
    dividend_amount: Decimal
    tds_amount: Decimal | None
    #: Authoritative rupees moved; None only on a DIVIDEND row, whose cash is
    #: ``dividend_amount`` already.
    cash_amount: Decimal | None
    statement_kind: str
    printed_amount: Decimal
    stamp_duty: Decimal | None
    tds: Decimal | None
    source_sequence: int
    source_ref: str


@dataclass(frozen=True)
class MfSectionImport:
    """What one fund section of the statement would add, or why it cannot."""

    index: int
    isin: str | None
    scheme: str
    amc: str
    registrar: str
    folio_key: str
    folio_label: str
    #: The statement's own period (ISO dates) and this fund's printed closing units: what
    #: app.db.imports reconciles the ledger against, at staging and again at promote.
    period: tuple[date, date] | None
    closing_units: Decimal
    rows: list[MfLedgerRow] = field(default_factory=list)
    blocked_reason: str | None = None

    @property
    def importable(self) -> bool:
        return self.blocked_reason is None


def folio_key(investor_id: str, amc: str, folio: str) -> str:
    """The investor-scoped opaque key for an AMC folio -- the printed number never leaves
    this function. Same derivation as app.db.cas_folios (investor, AMC, folio)."""
    number = "".join(folio.split())
    identity = "\0".join((str(investor_id), amc.casefold(), number))
    return hashlib.sha256(identity.encode()).hexdigest()


def canonical(value: Decimal) -> str:
    """One spelling per number: ``1000.000``, ``1000.0000`` and ``1E+3`` are all ``1000``."""
    return format(value.normalize(), "f")


def row_identity(txn_date: date, statement_kind: str, txn_type: object, quantity: Decimal,
                 price: Decimal, printed_amount: Decimal) -> tuple[str, ...]:
    """The economic identity of one ledger row within its folio and fund.

    The same allotment printed to more or fewer decimal places is the same row; a different
    date, units, NAV or printed amount is a different one. ``source_ref`` is built from
    this, and app.db.imports compares a statement against the ledger by it.
    """
    return (txn_date.isoformat(), statement_kind, str(txn_type), canonical(quantity),
            canonical(price), canonical(printed_amount))


def statement_digest(preview: MfStatementPreview) -> str:
    """A fingerprint of exactly what the user reviewed: every fund and row as parsed.

    The import route re-parses the uploaded file and refuses when this differs from the
    digest the preview showed, so what lands in the ledger is what was on screen. Folio
    numbers are hashed into it rather than included, so the digest itself reveals none.
    """
    body = {
        "issuer": preview.issuer,
        "period": [preview.period_from, preview.period_to],
        "schemes": [
            {
                "folio": hashlib.sha256(("\0".join((s.amc, s.folio))).encode()).hexdigest(),
                "isin": s.isin,
                "open": str(s.opening_units),
                "close": str(s.closing_units),
                "issues": s.issues,
                "rows": [
                    [t.date, t.kind, str(t.units), str(t.nav), str(t.amount), str(t.balance)]
                    for t in s.transactions
                ],
            }
            for s in preview.schemes
        ],
        "warnings": preview.warnings,
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def _places(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    return -exponent if isinstance(exponent, int) and exponent < 0 else 0


def rounding_bound(units: Decimal, nav: Decimal) -> Decimal:
    """Largest gap units x NAV can show against an exact amount, given the decimals the
    statement printed them to (round-half: half a unit in the last place each), plus one
    paisa for the amount's own rounding. docs/.../RECONCILIATION.md tolerance formula."""
    e_u = Decimal(5) * Decimal(10) ** -(_places(units) + 1)
    e_p = Decimal(5) * Decimal(10) ** -(_places(nav) + 1)
    return abs(nav) * e_u + abs(units) * e_p + e_u * e_p + Decimal("0.01")


@dataclass
class _Pending:
    kind: str
    tx: MfTransaction
    sequence: int
    charges: dict[str, Decimal] = field(default_factory=dict)


def _block(section: MfSectionImport, reason: str) -> MfSectionImport:
    return replace(section, rows=[], blocked_reason=reason)


def section_rows(index: int, scheme: MfScheme, investor_id: str,
                 period: tuple[date, date] | None = None) -> MfSectionImport:
    """Map one fund section, or block it with the first reason found."""
    key = folio_key(investor_id, scheme.amc, scheme.folio)
    base = MfSectionImport(
        index, scheme.isin, scheme.scheme, scheme.amc, scheme.registrar, key,
        scheme.folio_label, period, scheme.closing_units,
    )
    for issue in scheme.issues:
        # NO_VALUATION only matters for a fund still held: a closed one needs no NAV.
        if issue == "NO_VALUATION" and scheme.closing_units == 0:
            continue
        return _block(base, ISSUE_REASON.get(issue, issue))
    if scheme.isin is None:
        return _block(base, ISSUE_REASON["NO_ISIN"])

    # Pass 1: priced rows in printed order, each collecting the charge rows that follow it.
    pending: list[_Pending] = []
    for sequence, t in enumerate(scheme.transactions):
        if t.kind in _CHARGE_KINDS:
            parent = pending[-1] if pending else None
            if parent is None or parent.tx.date != t.date:
                return _block(base, f"A {_charge_name(t.kind)} row on {t.date} has no "
                              "transaction before it to belong to.")
            slot = _CHARGE_KINDS[t.kind]
            if slot in parent.charges:
                return _block(base, f"Two {_charge_name(t.kind)} rows follow one transaction "
                              f"on {t.date}; which belongs where is ambiguous.")
            if t.amount is None:
                return _block(base, f"A {_charge_name(t.kind)} row on {t.date} has no amount.")
            parent.charges[slot] = abs(t.amount)
            continue
        if t.kind == "REVERSAL":
            return _block(base, "It has a cancelled or reversed transaction, which Trackr+ "
                          "cannot import safely yet.")
        if t.kind not in _BUY_KINDS | _SELL_KINDS | {"DIVIDEND_PAYOUT", "DIVIDEND_REINVEST"}:
            return _block(base, f"It has a row Trackr+ does not import yet ({t.kind.lower()}).")
        pending.append(_Pending(t.kind, t, sequence))

    # Pass 2: each priced row with its charges -> ledger rows, checked against units x NAV.
    rows: list[MfLedgerRow] = []
    seen: Counter[tuple[str, ...]] = Counter()
    for p in pending:
        mapped = _map(p)
        if isinstance(mapped, str):
            return _block(base, mapped)
        for row in mapped:
            identity = row_identity(row.txn_date, row.statement_kind, row.txn_type,
                                    row.quantity, row.price, row.printed_amount)
            seen[identity] += 1
            # The occurrence number keeps two genuine identical same-day SIPs apart while
            # an overlapping later statement, printing the same rows, maps to the same refs.
            ref = "|".join(("mf", key, scheme.isin, *identity, str(seen[identity])))
            rows.append(_with_ref(row, ref))
    return replace(base, rows=rows)


def _charge_name(kind: str) -> str:
    return {"STAMP_DUTY_TAX": "stamp duty", "STT_TAX": "STT", "TDS_TAX": "TDS"}[kind]


def _with_ref(row: MfLedgerRow, ref: str) -> MfLedgerRow:
    return replace(row, source_ref=ref)


def _map(p: _Pending) -> list[MfLedgerRow] | str:
    t, on = p.tx, date.fromisoformat(p.tx.date)
    stamp, stt, tds = p.charges.get("stamp_duty"), p.charges.get("stt"), p.charges.get("tds")
    amount = abs(t.amount) if t.amount is not None else None
    if amount is None:
        return f"A row on {t.date} has no amount."

    if p.kind in _BUY_KINDS:
        if stt is not None or tds is not None:
            return f"A purchase on {t.date} carries STT or TDS, which a purchase should not."
        units, nav = _units_nav(t)
        if units is None:
            return f"A purchase on {t.date} has no units or NAV."
        if abs(units * nav - amount) > rounding_bound(units, nav):
            return (f"A purchase on {t.date}: units x NAV is {units * nav:.2f} but the "
                    f"statement prints {amount}.")
        return [_row(TxnType.BUY, on, units, nav, stamp or ZERO, ZERO, amount + (stamp or ZERO),
                     p, amount, stamp, None)]

    if p.kind in _SELL_KINDS:
        if stamp is not None:
            return f"A redemption on {t.date} carries stamp duty, which a redemption should not."
        if tds is not None:
            return (f"A redemption on {t.date} had tax deducted (TDS). Trackr+ cannot yet "
                    "split that out for capital gains, so this fund is not imported.")
        units, nav = _units_nav(t)
        if units is None:
            return f"A redemption on {t.date} has no units or NAV."
        gross = amount + (stt or ZERO)
        if abs(units * nav - gross) > rounding_bound(units, nav):
            return (f"A redemption on {t.date}: units x NAV is {units * nav:.2f} but the "
                    f"statement's amount plus STT is {gross}.")
        return [_row(TxnType.SELL, on, units, nav, stt or ZERO, stt or ZERO, amount,
                     p, amount, None, None)]

    if p.kind == "DIVIDEND_PAYOUT":
        if stamp is not None or stt is not None:
            return f"A payout on {t.date} carries stamp duty or STT."
        # Net credited is the realised cash (invariant 18). TDS stays unknown unless printed.
        return [MfLedgerRow(TxnType.DIVIDEND, on, ZERO, ZERO, ZERO, ZERO, amount, tds, None,
                            p.kind, amount, None, tds, p.sequence, "")]

    # DIVIDEND_REINVEST: income and a fresh allotment of the same rupees, so the investor's
    # cash flows net to zero (docs/MF_ETF_PLAN.md 5.4). Stamp duty on the allotment comes
    # out of the distribution, so both legs carry it.
    if stt is not None:
        return f"A reinvestment on {t.date} carries STT."
    units, nav = _units_nav(t)
    if units is None:
        return f"A reinvestment on {t.date} has no units or NAV."
    if abs(units * nav - amount) > rounding_bound(units, nav):
        return (f"A reinvestment on {t.date}: units x NAV is {units * nav:.2f} but the "
                f"statement prints {amount}.")
    reinvested = amount + (stamp or ZERO)
    return [
        MfLedgerRow(TxnType.DIVIDEND, on, ZERO, ZERO, ZERO, ZERO, reinvested, tds, None,
                    p.kind, amount, stamp, tds, p.sequence, ""),
        _row(TxnType.BUY, on, units, nav, stamp or ZERO, ZERO, reinvested, p, amount, stamp,
             None),
    ]


def _units_nav(t: MfTransaction) -> tuple[Decimal | None, Decimal]:
    if t.units is None or t.nav is None or t.units == 0 or t.nav <= 0:
        return None, ZERO
    return abs(t.units), t.nav


def _row(kind: TxnType, on: date, units: Decimal, nav: Decimal, charges: Decimal,
         stt: Decimal, cash: Decimal, p: _Pending, printed: Decimal,
         stamp: Decimal | None, tds: Decimal | None) -> MfLedgerRow:
    return MfLedgerRow(kind, on, units, nav, charges, stt, ZERO, None, cash, p.kind, printed,
                       stamp, tds, p.sequence, "")


def statement_rows(preview: MfStatementPreview, investor_id: str) -> list[MfSectionImport]:
    """Every fund section of a checked preview, mapped or blocked."""
    printed = _period(preview.period_from, preview.period_to)
    period = None if printed is None else (date.fromisoformat(printed[0]),
                                           date.fromisoformat(printed[1]))
    sections = [section_rows(i, s, investor_id, period) for i, s in enumerate(preview.schemes)]
    if not preview.complete:
        reason = ("The statement could not be read completely: " + "; ".join(preview.warnings))
        return [_block(section, reason) for section in sections]
    return sections
