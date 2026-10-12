"""Orchestrates the intraday live-price cache: check it, refresh what's stale/missing from
yfinance, store, return. The role this plays relative to `app.db.live_prices` /
`app.prices.yfinance_live` mirrors `app.prices.pipe` relative to `app.db.prices` /
`app.prices.sources` -- source module fetches, db module persists, this module is the glue
a caller actually reaches for.

The one rule that makes the whole live-pricing feature safe, stated once here rather than
left implicit at every call site: this is consulted ONLY for `as_of == ist_today()`, and
ONLY to fill ISINs bhavcopy has no row for yet (see app/routers/holdings.py::_portfolio).
Every other date is bhavcopy-only, unconditionally -- this module has no opinion on, and no
path that could touch, a historical valuation.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app import securities
from app.db.live_prices import get_fresh_quotes, upsert_live_prices
from app.db.prices import latest_price_dates, latest_prices
from app.db.quote_mapping import usable_tickers
from app.prices.yfinance_live import fetch_quotes, resolve_ticker

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger("prices.live")

#: The widest normal one-day move: 20% is the exchanges' largest price band. A live quote
#: further than this from the ISIN's own last exchange close is taken to be ANOTHER
#: security's quote -- a ticker is a symbol, and a symbol is not unique across exchanges
#: (KALYANI.NS is Kalyani Commercials; the ISIN held was Kalyani Cast-Tech, BSE-only, and
#: was valued at a seventh of its price, 2026-10-10) -- and is dropped, unless a corporate
#: action went ex in between, when a large drop is the expected shape.
MAX_LIVE_MOVE = Decimal("0.20")


def ist_now() -> datetime:
    return datetime.now(IST)


def ist_today() -> date:
    """NSE's trading day, not the server's. The app runs in UTC (Vercel); naive
    ``date.today()`` would be wrong for the ~5.5 hours a day IST and UTC disagree on the
    date."""
    return ist_now().date()


def refresh_and_get(conn: Any, isins: list[str]) -> dict[str, Decimal]:
    """Indicative prices for ``isins`` -- today's session only, through each ISIN's quote
    mapping, refreshing from the provider whatever the cache doesn't cover.

    Three gates, each falling back to the ISIN's official close (``app.prices.current``),
    never to zero and never removing a holding:

    - MAPPING. The ticker comes from ``quote_mapping`` (the exchange's own bhavcopy row);
      a BLOCKED mapping -- one the evening check proved wrong -- gets no quote at all. An
      ISIN with no mapping on file yet falls back to its symbol on the exchange its closes
      come from, and is screened by the plausibility guard below.
    - SESSION. A quote counts only if the provider's bar is dated today (IST). On a weekend
      or holiday the newest bar is the last session's, whose official close is the price.
    - PLAUSIBILITY. ``_plausible``: no more than ``MAX_LIVE_MOVE`` from the last close,
      unless a corporate action went ex in between.

    Never polled by itself: this runs on demand (a dashboard asking for as_of == today)
    and from the intraday cron, and the cache TTL bounds provider calls either way.
    """
    if not isins:
        return {}
    today = ist_today()
    mapped, blocked = usable_tickers(conn, isins)
    wanted = [isin for isin in isins if isin not in blocked]

    cached = get_fresh_quotes(conn, wanted)
    todays = {isin: price for isin, (price, on) in cached.items() if on == today}
    still_missing = [isin for isin in wanted if isin not in cached]
    if not still_missing:
        return _plausible(conn, todays)

    tickers = {isin: mapped[isin] for isin in still_missing if isin in mapped}
    unmapped = [isin for isin in still_missing if isin not in mapped]
    if unmapped:
        meta = securities.lookup(conn, unmapped)
        traded_on = _exchange_from_prices(conn, unmapped)
        tickers.update(
            {
                isin: resolve_ticker(row["symbol"], row["exchange"] or traded_on.get(isin))
                for isin in unmapped
                if (row := meta.get(isin)) and row.get("symbol")
            }
        )
    quotes = fetch_quotes(tickers)
    plausible = _plausible(conn, {isin: q.price for isin, q in quotes.items()})
    if plausible:
        # Cached with its session, today's or not: an earlier session's bar is remembered
        # for the TTL as "nothing newer yet" so a weekend load does not re-ask the provider.
        upsert_live_prices(
            conn, plausible, ist_now(), {isin: quotes[isin].session_date for isin in plausible}
        )
    fresh = {isin: price for isin, price in plausible.items() if quotes[isin].session_date == today}
    # Cached quotes are screened too: one stored before these checks existed may be wrong.
    return {**_plausible(conn, todays), **fresh}


def _exchange_from_prices(conn: Any, isins: list[str]) -> dict[str, str]:
    """Where each ISIN's exchange closes come from, for a security whose ``exchange`` is
    unset -- so a BSE-only stock is quoted as ``.BO``, not guessed onto NSE where its
    symbol may belong to a different company."""
    rows = conn.execute(
        "SELECT isin, bool_or(source = 'NSE_BHAVCOPY'), bool_or(source = 'BSE_BHAVCOPY') "
        "FROM price WHERE isin = ANY(%s) GROUP BY isin",
        (isins,),
    ).fetchall()
    return {
        isin: "BOTH" if nse and bse else "NSE" if nse else "BSE"
        for isin, nse, bse in rows
        if nse or bse
    }


def _plausible(conn: Any, quotes: dict[str, Decimal]) -> dict[str, Decimal]:
    """``quotes`` without any that cannot be this ISIN's price: more than
    ``MAX_LIVE_MOVE`` from its last exchange close with no corporate action ex since.
    A dropped ISIN falls back to that close (``app.prices.current``) -- never to zero."""
    if not quotes:
        return quotes
    today = ist_today()
    isins = list(quotes)
    closes = latest_prices(conn, isins, today)
    close_dates = latest_price_dates(conn, isins, today)
    ex_dates: dict[str, list[date]] = {}
    for isin, ex_date in conn.execute(
        "SELECT isin, ex_date FROM corporate_action WHERE isin = ANY(%s) AND ex_date <= %s",
        (isins, today),
    ).fetchall():
        ex_dates.setdefault(isin, []).append(ex_date)

    out: dict[str, Decimal] = {}
    for isin, price in quotes.items():
        close, since = closes.get(isin), close_dates.get(isin)
        if close is None or close <= 0 or abs(price / close - 1) <= MAX_LIVE_MOVE:
            out[isin] = price
        elif since is not None and any(d > since for d in ex_dates.get(isin, [])):
            out[isin] = price  # ex-date since the close: a big move is the expected shape
        else:
            logger.warning(
                "live_quote_rejected",
                extra={"isin": isin, "live": str(price), "last_close": str(close)},
            )
    return out
