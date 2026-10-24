"""Per-company fundamentals from Yahoo Finance, for the analytics module.

Same posture as ``app/prices/yfinance_live.py``: this module knows nothing about dates,
the ledger or an investor. It turns tickers into ratio rows and stops. A failure is
routine, not exceptional -- it degrades to a missing row and the caller shows reduced
coverage rather than a broken page.

THE ONE RULE THAT DIFFERS FROM LIVE PRICES, AND WHY
---------------------------------------------------
``yfinance_live`` insists on ONE BATCHED CALL for every ticker, because a per-ISIN loop is
what gets an IP blocked. **That rule cannot apply here.** ``Ticker.info`` is per-ticker;
there is no batched fundamentals endpoint to call. So the protections are different ones:

  - NEVER CALLED FROM A REQUEST PATH. Only the cron reaches this. A dashboard load must
    never wait on it, which is what makes the read path fast whatever Yahoo is doing.
  - BOUNDED BATCH per run, oldest-first, so a run is a predictable number of requests.
  - PER-ISIN PERSISTENCE by the caller, so a 429 halfway through keeps what it already got
    instead of discarding the run -- the same per-unit resumability the price backfill uses.
  - A LONG TTL. Fundamentals describe a filed quarter. Re-fetching them daily would be
    hammering a rate-limited endpoint to re-read a number that changes four times a year.

WHAT THE FEED ACTUALLY RETURNS, MEASURED
----------------------------------------
Across a 24-holding test book, ``info["returnOnEquity"]`` was NULL for 81% of weight --
including Reliance. ``interestCoverage`` does not exist in ``info`` at all. So for most of
the book this is not a fundamentals source, it is a STATEMENTS source that we compute
ratios from, and which of the two produced a number is recorded in ``roe_basis`` rather
than smoothed over. ``info["beta"]`` is present but unusable (it returned -0.213 for one
Indian mid-cap), so beta is computed from our own price history instead and is not read
here at all.
"""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

logger = logging.getLogger("prices.yfinance_fundamentals")

#: Ratios are stored at six decimal places; more is noise from a vendor's float64.
_QUANTUM = Decimal("0.000001")


@dataclass(frozen=True, slots=True)
class FundamentalsRow:
    """One scrip's ratios. Every field optional -- absent means UNKNOWN, never zero."""

    symbol: str
    verification_reason: str | None = None
    #: When the identity check fails but the vendor named a company: that name, and the
    #: ratios it supplied, kept for the investor to confirm. Never stored as fundamentals.
    vendor_name: str | None = None
    candidate: FundamentalsRow | None = None
    pe: Decimal | None = None
    pb: Decimal | None = None
    roe: Decimal | None = None
    net_margin: Decimal | None = None
    debt_to_equity: Decimal | None = None
    interest_cover: Decimal | None = None
    dividend_yield: Decimal | None = None
    sales_growth_1y: Decimal | None = None
    profit_growth_1y: Decimal | None = None
    market_cap: Decimal | None = None
    roe_basis: str | None = None
    fiscal_period: date | None = None
    #: EBIT / (stockholders' equity + total debt), from the same annual statements.
    roce: Decimal | None = None


def _dec(value: Any) -> Decimal | None:
    """Vendor number -> Decimal, via ``str``. CLAUDE.md rule 1.

    yfinance hands back numpy float64. ``Decimal(float)`` would smuggle in exactly the
    binary-approximation error this codebase exists to avoid, so the conversion goes
    through ``str`` and is then quantised.
    """
    if value is None:
        return None
    try:
        as_float = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(as_float) or math.isinf(as_float):
        return None
    try:
        return Decimal(str(as_float)).quantize(_QUANTUM)
    except (InvalidOperation, ValueError):
        return None


def _percent(value: Any) -> Decimal | None:
    """A vendor PERCENT as a fraction or ratio (3.86 -> 0.0386)."""
    number = _dec(value)
    return (number / 100).quantize(_QUANTUM) if number is not None else None


