"""BSE corporate actions: the export an operator downloads from bseindia.com, into an
operator ``ActionBatch``.

WHY A FILE AND NOT A FETCH. ``api.bseindia.com`` answers 403 (Akamai) to every non-browser
client, with or without browser headers (re-checked 7 Oct 2026), and plan OD-8 forbids a
scraper or a block bypass. The export of the public corporate-actions search
(https://www.bseindia.com/corporates/corporates_act.html) is the permitted entry point:
an operator downloads it and posts it to ``POST /ops/corporate-actions/import/bse``. This
is the path BSE-only securities need, because the NSE feed never lists them.

WHY A SCRIP-CODE MAP. The export identifies a security by its BSE scrip code, not its
ISIN, and this system keys securities by ISIN (CLAUDE.md rule 2). The code is resolved
through the BSE UDiFF bhavcopy -- the same public file the price pipe already reads,
whose ``FinInstrmId`` column is the scrip code and ``ISIN`` the ISIN. A code the
bhavcopy does not carry stays unresolved and is reported, never matched by name.

Pure: no database, no network. Detection only -- every row still goes through
``app.db.action_ingestion`` (security check, DRAFT, price verification) before anything
can reach a ledger.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from app.ingest.action_source import ActionBatch, ActionIngestionError
from app.ingest.corporate_action_feed import (
    _BONUS_RE,
    _DIVIDEND_KINDS,
    _DIVIDEND_RE,
    _SPLIT_RE,
    classify_subject,
)

#: The provider name every BSE export observation, revision and coverage row carries.
BSE_EXPORT_PROVIDER = "BSE_CORPORATE_ACTIONS_EXPORT"
#: The public search the export comes from; the evidence URL on every notice.
BSE_CORP_ACTIONS_URL = "https://www.bseindia.com/corporates/corporates_act.html"

#: Normalised header (lower case, single spaces) -> field. Only the first three are
#: required; the export has carried book-closure and no-delivery columns too, which no
#: rule here reads.
_HEADERS = {
    "security code": "scrip_code",
    "ex date": "ex_date",
    "purpose": "purpose",
    "record date": "record_date",
    "actual payment date": "payment_date",
    "payment date": "payment_date",
    "security name": "security_name",
    "company name": "company_name",
}
_REQUIRED = {"scrip_code", "ex_date", "purpose"}

#: Month-name and ISO forms only. A purely numeric dd/mm/yyyy vs mm/dd/yyyy guess is
#: refused: a wrong ex-date silently changes who is entitled.
_DATE_FORMATS = ("%d %b %Y", "%d-%b-%Y", "%d %B %Y", "%d-%B-%Y", "%Y-%m-%d")
_BLANK_DATES = {"", "-", "--", "na", "n/a"}

#: BSE writes "Interim Dividend - Rs. - 5.0000": a dash may sit between the currency
#: marker and the number, which the NSE amount pattern does not allow. A bare number is
#: still never an amount (see corporate_action_feed._DIVIDEND_AMOUNT_RE).
_BSE_AMOUNT_RE = re.compile(r"\b(?:rs|re)\.?[\s-]*(\d+(?:\.\d+)?)", re.IGNORECASE)


class BseExportFormatError(ActionIngestionError):
    """The file is not a BSE corporate-actions export (400). One bad row is not this:
    a bad row becomes a REJECTED observation and its neighbours still import."""


@dataclass(frozen=True)
class BseExportRow:
    scrip_code: str
    purpose: str
    ex_date_text: str
    record_date_text: str
    payment_date_text: str
    raw: dict[str, str]


def _normalise(header: str) -> str:
    return " ".join(header.replace("﻿", "").strip().lower().split())


def parse_bse_export(payload: bytes) -> list[BseExportRow]:
    """Every non-blank data row of the export. Raises ``BseExportFormatError`` when the
    required columns are absent or the file is not text."""
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BseExportFormatError("BSE export is not UTF-8 CSV text") from exc
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if header is None:
        raise BseExportFormatError("BSE export is empty")
    fields = [_HEADERS.get(_normalise(h)) for h in header]
    missing = _REQUIRED - set(fields)
    if missing:
        raise BseExportFormatError(f"BSE export is missing column(s): {sorted(missing)}")
    rows: list[BseExportRow] = []
    for cells in reader:
        if not any(c.strip() for c in cells):
            continue
        raw = {h.strip(): c.strip() for h, c in zip(header, cells, strict=False)}
        values: dict[str, str] = {}
        for field, cell in zip(fields, cells, strict=False):
            if field and field not in values:
                values[field] = cell.strip()
        rows.append(
            BseExportRow(
                scrip_code=values.get("scrip_code", ""),
                purpose=" ".join(values.get("purpose", "").split()),
                ex_date_text=values.get("ex_date", ""),
                record_date_text=values.get("record_date", ""),
                payment_date_text=values.get("payment_date", ""),
                raw=raw,
            )
        )
    return rows


def parse_bse_date(raw: str) -> date | None:
    """``None`` for a blank; raises ``ValueError`` for text in no accepted format."""
    if raw.strip().lower() in _BLANK_DATES:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised BSE date {raw!r}")


def parse_scrip_isin_map(bhavcopy: bytes) -> dict[str, str]:
    """BSE scrip code -> ISIN from a BSE UDiFF bhavcopy (equity rows only). A code that
    maps to two ISINs in one file is left out: ambiguous is unresolved, not a choice."""
    text = bhavcopy.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    columns = {(f or "").strip() for f in reader.fieldnames or []}
    if not {"FinInstrmId", "ISIN"} <= columns:
        # Upstream layout change, not operator input: a server-side failure (500).
        from app.ingest.action_source import ActionSourceError

        raise ActionSourceError("BSE bhavcopy lacks FinInstrmId/ISIN; cannot map scrip codes")
    seen: dict[str, set[str]] = {}
    for raw in reader:
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        if row.get("FinInstrmTp", "").upper() not in {"STK", "EQ", ""}:
            continue
        code, isin = row.get("FinInstrmId", ""), row.get("ISIN", "")
        if code and re.fullmatch(r"IN[A-Z0-9]{10}", isin):
            seen.setdefault(code, set()).add(isin)
    return {code: next(iter(isins)) for code, isins in seen.items() if len(isins) == 1}


def fetch_scrip_isin_map(source: Any, on: date, lookback_days: int) -> dict[str, str]:
    """The scrip-code map from the latest BSE bhavcopy published on or before ``on``,
    looking back at most ``lookback_days`` days (weekends and holidays publish nothing).
    ``source`` is a bhavcopy source (``app.prices.sources.bse_source()``). Raises
    ``ActionSourceError`` when nothing was published in the window: an empty map would
    reject every row as unresolved, which is an outage, not a finding."""
    from app.ingest.action_source import ActionSourceError
    from app.prices.sources import FetchFailed, NotPublished

    for back in range(lookback_days + 1):
        day = on - timedelta(days=back)
        try:
            payload = source.fetch(day)
        except NotPublished:
            continue
        except FetchFailed as exc:
            raise ActionSourceError(f"BSE bhavcopy fetch failed for {day}: {exc}") from exc
        return parse_scrip_isin_map(payload)
    raise ActionSourceError(
        f"No BSE bhavcopy published in the {lookback_days} days up to {on}; "
        "cannot map BSE scrip codes to ISINs"
    )


def _terms(purpose: str) -> dict[str, Any]:
    """The notice type and the terms a deterministic reading supports. A purpose naming
    more than one of bonus/split/dividend is UNKNOWN: a multi-component notice is never
    split by guesswork (it lands in review)."""
    shapes = [
        bool(_BONUS_RE.search(purpose)),
        bool(_SPLIT_RE.search(purpose)),
        bool(_DIVIDEND_RE.search(purpose)),
    ]
    classification = classify_subject(purpose)
    if sum(shapes) > 1 and classification in ("BONUS", "SPLIT", "DIVIDEND"):
        return {"action_type": "UNKNOWN"}
    terms: dict[str, Any] = {"action_type": classification}
    if classification == "BONUS":
        match = _BONUS_RE.search(purpose)
        terms["bonus_new"], terms["bonus_for"] = int(match.group(1)), int(match.group(2))
    elif classification == "SPLIT":
        match = _SPLIT_RE.search(purpose)
        try:
            before, after = Decimal(match.group(1)), Decimal(match.group(2))
        except InvalidOperation:
            return {"action_type": "UNKNOWN"}
        terms["face_value_before"], terms["face_value_after"] = str(before), str(after)
    elif classification == "DIVIDEND":
        amounts = _BSE_AMOUNT_RE.findall(purpose)
        kinds = [name for name, pattern in _DIVIDEND_KINDS if pattern.search(purpose)]
        terms["dividend_kind"] = kinds[0] if len(kinds) == 1 else "UNSPECIFIED"
        if len(set(amounts)) == 1 and Decimal(amounts[0]) > 0:
            # Exactly one stated amount; anything else is MISSING_DIVIDEND_TERMS review.
            terms["amount_per_share"] = str(Decimal(amounts[0]))
            terms["currency"] = "INR"
    return terms


def to_events(
    rows: list[BseExportRow], scrip_to_isin: dict[str, str]
) -> tuple[list[dict[str, Any]], list[BseExportRow]]:
    """Operator-batch events for the rows whose scrip code resolves, and the rows that
    do not. An event that cannot be read is still emitted, with the unreadable value
    left in place, so ``ingest_batch`` records it REJECTED rather than dropping it."""
    events: list[dict[str, Any]] = []
    unresolved: list[BseExportRow] = []
    for row in rows:
        isin = scrip_to_isin.get(row.scrip_code)
        if isin is None:
            unresolved.append(row)
            continue
        terms = _terms(row.purpose)
        event: dict[str, Any] = {
            "isin": isin,
            "exchange": "BSE",
            "status": "CONFIRMED",
            "source_url": BSE_CORP_ACTIONS_URL,
            "subject": row.purpose or "(blank purpose)",
            **terms,
        }
        dates: dict[str, date | None] = {}
        for field, text in (
            ("ex_date", row.ex_date_text),
            ("record_date", row.record_date_text),
            ("payment_date", row.payment_date_text),
        ):
            try:
                dates[field] = parse_bse_date(text)
            except ValueError:
                # Left as text: validation rejects it and the observation keeps the row.
                event[field] = text
                dates[field] = None
                continue
            event[field] = dates[field].isoformat() if dates[field] else None
        ex, rec = dates["ex_date"], dates["record_date"]
        if ex and rec and rec < ex:
            # Cannot be true; transposed fields. Same posture as the NSE parser: drop the
            # record date, keep the notice (a bonus lot date only becomes provisional).
            event["record_date"] = None
        # BSE has no event ID. The (scrip, type, kind, ex-date) key is deterministic, so a
        # re-import is a no-op and changed terms for the same key are a TERMS_CONFLICT.
        component = terms["action_type"]
        if terms.get("dividend_kind"):
            component = f"{component}:{terms['dividend_kind']}"
        event["event_id"] = f"BSE:{row.scrip_code}:{component}:{event.get('ex_date') or 'NA'}"
        event["component_id"] = component
        event["revision"] = 1
        events.append(event)
    return events, unresolved


def build_batch(
    payload: bytes, scrip_to_isin: dict[str, str], *, start: date, end: date
) -> tuple[ActionBatch, list[BseExportRow]]:
    """The export as one operator batch over ``[start, end]`` (the range the operator
    searched), plus the rows whose scrip code did not resolve."""
    events, unresolved = to_events(parse_bse_export(payload), scrip_to_isin)
    batch = ActionBatch(
        provider=BSE_EXPORT_PROVIDER,
        start=start,
        end=end,
        exchanges=["BSE"],
        complete=True,
        events=events,
    )
    return batch, unresolved
