"""Dated P/E, P/B and yield from NSE Indices' own historical-data report.

Ops/cron only. Analytics reads the cache; no external fetch occurs on a page load.
"""

import json
import urllib.request
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from app.exchange_reference import urlopen
from app.prices.index_sources import TRI_INDICES, _format_request_date, _parse_response_date

SOURCE_URL = "https://www.niftyindices.com/BackPage/getpepbHistoricaldataDBtoString"
REPORT_URL = "https://www.niftyindices.com/reports/historical-data"


@dataclass(frozen=True)
class IndexValuation:
    index_name: str
    valuation_date: date
    pe: Decimal | None
    pb: Decimal | None
    dividend_yield: Decimal | None
    source_url: str = REPORT_URL


def _decimal(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def parse_rows(payload, index_name: str) -> list[IndexValuation]:
    """An unknown schema is an error, never a successful empty cache refresh."""
    if isinstance(payload, dict) and "d" in payload:
        payload = payload["d"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, list):
        raise ValueError("Unexpected NSE index valuation response")
    expected = TRI_INDICES[index_name].request_name.upper()
    out = []
    for row in payload:
        if str(row.get("Index Name", "")).upper() != expected:
            raise ValueError("NSE valuation response has a different index")
        on = _parse_response_date(row["DATE"])
        yield_pct = _decimal(row.get("divYield"))
        out.append(
            IndexValuation(
                index_name,
                on,
                _decimal(row.get("pe")),
                _decimal(row.get("pb")),
                yield_pct / 100 if yield_pct is not None else None,
            )
        )
    return out


def fetch(index_name: str, start: date, end: date, opener=None) -> list[IndexValuation]:
    name = TRI_INDICES[index_name].request_name
    body = {
        "cinfo": json.dumps(
            {
                "name": name,
                "indexName": name,
                "startDate": _format_request_date(start),
                "endDate": _format_request_date(end),
            }
        )
    }
    request = urllib.request.Request(
        SOURCE_URL,
        data=json.dumps(body).encode(),
        headers={
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/json; charset=utf-8",
            "Referer": REPORT_URL,
            "Accept": "application/json",
        },
    )
    with (opener or urlopen)(request, timeout=20) as response:
        rows = parse_rows(json.loads(response.read()), index_name)
    if any(not start <= row.valuation_date <= end for row in rows):
        raise ValueError("NSE valuation returned dates outside the requested range")
    return rows