def _statement_value(frame: Any, row_label: str, column: int = 0) -> Decimal | None:
    """One cell from a yfinance statements frame, or None if it is not there."""
    try:
        if frame is None or getattr(frame, "empty", True) or row_label not in frame.index:
            return None
        series = frame.loc[row_label]
        if column >= len(series):
            return None
        return _dec(series.iloc[column])
    except Exception:  # a statements frame is frequently absent or oddly shaped
        return None


def _growth(frame: Any, row_label: str) -> Decimal | None:
    """Year-on-year growth from two consecutive annual columns.

    Requires a POSITIVE base. Growth off a negative or zero prior year is not a percentage
    anyone can read -- "profit grew 300%" from a loss is arithmetic, not information.
    """
    current = _statement_value(frame, row_label, 0)
    prior = _statement_value(frame, row_label, 1)
    if current is None or prior is None or prior <= 0:
        return None
    return (current / prior - 1).quantize(_QUANTUM)


def company_key(name: str | None) -> str | None:
    """A legal name reduced to what identifies the company, for an EXACT comparison.

    Case, punctuation and "&" vs "and" are presentation, not identity: the exchange master
    says "ADITYA BIRLA FASHION AND RETAIL LIMITED", Yahoo "Aditya Birla Fashion and Retail
    Limited", and "SKM EGG PRODUCTS EXPORT INDIA" vs "SKM Egg Products Export (India)".
    Nothing else is normalised -- no fuzzy match, no dropped words -- so "Kalyani Cast-Tech"
    can never equal "Kalyani Commercials".
    """
    if not name:
        return None
    # Accents are presentation (Yahoo "Nestlé", NSE "Nestle"); apostrophes join words
    # ("Children's" = "Childrens"); "(I)" is NSE's abbreviation for "(India)".
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().casefold()
    text = text.replace("&", " and ").replace("'", "").replace("(i)", " india ")
    words = re.sub(r"[^a-z0-9]+", " ", text).split()
    # Pure abbreviations NSE's index files use, and the s/z spelling variant
    # ("Fertilizers" vs "Fertilisers"). Only formatting: no word is dropped or guessed,
    # except a leading "The", which Yahoo prefixes and NSE omits.
    expand = {"ltd": "limited", "co": "company", "corp": "corporation"}
    words = [expand.get(w, w).replace("z", "s") for w in words]
    if words and words[0] == "the":
        words = words[1:]
    # Spacing is not identity: "One 97" = "One97", "Path Labs" = "PathLabs".
    return "".join(words) or None


def identity_check(info: dict, expected_name: str | None) -> str | None:
    """None when the vendor record is the expected company, else the reason it is not.

    Measured 2026-10-09 on yfinance 1.7.0: Yahoo carries NO ISIN for Indian equities
    (``Ticker.isin`` is "-" for IEX.NS, BLS.NS, GILLETTE.NS), so an ISIN-equality guard
    rejects every company. What it does carry is the issuer's legal name, and the exchange
    master gives ours, keyed by ISIN. A symbol resolving to another listed company --
    KALYANI.NS is Kalyani Commercials, while ISIN INE0N6U01018 is Kalyani Cast-Tech
    (BSE 544023) -- fails this check, and its ratios are never attached.
    """
    expected = company_key(expected_name)
    vendor = company_key(info.get("longName"))
    if expected is None or vendor is None:
        return "identity_unverified"
    return None if vendor == expected else "identity_mismatch"


