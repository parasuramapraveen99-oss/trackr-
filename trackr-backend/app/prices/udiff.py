"""UDiFF bhavcopy parsing -- one parser, both exchanges (and, via
``app.prices.sources.legacy_nse_source``, the pre-2024 NSE archive too).

NSE and BSE now publish the same UDiFF layout with identical column headers, including an
ISIN column. That column is what makes this pipe trustworthy at all -- keying on SYMBOL
would have reintroduced exactly the ticker-reuse problem CLAUDE.md opens with (Indosolar's
ISIN now resolves to WAAREEINDO; the ISIN never moved).

The legacy NSE ``cmDDMMMYYYYbhav`` format (pre-2024, a different URL and column layout --
see ``app.prices.sources.LEGACY_NSE_URL``) DOES carry an ISIN column too, confirmed against
a real fetched file -- an earlier version of this docstring claimed otherwise. Rather than
add a second parser, ``LegacyNseBhavcopySource`` re-shapes that file into these same column
names before this function ever sees it, so this stays the only bhavcopy parser.
"""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

ISIN_COLUMN = "ISIN"
CLOSE_COLUMN = "ClsPric"
DATE_COLUMN = "TradDt"
SYMBOL_COLUMN = "TckrSymb"
SERIES_COLUMN = "SctySrs"
INSTRUMENT_COLUMN = "FinInstrmTp"
#: The exchange's own instrument id (BSE: the scrip code). Present only in a real UDiFF
#: file -- a legacy file re-shaped by app.prices.sources._to_udiff_csv has no such column,
#: which is how a caller tells a real BSE symbol from BSE legacy's company NAME.
INSTRUMENT_ID_COLUMN = "FinInstrmId"

#: Cash-segment equity only. A UDiFF file also carries derivatives, whose "close" is a
#: settlement price for a contract - valuing a share at one would be nonsense.
EQUITY_INSTRUMENTS = {"STK", "EQ", ""}


@dataclass(frozen=True, slots=True)
class BhavcopyRow:
    isin: str
    close: Decimal
    price_date: date
    symbol: str
    series: str
    exchange: str
    instrument_id: str = ""


class BhavcopyFormatError(ValueError):
    """The file is not a UDiFF bhavcopy. Raised rather than returning nothing, because
    silently parsing zero rows out of a changed format is how a pipe reports success while
    delivering no prices."""


def _decompress(payload: bytes) -> str:
    """NSE ships a ZIP, BSE ships bare CSV. Detect rather than configure."""
    if payload[:2] == b"PK":
        archive = zipfile.ZipFile(io.BytesIO(payload))
        return archive.read(archive.namelist()[0]).decode("utf-8", "replace")
    return payload.decode("utf-8-sig", "replace")


def parse(payload: bytes, exchange: str) -> list[BhavcopyRow]:
    """Rows with a usable ISIN and close. Everything else is skipped, quietly and safely.

    Raises:
        BhavcopyFormatError: the expected columns are absent, which means the format
            changed and nothing downstream should trust this file.
    """
    reader = csv.DictReader(io.StringIO(_decompress(payload)))
    if reader.fieldnames is None:
        raise BhavcopyFormatError(f"{exchange}: file is empty")

    columns = {(f or "").strip() for f in reader.fieldnames}
    missing = {ISIN_COLUMN, CLOSE_COLUMN, DATE_COLUMN} - columns
    if missing:
        raise BhavcopyFormatError(
            f"{exchange}: not a UDiFF bhavcopy, missing {sorted(missing)}. A legacy-format "
            "file reaches this parser only after app.prices.sources._to_udiff_csv has "
            "re-shaped it, so this means the layout changed, not that it is old."
        )

    rows: list[BhavcopyRow] = []
    for raw in reader:
        isin = (raw.get(ISIN_COLUMN) or "").strip()
        if not isin:
            continue
        instrument = (raw.get(INSTRUMENT_COLUMN) or "").strip().upper()
        if instrument not in EQUITY_INSTRUMENTS:
            continue
        try:
            close = Decimal(str(raw.get(CLOSE_COLUMN) or "").strip())
            price_date = date.fromisoformat((raw.get(DATE_COLUMN) or "").strip())
        except (InvalidOperation, ValueError):
            continue
        if close <= 0:
            # A zero close is a suspended or untraded scrip, not a price. Storing it would
            # value a holding at nothing, which is the failure MissingPriceError exists for.
            continue
        rows.append(
            BhavcopyRow(
                isin=isin,
                close=close,
                price_date=price_date,
                symbol=(raw.get(SYMBOL_COLUMN) or "").strip(),
                series=(raw.get(SERIES_COLUMN) or "").strip(),
                exchange=exchange,
                instrument_id=(raw.get(INSTRUMENT_ID_COLUMN) or "").strip(),
            )
        )
    return rows
