"""Closing ledger gaps from public records -- docs/ARCHITECTURE_INVARIANTS.md rule 20.

Pure: no database, no network, Decimal only. The orchestrator
(``app.db.gap_resolution``) fetches public terms and calls these; nothing here decides
WHERE terms come from, only whether given terms close a given gap.

A GAP is a security whose replay goes negative: the ledger disposes of shares it never
shows being acquired (an IPO allotment, a rights allotment, a bonus nobody recorded).

THE ACCEPTANCE TEST IS A REPLAY, NOT ARITHMETIC. Every fit check below ends in
``closes_exactly``: the proposed row is added and the ledger is replayed by the same
``compute_holdings_partitioned`` every analytic uses. The candidate is accepted only if that
security then reconciles AND nothing that reconciled before stops reconciling (rule 20c).
The public terms decide price, ratio and date (rule 20a); the replay only proves fit.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal

from app.engine.holdings import compute_holdings_partitioned, quantity_held_before
from app.engine.types import (
    ACQUIRING_TYPES,
    DISPOSING_TYPES,
    FREE_SHARE_TYPES,
    ZERO,
    Source,
    Transaction,
    TxnType,
)

#: SEBI's retail-individual application ceiling. An "IPO allotment" larger than this is
#: an HNI/QIB application -- possible, but no longer bounded by public terms (rule 20d),
#: so it is refused rather than assumed.
RETAIL_IPO_MAX_AMOUNT = Decimal("200000")

#: ``raw_payload.resolution`` on every row this module proposes (rule 20e).
AUTO_RESOLVED = "AUTO_RESOLVED"


@dataclass(frozen=True)
class Gap:
    """One security's missing acquisition. ``shortfall`` is the largest running deficit:
    the minimum number of shares that must have arrived before ``first_negative``."""

    isin: str
    account_id: str | None
    shortfall: Decimal
    first_negative: date
    first_txn: date


@dataclass(frozen=True)
class IpoTerms:
    isin: str
    issue_price: Decimal
    lot_size: int
    listing_date: date
    allotment_date: date
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RightsTerms:
    isin: str
    ratio_new: int  # new shares ...
    ratio_held: int  # ... for every this many held
    price: Decimal  # face value + premium, per share
    ex_date: date
    allotment_date: date
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Resolution:
    """A proposed append-only ledger row and why it is allowed (rule 20e)."""

    row: Transaction
    kind: str
    clauses: dict[str, str]


@dataclass(frozen=True)
class Refusal:
    isin: str
    kind: str
    reason: str


def _delta(t: Transaction) -> Decimal:
    if t.txn_type in ACQUIRING_TYPES or t.txn_type in FREE_SHARE_TYPES:
        return t.quantity
    if t.txn_type in DISPOSING_TYPES:
        return -t.quantity
    return ZERO


def find_gaps(transactions: list[Transaction], as_of: date | None = None) -> list[Gap]:
    """Every security ``compute_holdings_partitioned`` cannot reconcile, with its shortfall.

    The broken set comes from the engine itself, so a gap here is exactly a gap there. The
    shortfall is measured on end-of-day positions (same-day buys net against same-day
    sells, as intraday trading actually settles), which is the quantity a single earlier
    acquisition must supply; ``closes_exactly`` is still the final word on fit.
    """
    _holdings, skipped = compute_holdings_partitioned(transactions, as_of)
    gaps: list[Gap] = []
    for s in skipped:
        rows = [
            t for t in transactions if t.isin == s.isin and (as_of is None or t.txn_date <= as_of)
        ]
        by_day: dict[date, Decimal] = defaultdict(lambda: ZERO)
        for t in rows:
            by_day[t.txn_date] += _delta(t)
        running, worst, first_negative = ZERO, ZERO, None
        for day in sorted(by_day):
            running += by_day[day]
            if running < worst:
                worst = running
            if running < ZERO and first_negative is None:
                first_negative = day
        if first_negative is None:
            # Negative only intraday (ordering), or a non-quantity failure (e.g. withdrawn
            # evidence). Not a missing acquisition; nothing here can close it.
            continue
        accounts = {t.account_id for t in rows}
        gaps.append(
            Gap(
                isin=s.isin,
                account_id=accounts.pop() if len(accounts) == 1 else None,
                shortfall=-worst,
                first_negative=first_negative,
                first_txn=min(t.txn_date for t in rows),
            )
        )
    return gaps


def closes_exactly(transactions: list[Transaction], isin: str, extra: list[Transaction]) -> bool:
    """Rule 20c: with ``extra`` added, ``isin`` reconciles and nothing else newly breaks."""
    _, before = compute_holdings_partitioned(transactions)
    _, after = compute_holdings_partitioned(transactions + extra)
    before_broken = {s.isin for s in before}
    after_broken = {s.isin for s in after}
    return isin not in after_broken and after_broken <= before_broken


def _row(
    gap: Gap, txn_type: TxnType, quantity: Decimal, price: Decimal, on: date, kind: str
) -> Transaction:
    return Transaction(
        isin=gap.isin,
        txn_date=on,
        txn_type=txn_type,
        quantity=quantity,
        price=price,
        account_id=gap.account_id,
        source=Source.CORP_ACTION,
        source_ref=f"{AUTO_RESOLVED}:{kind}:{gap.isin}:{on.isoformat()}",
    )


def ipo_fits(gap: Gap, terms: IpoTerms, transactions: list[Transaction]) -> Resolution | Refusal:
    """An IPO allotment explains the gap only if every public bound holds (rule 20d)."""
    kind = "IPO_ALLOTMENT"
    if terms.isin != gap.isin:
        return Refusal(gap.isin, kind, "terms are for a different ISIN")
    if gap.first_txn < terms.listing_date:
        return Refusal(gap.isin, kind, "account traded the security before it listed")
    if gap.first_negative < terms.allotment_date:
        return Refusal(gap.isin, kind, "shares were needed before the allotment date")
    if terms.lot_size <= 0 or gap.shortfall % terms.lot_size != 0:
        return Refusal(
            gap.isin,
            kind,
            f"shortfall {gap.shortfall} is not a whole number of {terms.lot_size}-share lots",
        )
    if gap.shortfall * terms.issue_price > RETAIL_IPO_MAX_AMOUNT:
        return Refusal(gap.isin, kind, "exceeds the retail application ceiling")
    row = _row(gap, TxnType.IPO_ALLOT, gap.shortfall, terms.issue_price, terms.allotment_date, kind)
    if not closes_exactly(transactions, gap.isin, [row]):
        return Refusal(gap.isin, kind, "allotment does not reconcile the ledger")
    return Resolution(
        row,
        kind,
        {
            "20a": "issue price, lot size, listing and allotment dates from the exchange",
            "20c": "replay reconciles with the allotment and breaks nothing else",
            "20d": f"{gap.shortfall} = {gap.shortfall // terms.lot_size} lot(s) of "
            f"{terms.lot_size}, within the retail ceiling, no trade before listing",
        },
    )


def rights_fits(
    gap: Gap, terms: RightsTerms, transactions: list[Transaction]
) -> Resolution | Refusal:
    """A rights allotment, priced at the rights price and dated at allotment (rule 20d).

    The subscribed quantity is the investor's own fact; the ledger deficit supplies it.
    It may exceed the record-date entitlement (additional shares applied for) -- recorded
    in the clauses either way, so the row says which it was.
    """
    kind = "RIGHTS_ALLOTMENT"
    if terms.isin != gap.isin:
        return Refusal(gap.isin, kind, "terms are for a different ISIN")
    if gap.first_negative < terms.allotment_date:
        return Refusal(gap.isin, kind, "shares were needed before the allotment date")
    held = quantity_held_before(transactions, gap.isin, terms.ex_date)
    if held <= ZERO:
        return Refusal(gap.isin, kind, "nothing held on the record date -- no entitlement")
    entitlement = (held * terms.ratio_new) // terms.ratio_held
    row = _row(gap, TxnType.BUY, gap.shortfall, terms.price, terms.allotment_date, kind)
    if not closes_exactly(transactions, gap.isin, [row]):
        return Refusal(gap.isin, kind, "allotment does not reconcile the ledger")
    basis = (
        "equals the record-date entitlement"
        if gap.shortfall == entitlement
        else f"entitlement {entitlement} + {gap.shortfall - entitlement} additional applied for"
        if gap.shortfall > entitlement
        else f"{entitlement - gap.shortfall} of {entitlement} entitled shares renounced"
    )
    return Resolution(
        row,
        kind,
        {
            "20a": f"rights {terms.ratio_new}:{terms.ratio_held} at {terms.price}, exchange notice",
            "20c": "replay reconciles with the allotment and breaks nothing else",
            "20d": f"held {held} at record date; {gap.shortfall} allotted -- {basis}",
        },
    )


# --- researched events: what an LLM read in an exchange document (rule 20a/20b) --------
#
# The model proposes; this verifier disposes -- the same split as app/ingest/llm_verify.py.
# The model is shown exchange documents and asked to fill ``ResearchedEvent``. Nothing it
# returns is trusted until every check below passes, and each check is deterministic:
# its quote must appear word for word in the document the system itself fetched and
# hashed, every number it claims must appear in that quote, its date must sit on an
# observed price discontinuity in the exchange's own bhavcopy, the ratio must agree with
# that discontinuity, and the event must close the ledger gap exactly.

#: A day-over-day close ratio this large is a candidate corporate-action date. Exchange
#: circuit limits cap an ordinary day near 20%; the band only CORROBORATES a document's
#: date, it never names an event on its own.
JUMP_MIN_RATIO = Decimal("1.2")

#: How far a document's ex/record date may sit from the observed price jump. Record and
#: ex dates coincide under T+1; older T+2 notices sit one session apart; holidays stretch it.
JUMP_DATE_TOLERANCE_DAYS = 7


@dataclass(frozen=True)
class PriceJump:
    on: date  # first session at the new price level (the ex-date as traded)
    cum: Decimal
    ex: Decimal

    @property
    def ratio(self) -> Decimal:
        return self.cum / self.ex


def find_price_jumps(closes: list[tuple[date, Decimal]]) -> list[PriceJump]:
    """Consecutive sessions whose close falls by ``JUMP_MIN_RATIO`` or more -- the shape a
    bonus or split leaves in a bhavcopy series. ``closes`` may span an ISIN change (old
    ISIN's last close, then the new ISIN's first), which is exactly GUJTHEM's 2023 split."""
    ordered = sorted(closes)
    return [
        PriceJump(on=d2, cum=c1, ex=c2)
        for (d1, c1), (d2, c2) in zip(ordered, ordered[1:], strict=False)
        if c2 > ZERO and c1 / c2 >= JUMP_MIN_RATIO
    ]


