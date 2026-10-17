"""AMFI NAV files: fetch, and parse into scheme records and dated NAV observations.

docs/MF_ETF_PLAN.md section 3. Two layouts, both eight semicolon-separated columns, mapped
BY NAME from their header lines -- never by blind column offset:

    daily   (NAVAll.txt)       Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;
                               Scheme Name;Plan;Option;Net Asset Value;Date
    history (NAV history rpt)  Scheme Code;NAV Name;Plan;Option;ISIN Div Payout/ISIN Growth;
                               ISIN Div Reinvestment;Net Asset Value;Date

Between rows the files carry category headings ("Open Ended Schemes(Debt Scheme - Liquid
Fund)") and AMC names ("Nippon India Mutual Fund"); each row belongs to the most recent of
both -- an AMC line is omitted when it would repeat across a heading, so the AMC carries
over. Classification of the heading lives in ``app.prices.fund_category``.

WHAT FAILS THE WHOLE FILE, AND WHAT DOES NOT. Structural damage -- an HTML page served with
status 200, an unknown or duplicated header column, a row with the wrong number of fields,
a data row before any heading -- raises ``AmfiFormatError``: a file we cannot read reliably
publishes nothing. A single bad OBSERVATION does not: a NAV printed as ``N.A.``, a zero NAV
(AMFI prints 0 for segregated portfolios), an unparseable ISIN or date, a row under a
heading nobody has reviewed -- each is quarantined in ``rejects`` with a reason code and
the rest of the file stands.

NAVs are ``Decimal`` from the printed string, never via a binary float. A NAV is never
defaulted: unavailable is a counted observation, not zero (CLAUDE.md rule 4). The daily
file's download date is NOT its NAV date -- it carries rows dated years ago (matured
schemes), and each observation keeps its own printed date.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from app.prices.fund_category import Category, classify_heading, normalize_heading

#: Bumped whenever parsing rules change, and stored on every batch.
PARSER_VERSION = "amfi-nav-1"

DAILY_URL = "https://portal.amfiindia.com/spages/NAVAll.txt"
HISTORY_URL = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"
#: Hosts a response may finally come from (after redirects).
ALLOWED_HOSTS = frozenset({"portal.amfiindia.com"})
#: Far above the 1.5 MB daily file and a 30-day single-AMC history window (~2 MB
#: measured 13-Oct-2026); a response this large is not a file we expect.
MAX_RESPONSE_BYTES = 25 * 1024 * 1024


class Layout(StrEnum):
    DAILY = "DAILY"
    HISTORY = "HISTORY"


class AmfiFormatError(ValueError):
    """The response is not a readable AMFI NAV file. Nothing from it may be published."""


class AmfiFetchError(RuntimeError):
    """The request itself failed (network, status, host, size). Retryable."""

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


#: Canonical field -> the header spellings observed for it. Exact after whitespace
#: normalisation; anything else in a header is an unknown structural column.
_HEADER_ALIASES = {
    "scheme_code": {"Scheme Code"},
    "isin_growth": {"ISIN Div Payout/ ISIN Growth", "ISIN Div Payout/ISIN Growth"},
    "isin_reinvest": {"ISIN Div Reinvestment"},
    "scheme_name": {"Scheme Name", "NAV Name"},
    "plan": {"Plan"},
    "option": {"Option"},
    "nav": {"Net Asset Value"},
    "nav_date": {"Date"},
}
_FIELDS = tuple(_HEADER_ALIASES)
_ISIN = re.compile(r"^IN[A-Z0-9]{10}$")
#: Explicit English month tokens: never the host locale's strptime.
_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
    )
}
_DATE = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$")
_UNAVAILABLE = {"", "-", "N.A.", "NA", "N.A", "n.a."}


@dataclass(frozen=True)
class SchemeRecord:
    scheme_code: int
    amc_name: str
    scheme_name: str
    plan: str | None
    option: str | None
    heading: str
    category: Category
    isin_growth: str | None
    isin_reinvest: str | None
    #: The NAV date of the row this record was read from: later rows win.
    seen_on: date


@dataclass(frozen=True)
class NavObservation:
    scheme_code: int
    nav_date: date
    nav: Decimal


@dataclass(frozen=True)
class Reject:
    line_no: int
    reason: str
    raw: str
    scheme_code: int | None = None


@dataclass
class ParsedFile:
    layout: Layout
    sha256: str
    schemes: dict[int, SchemeRecord] = field(default_factory=dict)
    navs: dict[tuple[int, date], NavObservation] = field(default_factory=dict)
    rejects: list[Reject] = field(default_factory=list)
    #: Field-level problems on rows that were still used (an unreadable ISIN column).
    warnings: list[Reject] = field(default_factory=list)
    #: (scheme_code, nav_date) printed with two different NAVs in this one file.
    conflicts: list[tuple[int, date]] = field(default_factory=list)
    rows: int = 0
    amc_names: set[str] = field(default_factory=set)

    @property
    def unavailable(self) -> int:
        return sum(1 for r in self.rejects if r.reason == "NAV_UNAVAILABLE")


def _header_map(header_line: str) -> tuple[Layout, dict[str, int]]:
    columns = [re.sub(r"\s+", " ", c).strip() for c in header_line.lstrip("﻿").split(";")]
    mapping: dict[str, int] = {}
    for index, column in enumerate(columns):
        name = next((f for f, spellings in _HEADER_ALIASES.items() if column in spellings), None)
        if name is None:
            raise AmfiFormatError(f"unknown AMFI header column {column!r}")
        if name in mapping:
            raise AmfiFormatError(f"duplicate AMFI header column {column!r}")
        mapping[name] = index
    missing = [f for f in _FIELDS if f not in mapping]
    if missing:
        raise AmfiFormatError(f"AMFI header is missing {missing}")
    layout = Layout.HISTORY if columns[1] == "NAV Name" else Layout.DAILY
    return layout, mapping


def parse_date(text: str) -> date | None:
    match = _DATE.match(text.strip())
    if match is None:
        return None
    month = _MONTHS.get(match.group(2).lower())
    if month is None:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1)))
    except ValueError:
        return None


def _isin(text: str) -> tuple[str | None, bool]:
    """``(isin, valid)``. A dash or blank is a printed absence, which is valid."""
    value = text.strip()
    if value in ("", "-"):
        return None, True
    return (value, True) if _ISIN.match(value) else (None, False)


def _decode(payload: bytes) -> str:
    if len(payload) > MAX_RESPONSE_BYTES:
        raise AmfiFormatError(f"AMFI response is {len(payload)} bytes, over the cap")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        text = payload.decode("cp1252")
    head = text.lstrip("﻿ \r\n\t")[:512].lower()
    if head.startswith("<") or "<html" in head or "<!doctype" in head or head.startswith("{"):
        raise AmfiFormatError("AMFI answered with an HTML/script page, not a NAV file")
    return text


def parse(payload: bytes, today: date) -> ParsedFile:
    """Parse one AMFI NAV file. Raises ``AmfiFormatError`` on structural damage."""
    text = _decode(payload)
    lines = text.splitlines()
    if not lines or not lines[0].strip():
        raise AmfiFormatError("empty AMFI response")
    layout, columns = _header_map(lines[0])
    parsed = ParsedFile(layout=layout, sha256=hashlib.sha256(payload).hexdigest())

    heading: str | None = None
    category: Category | None = None
    amc: str | None = None
    for line_no, raw_line in enumerate(lines[1:], start=2):
        line = raw_line.strip()
        if not line:
            continue
        if ";" not in line:
            if normalize_heading(line) is not None:
                # The AMC is NOT reset here: NAVAll.txt (13-Oct-2026, line 964) prints a
                # heading followed directly by Franklin rows, with the AMC line omitted
                # because it is unchanged from the section before.
                heading, category = line, classify_heading(line)
            else:
                amc = re.sub(r"\s+", " ", line)
                parsed.amc_names.add(amc)
            continue
        fields = line.split(";")
        if len(fields) != len(columns):
            raise AmfiFormatError(
                f"line {line_no}: {len(fields)} fields, header has {len(columns)}"
            )
        if heading is None or amc is None:
            raise AmfiFormatError(f"line {line_no}: data row before any heading/AMC line")
        parsed.rows += 1
        value = {name: fields[index].strip() for name, index in columns.items()}
        try:
            code = int(value["scheme_code"])
        except ValueError:
            parsed.rejects.append(Reject(line_no, "BAD_SCHEME_CODE", line))
            continue
        if category is None:
            parsed.rejects.append(Reject(line_no, "UNKNOWN_HEADING", line, code))
            continue
        nav_date = parse_date(value["nav_date"])
        if nav_date is None:
            parsed.rejects.append(Reject(line_no, "BAD_DATE", line, code))
            continue
        if nav_date > today:
            parsed.rejects.append(Reject(line_no, "FUTURE_DATE", line, code))
            continue
        growth, growth_ok = _isin(value["isin_growth"])
        reinvest, reinvest_ok = _isin(value["isin_reinvest"])
        if not (growth_ok and reinvest_ok):
            # The column holds something that is not an ISIN ("Redeemed", "HDFCNIVODG",
            # both in NAVAll.txt 13-Oct-2026). That column carries no identity; the
            # other column and the NAV are still good. Nothing is guessed in its place.
            parsed.warnings.append(Reject(line_no, "BAD_ISIN_FIELD", line, code))

        record = SchemeRecord(
            scheme_code=code,
            amc_name=amc,
            scheme_name=value["scheme_name"],
            plan=value["plan"] or None,
            option=value["option"] or None,
            heading=re.sub(r"\s+", " ", heading),
            category=category,
            isin_growth=growth,
            isin_reinvest=reinvest,
            seen_on=nav_date,
        )
        previous = parsed.schemes.get(code)
        if previous is None or previous.seen_on <= nav_date:
            parsed.schemes[code] = record

        nav_text = value["nav"]
        if nav_text in _UNAVAILABLE:
            parsed.rejects.append(Reject(line_no, "NAV_UNAVAILABLE", line, code))
            continue
        try:
            nav = Decimal(nav_text)
        except InvalidOperation:
            parsed.rejects.append(Reject(line_no, "BAD_NAV", line, code))
            continue
        if not nav.is_finite() or nav <= 0:
            parsed.rejects.append(Reject(line_no, "NAV_NON_POSITIVE", line, code))
            continue
        key = (code, nav_date)
        seen = parsed.navs.get(key)
        if seen is not None and seen.nav != nav:
            parsed.conflicts.append(key)
            continue
        parsed.navs[key] = NavObservation(code, nav_date, nav)
    return parsed


# --- fetching ---------------------------------------------------------------------------


def history_url(amc_id: int, start: date, end: date) -> str:
    return (
        f"{HISTORY_URL}?mf={int(amc_id)}&tp=1"
        f"&frmdt={start.strftime('%d')}-{_month(start)}-{start.year}"
        f"&todt={end.strftime('%d')}-{_month(end)}-{end.year}"
    )


def _month(on: date) -> str:
    return ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")[
        on.month - 1
    ]


def fetch(url: str, timeout_seconds: float, client: Any = None) -> bytes:
    """GET an AMFI URL. Raises ``AmfiFetchError`` for anything but a 200 from AMFI's host.

    Never called inside an open database transaction (plan section 3.2): callers fetch,
    parse, and only then open a short write transaction.
    """
    import httpx

    owns = client is None
    client = client or httpx.Client(
        timeout=httpx.Timeout(timeout_seconds, connect=min(10.0, timeout_seconds)),
        follow_redirects=True,
        headers={"User-Agent": "Trackr+/1.0 (+portfolio NAV reference)"},
    )
    try:
        try:
            response = client.get(url)
        except httpx.HTTPError as exc:
            raise AmfiFetchError(f"AMFI request failed: {type(exc).__name__}: {exc}") from exc
        host = response.url.host
        if host not in ALLOWED_HOSTS:
            raise AmfiFetchError(f"AMFI redirected to an unexpected host {host!r}")
        if response.status_code == 429 or response.status_code >= 500:
            retry_after = response.headers.get("Retry-After")
            raise AmfiFetchError(
                f"AMFI answered {response.status_code}",
                retry_after=int(retry_after) if retry_after and retry_after.isdigit() else None,
            )
        if response.status_code != 200:
            raise AmfiFetchError(f"AMFI answered {response.status_code}")
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise AmfiFetchError(f"AMFI response is {len(response.content)} bytes, over the cap")
        return response.content
    finally:
        if owns:
            client.close()
