"""Where sector/industry classification comes from -- FEATURE_BACKLOG.md's "Allocation
(by holding, sector)" line.

NSE publishes its own macro-sector classification ("Industry") as part of every index
constituent CSV it maintains, keyed by ISIN. We use the NIFTY Total Market list rather
than a narrower index (NIFTY 500) because it is a strict superset for the same fetch
cost -- broader real-world coverage of a retail portfolio's mid/small-caps.

    https://nsearchives.nseindia.com/content/indices/ind_niftytotalmarket_list.csv

Same host and anti-bot header requirement as bhavcopy (``app.prices.sources``), so this
reuses that module's headers/SSL context rather than duplicating them.

BOUNDARY, STATED RATHER THAN HIDDEN: this covers roughly the 750 largest listed
companies, not every ISIN NSE/BSE lists. An ISIN this file does not carry stays
unclassified (``security.sector`` stays NULL) rather than being guessed at -- CLAUDE.md
rule 4's "never default" applies to reference data here exactly as it does to prices.
"""

from __future__ import annotations

import csv
import io
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from app.exchange_reference import urlopen
from app.prices.sources import BROWSER_HEADERS, TIMEOUT_SECONDS, _ssl_context

NSE_SECTOR_URL = "https://nsearchives.nseindia.com/content/indices/ind_niftytotalmarket_list.csv"

#: Nifty 500 constituents, same publisher and CSV shape -- the factor ranking universe.
NSE_NIFTY500_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"

#: Phase 1 is EQ-only (CLAUDE.md) -- a handful of NSE's index constituent files carry
#: non-EQ series rows (e.g. rights/partly-paid), and those are never a sector we display.
_EQ_SERIES = "EQ"

#: The two columns this parser actually depends on: ISIN is the join key, Industry is
#: the value. Missing either is the clearest signal NSE reshaped the file under us.
_REQUIRED_COLUMNS = {"Industry", "ISIN Code"}


@dataclass(frozen=True, slots=True)
class SectorRow:
    isin: str
    sector: str
    symbol: str
    company_name: str


class SectorSourceFetchFailed(Exception):
    """The request should have worked and did not -- network error or non-200. Alerts."""


class SectorSourceFormatError(Exception):
    """The response parsed as CSV but is missing a column this parser depends on -- the
    clearest signal the undocumented file changed shape."""


class NseSectorSource:
    def __init__(self, opener: Any = None):
        self._opener = opener or urlopen
        self._url = NSE_SECTOR_URL

    def fetch(self) -> bytes:
        request = urllib.request.Request(self._url, headers=BROWSER_HEADERS)
        try:
            with self._opener(request, timeout=TIMEOUT_SECONDS, context=_ssl_context()) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise SectorSourceFetchFailed(f"NSE sector list: HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SectorSourceFetchFailed(f"NSE sector list: {type(exc).__name__}") from exc


def nse_sector_source(opener: Any = None) -> NseSectorSource:
    return NseSectorSource(opener)


def nse_nifty500_source(opener: Any = None) -> NseSectorSource:
    """The same fetcher pointed at the Nifty 500 list: identical columns and parser."""
    source = NseSectorSource(opener)
    source._url = NSE_NIFTY500_URL
    return source


def parse_nse_sector_list(payload: bytes) -> list[SectorRow]:
    """EQ-series rows from NSE's Industry-classified constituent CSV.

    A row with a blank ISIN or Industry value is simply skipped (rare header/footer
    artefacts in these files) rather than raising -- only a missing COLUMN is treated as
    a format break; a missing individual VALUE is not this parser's problem to police.

    Raises:
        SectorSourceFormatError: the header is missing ``Industry`` or ``ISIN Code``.
    """
    text = payload.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = set(reader.fieldnames or [])
    missing = _REQUIRED_COLUMNS - fieldnames
    if missing:
        raise SectorSourceFormatError(
            f"NSE sector list: missing column(s) {sorted(missing)} in header {reader.fieldnames}"
        )

    rows: list[SectorRow] = []
    for record in reader:
        if (record.get("Series") or "").strip() != _EQ_SERIES:
            continue
        isin = (record.get("ISIN Code") or "").strip()
        sector = (record.get("Industry") or "").strip()
        if not isin or not sector:
            continue
        rows.append(
            SectorRow(
                isin=isin,
                sector=sector,
                symbol=(record.get("Symbol") or "").strip(),
                company_name=(record.get("Company Name") or "").strip(),
            )
        )
    return rows