EVENT_TYPES = ("BONUS", "SPLIT", "ISIN_CHANGE")


@dataclass(frozen=True)
class ResearchedEvent:
    """One event as an LLM read it from a fetched exchange document.

    ``ex_date`` is None when the document states the terms of a completed bonus/split but
    no date (an annual report: "issued 1 bonus share for every 2 held"). Such an event is
    dated only by the exchange's own price data, and only when exactly ONE observed jump
    fits its ratio (``jump_for``); ``dated_by_price`` then records that the date came from
    the bhavcopy, not the document."""

    type: str
    ex_date: date | None
    doc_url: str
    quote: str
    bonus_new: int | None = None
    bonus_for: int | None = None
    fv_before: Decimal | None = None
    fv_after: Decimal | None = None
    old_isin: str | None = None
    new_isin: str | None = None
    dated_by_price: bool = False

    def multiplier(self) -> Decimal | None:
        if self.type == "BONUS" and self.bonus_new and self.bonus_for:
            return Decimal(self.bonus_new + self.bonus_for) / Decimal(self.bonus_for)
        if self.type == "SPLIT" and self.fv_before and self.fv_after:
            return self.fv_before / self.fv_after
        return None


def _norm(text: str) -> str:
    return " ".join(text.replace("’", "'").split()).lower()


