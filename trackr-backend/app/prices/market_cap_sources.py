"""Where the large/mid/small-cap classification comes from -- FEATURE_BACKLOG.md's
"Market-cap breakdown" line, the other half of "Sector / market-cap breakdown".

SEBI's circular dated 6 Oct 2017 directs AMFI to prepare and publish this list twice a
year, in consultation with SEBI and the exchanges, from BSE/NSE/MSEI six-month average
market-cap data. It is the mandated, official classification -- the same standing NSE's
Industry list has for sector (``app.prices.sector_sources``).

    https://www.amfiindia.com/otherdata/categorisation-of-stocks

BOUNDARY, STATED RATHER THAN HIDDEN: unlike NSE's sector CSV, which lives at one stable
undated URL, AMFI's file name embeds the six-month period end date
(``AverageMarketCapitalization30Jun2026.xlsx``) and the naming SHAPE has changed before
(pre-2024 files used a longer name ending in the same date). There is no formulaic
"latest" URL to template the way ``app.prices.sources`` templates bhavcopy's daily URL.
``AMFI_MARKET_CAP_URL`` below is the current period's URL and MUST be updated roughly
every six months (each new Jan/Jul) by checking the categorisation-of-stocks page above
for the current file link -- this is a genuinely different staleness profile from
sector's, not an oversight.
"""

from __future__ import annotations

import io
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

import openpyxl

from app.prices.sources import BROWSER_HEADERS, TIMEOUT_SECONDS, _ssl_context

#: Update every ~6 months -- see the module docstring's BOUNDARY note. Verified live
#: against AMFI's site: 5,427 ISINs, Large/Mid/Small Cap boundaries at rank 100/101 and
#: 250/251.
AMFI_MARKET_CAP_URL = (
    "https://portal.amfiindia.com/spages/AverageMarketCapitalization30Jun2026.xlsx"
)

#: The two columns this parser actually depends on. Missing either is the clearest
#: signal AMFI reshaped the file under us.
_ISIN_COLUMN = "ISIN"
_CATEGORY_COLUMN = "Categorization as per SEBI Circular dated Oct 6, 2017"
_REQUIRED_COLUMNS = {_ISIN_COLUMN, _CATEGORY_COLUMN}

#: The only three values AMFI's classification column carries -- confirmed against a
#: live pull. A row with any other value is treated as a format break, not silently kept
#: (CLAUDE.md rule 4's "never default" applies to reference data as much as prices).
VALID_BUCKETS = {"Large Cap", "Mid Cap", "Small Cap"}


@dataclass(frozen=True, slots=True)
class MarketCapRow:
    isin: str
    bucket: str
    company_name: str


class MarketCapSourceFetchFailed(Exception):
    """The request should have worked and did not -- network error or non-200. Alerts."""


class MarketCapSourceFormatError(Exception):
    """The response parsed as a workbook but is missing a column, or a category value
    this parser doesn't recognise -- the clearest signal AMFI changed the file's shape or
    the classification's value set."""


class AmfiMarketCapSource:
    def __init__(self, opener: Any = None):
        self._opener = opener or urllib.request.urlopen

    def fetch(self) -> bytes:
        request = urllib.request.Request(AMFI_MARKET_CAP_URL, headers=BROWSER_HEADERS)
        try:
            with self._opener(request, timeout=TIMEOUT_SECONDS, context=_ssl_context()) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise MarketCapSourceFetchFailed(f"AMFI market-cap list: HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MarketCapSourceFetchFailed(f"AMFI market-cap list: {type(exc).__name__}") from exc


def amfi_market_cap_source(opener: Any = None) -> AmfiMarketCapSource:
    return AmfiMarketCapSource(opener)


def parse_amfi_market_cap_list(payload: bytes) -> list[MarketCapRow]:
    """Rows from AMFI's large/mid/small-cap categorisation workbook.

    A row with a blank ISIN or category value is skipped (rare footer/note artefacts in
    these files) rather than raising -- only a missing COLUMN, or a category value
    outside ``VALID_BUCKETS``, is a format break; a missing individual value is not this
    parser's problem to police.

    Raises:
        MarketCapSourceFormatError: the header is missing ``ISIN`` or the categorisation
            column, or a row's category is not one of ``VALID_BUCKETS``.
    """
    workbook = openpyxl.load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
    sheet = workbook.worksheets[0]
    rows = sheet.iter_rows(values_only=True)

    header: list[str] = []
    for candidate in rows:
        header = [str(cell).strip() if cell is not None else "" for cell in candidate]
        if _REQUIRED_COLUMNS <= set(header):
            break
    else:
        raise MarketCapSourceFormatError(
            f"AMFI market-cap list: missing column(s) {sorted(_REQUIRED_COLUMNS)}"
        )

    isin_idx = header.index(_ISIN_COLUMN)
    category_idx = header.index(_CATEGORY_COLUMN)
    name_idx = header.index("Company name") if "Company name" in header else None

    out: list[MarketCapRow] = []
    for record in rows:
        if len(record) <= max(isin_idx, category_idx):
            continue
        isin = str(record[isin_idx]).strip() if record[isin_idx] is not None else ""
        bucket = str(record[category_idx]).strip() if record[category_idx] is not None else ""
        if not isin or not bucket:
            continue
        if bucket not in VALID_BUCKETS:
            raise MarketCapSourceFormatError(
                f"AMFI market-cap list: unrecognised category {bucket!r} for ISIN {isin}"
            )
        company_name = (
            str(record[name_idx]).strip()
            if name_idx is not None and record[name_idx] is not None
            else ""
        )
        out.append(MarketCapRow(isin=isin, bucket=bucket, company_name=company_name))
    return out
