"""Zerodha Console tradebook CSV adapter.

Console caps each download at 365 days, so a full history is several files that a client
sends together and which may overlap. Merging and deduplicating them is the normal case,
not an edge case.

WHY ``trade_id`` IS LOAD-BEARING. Measured against the four real exports in this project:
``trade_id`` is unique across all 259 rows, but ``order_id`` is unique across only 125 --
one order fills as several trades. More pointedly, 12 groups of rows are identical on
(isin, date, type, quantity, price), covering 16 trades. A dedupe key without ``trade_id``
would silently destroy those 16 and look like it had worked. So ``source_ref`` carries
``trade_id`` and nothing else.

Real example: INE142Q01026 on 2024-09-10, three separate sells of 2 @ 484.100006.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.engine.types import Source, Transaction, TxnType
from app.ingest.base import CONFIDENCE_CERTAIN, ParseResult, Rejection, StagedRow

TRADE_TYPES = {"buy": TxnType.BUY, "sell": TxnType.SELL}

REQUIRED_COLUMNS = {"isin", "trade_date", "trade_type", "quantity", "price", "trade_id"}

#: Phase 1 is equities. Anything else is rejected rather than quietly mis-modelled: a
#: derivative in an equity ledger produces a cost basis that is wrong in a way nobody
#: notices until a tax return.
SUPPORTED_SEGMENT = "EQ"

#: How far down the file to hunt for the header row. A tradebook pasted through Google
#: Sheets carries blank rows and a title block above it -- the real upload that motivated
#: this had its header on line 15. Real Console exports have it on line 1, so a bounded
#: scan costs nothing there and keeps a wrong file from being read to the bottom.
MAX_HEADER_SCAN_LINES = 50


def _norm_key(k: str | None) -> str:
    return (k or "").strip().lower().replace(" ", "_").replace("-", "_")


def _split_line(line: str) -> tuple[str, list[str]]:
    """Split one physical line into (delimiter, cells). Tabs win because a tab inside
    a comma-separated cell is data, while comma-separated exports never contain tabs."""
    delimiter = "\t" if "\t" in line else ","
    try:
        return delimiter, next(csv.reader([line], delimiter=delimiter))
    except (csv.Error, StopIteration):
        return delimiter, []


def _locate_header(lines: list[str]) -> tuple[int | None, str]:
    """Return (0-based index of the header line, its delimiter), or (None, ",")."""
    for idx, line in enumerate(lines[:MAX_HEADER_SCAN_LINES]):
        if not line.strip():
            continue
        delimiter, cells = _split_line(line)
        if REQUIRED_COLUMNS <= {_norm_key(c) for c in cells}:
            return idx, delimiter
    return None, ","


class ZerodhaTradebookAdapter:
    name = "zerodha_tradebook"

    def parse(self, content: bytes, account_id: str, filename: str = "") -> ParseResult:
        rows: list[StagedRow] = []
        rejections: list[Rejection] = []
        notes: list[str] = [
            "Tradebook exports carry no charges; brokerage, STT, GST and stamp duty live "
            "on the contract note. charges is 0 on every row here - that is absence of "
            "data, not evidence the trades were free."
        ]

        text = content.decode("utf-8-sig", errors="replace")
        lines = text.splitlines()
        if not any(line.strip() for line in lines):
            return ParseResult(rejections=[Rejection(0, "file is empty")], notes=notes)

        header_idx, delimiter = _locate_header(lines)
        if header_idx is None:
            # Report against the first non-empty line so the message names what the
            # file actually opens with, exactly as the row-0 check used to.
            probe = next(line for line in lines if line.strip())
            _, cells = _split_line(probe)
            missing = sorted(REQUIRED_COLUMNS - {_norm_key(c) for c in cells})
            return ParseResult(
                rejections=[Rejection(0, f"not a Zerodha tradebook: missing {missing}")],
                notes=notes,
            )

        reader = csv.DictReader(
            io.StringIO("\n".join(lines[header_idx:]) + "\n"), delimiter=delimiter
        )

        # The header sits on physical line ``header_idx + 1``, so data starts at
        # ``header_idx + 2`` -- which is what a person sees when they open the file to
        # find the row a rejection names.
        for row_number, raw in enumerate(reader, start=header_idx + 2):
            norm_raw = {_norm_key(k): v for k, v in raw.items() if k is not None}
            parsed = self._parse_row(norm_raw, account_id, row_number)
            if isinstance(parsed, Rejection):
                rejections.append(parsed)
            else:
                rows.append(parsed)

        return ParseResult(rows=rows, rejections=rejections, notes=notes)

    def _parse_row(
        self, raw: dict[str, Any], account_id: str, row_number: int
    ) -> StagedRow | Rejection:
        def reject(reason: str) -> Rejection:
            return Rejection(row_number, reason, {k: v for k, v in raw.items() if v})

        segment = (raw.get("segment") or "").strip().upper()
        if segment and segment != SUPPORTED_SEGMENT:
            return reject(f"segment {segment!r} is not supported in Phase 1 (equities only)")

        # Validated centrally, in app.db.imports._screen_isins, against app.isin -- which
        # checks the ISO 6166 check digit this adapter's own pattern never did. Taken as
        # printed here; normalisation is that screen's job, on every source rather than
        # this one.
        isin = (raw.get("isin") or "").strip().upper()

        trade_type = (raw.get("trade_type") or "").strip().lower()
        if trade_type not in TRADE_TYPES:
            return reject(f"unknown trade_type {trade_type!r}")

        try:
            txn_date = date.fromisoformat((raw.get("trade_date") or "").strip())
        except ValueError:
            return reject(f"unparseable trade_date {raw.get('trade_date')!r}")

        try:
            # Decimal(str(...)) throughout. Routing a price through a binary numeric type
            # here would undo the precision discipline the whole engine is built on.
            quantity = Decimal(str(raw.get("quantity") or "").strip())
            price = Decimal(str(raw.get("price") or "").strip())
        except (InvalidOperation, ValueError):
            return reject(
                f"unparseable quantity/price: {raw.get('quantity')!r}/{raw.get('price')!r}"
            )

        if quantity <= 0:
            return reject(f"quantity must be positive, got {quantity}")
        if price < 0:
            return reject(f"price cannot be negative, got {price}")

        trade_id = (raw.get("trade_id") or "").strip()
        if not trade_id:
            return reject("missing trade_id, which is the only stable identity a tradebook row has")

        return StagedRow(
            row_number=row_number,
            transaction=Transaction(
                isin=isin,
                txn_date=txn_date,
                txn_type=TRADE_TYPES[trade_type],
                quantity=quantity,
                price=price,
                charges=Decimal("0"),
                account_id=account_id,
                source=Source.TRADEBOOK,
                source_ref=trade_id,
            ),
            # Nothing here is inferred. Every field was read from a structured column.
            confidence=CONFIDENCE_CERTAIN,
            raw={k: v for k, v in raw.items() if v},
        )