def _number_in(value: Decimal | int, text: str) -> bool:
    plain = format(Decimal(value).normalize(), "f")
    return re.search(rf"(?<![\d.]){re.escape(plain)}(?:\.0+)?(?![\d])", text) is not None


#: A bonus and a split with the same multiplier leave the same price jump and the same
#: share count, but differ for tax (rule 20b): a bonus opens new zero-cost lots, a split
#: keeps the original dates. The quote must therefore NAME the event it is evidence for.
_TYPE_WORDS = {
    "BONUS": ("bonus",),
    "SPLIT": ("split", "sub-division", "subdivision", "sub division"),
}

#: An Indian ISIN's real shape (INE942C01045: IN, issuer type, 4-char issuer, 2-digit
#: security type, 2-char serial, check digit). A looser "IN + 10 alphanumerics" matched
#: plain words -- INCORPORATED, INTERMEDIATE -- and called every annual report a
#: multi-issuer circular (seen live on GUJTHEM, 2026-10-08).
_ISIN_RE = re.compile(r"\bIN[EF9][A-Z0-9]{4}\d{2}[A-Z0-9]{2}\d\b")
_NAME_SUFFIX_RE = re.compile(r"\b(limited|ltd\.?)$")


def _identifier_forms(identifiers: tuple[str, ...]) -> list[str]:
    forms = []
    for ident in identifiers:
        n = _norm(ident)
        if not n:
            continue
        forms.append(n)
        bare = _NAME_SUFFIX_RE.sub("", n).strip()
        if bare and bare != n:
            forms.append(bare)
    return forms


