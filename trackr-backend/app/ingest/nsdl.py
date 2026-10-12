"""NSDL public participant search. No credentials, fabricated rows or identity guesses."""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from app.ingest.nse_session import cookie_session
from app.isin import has_valid_check_digit

SEARCH_URL = "https://nsdl.com/web/api/v1/participant/search"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class NsdlError(ValueError):
    pass


@dataclass(frozen=True)
class SecurityRecord:
    isin: str
    issuer: str
    description: str
    instrument: str
    status: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class SearchPage:
    records: tuple[SecurityRecord, ...]
    count: int
    url: str
    sha256: str


def parse_page(payload: bytes, url: str) -> SearchPage:
    try:
        obj = json.loads(payload)
        if not isinstance(obj, dict) or obj.get("status_code") != 200:
            raise ValueError("NSDL did not report success")
        rows = obj["data"]
        count_raw = str(obj["count"])
        if not count_raw.isdigit() or not isinstance(rows, list):
            raise ValueError("invalid count or data")
        count = int(count_raw)
        records = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("invalid security row")
            fields = [
                row[k]
                for k in (
                    "field_isin",
                    "name",
                    "isin_description__value",
                    "security_description",
                    "isin_status",
                )
            ]
            if any(not isinstance(f, str) or not f.strip() for f in fields):
                raise ValueError("missing security field")
            isin, issuer, description, instrument, status = (f.strip() for f in fields)
            if not re.fullmatch(r"IN[A-Z0-9]{9}[0-9]", isin) or not has_valid_check_digit(isin):
                raise ValueError("invalid ISIN in NSDL response")
            records.append(SecurityRecord(isin, issuer, description, instrument, status, row))
        if len(records) > count or (count > 0 and not records):
            raise ValueError("inconsistent page count")
        if len({r.isin for r in records}) != len(records):
            raise ValueError("duplicate securities within page")
        return SearchPage(tuple(records), count, url, hashlib.sha256(payload).hexdigest())
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise NsdlError(f"Invalid NSDL participant response: {exc}") from exc


class NsdlParticipantSource:
    def __init__(self, opener=None, timeout: int = 20, sleep=time.sleep):
        self._open = opener if opener is not None else cookie_session().open
        self.timeout = timeout
        self.sleep = sleep

    def search(self, query: str, page: int = 1, page_size: int = 100) -> SearchPage:
        if not re.fullmatch(r"IN[A-Z0-9]{5}(?:[A-Z0-9]{5})?", query):
            raise ValueError("search requires a seven-character issuer prefix or full ISIN")
        if page < 1 or not 1 <= page_size <= 500:
            raise ValueError("invalid pagination")
        url = (
            SEARCH_URL
            + "?"
            + urllib.parse.urlencode(
                {
                    "search_type": "DetailedSearch",
                    "name": "",
                    "isin": query,
                    "page": page,
                    "per_page": page_size,
                }
            )
        )
        for attempt in range(3):
            try:
                request = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Accept": "application/json",
                        "Referer": "https://nsdl.com/",
                    },
                )
                with self._open(request, timeout=self.timeout) as response:
                    payload = response.read(MAX_RESPONSE_BYTES + 1)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise NsdlError("NSDL response exceeds size limit")
                return parse_page(payload, url)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                retryable = not isinstance(exc, urllib.error.HTTPError) or exc.code >= 500
                if not retryable or attempt == 2:
                    raise NsdlError(
                        f"NSDL participant search failed: {type(exc).__name__}"
                    ) from exc
                self.sleep(2**attempt)
        raise AssertionError("unreachable")
