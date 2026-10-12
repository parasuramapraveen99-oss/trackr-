"""Where NIFTY TRI index levels come from -- docs/ENGINE_SPEC.md section 6.

A separate module from ``sources.py`` on purpose: bhavcopy is one GET per day returning a
CSV/ZIP; this is one POST for an entire date range returning JSON. Different shape,
different failure modes, but the same discipline -- the URL and payload shape live in one
place because an undocumented endpoint changing shape under us is the single most likely
way this silently stops working.

SENSEX has no equivalent here. BSE's own index archive is a JS-rendered page with no
reachable data endpoint, Asia Index (BSE/S&P's index calculator) has no documented API,
and third-party aggregators are not fit as a system-of-record source for a financial
product. ``app.routers.analytical`` rejects a ``SENSEX_TRI`` request explicitly (400)
rather than this module guessing at a source for it.

TRI ONLY -- never the plain price index. ``getTotalReturnIndexString`` and the
``TotalReturnsIndex`` field are the load-bearing names here: swapping either for a plain
index-value endpoint/field would silently understate every benchmark comparison (a price
index excludes dividends), which is exactly the failure ``tests/test_index_sources.py``
pins against.

Two indices, one endpoint. NIFTY 50 TRI backs section 6's cash-flow benchmark comparison;
NIFTY 500 TRI backs the dashboard's benchmark line, where a broad-market index is the
honest comparator for a portfolio that is not a large-cap tracker. They differ ONLY in the
name sent in the request and the key rows are stored under -- ``TriIndex`` carries that
pair so a new index is a registry entry rather than a second copy of this module.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.prices.sources import BROWSER_HEADERS, TIMEOUT_SECONDS, _ssl_context

#: The ASP.NET ``[WebMethod]`` behind niftyindices' own historical-data report.
#:
#: This moved. The original path was ``/Backpage.aspx/getTotalReturnIndexString``; that
#: URL now answers 302 to an HTML block page for every request, which is what "the
#: benchmark line silently has no data" looked like from the outside -- a fetch that
#: succeeds at the HTTP level and parses as nothing. ``/BackPage/`` (no ``.aspx``) is the
#: live route. Keeping the dead one documented here is deliberate: the next time this
#: breaks, the first question is whether the path moved again, and the answer is faster
#: with the history in front of you.
NIFTY_TRI_URL = "https://niftyindices.com/BackPage/getTotalReturnIndexString"
NIFTY_TRI_REFERER = "https://niftyindices.com/reports/historical-data"

#: ``app.prices.sources.BROWSER_HEADERS`` asks for ``text/html`` -- right for a bhavcopy
#: file, wrong here. This endpoint is content-negotiated: an HTML Accept is one of the
#: things that steers a request onto the block page rather than the JSON body.
JSON_ACCEPT = "application/json, text/javascript, */*; q=0.01"


@dataclass(frozen=True, slots=True)
class TriIndex:
    """One TRI series: the key rows are stored under, and the endpoint's own spelling.

    The two are deliberately separate fields rather than one string transformed into the
    other -- a typo in the request name would otherwise silently become a typo in the
    storage key, and levels would be written under a name nothing reads back.
    """

    storage_name: str
    request_name: str


NIFTY_50_TRI = TriIndex(storage_name="NIFTY50_TRI", request_name="NIFTY 50")
NIFTY_500_TRI = TriIndex(storage_name="NIFTY500_TRI", request_name="NIFTY 500")

#: Storage key -> index. The router validates a caller-supplied benchmark name against
#: its own literal (contracts/openapi.yaml); this maps the survivors to a fetchable index.
TRI_INDICES: dict[str, TriIndex] = {i.storage_name: i for i in (NIFTY_50_TRI, NIFTY_500_TRI)}

#: The section 6 default, kept under its original name so every existing caller and test
#: keeps reading the same thing.
NIFTY_TRI_INDEX_NAME = NIFTY_50_TRI.storage_name

_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
#: Explicit month-name maps rather than strptime/strftime's "%b" -- that is locale-
#: dependent, and a server running under a non-English locale would silently send or
#: misparse dates without erroring.
_MONTH_TO_NUM = {m: i + 1 for i, m in enumerate(_MONTHS)}


def _format_request_date(d: date) -> str:
    """``date`` -> ``DD-Mon-YYYY``, the request payload's date format."""
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year:04d}"


def _parse_response_date(text: str) -> date:
    """``DD Mon YYYY`` -> ``date``, the response payload's date format (no separator
    dashes, unlike the request format)."""
    day_str, month_str, year_str = text.strip().split()
    month = _MONTH_TO_NUM.get(month_str)
    if month is None:
        raise ValueError(f"unrecognised month {month_str!r} in {text!r}")
    return date(int(year_str), month, int(day_str))


@dataclass(frozen=True, slots=True)
class IndexLevelRow:
    index_name: str
    level_date: date
    close: Decimal
    source: str


class IndexSourceFetchFailed(Exception):
    """The request should have worked and did not -- network error, non-200, or a
    response shape too broken to even attempt parsing. This one alerts, same posture as
    ``app.prices.sources.FetchFailed``."""


class IndexSourceFormatError(Exception):
    """The response parsed as JSON but not into the shape this endpoint has always
    returned -- the clearest signal the undocumented endpoint changed shape."""