def _contains_identifier(text: str, forms: list[str]) -> bool:
    t = _norm(text)
    return any(re.search(rf"(?<![a-z0-9]){re.escape(f)}(?![a-z0-9])", t) for f in forms)


def check_event_against_document(
    event: ResearchedEvent, document_text: str, company: tuple[str, ...]
) -> str | None:
    """None when the event is literally what the document says ABOUT THIS COMPANY;
    otherwise why not.

    ``company`` is the security's identifiers: its ISIN(s), trading symbol and name. The
    document must name the company; a document that also names other issuers (a circular
    listing many companies) must name it in the quote itself, so a ratio read off another
    company's line can never be attributed to this one.
    """
    if event.type not in EVENT_TYPES:
        return f"unknown event type {event.type!r}"
    quote = _norm(event.quote)
    if len(quote) < 20:
        return "quote too short to be evidence"
    if quote not in _norm(document_text):
        return "quote does not appear in the fetched document"
    forms = _identifier_forms(company)
    if not forms or not _contains_identifier(document_text, forms):
        return "document does not name the company"
    own_prefixes = {i[:7].upper() for i in company if _ISIN_RE.fullmatch(i.upper())}
    others = {i[:7] for i in _ISIN_RE.findall(document_text.upper())} - own_prefixes
    if others and not _contains_identifier(event.quote, forms):
        return "document names several issuers and the quote does not name this company"
    words = _TYPE_WORDS.get(event.type)
    if words and not any(w in quote for w in words):
        return f"quote does not call the event a {event.type.lower()}"
    numbers: list[Decimal | int] = []
    if event.type == "BONUS":
        if not (event.bonus_new and event.bonus_for):
            return "bonus without a ratio"
        numbers = [event.bonus_new, event.bonus_for]
    elif event.type == "SPLIT":
        if not (event.fv_before and event.fv_after) or event.fv_after >= event.fv_before:
            return "split without a reducing face value"
        numbers = [event.fv_before, event.fv_after]
    elif not (event.old_isin and event.new_isin):
        return "ISIN change without both ISINs"
    elif not {event.old_isin[:7], event.new_isin[:7]} <= own_prefixes:
        # Same issuer only (rule 10): a cross-issuer re-identification is a merger or
        # demerger, which needs a swap ratio and is never closed by a rename.
        return "ISIN change outside this issuer's ISIN family"
    missing = [n for n in numbers if not _number_in(n, quote)]
    if missing:
        return f"quote does not state {missing}"
    for isin in (event.old_isin, event.new_isin):
        if isin and isin.lower() not in _norm(document_text):
            return f"document does not mention {isin}"
    return None


