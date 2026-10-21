"""Structured demat CAS holdings in CSV/XLSX, validated by the CAS PDF reader.

The sheet names its broker, statement date, security count and portfolio value before
an ISIN/quantity/market-price/market-value table. This is a holdings snapshot, never
trade evidence. Original CAS PDFs remain the source for folios and movement details.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.ingest.cas_pdf import CasPdfAdapter, CasStatementMismatchError
from app.ingest.tabular import cell_text, csv_rows, workbook_sheets
from app.ingest.zerodha_tradebook import MAX_HEADER_SCAN_LINES, _norm_key

TABLE_COLUMNS = {"isin", "quantity", "market_price", "market_value"}
TABLE_MARKERS = {"CAS HOLDINGS", "CONSOLIDATED ACCOUNT STATEMENT"}


def holdings_header(rows: list[tuple[Any, ...]]) -> int | None:
    marked = False
    for index, row in enumerate(rows[:MAX_HEADER_SCAN_LINES]):
        marked |= any(cell_text(cell).upper() in TABLE_MARKERS for cell in row)
        if marked and TABLE_COLUMNS <= {_norm_key(cell_text(cell)) for cell in row}:
            return index
    return None


def _decimal_cell(value: Any) -> Decimal:
    number = Decimal(cell_text(value).replace(",", ""))
    if not number.is_finite() or number < 0:
        raise ValueError("amounts and quantities must be finite and nonnegative")
    return number


def _printed(number: Decimal) -> str:
    value = format(number, "f")
    return value if "." in value else value + ".0"


class CasCsvAdapter(CasPdfAdapter):
    name = "cas_csv"

    def _tables(self, content: bytes) -> list[list[tuple[Any, ...]]]:
        return [csv_rows(content)]

    def _read_text(self, content: bytes) -> str:
        if content == self._text_content:
            return self._text
        try:
            tables = self._tables(content)
        except Exception as exc:  # unreadable CSV/XLSX must be a per-file refusal
            raise CasStatementMismatchError("Could not read this CAS holdings table.") from exc
        candidates = [(rows, holdings_header(rows)) for rows in tables]
        if any(
            index is None and any(
                cell_text(cell).upper() in TABLE_MARKERS
                for row in rows[:MAX_HEADER_SCAN_LINES] for cell in row
            ) for rows, index in candidates
        ):
            raise CasStatementMismatchError(
                "Every CAS holdings table needs ISIN, Quantity, Market Price and Market Value."
            )
        candidates = [(rows, index) for rows, index in candidates if index is not None]
        if len(candidates) != 1:
            raise CasStatementMismatchError(
                "Upload one CAS holdings table with ISIN, Quantity, Market Price and Market Value."
            )
        rows, header = candidates[0]
        metadata = {}
        for row in rows[:header]:
            if not row or not cell_text(row[0]):
                continue
            key = _norm_key(cell_text(row[0])).rstrip(":")
            values = [cell for cell in row[1:] if cell_text(cell)]
            if not values:
                continue
            if key in metadata or len(values) != 1:
                raise CasStatementMismatchError("CAS metadata is repeated or ambiguous.")
            metadata[key] = values[0]
        required = {"statement_date", "broker", "total_securities", "portfolio_value"}
        if not required <= metadata.keys():
            missing = ", ".join(sorted(required - metadata.keys())).replace("_", " ")
            raise CasStatementMismatchError(f"CAS holdings table is missing: {missing}.")
        try:
            raw_date = metadata["statement_date"]
            if isinstance(raw_date, datetime):
                on = raw_date.date()
            elif isinstance(raw_date, date):
                on = raw_date
            else:
                on = date.fromisoformat(cell_text(raw_date))
            count = _decimal_cell(metadata["total_securities"])
            total = _decimal_cell(metadata["portfolio_value"])
            if count != count.to_integral_value():
                raise ValueError("security count must be an integer")
            if total != total.quantize(Decimal("0.01")):
                raise ValueError("portfolio value must be stated to the paisa")
        except (ValueError, InvalidOperation) as exc:
            raise CasStatementMismatchError(
                "CAS statement date must be YYYY-MM-DD and its count/value must be valid numbers."
            ) from exc
        broker = cell_text(metadata["broker"])
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9 .&-]+", broker):
            raise CasStatementMismatchError("CAS holdings table has no readable broker name.")
        # Render the already-structured cells into the established CAS row grammar.
        # The existing reader verifies account attribution, ISINs, per-row arithmetic,
        # duplicate securities and both statement totals before recording any snapshot.
        text = [
            "CONSOLIDATED ACCOUNT STATEMENT",
            f"{broker.upper()} LIMITED",
            f"CDSL Demat Account {int(count)} {total:.2f}",
        ]
        if "pan" in metadata:
            pan = cell_text(metadata["pan"]).upper()
            if not re.fullmatch(r"[A-Z]{5}[0-9]{4}[A-Z]", pan):
                raise CasStatementMismatchError("The CAS holder PAN is not valid.")
            text.append(f"(PAN: {pan})")
        columns = [_norm_key(cell_text(cell)) for cell in rows[header]]
        named = [key for key in columns if key]
        if len(named) != len(set(named)):
            raise CasStatementMismatchError("CAS holdings table repeats a column name.")
        for index, row in enumerate(rows[header + 1:], header + 2):
            if not any(cell_text(cell) for cell in row):
                continue
            if any(cell_text(cell) for cell in row[len(columns):]):
                raise CasStatementMismatchError(f"CAS row {index} has data outside its columns.")
            values = {key: row[i] if i < len(row) else None for i, key in enumerate(columns)}
            try:
                quantity, price, value = (
                    _decimal_cell(values[key])
                    for key in ("quantity", "market_price", "market_value")
                )
            except (ValueError, InvalidOperation) as exc:
                raise CasStatementMismatchError(
                    f"CAS row {index} has invalid numeric data."
                ) from exc
            isin = cell_text(values["isin"]).upper()
            if not re.fullmatch(r"IN[A-Z0-9]{9}\d", isin):
                raise CasStatementMismatchError(f"CAS row {index} has an invalid ISIN.")
            qty = _printed(quantity)
            text.append(f"{isin} {qty} -- -- -- {qty} {_printed(price)} {_printed(value)}")
        text.append(f"Portfolio Value {total:.2f} as on {on:%d-%m-%Y}")
        self._text_content, self._text = content, "\n".join(text)
        return self._text


class CasXlsxAdapter(CasCsvAdapter):
    name = "cas_xlsx"

    def _tables(self, content: bytes) -> list[list[tuple[Any, ...]]]:
        return [sheet.rows for sheet in workbook_sheets(content)]


CAS_TABLE_ADAPTERS = {CasCsvAdapter.name: CasCsvAdapter, CasXlsxAdapter.name: CasXlsxAdapter}
CAS_ADAPTER_NAMES = frozenset({CasPdfAdapter.name, *CAS_TABLE_ADAPTERS})
