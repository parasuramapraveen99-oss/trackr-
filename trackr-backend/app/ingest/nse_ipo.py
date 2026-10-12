"""NSE's public IPO records -- the terms of an IPO allotment, for rule 20 (gap resolution).

An IPO allotment never appears on a contract note, so an investor who sold IPO shares
shows a ledger gap. Everything needed to close it is published free by the exchange, and
verified live 2026-10-08 on SULA (issue Dec 2022):

  - ``api/public-past-issues`` -- issue price and listing date per symbol
    (SULA: "issuePrice": "   357", "listingDate": "22-DEC-2022").
  - ``api/ipo-detail?symbol=..`` -- the issue's own fact sheet; "Bid Lot" carries the lot
    size ("42 Equity Shares and in multiples thereof").
  - The exchange's listing circular, "Listing of Equity Shares of <Company> (IPO)", a ZIP
    whose SHP_<SYMBOL>.pdf is the POST-ISSUE shareholding pattern dated as of the
    allotment ("POST ISSUE ... 19.12.2022"). That date is the allotment date.

Same access posture as the corporate-actions feed (warmed NSE session, browser headers,
no evasion): ``app.ingest.nse_session`` / ``NseCircularListSource``. Parsing is pure;
fetching is injectable so tests run on captured excerpts. Detection-side only: terms go to
``app.engine.gap_resolution.ipo_fits``, which decides whether they close a gap.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import urllib.request
import zipfile
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from app.engine.gap_resolution import IpoTerms

NSE_PAST_ISSUES_URL = "https://www.nseindia.com/api/public-past-issues"
NSE_IPO_DETAIL_URL = "https://www.nseindia.com/api/ipo-detail?symbol={symbol}&series={series}"

_LOT_RE = re.compile(r"^\s*(\d+)\s+Equity Shares", re.IGNORECASE)
_POST_ISSUE_DATE_RE = re.compile(r"POST\s+ISSUE.{0,200}?(\d{2})\.(\d{2})\.(\d{4})", re.S | re.I)
_LISTING_SUBJECT_RE = re.compile(r"Listing of Equity Shares of (?P<name>.+?)\s*\(IPO\)", re.I)


class NseIpoFormatError(ValueError):
    """A payload did not have the shape verified on 2026-10-08."""


def parse_past_issue(payload: bytes, symbol: str) -> tuple[Decimal, date]:
    """(issue price, listing date) for ``symbol`` from ``api/public-past-issues``."""
    try:
        rows = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise NseIpoFormatError(f"past issues: not JSON: {exc}") from exc
    if not isinstance(rows, list):
        raise NseIpoFormatError("past issues: expected a list")
    matches = [r for r in rows if isinstance(r, dict) and r.get("symbol") == symbol]
    if len(matches) != 1:
        raise NseIpoFormatError(f"past issues: {len(matches)} rows for {symbol}, need exactly 1")
    row = matches[0]
    try:
        price = Decimal(str(row["issuePrice"]).strip().replace(",", ""))
        listed = datetime.strptime(str(row["listingDate"]).strip(), "%d-%b-%Y").date()
    except (KeyError, InvalidOperation, ValueError) as exc:
        raise NseIpoFormatError(f"past issues: unreadable row for {symbol}: {exc}") from exc
    return price, listed


def parse_lot_size(payload: bytes) -> tuple[int, str | None]:
    """(lot size, ISIN) from ``api/ipo-detail``'s "Bid Lot" line and metaInfo."""
    try:
        detail = json.loads(payload)
        items = detail["issueInfo"]["dataList"]
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError) as exc:
        raise NseIpoFormatError(f"ipo detail: unexpected shape: {exc}") from exc
    lots = [
        _LOT_RE.match(str(item.get("value", "")))
        for item in items
        if isinstance(item, dict) and str(item.get("title", "")).strip().lower() == "bid lot"
    ]
    lots = [m for m in lots if m]
    if len(lots) != 1:
        raise NseIpoFormatError("ipo detail: no single readable 'Bid Lot'")
    isin = (detail.get("metaInfo") or {}).get("isin")
    return int(lots[0].group(1)), isin