def jump_for(event: ResearchedEvent, jumps: list[PriceJump]) -> PriceJump | None:
    """The observed discontinuity this event explains: nearest within tolerance whose
    ratio agrees with the stated multiplier inside ``check_price_consistency``'s band.

    An undated event matches only when exactly one jump in the whole window fits the
    ratio; two fitting jumps are two candidate dates, and a date is never chosen."""
    from app.engine.corporate_actions import PRICE_BAND_HIGH, PRICE_BAND_LOW

    m = event.multiplier()
    if m is None:
        return None
    low = Decimal(PRICE_BAND_LOW.numerator) / Decimal(PRICE_BAND_LOW.denominator)
    high = Decimal(PRICE_BAND_HIGH.numerator) / Decimal(PRICE_BAND_HIGH.denominator)
    fitting = [j for j in jumps if low <= j.ratio / m <= high]
    if event.ex_date is None:
        return fitting[0] if len(fitting) == 1 else None
    near = [j for j in fitting if abs((j.on - event.ex_date).days) <= JUMP_DATE_TOLERANCE_DAYS]
    return min(near, key=lambda j: abs((j.on - event.ex_date).days)) if near else None


def consolidate_events(
    events: list[ResearchedEvent],
    jumps: list[PriceJump],
    transactions: list[Transaction],
    isin: str,
) -> tuple[list[ResearchedEvent], str | None]:
    """One event per real-world event, or a reason the set is ambiguous (rule 20c).

    Several documents describe the same event (board approval, record-date notice, the
    exchange's own circular); applying each would multiply the bonus. So:

      - ISIN changes collapse on (old, new); the same old ISIN mapped to two different new
        ISINs is a contradiction.
      - Bonuses and splits are grouped by the price jump they explain. Within a jump every
        reading must agree on the type and multiplier; a bonus and a split on one jump is
        exactly the ambiguity rule 20b forbids guessing through.
      - The ex-date kept is the one the exchange actually traded (the jump), preferring a
        document that states it; if the documents' date and the traded date differ and
        the account traded this security between them, which side those trades fall on
        changes the entitlement, so the set is refused rather than resolved.
    """
    out: list[ResearchedEvent] = []
    by_old: dict[str, ResearchedEvent] = {}
    for e in events:
        if e.type != "ISIN_CHANGE":
            continue
        seen = by_old.get(e.old_isin)
        if seen is None:
            by_old[e.old_isin] = e
        elif seen.new_isin != e.new_isin:
            return [], f"documents map {e.old_isin} to both {seen.new_isin} and {e.new_isin}"
        elif abs((seen.ex_date - e.ex_date).days) > JUMP_DATE_TOLERANCE_DAYS:
            return [], f"documents date the {e.old_isin} ISIN change far apart"
    out.extend(by_old.values())

    by_jump: dict[date, list[ResearchedEvent]] = defaultdict(list)
    for e in events:
        if e.type == "ISIN_CHANGE":
            continue
        jump = jump_for(e, jumps)
        if jump is None:
            return (
                [],
                (
                    f"{e.type} on {e.ex_date or 'an unstated date'} has no single matching "
                    "price discontinuity"
                ),
            )
        by_jump[jump.on].append(e)
    traded = sorted(
        {t.txn_date for t in transactions if t.isin == isin}
        | {t.txn_date for t in transactions if any(t.isin == x.old_isin for x in by_old.values())}
    )
    for on, group in sorted(by_jump.items()):
        types = {e.type for e in group}
        if len(types) > 1:
            return [], f"documents call the {on} event both a bonus and a split"
        if len({e.multiplier() for e in group}) > 1:
            return [], f"documents state different ratios for the {on} event"
        dated = [e for e in group if e.ex_date is not None]
        if not dated:
            out.append(replace(group[0], ex_date=on, dated_by_price=True))
            continue
        chosen = next((e for e in dated if e.ex_date == on), dated[0])
        if chosen.ex_date != on:
            lo, hi = sorted((chosen.ex_date, on))
            if any(lo <= d < hi for d in traded):
                return [], (
                    f"document date {chosen.ex_date} and traded ex-date {on} differ and the "
                    "account traded in between"
                )
            chosen = replace(chosen, ex_date=on)
        out.append(chosen)
    return out, None


