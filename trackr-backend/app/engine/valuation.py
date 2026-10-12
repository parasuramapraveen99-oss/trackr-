"""Valuation -- docs/ENGINE_SPEC.md section 2.

Where prices meet holdings, and nowhere else. Kept separate from ``compute_holdings``
because prices are a SEPARATE PIPE: client transactions arrive on trade days from files
and email, prices arrive every day from the exchange. Neither blocks the other, and on a
day with no trades the price job still moves the dashboard.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

from app.engine.errors import MissingPriceError
from app.engine.types import ZERO, Holding

#: 365.25, not 365 -- ENGINE_SPEC section 5. Averages leap years into the year length
#: rather than biasing every CAGR very slightly high.
DAYS_PER_YEAR = Decimal("365.25")


def _cagr(holding: Holding, market_value: Decimal, as_of: date | None) -> Decimal | None:
    """Per-holding CAGR -- ENGINE_SPEC section 5.

    ``cagr = (end_value / start_value) ** (365.25 / days) - 1``, valid only as a
    single-lump-sum approximation. ``start_value`` is the current weighted-average
    ``invested`` cost pool and ``start_date`` is ``first_buy_date`` (the earliest
    acquisition) -- treating the whole blended cost as if it had all gone in on that
    first date, since a real position built from several buys has no single "start"
    that isn't itself an approximation.

    None -- never zero, never a guess -- whenever the inputs cannot support an answer:
    no ``as_of`` (caller didn't ask for a dated valuation), no ``first_buy_date`` (a
    position built entirely from corporate actions, with no acquisition to anchor a
    start date), a non-positive cost basis (undefined base for a fractional power), or
    zero holding days (bought on ``as_of`` itself).
    """
    if as_of is None or holding.first_buy_date is None or holding.invested <= ZERO:
        return None
    days = (as_of - holding.first_buy_date).days
    if days <= 0:
        return None
    return (market_value / holding.invested) ** (DAYS_PER_YEAR / Decimal(days)) - 1


def value_holdings(
    holdings: list[Holding],
    prices: dict[str, Decimal],
    as_of: date | None = None,
    previous_closes: dict[str, Decimal] | None = None,
) -> list[Holding]:
    """Attach price-derived fields to holdings.

    ::

        market_value   = quantity * last_price
        unrealised     = market_value - invested
        unrealised_pct = unrealised / invested        (None when invested is 0)
        weight_pct     = market_value / total market_value
        cagr            = see `_cagr` -- None unless `as_of` is supplied
        day_change_pct  = (last_price - previous_close) / previous_close

    ``previous_closes`` is optional and keyed the same way as ``prices``. It is a
    SEPARATE mapping rather than a second lookup inside this function because this module
    is given prices, never asked to fetch them -- the caller decides what "the previous
    close" means (``app.routers.holdings._portfolio`` walks back from the day before
    ``as_of``, so a weekend or holiday resolves to the last real trading day). An ISIN
    absent from it simply has no ``day_change_pct``, which is not an error.

    ``as_of`` is optional and defaults to ``None`` (rather than ``date.today()`` --
    the engine never reads the clock, see ``compute_snapshot``'s docstring) purely so
    CAGR has a dated reference point; every other field above is unaffected by it.

    Raises:
        MissingPriceError: naming EVERY unpriced ISIN, not just the first. Someone has to
            go and find those prices; handing them one ISIN per attempt wastes their day.

    A missing price is never substituted with zero. Valuing an unpriced holding at zero
    understates XIRR catastrophically and does it silently, which is the worst
    combination. Two holdings in the golden fixture trade only on BSE, so a price pipe
    that fetched NSE alone would land exactly here.

    Staleness is not checked here: this takes a flat ISIN-to-price mapping with no dates.
    Enforcing MAX_PRICE_STALENESS_DAYS belongs to the price pipe that builds the mapping.
    """
    missing = [h.isin for h in holdings if h.isin not in prices]
    if missing:
        raise MissingPriceError(missing)

    previous_closes = previous_closes or {}
    valued = []
    for holding in holdings:
        last_price = prices[holding.isin]
        market_value = holding.quantity * last_price
        unrealised = market_value - holding.invested
        previous_close = previous_closes.get(holding.isin)
        valued.append(
            replace(
                holding,
                last_price=last_price,
                market_value=market_value,
                unrealised=unrealised,
                unrealised_pct=(
                    unrealised / holding.invested if holding.invested != ZERO else None
                ),
                cagr=_cagr(holding, market_value, as_of),
                day_change_pct=(
                    (last_price - previous_close) / previous_close
                    if previous_close is not None and previous_close > ZERO
                    else None
                ),
            )
        )

    total = sum((h.market_value for h in valued), ZERO)
    if total == ZERO:
        return valued
    return [replace(h, weight_pct=h.market_value / total) for h in valued]


def total_market_value(holdings: list[Holding], prices: dict[str, Decimal]) -> Decimal:
    """Market value of everything held. The terminal cash flow of an XIRR series.

    This is why a tradebook alone cannot produce XIRR: trade files record what already
    happened and have no idea what an unsold position is worth today.
    """
    return sum((h.market_value for h in value_holdings(holdings, prices)), ZERO)