def parse_allotment_date(listing_zip: bytes, symbol: str, pdf_text: Any) -> date:
    """The allotment date: the as-of date of the POST-ISSUE shareholding pattern
    (SHP_<SYMBOL>.pdf) inside the exchange's listing circular ZIP."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(listing_zip))
    except zipfile.BadZipFile as exc:
        raise NseIpoFormatError(f"listing circular: not a ZIP: {exc}") from exc
    names = [n for n in archive.namelist() if n.upper() == f"SHP_{symbol.upper()}.PDF"]
    if len(names) != 1:
        raise NseIpoFormatError(f"listing circular: no SHP_{symbol}.pdf")
    text = " ".join(pdf_text(archive.read(names[0])).split())
    match = _POST_ISSUE_DATE_RE.search(text)
    if match is None:
        raise NseIpoFormatError("listing circular: post-issue shareholding has no date")
    day, month, year = (int(g) for g in match.groups())
    return date(year, month, day)


def find_listing_circular(listing_payload: bytes, company: str) -> dict:
    """The one "Listing of Equity Shares of <company> (IPO)" circular carrying the ZIP."""
    data = json.loads(listing_payload)
    rows = data.get("data", data) if isinstance(data, dict) else data
    wanted = company.strip().lower()
    hits = [
        r for r in rows
        if isinstance(r, dict)
        and str(r.get("fileExt", "")).lower() == "zip"
        and (m := _LISTING_SUBJECT_RE.search(str(r.get("sub", ""))))
        and m.group("name").strip().lower() == wanted
    ]
    if len(hits) != 1:
        raise NseIpoFormatError(f"listing circular: {len(hits)} matches for {company!r}")
    return hits[0]


class NseIpoSource:
    """Live fetches, behind one warmed NSE session. Injectable for tests."""

    def __init__(self, opener: Any = None, circulars: Any = None):
        from app.ingest.isin_change_feed import NseCircularListSource
        from app.ingest.nse_session import cookie_session

        self._opener = opener or cookie_session()
        self._circulars = circulars or NseCircularListSource()
        self._warm = False

    def _get(self, url: str) -> bytes:
        from app.ingest.corporate_action_feed import BROWSER_HEADERS, NSE_HOMEPAGE_URL

        if not self._warm:
            self._opener.open(urllib.request.Request(NSE_HOMEPAGE_URL, headers=BROWSER_HEADERS),
                              timeout=30).read()
            self._warm = True
        with self._opener.open(urllib.request.Request(url, headers=BROWSER_HEADERS),
                               timeout=30) as response:
            return response.read()

    def terms(self, symbol: str, company: str, series: str = "EQ") -> IpoTerms:
        """Every IPO term rule 20d needs, each with the URL and SHA-256 it came from."""
        from app.ingest.isin_change_feed import extract_pdf_text

        past = self._get(NSE_PAST_ISSUES_URL)
        price, listed = parse_past_issue(past, symbol)
        detail_url = NSE_IPO_DETAIL_URL.format(symbol=symbol, series=series)
        detail = self._get(detail_url)
        lot, isin = parse_lot_size(detail)
        if not isin:
            raise NseIpoFormatError(f"ipo detail: no ISIN for {symbol}")
        listing = self._circulars.list_circulars(listed - timedelta(days=7), listed)
        circular = find_listing_circular(listing, company)
        archive = self._circulars.fetch_pdf(circular["circFilelink"])
        allotted = parse_allotment_date(archive, symbol, extract_pdf_text)

        def receipt(url: str, body: bytes) -> dict:
            return {"url": url, "sha256": hashlib.sha256(body).hexdigest()}

        return IpoTerms(
            isin=isin,
            issue_price=price,
            lot_size=lot,
            listing_date=listed,
            allotment_date=allotted,
            evidence={
                "past_issues": receipt(NSE_PAST_ISSUES_URL, past),
                "ipo_detail": receipt(detail_url, detail),
                "listing_circular": receipt(circular["circFilelink"], archive),
            },
        )