def isin_handover_observed(
    old_dates: list[date], new_dates: list[date], effective: date
) -> str | None:
    """Rule 20b for an ISIN change: the exchange's own bhavcopy shows the old ISIN stop and
    the new one start, in that order, at the stated date. None when observed; else why not."""
    if not old_dates:
        return "no exchange price on file for the old ISIN"
    if not new_dates:
        return "no exchange price on file for the new ISIN"
    if max(old_dates) >= min(new_dates):
        return "old and new ISIN traded side by side -- not a handover"
    if abs((min(new_dates) - effective).days) > JUMP_DATE_TOLERANCE_DAYS:
        return f"new ISIN first trades {min(new_dates)}, not near the stated {effective}"
    return None


def apply_events(
    transactions: list[Transaction], isin: str, events: list[ResearchedEvent]
) -> list[Transaction]:
    """The ledger as it would read with ``events`` on file -- for the fit check only.

    ISIN changes become a read-time rename (rule 7: an alias, never a transaction);
    bonuses and splits become the same quantity rows ``apply_known_actions_for_account``
    writes, sized from what was held the day before the ex-date (fractions dropped, as
    Indian practice credits whole shares and pays cash for the fraction)."""
    ledger = list(transactions)
    for event in sorted(events, key=lambda e: e.ex_date):
        if event.type == "ISIN_CHANGE":
            ledger = [
                replace(t, isin=event.new_isin) if t.isin == event.old_isin else t for t in ledger
            ]
            continue
        target = event.new_isin or isin
        held = quantity_held_before(ledger, target, event.ex_date)
        if held <= ZERO:
            continue
        if event.type == "BONUS":
            extra = (held * event.bonus_new) // event.bonus_for
            kind = TxnType.BONUS
        else:
            extra = held * event.multiplier() - held
            kind = TxnType.SPLIT_ADJ
        ledger.append(
            Transaction(
                isin=target,
                txn_date=event.ex_date,
                txn_type=kind,
                quantity=extra,
                price=ZERO,
                account_id=next(iter({t.account_id for t in ledger}), None),
                source=Source.CORP_ACTION,
                source_ref=f"{AUTO_RESOLVED}:{event.type}:{target}",
            )
        )
    return ledger


def events_close_gap(
    transactions: list[Transaction], isin: str, events: list[ResearchedEvent]
) -> bool:
    """Rule 20c for researched events: with every event applied, the security (under its
    final ISIN) reconciles and nothing that reconciled before stops reconciling."""
    _, before = compute_holdings_partitioned(transactions)
    after_ledger = apply_events(transactions, isin, events)
    _, after = compute_holdings_partitioned(after_ledger)
    final = next((e.new_isin for e in events if e.type == "ISIN_CHANGE" and e.new_isin), isin)
    renamed = {e.old_isin for e in events if e.type == "ISIN_CHANGE"}
    after_broken = {s.isin for s in after}
    before_broken = {s.isin for s in before} - renamed
    return final not in after_broken and isin not in after_broken and after_broken <= before_broken


# --- ISIN change from the depository's own record (rule 20a: NSDL is a public record) ---
#
# A split changes the face value, and a face-value change retires the ISIN. Issuer filings
# state the split but rarely the new ISIN (GUJTHEM: none of its filings Exa indexes name
# INE942C01029 at all). The depository does: its ISIN master marks the old ISIN DELETED and
# describes both with their face values. That is accepted, deterministically and with no
# model involved, only when four independent facts agree.