class NiftyTriSource:
    """Fetches one index's TRI levels for a date range in one call.

    Unlike ``HttpBhavcopySource`` (one file per day), this endpoint answers a whole
    range at once -- so ``fetch`` takes ``start``/``end`` rather than a single date.

    ``index`` defaults to NIFTY 50 so the section 6 call sites read unchanged.
    """

    def __init__(self, opener: Any = None, index: TriIndex = NIFTY_50_TRI):
        self._opener = opener or urllib.request.urlopen
        self.index = index

    @property
    def index_name(self) -> str:
        return self.index.storage_name

    def fetch(self, start: date, end: date) -> bytes:
        name = self.index.request_name
        cinfo = (
            f"{{'name':'{name}',"
            f"'startDate':'{_format_request_date(start)}',"
            f"'endDate':'{_format_request_date(end)}',"
            f"'indexName':'{name}'}}"
        )
        body = json.dumps({"cinfo": cinfo}).encode("utf-8")
        headers = {
            **BROWSER_HEADERS,
            "Accept": JSON_ACCEPT,
            "Content-Type": "application/json; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": NIFTY_TRI_REFERER,
        }
        request = urllib.request.Request(NIFTY_TRI_URL, data=body, headers=headers, method="POST")
        try:
            with self._opener(request, timeout=TIMEOUT_SECONDS, context=_ssl_context()) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise IndexSourceFetchFailed(f"{name} TRI: HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise IndexSourceFetchFailed(f"{name} TRI: {type(exc).__name__}") from exc


def parse_nifty_tri(payload: bytes, index: TriIndex = NIFTY_50_TRI) -> list[IndexLevelRow]:
    """Rows with a usable date and TRI close, stored under ``index``'s key.

    TWO envelope shapes are accepted, because this endpoint has served both:

    * a bare JSON array of records -- what ``/BackPage/`` returns today;
    * ASP.NET's ``[WebMethod]`` double-encoding, ``{"d": "<json-string>"}``, where ``d``
      must be parsed a second time -- what ``/Backpage.aspx/`` returned.

    Accepting both is not defensive clutter. The move between them is precisely the kind
    of change that took the benchmark line dark once already, and a reader that handles
    only the shape seen today would go dark again the moment it moves back. Which
    envelope arrived says nothing about the records inside it, so nothing downstream
    needs to know.

    Raises:
        IndexSourceFormatError: the payload is not JSON at all (the block page this
            endpoint serves a rejected request is HTML, and lands here), it is neither
            envelope shape, or -- deliberately checked, not just assumed -- the
            ``TotalReturnsIndex`` field is missing from a record, which is exactly what a
            silent swap to a plain price-index endpoint would look like.
    """
    label = f"{index.request_name} TRI"
    try:
        outer = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise IndexSourceFormatError(
            f"{label}: response is not JSON ({exc}). The endpoint serves an HTML page "
            f"to a request it rejects, so this usually means {NIFTY_TRI_URL} moved or "
            "the request no longer looks like the report page's own."
        ) from exc

    if isinstance(outer, list):
        records: Any = outer
    elif isinstance(outer, dict) and "d" in outer:
        try:
            records = json.loads(outer["d"])
        except json.JSONDecodeError as exc:
            raise IndexSourceFormatError(f"{label}: 'd' payload is not JSON: {exc}") from exc
    else:
        raise IndexSourceFormatError(
            f"{label}: response is neither a list of records nor a 'd' envelope"
        )

    if not isinstance(records, list):
        raise IndexSourceFormatError(f"{label}: 'd' payload is not a list of records")

    rows: list[IndexLevelRow] = []
    for record in records:
        if not isinstance(record, dict) or "TotalReturnsIndex" not in record:
            raise IndexSourceFormatError(
                f"{label}: record missing 'TotalReturnsIndex' -- this endpoint must "
                "never be read from a plain price-index field"
            )
        try:
            close = Decimal(str(record["TotalReturnsIndex"]).strip())
            level_date = _parse_response_date(str(record.get("Date", "")))
        except (InvalidOperation, ValueError) as exc:
            raise IndexSourceFormatError(f"{label}: unparseable record {record!r}: {exc}") from exc
        rows.append(
            IndexLevelRow(
                index_name=index.storage_name,
                level_date=level_date,
                close=close,
                source="NIFTYINDICES_TRI",
            )
        )
    return rows


def nifty_tri_source(opener: Any = None, index: TriIndex = NIFTY_50_TRI) -> NiftyTriSource:
    return NiftyTriSource(opener, index)


def fetch_and_store(
    conn: Any,
    start: date,
    end: date,
    source: NiftyTriSource | None = None,
    index: TriIndex = NIFTY_50_TRI,
) -> int:
    """Fetch one index's TRI levels for ``[start, end]`` and upsert. The one write path
    into ``index_level`` -- mirrors ``app.prices.pipe.fetch_day`` +
    ``app.db.prices.upsert_prices`` composed together, just without a separate pipe
    module.

    ``source`` wins over ``index`` when both are given: an injected source already
    carries the index it was built for, and re-deriving one from a defaulted argument
    would let a test's NIFTY 500 source silently write NIFTY 50 rows.
    """
    from app.db.index_levels import upsert_index_levels

    source = source or nifty_tri_source(index=index)
    rows = parse_nifty_tri(source.fetch(start, end), source.index)
    return upsert_index_levels(conn, rows)
