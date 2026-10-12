"""BSE's per-scrip industry classification, for ISINs NSE's Total Market list omits.

Owner-approved second sector source (10 Oct 2026, docs/ANALYTICS_COVERAGE.md). Two calls
shapes, both BSE's own site API: one list maps every active ISIN to its BSE scrip code,
then one header per scrip carries ``IndustryNew`` -- the harmonised "Sector" level, the
same level NSE's list calls ``Industry``. Called only for the few held or universe ISINs
NSE leaves unclassified, never for the whole market.
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Any

from app.prices.sources import BROWSER_HEADERS

SCRIP_LIST_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
    "?Group=&Scripcode=&industry=&segment=Equity&status=Active"
)
HEADER_URL = "https://api.bseindia.com/BseIndiaAPI/api/ComHeadernew/w?quotetype=EQ&scripcode={code}&seriesid="
HEADERS = {
    **BROWSER_HEADERS,
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
    "Accept": "application/json",
}


def _get(url: str, opener: Any = None) -> Any:
    request = urllib.request.Request(url, headers=HEADERS)
    with (opener or urllib.request.urlopen)(request, timeout=60) as response:
        return json.loads(response.read())


def scrip_codes(opener: Any = None) -> dict[str, str]:
    rows = _get(SCRIP_LIST_URL, opener)
    if not isinstance(rows, list):
        raise ValueError("Unexpected BSE scrip list response")
    return {
        r["ISIN_NUMBER"]: str(r["SCRIP_CD"])
        for r in rows
        if r.get("ISIN_NUMBER") and r.get("SCRIP_CD")
    }


def sectors(isins: list[str], opener: Any = None, pause: float = 0.4) -> dict[str, str]:
    """ISIN -> BSE ``IndustryNew`` for those BSE lists. Absent when BSE does not list the
    ISIN or gives no classification; a failure on one scrip skips only that scrip."""
    if not isins:
        return {}
    codes = scrip_codes(opener)
    out: dict[str, str] = {}
    for isin in isins:
        code = codes.get(isin)
        if not code:
            continue
        try:
            header = _get(HEADER_URL.format(code=code), opener)
        except Exception:
            continue
        if header.get("ISIN") not in (None, "", isin):
            continue  # the header describes another security; never attach it
        value = (header.get("IndustryNew") or "").strip()
        if value:
            out[isin] = value
        time.sleep(pause)
    return out