_FV_RE = re.compile(r"\bFV\b\s*(?:RS|RE|INR)?\.?\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)


def face_value_in(description: str) -> Decimal | None:
    """The face value an NSDL ISIN description states ("... EQ NEW FV RE 1/-" -> 1)."""
    found = {Decimal(m) for m in _FV_RE.findall(description or "")}
    return found.pop() if len(found) == 1 else None


@dataclass(frozen=True)
class DepositoryRecord:
    isin: str
    issuer: str
    description: str
    status: str
    source_url: str
    sha256: str


def isin_change_from_depository(
    old: DepositoryRecord,
    new: DepositoryRecord,
    split: ResearchedEvent,
    old_dates: list[date],
    new_dates: list[date],
) -> tuple[ResearchedEvent | None, str | None]:
    """The ISIN change a verified split implies, or why it is not established.

    All four must hold: (1) same issuer, same ISIN family; (2) the depository marks the old
    ISIN DELETED and the new one ACTIVE; (3) the depository's face values are exactly the
    split's before/after; (4) the exchange bhavcopy shows the handover at the split."""
    if split.type != "SPLIT" or split.ex_date is None:
        return None, "no dated split to anchor an ISIN change"
    if old.isin[:7] != new.isin[:7] or _norm(old.issuer) != _norm(new.issuer):
        return None, "depository records are for different issuers"
    if old.status.upper() != "DELETED" or new.status.upper() != "ACTIVE":
        return None, f"depository status {old.status}/{new.status}, not DELETED/ACTIVE"
    fv_old, fv_new = face_value_in(old.description), face_value_in(new.description)
    if (fv_old, fv_new) != (split.fv_before, split.fv_after):
        return None, (
            f"depository face values {fv_old}->{fv_new} are not the split's "
            f"{split.fv_before}->{split.fv_after}"
        )
    problem = isin_handover_observed(old_dates, new_dates, split.ex_date)
    if problem:
        return None, problem
    return (
        ResearchedEvent(
            type="ISIN_CHANGE",
            ex_date=min(new_dates),
            doc_url=new.source_url,
            quote=f"{old.isin} {old.description} [{old.status}] -> "
            f"{new.isin} {new.description} [{new.status}]",
            old_isin=old.isin,
            new_isin=new.isin,
        ),
        None,
    )


# --------------------------------------------------------------------- rule 20(f)

KEEP, CONFIRM, REVERSE = "KEEP", "CONFIRM", "REVERSE"


@dataclass(frozen=True)
class AutoRowVerdict:
    """What the investor's own documents say about one AUTO_RESOLVED row."""

    action: str  # KEEP | CONFIRM | REVERSE
    reason: str = ""


def _held_at(transactions: list[Transaction], isin: str, on: date) -> Decimal | None:
    """Quantity of ``isin`` at the end of ``on``; None when that replay does not reconcile
    (a skipped ISIN reads as zero, which a CAS of zero would wrongly 'match')."""
    holdings, skipped = compute_holdings_partitioned(transactions, on)
    if any(s.isin == isin for s in skipped):
        return None
    return next((h.quantity for h in holdings if h.isin == isin), ZERO)


def auto_row_verdict(
    transactions: list[Transaction], row: Transaction, cas_counts: dict[date, Decimal]
) -> AutoRowVerdict:
    """Rule 20(f): is an auto-resolved row still the only explanation, by the investor's own
    documents? ``transactions`` is the account's ledger including ``row``; ``cas_counts`` the
    CAS-declared quantity of ``row.isin`` in that account, by statement date.

    - Without the row the ISIN never goes negative: the investor's own records now account
      for every share sold, so the row's only basis -- closing a gap (20c) -- is gone.
      REVERSE.
    - A CAS dated on or after the row: matching the ledger WITH the row is CONFIRM; matching
      it only WITHOUT the row is REVERSE (the depository shows the shares never arrived).
      CAS statements that disagree with each other decide nothing (KEEP).
    - Anything else is another, unexplained difference, never guessed at: KEEP.
    """
    without = [t for t in transactions if t.id != row.id]
    if not any(g.isin == row.isin for g in find_gaps(without)):
        return AutoRowVerdict(REVERSE, "your own records now account for these shares")

    confirms, contradicts = [], []
    for on in sorted(d for d in cas_counts if d >= row.txn_date):
        declared = cas_counts[on]
        if _held_at(transactions, row.isin, on) == declared:
            confirms.append(on)
        elif _held_at(without, row.isin, on) == declared:
            contradicts.append(on)
    if confirms and not contradicts:
        return AutoRowVerdict(CONFIRM, f"matches your CAS of {confirms[-1].isoformat()}")
    if contradicts and not confirms:
        return AutoRowVerdict(
            REVERSE, f"your CAS of {contradicts[-1].isoformat()} shows these shares never arrived"
        )
    return AutoRowVerdict(KEEP)
