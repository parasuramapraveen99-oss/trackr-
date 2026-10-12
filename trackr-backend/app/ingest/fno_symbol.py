"""Contract-note F&O symbols: decode into candidates, resolve against the contract master.

docs/FNO_PLAN.md Piece 1, docs/ARCHITECTURE_INVARIANTS.md rule 21(a). Pure: no database,
no network. ``resolve`` takes a lookup callable; ``app.db.imports`` passes one backed by
``app.db.fno_reference.find_contracts``.

THE FORMS. Verified against real Zerodha notes and the exchange's own ``FinInstrmNm``
(NSE F&O bhavcopy, 2024-08-12 and 2026-09-02 -- the exchange prints the same strings):

* monthly option   ``ADANIENT24AUG3050PE``   underlying, YY, MON, strike, CE/PE
* monthly future   ``NIFTY24AUGFUT``         underlying, YY, MON, FUT
* weekly option    ``NIFTY2481424200PE``     underlying, YY, M, DD, strike, CE/PE
                                             (M is 1-9 for Jan-Sep, O/N/D for Oct-Dec)

THE DECODER NEVER CHOOSES. An underlying may end in digits (``NIFTYNXT50``), so where the
underlying stops and the date starts is not decidable from the string alone, and a weekly
symbol has more than one reading (``NIFTY2481424200PE`` also reads as underlying
``NIFTY24``, expiry 2081-04-24, strike 200). ``decode`` returns every reading that is a
real calendar date with a strike that has no leading zero; ``resolve`` accepts the symbol
only when EXACTLY ONE listed contract matches across all of them. Zero or several matches
reject the row with the candidates named -- detection may infer, resolution may not
(rule 1).

THE MONTHLY FORM NAMES A MONTH, NOT A DAY. Its candidate carries the whole month as an
expiry range. Where an index has weekly contracts at the same strike in that month and
more than one of them is still listed on the trade date, that is several matches. They
are told apart only by the exchange's own display name (``fin_instrm_nm``, see
``resolve``); where no name decides (legacy-archive contracts carry none), the row is
rejected, never resolved to "the last one".
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

MONTHS = {
    name: number
    for number, name in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"),
        start=1,
    )
}
WEEKLY_MONTHS = {**{str(m): m for m in range(1, 10)}, "O": 10, "N": 11, "D": 12}

FUT = "FUT"
_MON = "|".join(MONTHS)
#: A strike never has a leading zero; "0500" is not a strike, so it is not a reading.
_STRIKE = r"[1-9]\d*(?:\.\d+)?"
_MONTHLY_OPTION = re.compile(rf"^(\d{{2}})({_MON})({_STRIKE})$")
_MONTHLY_FUTURE = re.compile(rf"^(\d{{2}})({_MON})$")
_WEEKLY_OPTION = re.compile(rf"^(\d{{2}})([1-9OND])(\d{{2}})({_STRIKE})$")
#: An underlying has at least one letter; NSE symbols use A-Z, 0-9, & and -.
_UNDERLYING = re.compile(r"^[A-Z0-9&\-]*[A-Z][A-Z0-9&\-]*$")
_SUFFIX = re.compile(r"^(?P<body>[A-Z0-9&.\-]+?)(?P<kind>CE|PE|FUT)$")


@dataclass(frozen=True, slots=True)
class SymbolCandidate:
    """One reading of a symbol. ``expiry_from == expiry_to`` for the weekly form; the
    monthly form spans its calendar month. ``option_type`` is CE, PE or FUT; ``strike``
    is None exactly for FUT."""

    underlying: str
    expiry_from: date
    expiry_to: date
    strike: Decimal | None
    option_type: str
    form: str  # "MONTHLY" | "WEEKLY"

    @property
    def expiry(self) -> date | None:
        return self.expiry_from if self.expiry_from == self.expiry_to else None

    def describe(self) -> str:
        when = (
            self.expiry_from.isoformat()
            if self.expiry
            else f"{self.expiry_from:%Y-%m} (any day)"
        )
        strike = "" if self.strike is None else f" {self.strike}"
        return f"{self.underlying} {when}{strike} {self.option_type}"


def _month_range(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def decode(symbol: str) -> list[SymbolCandidate]:
    """Every plausible reading of ``symbol``; empty when none. Never picks one."""
    text = (symbol or "").strip().upper()
    match = _SUFFIX.match(text)
    if not match:
        return []
    body, kind = match.group("body"), match.group("kind")
    out: list[SymbolCandidate] = []
    for cut in range(1, len(body)):
        underlying, rest = body[:cut], body[cut:]
        if not _UNDERLYING.match(underlying):
            continue
        if kind == FUT:
            m = _MONTHLY_FUTURE.match(rest)
            if m:
                first, last = _month_range(2000 + int(m.group(1)), MONTHS[m.group(2)])
                out.append(SymbolCandidate(underlying, first, last, None, FUT, "MONTHLY"))
            continue
        m = _MONTHLY_OPTION.match(rest)
        if m:
            first, last = _month_range(2000 + int(m.group(1)), MONTHS[m.group(2)])
            strike = Decimal(m.group(3))
            out.append(SymbolCandidate(underlying, first, last, strike, kind, "MONTHLY"))
        m = _WEEKLY_OPTION.match(rest)
        if m:
            try:
                expiry = date(2000 + int(m.group(1)), WEEKLY_MONTHS[m.group(2)], int(m.group(3)))
            except ValueError:
                continue  # 31 September, day 00 -- not a date, so not a reading
            strike = Decimal(m.group(4))
            out.append(SymbolCandidate(underlying, expiry, expiry, strike, kind, "WEEKLY"))
    return out


#: (exchange, candidate, trade_date) -> every listed contract matching that candidate.
Lookup = Callable[[str, SymbolCandidate, date], Sequence[Any]]


@dataclass(frozen=True, slots=True)
class Resolution:
    """``contract`` is set only when exactly one listed contract matched. Otherwise
    ``reason`` says why, naming the candidates (and the matches, when several)."""

    symbol: str
    exchange: str
    candidates: tuple[SymbolCandidate, ...]
    matches: tuple[Any, ...]
    contract: Any | None
    reason: str | None


def resolve(symbol: str, exchange: str, trade_date: date, lookup: Lookup) -> Resolution:
    """Accept ``symbol`` only on an exact name or an exactly-one candidate match.

    1. EXACT NAME. Among the contracts the candidates matched, if exactly one carries
       the exchange's own display name (``fin_instrm_nm``, the bhavcopy ``FinInstrmNm``,
       migration 065) equal to the note's symbol, that one is accepted -- even when
       several readings matched. This is what separates ``NIFTY24AUG24200PE`` (the month's
       last expiry) from a same-strike weekly listed the same month. The name only picks
       between contracts the identity already matched; it is never the key (rule 21(a)).
    2. Otherwise (no contract carries that name, or several do -- the legacy archive has
       no names at all) the exactly-one-candidate rule: one match is accepted, zero or
       several reject with the candidates named. Nothing else ever chooses.

    ``lookup`` results are de-duplicated on their ``id`` -- two readings that land on the
    same contract are one match, not two. A contract object without ``fin_instrm_nm``
    simply has no name.
    """
    candidates = tuple(decode(symbol))
    if not candidates:
        return Resolution(
            symbol, exchange, (), (), None,
            f"{symbol!r} is not a recognisable F&O contract symbol",
        )
    found: dict[str, Any] = {}
    for candidate in candidates:
        for contract in lookup(exchange, candidate, trade_date):
            found.setdefault(str(contract.id), contract)
    matches = tuple(found.values())
    named = "; ".join(c.describe() for c in candidates)
    wanted = (symbol or "").strip().upper()
    by_name = [
        m for m in matches if (getattr(m, "fin_instrm_nm", None) or "").strip().upper() == wanted
    ]
    if len(by_name) == 1:
        return Resolution(symbol, exchange, candidates, matches, by_name[0], None)
    if len(matches) == 1:
        return Resolution(symbol, exchange, candidates, matches, matches[0], None)
    if not matches:
        reason = (
            f"{symbol} on {exchange} {trade_date}: no listed contract matches "
            f"{len(candidates)} candidate(s) [{named}]"
        )
    else:
        listed = "; ".join(_describe_contract(m) for m in matches)
        reason = (
            f"{symbol} on {exchange} {trade_date}: {len(matches)} listed contracts match "
            f"[{listed}] from candidate(s) [{named}] -- not guessed"
        )
    return Resolution(symbol, exchange, candidates, matches, None, reason)


def _describe_contract(contract: Any) -> str:
    strike = "" if contract.strike is None else f" {contract.strike.normalize():f}"
    kind = contract.option_type or FUT
    return f"{contract.underlying} {contract.expiry.isoformat()}{strike} {kind}"
