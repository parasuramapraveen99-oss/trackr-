"""The one answer to "what is each holding worth right now?" -- audit gap D1/D2.

Every current-value surface (holdings, summary, performance and its XIRR terminal value,
today's provisional snapshot) must call this and nothing else, so the same ISIN cannot be
valued at two different prices on one screen.

Each ISIN is valued on its route (``app.prices.routing``, docs/MF_ETF_PLAN.md section 4):

EXCHANGE (stocks, ETFs). Precedence, for ``as_of == today`` only:

    1. Bhavcopy SETTLED for today (exact date) -- official close, wins over everything.
    2. Live intraday quote (app.prices.live) for anything not yet settled.
    3. Most recent stored close inside the staleness window -- "carried".

For any other ``as_of`` there is no live quote: each ISIN takes its stored close on or
before ``as_of``, labelled ``settled`` when that close is dated ``as_of`` and ``carried``
when it is older.

AMFI (mutual fund units, only while ``MF_VALUATION_ENABLED``): the newest AMFI NAV dated
on or before ``as_of`` and at most ``MAX_NAV_AGE_DAYS`` old, labelled ``nav``, with its
own NAV date. Never an exchange close, a live quote or a future NAV.

UNRESOLVED: no price. It is absent from ``prices`` and named by the caller as unpriced.

An ISIN with no quote at all is absent from ``prices`` and is handled by the caller as
unpriced (named, never valued at zero -- CLAUDE.md rule 4). Where the route explains why,
``unpriced_reasons`` says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from app.db.fund_reference import MAX_NAV_AGE_DAYS, latest_fund_navs, valuation_routes
from app.db.prices import latest_price_dates, latest_prices, prices_on_date
from app.prices.live import ist_today, refresh_and_get
from app.prices.routing import Route

SOURCE_SETTLED = "settled"
SOURCE_LIVE = "live"
SOURCE_CARRIED = "carried"
SOURCE_NAV = "nav"


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
    #: Why an unpriced ISIN that is NOT on the exchange route has no price.
    unpriced_reasons: dict[str, str] = field(default_factory=dict)

    @property
    def prices(self) -> dict[str, Decimal]:
        """The bare price map that the engine's valuation functions take."""
        return {isin: quote.price for isin, quote in self.quotes.items()}

    @property
    def live_isins(self) -> set[str]:
        return {isin for isin, quote in self.quotes.items() if quote.source == SOURCE_LIVE}


def resolve_current_prices(
    conn: Any, isins: list[str], as_of: date, *, live: bool = True
) -> CurrentPrices:
    """Price every ISIN in ``isins`` under the single precedence above.

    ``live=False`` skips the intraday quote step, so a figure built on the result is
    reproducible from stored closes alone (the investment summary uses this)."""
    from app.config import get_settings

    if not isins:
        return CurrentPrices()

    routes = valuation_routes(conn, isins)
    exchange = [isin for isin in isins if routes[isin].route == Route.EXCHANGE]
    funds = [isin for isin in isins if routes[isin].route == Route.AMFI]
    reasons = {
        isin: routes[isin].reason for isin in isins if routes[isin].route == Route.UNRESOLVED
    }

    quotes: dict[str, CurrentQuote] = {}
    carried = latest_prices(conn, exchange, as_of)
    carried_dates = latest_price_dates(conn, exchange, as_of)
    for isin, price in carried.items():
        row_date = carried_dates.get(isin)
        source = SOURCE_SETTLED if row_date == as_of else SOURCE_CARRIED
        quotes[isin] = CurrentQuote(price=price, price_date=row_date, source=source)

    if live and exchange and as_of == ist_today():
        settled = prices_on_date(conn, exchange, as_of)
        unsettled = [isin for isin in exchange if isin not in settled]
        live_quotes = refresh_and_get(conn, unsettled) if unsettled else {}
        # Dict order encodes the precedence: later sources overwrite earlier ones.
        for isin, price in live_quotes.items():
            if isin not in settled:
                quotes[isin] = CurrentQuote(price=price, price_date=as_of, source=SOURCE_LIVE)
        for isin, price in settled.items():
            quotes[isin] = CurrentQuote(price=price, price_date=as_of, source=SOURCE_SETTLED)

    if funds:
        if not get_settings().mf_valuation_enabled:
            reasons.update(
                {isin: "Mutual fund valuation is not enabled on this deployment" for isin in funds}
            )
        else:
            navs = latest_fund_navs(conn, funds, as_of)
            for isin in funds:
                nav = navs.get(isin)
                if nav is None:
                    reasons[isin] = (
                        f"No AMFI NAV dated on or within {MAX_NAV_AGE_DAYS} days before {as_of}"
                    )
                    continue
                quotes[isin] = CurrentQuote(
                    price=nav.nav, price_date=nav.nav_date, source=SOURCE_NAV
                )

    return CurrentPrices(quotes=quotes, unpriced_reasons=reasons)
