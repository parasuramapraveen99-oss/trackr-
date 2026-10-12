"""Yahoo discovery preserves exact unclassified adjustment factors.

A factor cannot establish whether an event was a bonus or split. Candidates are
observations only; an issuer notice must establish the legal event type before any
ledger effect. Yahoo is not the historical fallback for NSE: NSE's notices API serves
arbitrary historical windows (re-probed live 2026-09-26), and the month-by-month tasks in
``app.db.reference_sync.schedule`` read them. The remaining uncovered gap is BSE-only
securities, which neither NSE nor this module covers.

Not gated on a rights setting (plan OD-2, 6 Oct 2026). ``YAHOO_DATA_RIGHTS_REFERENCE``,
when set, is provenance copied onto each observation by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol, runtime_checkable

#: The provider name on every Yahoo observation and dividend candidate, so a human can
#: see it did not come from a notice -- see corporate_actions.py's _FEED_SOURCE_NAME for
#: the equivalent on the NSE-notices path.
YFINANCE_SOURCE_NAME = "Yahoo Finance (yfinance)"


@dataclass(frozen=True, slots=True)
class YfinanceSplitCandidate:
    """One unclassified adjustment factor. ``app.db.corporate_actions.discover_from_yfinance``
    records it as an UNKNOWN_ADJUSTMENT observation and never as a corporate action."""

    isin: str
    symbol: str
    ex_date: date
    adjustment_factor: Decimal
    source_url: str


@runtime_checkable
class YfinanceSplitSource(Protocol):
    def splits(self, symbol: str) -> dict[date, float]:
        """Every split/bonus adjustment yfinance has on file for ``symbol`` (an NSE
        ticker, no exchange suffix), keyed by ex-date, valued as the shares-after-per-
        share-before multiplier -- e.g. ``{date(2023, 10, 17): 3.0}`` for GENSOL's real
        2:1 bonus (confirmed live, matches this system's own on-file record exactly)."""
        ...


class YfinanceSource:
    """The real ``yfinance`` library, kept behind ``YfinanceSplitSource`` for the same
    reason ``CorporateActionFeedSource`` wraps the NSE notices fetch: a test can inject
    a fake with no real network call and no dependency on Yahoo's own data still saying
    the same thing tomorrow."""

    def splits(self, symbol: str) -> dict[date, float]:
        import yfinance  # imported here, not at module load, so this stays optional

        series = yfinance.Ticker(f"{symbol}.NS").splits
        return {timestamp.date(): float(factor) for timestamp, factor in series.items()}

    def dividends(self, symbol: str) -> dict[date, float]:
        """Satisfies ``YfinanceDividendSource`` as well, so one injected fake can stand in
        for both in a test exactly as one real ``Ticker`` serves both here."""
        import yfinance

        series = yfinance.Ticker(f"{symbol}.NS").dividends
        return {timestamp.date(): float(amount) for timestamp, amount in series.items()}


def discover_candidates(
    source: YfinanceSplitSource, isin: str, symbol: str
) -> list[YfinanceSplitCandidate]:
    """Unclassified factors, including fractions and reverse adjustments, without rounding."""
    candidates = []
    for ex_date, factor in source.splits(symbol).items():
        value = Decimal(str(factor))
        if not value.is_finite() or value <= 0:
            raise ValueError("Invalid Yahoo corporate action adjustment factor")
        candidates.append(YfinanceSplitCandidate(
            isin=isin, symbol=symbol, ex_date=ex_date, adjustment_factor=value,
            source_url=f"https://finance.yahoo.com/quote/{symbol}.NS/history",
        ))
    return candidates


# ===========================================================================
# DIVIDENDS -- a discovery source alongside historical exchange notices.
# Neither discovery nor a declared amount proves receipt by any investor.
#
# WHAT IT CARRIES, AND WHAT IT THEREFORE CANNOT SAY. An ex-date and an amount per share.
# No record date, no payment date, no kind (interim/final/special), no currency marker,
# and above all NO EVIDENCE OF PAYMENT TO ANY PARTICULAR HOLDER. Every candidate from here
# is a DECLARATION and nothing more; the distance from there to "this investor received
# this cash" is the whole subject of migration 029's header and docs/INGESTION.md's
# Adapter 6.
#
# WHY EVERY CANDIDATE IS UNSPECIFIED. The kind is genuinely absent from the data rather
# than merely hard to parse, so inferring it -- from the month, from a position in the
# year, from the size relative to previous payouts -- would be exactly the invention
# CLAUDE.md forbids. UNSPECIFIED is the honest value, and
# ``app.db.dividend_events.match_unspecified`` is what reconciles such a candidate against
# a classified NSE notice for the same ex-date instead of duplicating it.
#
# THE ADJUSTMENT TRAP, and why this asks for actions rather than the plain series. Yahoo
# serves dividend amounts SPLIT-ADJUSTED by default in some views: a Rs 10 dividend
# declared before a 1:10 split can come back as Rs 1, silently, with no marker. That is a
# 10x error indistinguishable from a real small dividend. It is also the same class of
# hazard as the split-window guard in the projection path -- an amount and a quantity on
# different bases -- and neither this module nor the caller can detect it from one number
# in isolation. What defends against it here is CROSS-SOURCING: the NSE notice carries the
# as-declared amount, and a disagreement lands the event in DISPUTED rather than being
# averaged or preferred away. A dividend seen ONLY by this source stays DRAFT and is never
# projected from, which is the fail-closed answer to a hazard that cannot be detected
# locally.
# ===========================================================================


@dataclass(frozen=True, slots=True)
class YfinanceDividendCandidate:
    """One declared dividend, UNVERIFIED -- this module's boundary, same as its split twin.

    ``kind`` is deliberately absent rather than defaulted here: the caller supplies
    ``UNSPECIFIED`` when it builds a ``DividendEvent``, so nothing in this module has to
    import an engine enum to say "the source did not tell me".
    """

    isin: str
    symbol: str
    ex_date: date
    amount_per_share: Decimal
    source_url: str


@runtime_checkable
class YfinanceDividendSource(Protocol):
    def dividends(self, symbol: str) -> dict[date, float]:
        """Every dividend yfinance has on file for ``symbol`` (an NSE ticker, no exchange
        suffix), keyed by ex-date, valued as the amount per share in rupees."""
        ...


def discover_dividend_candidates(
    source: YfinanceDividendSource, isin: str, symbol: str
) -> list[YfinanceDividendCandidate]:
    """Every dividend ``source`` has on file for ``symbol``, as unverified candidates.

    A non-positive amount is skipped rather than recorded: Yahoo occasionally carries a
    zero row for a cancelled or restated declaration, and a zero dividend is not a
    dividend. It is skipped silently for the same reason the split path skips an
    unrepresentable factor -- no candidate beats a wrong one.

    THE FLOAT BOUNDARY IS HERE, DELIBERATELY AND ONLY HERE. ``yfinance`` returns floats;
    this system's engine may not contain one (``make lint`` fails the build over it). The
    conversion goes through ``str`` rather than ``Decimal(float)`` because the latter
    preserves the binary representation error -- ``Decimal(1.1)`` is
    ``1.100000000000000088817841970012523233890533447265625``, which would then be
    compared for exact equality against the NSE notice's clean ``1.1`` during
    corroboration and would never match, silently turning every cross-source agreement
    into a DISPUTED conflict. Quantising to paise afterwards is what makes the two
    sources' readings comparable at all.
    """
    candidates = []
    for ex_date, amount in source.dividends(symbol).items():
        if amount is None or amount <= 0:
            continue
        as_decimal = Decimal(str(amount)).quantize(Decimal("0.000001"))
        if as_decimal <= 0:
            continue
        candidates.append(
            YfinanceDividendCandidate(
                isin=isin,
                symbol=symbol,
                ex_date=ex_date,
                amount_per_share=as_decimal,
                source_url=f"https://finance.yahoo.com/quote/{symbol}.NS/history",
            )
        )
    return candidates
