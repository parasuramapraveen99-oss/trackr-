"""Intraday quotes from Yahoo Finance (https://github.com/ranaroussi/yfinance), for
"today, before tonight's bhavcopy settles" only -- see app/prices/live.py for the gate
that enforces that boundary. This module knows nothing about dates, staleness, or the
ledger; it converts ISINs to tickers and tickers to prices, and stops.

yfinance is unofficial scraping of Yahoo's own endpoints, not a licensed API, and Yahoo's
rate limits tightened considerably through 2025-2026. The one rule that keeps this usable:
ALWAYS ONE BATCHED CALL for every ticker in a refresh, never a per-ISIN loop -- a loop is
exactly the pattern that gets an IP blocked. A failure here (429, network error, an empty
frame) is routine, not exceptional: it degrades to an empty result, and the caller falls
through to bhavcopy or the unpriced bucket, the same way a bad bhavcopy fetch does NOT
degrade (that one alerts -- see app/prices/sources.py). Intraday flakiness self-heals on
the next read or at the latest, tonight's bhavcopy; it is not worth paging anyone over.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

logger = logging.getLogger("prices.yfinance_live")

#: NSE/BSE equities trade in paisa ticks -- there is no such thing as a real market
#: price with more than 2 decimal places. Yahoo's own backend stores prices at float32
#: precision (confirmed: casting a returned Close down to float32 and back round-trips
#: exactly), so a value like 2320.10 routinely arrives as 2320.10009765625 -- a binary
#: artifact from Yahoo's side, not from anything this module does. Quantizing recovers
#: the real tradable price instead of faithfully preserving someone else's rounding
#: noise; ROUND_HALF_UP to match app/engine/holdings.py's own convention.
_PAISA = Decimal("0.01")


def resolve_ticker(symbol: str, exchange: str | None) -> str:
    """NSE trading symbol -> Yahoo ticker. ``.BO`` only when the security is BSE-only;
    ``.NS`` for NSE and for ``BOTH`` -- deeper liquidity, the same reason
    app.prices.pipe.SOURCE_PRIORITY has NSE win a bhavcopy conflict."""
    suffix = ".BO" if exchange == "BSE" else ".NS"
    return f"{symbol}{suffix}"


@dataclass(frozen=True)
class LiveQuote:
    """A provider quote and the trading day it belongs to -- the bar's own date, not the
    time it was fetched. On a weekend the newest bar is Friday's."""

    price: Decimal
    session_date: date


def _closes(frame: Any, symbol: str) -> Any:
    """One ticker's non-null Close series out of a yfinance download frame, or None."""
    try:
        # yfinance keeps a (Ticker, Field) MultiIndex on frame.columns whenever
        # group_by="ticker" is used -- confirmed empirically this holds even for a
        # single-symbol request, it does NOT flatten to bare field columns. Symbol
        # *count* is not a reliable proxy for this shape (an earlier version of this
        # function assumed it was, and silently dropped every single-ISIN live fetch
        # via a caught KeyError -- exactly the common case of one holding's bhavcopy
        # lagging while everything else is already priced).
        multi = getattr(frame.columns, "nlevels", 1) > 1
        series = frame[symbol]["Close"] if multi else frame["Close"]
        closes = series.dropna()
    except (KeyError, IndexError, TypeError):
        return None
    return None if closes.empty else closes


def _paisa(value: Any) -> Decimal | None:
    try:
        # str() first, deliberately -- see CLAUDE.md rule 1. yfinance returns numpy
        # float64; Decimal(float) would smuggle in exactly the binary-approximation
        # error this codebase exists to avoid. Quantizing to paisa on top of that
        # cleans up Yahoo's own float32-precision noise -- see _PAISA above.
        return Decimal(str(value)).quantize(_PAISA, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None


def _bar_date(label: Any) -> date | None:
    try:
        return label.date() if hasattr(label, "date") else date.fromisoformat(str(label)[:10])
    except (TypeError, ValueError):
        return None


def _last_bar(frame: Any, symbol: str) -> LiveQuote | None:
    """The most recent non-null Close for one ticker, with its bar date. ``None`` on
    anything short of a clean value -- a delisted or mistyped ticker must degrade to "no
    live price", never a fabricated one."""
    closes = _closes(frame, symbol)
    if closes is None:
        return None
    price, on = _paisa(closes.iloc[-1]), _bar_date(closes.index[-1])
    return LiveQuote(price, on) if price is not None and on is not None else None


def _last_close(frame: Any, symbol: str) -> Decimal | None:
    bar = _last_bar(frame, symbol)
    return bar.price if bar else None


def closes_by_date(frame: Any, symbol: str) -> dict[date, Decimal]:
    """Every clean close for one ticker, by bar date -- for the evening check, which
    compares the provider's close for a session with that session's official close."""
    closes = _closes(frame, symbol)
    out: dict[date, Decimal] = {}
    if closes is None:
        return out
    for label, value in closes.items():
        price, on = _paisa(value), _bar_date(label)
        if price is not None and on is not None:
            out[on] = price
    return out


def download(symbols: list[str], period: str) -> Any:
    """ONE batched yfinance call for ``symbols``; None on any failure (never raises)."""
    try:
        import yfinance as yf

        return yf.download(
            symbols,
            period=period,
            group_by="ticker",
            progress=False,
            threads=True,
            auto_adjust=False,
        )
    except Exception:
        logger.info("yfinance fetch failed for %d ticker(s)", len(symbols), exc_info=True)
        return None


def fetch_quotes(tickers: dict[str, str]) -> dict[str, LiveQuote]:
    """ISIN -> Yahoo ticker in, ISIN -> quote with its session date out.

    One yfinance call for every ticker requested, however many ISINs that is. Any ISIN
    whose ticker didn't resolve to a usable price -- ticker wrong, Yahoo has nothing for
    it, or the whole call failed -- is simply absent from the result. Never raises.
    """
    if not tickers:
        return {}

    isins_by_ticker: dict[str, list[str]] = {}
    for isin, ticker in tickers.items():
        isins_by_ticker.setdefault(ticker, []).append(isin)
    symbols = sorted(isins_by_ticker)

    frame = download(symbols, period="1d")
    if frame is None or frame.empty:
        return {}

    out: dict[str, LiveQuote] = {}
    for symbol in symbols:
        quote = _last_bar(frame, symbol)
        if quote is None:
            continue
        for isin in isins_by_ticker[symbol]:
            out[isin] = quote
    return out


def fetch_live(tickers: dict[str, str]) -> dict[str, Decimal]:
    """``fetch_quotes`` without the session dates."""
    return {isin: quote.price for isin, quote in fetch_quotes(tickers).items()}
