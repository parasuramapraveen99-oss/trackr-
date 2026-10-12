"""The one answer to "what is each holding worth right now?" -- audit gap D1/D2.

Every current-value surface (holdings, summary, performance and its XIRR terminal value,
today's provisional snapshot) must call this and nothing else, so the same ISIN cannot be
valued at two different prices on one screen.

Precedence, for ``as_of == today`` only:

    1. Bhavcopy SETTLED for today (exact date) -- official close, wins over everything.
    2. Live intraday quote (app.prices.live) for anything not yet settled.
    3. Most recent stored close inside the staleness window -- "carried".

For any other ``as_of`` there is no live quote: each ISIN takes its stored close on or
before ``as_of``, labelled ``settled`` when that close is dated ``as_of`` and ``carried``
when it is older.

An ISIN with no quote at all is absent from ``prices`` and is handled by the caller as
unpriced (named, never valued at zero -- CLAUDE.md rule 4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from app.db.prices import latest_price_dates, latest_prices, prices_on_date
from app.prices.live import ist_today, refresh_and_get

SOURCE_SETTLED = "settled"
SOURCE_LIVE = "live"
SOURCE_CARRIED = "carried"


@dataclass(frozen=True)
class CurrentQuote:
    """One holding's price, with where it came from and when it was observed.

    ``price_date`` is None only for a carried close whose row date could not be read
    (the verified alias fallback in ``latest_prices``). It is never invented.
    """

    price: Decimal
    price_date: date | None
    source: str


@dataclass(frozen=True)
class CurrentPrices:
    quotes: dict[str, CurrentQuote] = field(default_factory=dict)

    @property
    def prices(self) -> dict[str, Decimal]:
        """The bare price map that the engine's valuation functions take."""
        return {isin: quote.price for isin, quote in self.quotes.items()}

    @property
    def live_isins(self) -> set[str]:
        return {isin for isin, quote in self.quotes.items() if quote.source == SOURCE_LIVE}


def resolve_current_prices(conn: Any, isins: list[str], as_of: date) -> CurrentPrices:
    """Price every ISIN in ``isins`` under the single precedence above."""
    if not isins:
        return CurrentPrices()

    carried = latest_prices(conn, isins, as_of)
    carried_dates = latest_price_dates(conn, isins, as_of)
    quotes: dict[str, CurrentQuote] = {}
    for isin, price in carried.items():
        row_date = carried_dates.get(isin)
        source = SOURCE_SETTLED if row_date == as_of else SOURCE_CARRIED
        quotes[isin] = CurrentQuote(price=price, price_date=row_date, source=source)

    if as_of == ist_today():
        settled = prices_on_date(conn, isins, as_of)
        unsettled = [isin for isin in isins if isin not in settled]
        live = refresh_and_get(conn, unsettled) if unsettled else {}
        # Dict order encodes the precedence: later sources overwrite earlier ones.
        for isin, price in live.items():
            if isin not in settled:
                quotes[isin] = CurrentQuote(price=price, price_date=as_of, source=SOURCE_LIVE)
        for isin, price in settled.items():
            quotes[isin] = CurrentQuote(price=price, price_date=as_of, source=SOURCE_SETTLED)

    return CurrentPrices(quotes=quotes)