def fetch_one(
    symbol: str,
    ticker_factory: Any = None,
    expected_name: str | None = None,
) -> FundamentalsRow:
    """Ratios for one Yahoo ticker. Never raises; an unusable field is simply absent.

    UNITS ARE NORMALISED HERE, at the boundary, to the ones the rest of the system declares
    (``app/narrative/registry.py``): Yahoo's ``dividendYield`` is a PERCENT (3.86 for a
    3.86% yield) and ``debtToEquity`` is a PERCENT (81.8 for 0.818x). Stored, they are a
    fraction and a ratio, like ``roe`` and ``net_margin`` already are.
    """
    row = FundamentalsRow(symbol=symbol)
    try:
        if ticker_factory is None:
            import yfinance as yf

            ticker_factory = yf.Ticker
        ticker = ticker_factory(symbol)
        info = ticker.info or {}
    except Exception:
        logger.info("fundamentals fetch failed for %s", symbol, exc_info=True)
        return row

    # Symbols are reused across exchanges and listings. Never attach another company's
    # ratios to an ISIN merely because its display symbol happens to resolve in Yahoo.
    reason = identity_check(info, expected_name)
    ratios = _ratios(symbol, info, ticker) if reason is None or info.get("longName") else None
    if reason is not None:
        # Withheld, not discarded: the vendor's name and ratios become a question for the
        # investor (vendor_identity_review). Nothing here attaches them to the ISIN.
        return FundamentalsRow(
            symbol=symbol,
            verification_reason=reason,
            vendor_name=info.get("longName"),
            candidate=ratios,
        )
    return ratios


def _ratios(symbol: str, info: dict, ticker: Any) -> FundamentalsRow:
    """Every ratio field from one vendor record; identity is the caller's question."""
    roe = _dec(info.get("returnOnEquity"))
    roe_basis = "vendor" if roe is not None else None
    interest_cover = roce = None
    sales_growth = profit_growth = None
    fiscal_period: date | None = None

    try:
        financials = ticker.financials
        balance = ticker.balance_sheet

        if roe is None:
            net_income = _statement_value(financials, "Net Income")
            equity = _statement_value(balance, "Stockholders Equity")
            if net_income is not None and equity and equity > 0:
                roe = (net_income / equity).quantize(_QUANTUM)
                roe_basis = "derived_statements"

        ebit = _statement_value(financials, "EBIT")
        interest = _statement_value(financials, "Interest Expense")
        if ebit is not None and interest and interest > 0:
            interest_cover = (ebit / interest).quantize(_QUANTUM)
        equity = _statement_value(balance, "Stockholders Equity")
        debt = _statement_value(balance, "Total Debt")
        if ebit is not None and equity is not None and equity > 0:
            roce = (ebit / (equity + (debt or Decimal(0)))).quantize(_QUANTUM)

        sales_growth = _growth(financials, "Total Revenue")
        profit_growth = _growth(financials, "Net Income")

        if financials is not None and not getattr(financials, "empty", True):
            columns = list(financials.columns)
            if columns:
                stamp = columns[0]
                fiscal_period = getattr(stamp, "date", lambda: None)()
    except Exception:
        logger.info("statements unavailable for %s", symbol, exc_info=True)

    return FundamentalsRow(
        symbol=symbol,
        pe=_dec(info.get("trailingPE")),
        pb=_dec(info.get("priceToBook")),
        roe=roe,
        net_margin=_dec(info.get("profitMargins")),
        debt_to_equity=_percent(info.get("debtToEquity")),
        interest_cover=interest_cover,
        dividend_yield=_percent(info.get("dividendYield")),
        sales_growth_1y=sales_growth,
        profit_growth_1y=profit_growth,
        market_cap=_dec(info.get("marketCap")),
        roe_basis=roe_basis,
        fiscal_period=fiscal_period,
        roce=roce,
    )


def fetch_many(
    tickers: dict[str, str],
    ticker_factory: Any = None,
    on_row: Any = None,
    expected_names: dict[str, str | None] | None = None,
) -> dict[str, FundamentalsRow]:
    """ISIN -> ticker in, ISIN -> row out. Sequential, because ``info`` cannot be batched.

    ``on_row`` is called with (isin, row) after each fetch so the caller can persist as it
    goes. That is what makes a rate-limited run resumable: whatever was fetched before the
    wall is already stored, and the next run picks a different oldest-first slice.
    """
    out: dict[str, FundamentalsRow] = {}
    for isin, symbol in tickers.items():
        row = fetch_one(
            symbol,
            ticker_factory=ticker_factory,
            expected_name=(expected_names or {}).get(isin),
        )
        out[isin] = row
        if on_row is not None:
            on_row(isin, row)
    return out
