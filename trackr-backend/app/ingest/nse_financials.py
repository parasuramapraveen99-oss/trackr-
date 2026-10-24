"""Audited annual results from NSE integrated-filing XBRL (SEBI LODR Reg 33).

Listing:  www.nseindia.com/api/integrated-filing-results?index=<equities|sme>&symbol=S
          &type=Integrated Filing- Financials
Filing:   the row's ``xbrl`` URL on nsearchives.nseindia.com.

Same access posture as the corporate-action feed (warmed cookie session, NSE Referer);
like every nseindia.com/api job it runs only where ``NSE_API_ENABLED`` is true.

Identity is the ISIN inside the filing, never the symbol: a filing whose ISIN differs
from the one asked for is rejected. Money facts are full rupees with no dimensions; the
year is the duration context of about a year ending on the period end, the balance sheet
is the instant context on that day.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from app.ingest.corporate_action_feed import BROWSER_HEADERS, NSE_HOMEPAGE_URL
from app.ingest.nse_session import cookie_session

logger = logging.getLogger(__name__)

LIST_URL = "https://www.nseindia.com/api/integrated-filing-results"
REFERER = "https://www.nseindia.com/companies-listing/corporate-integrated-filing"
FILING_TYPE = "Integrated Filing- Financials"
TIMEOUT_SECONDS = 30
SOURCE = "NSE_INTEGRATED_FILING_XBRL"
IST = ZoneInfo("Asia/Kolkata")

#: exchange_financials column -> XBRL elements, first found wins. Ind-AS names first,
#: then the IGAAP (Schedule III) names SME filers use. Duration facts read the year;
#: instant facts read the year-end balance sheet.
DURATION = {
    "revenue": ("RevenueFromOperations",),
    "other_income": ("OtherIncome",),
    "profit_before_tax": ("ProfitBeforeTax",),
    "finance_costs": ("FinanceCosts",),
    "net_profit": ("ProfitLossForPeriod", "ProfitLossForThePeriod"),
    "share_capital": ("PaidUpValueOfEquityShareCapital",),
    "face_value": ("FaceValueOfEquityShareCapital",),
    "basic_eps": ("BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",),
}
INSTANT = {
    "equity": ("Equity", "ShareholdersFunds"),
    "borrowings_current": ("BorrowingsCurrent", "ShortTermBorrowings", "DebtSecurities"),
    "borrowings_noncurrent": (
        "BorrowingsNoncurrent",
        "LongTermBorrowings",
        "BorrowingsOtherThanDebtSecurities",
    ),
    "share_capital": ("EquityShareCapital", "ShareCapital"),
}


#: SEBI integrated-filing taxonomy, from the entry-point namespace -> filing format.
FORMAT = re.compile(rb"sebi\.gov\.in/xbrl/IntegratedFinance_([A-Za-z]+)/")
FORMATS = {"IndAS": "INDAS", "OtherThanBank": "NONINDAS", "NBFC": "NBFC", "Banking": "BANKING"}


@dataclass(frozen=True, slots=True)
class Filing:
    isin: str
    basis: str  # CONSOLIDATED | STANDALONE
    audited: bool
    filing_format: str  # INDAS | NONINDAS | NBFC | BANKING
    period_start: date
    period_end: date
    values: dict[str, Decimal | None]
    source_url: str
    #: When the exchange received the filing (listing ``creation_Date``, IST).
    filed_at: datetime | None = None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_xbrl(content: bytes, source_url: str) -> Filing | None:
    """One full-year filing, or None when the file has no full-year context. Pure."""
    match = FORMAT.search(content)
    if match is None:
        return None
    filing_format = FORMATS.get(match.group(1).decode())
    if filing_format is None:
        return None
    root = ET.fromstring(content)
    contexts: dict[str, tuple[str | None, str | None, bool]] = {}
    for ctx in root:
        if _local(ctx.tag) != "context":
            continue
        dates = {
            _local(e.tag): e.text
            for e in ctx.iter()
            if _local(e.tag) in ("startDate", "endDate", "instant")
        }
        dimensional = any(_local(e.tag) in ("explicitMember", "typedMember") for e in ctx.iter())
        start, end = dates.get("startDate"), dates.get("endDate") or dates.get("instant")
        contexts[ctx.get("id", "")] = (start, end, dimensional)
    facts: dict[str, list[tuple[str, str]]] = {}
    for el in root:
        if el.get("contextRef") and el.text is not None:
            facts.setdefault(_local(el.tag), []).append((el.get("contextRef", ""), el.text.strip()))

    def text(name: str) -> str | None:
        return facts[name][0][1] if facts.get(name) else None

    audited = (text("WhetherResultsAreAuditedOrUnaudited") or "").lower() == "audited"
    basis = (text("NatureOfReportStandaloneConsolidated") or "").upper()
    isin = text("ISIN")
    if basis not in ("CONSOLIDATED", "STANDALONE") or not isin:
        return None
    years = [
        (start, end)
        for start, end, dim in contexts.values()
        if start
        and end
        and not dim
        and 360 <= (date.fromisoformat(end) - date.fromisoformat(start)).days <= 370
    ]
    if not years:
        return None
    start, end = max(years, key=lambda p: p[1])

    def value(name: str, want: tuple[str | None, str | None]) -> Decimal | None:
        for ref, raw in facts.get(name, []):
            s, e, dim = contexts.get(ref, (None, None, True))
            if not dim and (s, e) == want:
                try:
                    parsed = Decimal(raw)
                    return parsed if parsed.is_finite() else None
                except InvalidOperation:
                    return None
        return None

    def first(names: tuple[str, ...], period: tuple[str | None, str | None]) -> Decimal | None:
        return next((v for v in (value(n, period) for n in names) if v is not None), None)

    values = {column: first(names, (start, end)) for column, names in DURATION.items()}
    for column, names in INSTANT.items():
        found = first(names, (None, end))
        if found is not None or column not in values:
            values[column] = found
    return Filing(
        isin,
        basis,
        audited,
        filing_format,
        date.fromisoformat(start),
        date.fromisoformat(end),
        values,
        source_url,
    )


class NseFinancialsSource:
    """Lists a symbol's integrated filings and downloads the latest audited annual one."""

    def __init__(self, opener: Any = None):
        self._opener = opener if opener is not None else cookie_session().open
        self._warm = False

    def _get(self, url: str, referer: str) -> bytes:
        if not self._warm:
            warmup = urllib.request.Request(NSE_HOMEPAGE_URL, headers=BROWSER_HEADERS)
            self._opener(warmup, timeout=TIMEOUT_SECONDS).close()
            self._warm = True
        request = urllib.request.Request(url, headers={**BROWSER_HEADERS, "Referer": referer})
        with self._opener(request, timeout=TIMEOUT_SECONDS) as response:
            return response.read()

    def listings(self, symbol: str) -> list[dict]:
        rows: list[dict] = []
        for index in ("equities", "sme"):
            query = urllib.parse.urlencode({"index": index, "symbol": symbol, "type": FILING_TYPE})
            payload = json.loads(self._get(f"{LIST_URL}?{query}", REFERER))
            rows += payload.get("data", []) if isinstance(payload, dict) else []
            if rows:
                break
        return rows

    def latest_annual(self, isin: str, symbol: str) -> Filing | None:
        """Newest year-end filing for this ISIN: audited before unaudited, then
        consolidated before standalone."""

        def key(row: dict) -> tuple:
            day = datetime.strptime(row.get("qe_Date", "01-JAN-1900"), "%d-%b-%Y").date()
            return (
                day,
                (row.get("audited") or "") == "Audited",
                (row.get("consolidated") or "") == "Consolidated",
            )

        candidates = sorted(
            (
                r
                for r in self.listings(symbol)
                if r.get("xbrl") and r.get("qe_Date")
            ),
            key=key,
            reverse=True,
        )
        for row in candidates:
            filing = parse_xbrl(self._get(row["xbrl"], NSE_HOMEPAGE_URL), row["xbrl"])
            if filing is None:
                continue
            # A quarterly filing can contain the prior full year as a comparator.
            # That does not make it a new annual result for the listed period.
            listed_end = datetime.strptime(row["qe_Date"], "%d-%b-%Y").date()
            if filing.period_end != listed_end:
                continue
            filed = row.get("creation_Date") or row.get("broadcast_Date")
            if filed:
                stamp = datetime.strptime(filed, "%d-%b-%Y %H:%M:%S").replace(tzinfo=IST)
                filing = replace(filing, filed_at=stamp)
            if filing.isin != isin:
                logger.warning(
                    "nse_filing_isin_mismatch", extra={"asked": isin, "filed": filing.isin}
                )
                return None
            return filing
        return None
