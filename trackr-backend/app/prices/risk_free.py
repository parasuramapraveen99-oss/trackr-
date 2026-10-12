"""The risk-free return series: NSE Indices' Nifty 1D Rate Index (overnight TREPS).

Ops/cron only, like ``index_valuation``. Analytics reads the stored levels; no page load
fetches anything. Same publisher and request shape as the index P/E feed: the
historical-data report's price endpoint, which also carries rate indices (the total-return
endpoint answers ``[]`` for them -- measured 2026-10-10).
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from app.prices.index_sources import _format_request_date, _parse_response_date

SOURCE_URL = "https://www.niftyindices.com/BackPage/getHistoricaldatatabletoString"
REPORT_URL = "https://www.niftyindices.com/reports/historical-data"
RATE_INDEX = "NIFTY_1D_RATE"
REQUEST_NAME = "Nifty 1D Rate Index"
#: Requests are chunked; one year per call keeps each response small and bounded.
CHUNK_DAYS = 365


@dataclass(frozen=True)
class RateLevel:
    index_name: str
    level_date: date
    close: Decimal
    source_url: str = REPORT_URL


def parse_rows(payload) -> list[RateLevel]:
    """An unexpected shape or another index's rows is an error, never an empty success."""
    if isinstance(payload, dict) and "d" in payload:
        payload = payload["d"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, list):
        raise ValueError("Unexpected NSE rate index response")
    out = []
    for row in payload:
        name = str(row.get("INDEX_NAME") or row.get("Index Name") or "")
        if name.strip().upper() != REQUEST_NAME.upper():
            raise ValueError(f"Rate index response carries another index: {name!r}")
        try:
            close = Decimal(str(row["CLOSE"]).replace(",", ""))
        except (InvalidOperation, KeyError):
            continue  # "-" on a non-publication day; the level is simply absent
        if close.is_finite() and close > 0:
            out.append(RateLevel(RATE_INDEX, _parse_response_date(row["HistoricalDate"]), close))
    return out


def _fetch_chunk(start: date, end: date, opener=None) -> list[RateLevel]:
    cinfo = {
        "name": REQUEST_NAME,
        "indexName": REQUEST_NAME,
        "startDate": _format_request_date(start),
        "endDate": _format_request_date(end),
    }
    request = urllib.request.Request(
        SOURCE_URL,
        data=json.dumps({"cinfo": json.dumps(cinfo)}).encode(),
        headers={
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/json; charset=utf-8",
            "Referer": REPORT_URL,
            "Accept": "application/json",
        },
    )
    with (opener or urllib.request.urlopen)(request, timeout=30) as response:
        rows = parse_rows(json.loads(response.read()))
    if any(not start <= r.level_date <= end for r in rows):
        raise ValueError("NSE rate index returned dates outside the requested range")
    return rows


def fetch(start: date, end: date, opener=None) -> list[RateLevel]:
    out, cursor = [], start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=CHUNK_DAYS - 1))
        out.extend(_fetch_chunk(cursor, chunk_end, opener))
        cursor = chunk_end + timedelta(days=1)
    return out
