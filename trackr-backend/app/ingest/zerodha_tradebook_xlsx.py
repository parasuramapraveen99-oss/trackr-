"""Zerodha Tradebook workbooks: the same columns and trade identity as CSV exports."""

from __future__ import annotations

from datetime import date, datetime

from app.ingest.base import ParseResult, Rejection
from app.ingest.tabular import cell_text, workbook_sheets
from app.ingest.zerodha_tradebook import (
    MAX_HEADER_SCAN_LINES,
    REQUIRED_COLUMNS,
    TRADEBOOK_NOTES,
    ZerodhaTradebookAdapter,
    _norm_key,
)


class ZerodhaTradebookXlsxAdapter(ZerodhaTradebookAdapter):
    name = "zerodha_tradebook_xlsx"

    def parse(self, content: bytes, account_id: str, filename: str = "") -> ParseResult:
        try:
            sheets = workbook_sheets(content)
        except Exception as exc:  # openpyxl reports several kinds of damaged workbooks
            return ParseResult(rejections=[Rejection(0, f"could not read XLSX: {exc}")])
        notes = list(TRADEBOOK_NOTES)
        rows, rejections, names = [], [], []
        offset = 0
        for sheet in sheets:
            header_index = next((
                index for index, row in enumerate(sheet.rows[:MAX_HEADER_SCAN_LINES])
                if REQUIRED_COLUMNS <= {_norm_key(cell_text(cell)) for cell in row}
            ), None)
            if header_index is None:
                offset += len(sheet.rows)
                continue
            names.append(sheet.name)
            headers = [_norm_key(cell_text(cell)) for cell in sheet.rows[header_index]]
            while headers and not headers[-1]:
                headers.pop()
            named = [key for key in headers if key]
            if len(named) != len(set(named)):
                return ParseResult(rejections=[Rejection(
                    offset + header_index + 1,
                    f"sheet {sheet.name!r}: duplicate column names in tradebook header",
                )], notes=notes)
            for index, cells in enumerate(sheet.rows[header_index + 1:], header_index + 2):
                if not any(cell_text(cell) for cell in cells):
                    continue
                raw = {"sheet": sheet.name, "sheet_row": index}
                if any(cell_text(cell) for cell in cells[len(headers):]):
                    rejections.append(Rejection(
                        offset + index,
                        f"sheet {sheet.name!r}: data outside the header columns", raw,
                    ))
                    continue
                for column, key in enumerate(headers):
                    if not key:
                        continue
                    cell = cells[column] if column < len(cells) else None
                    if key == "trade_date" and isinstance(cell, (date, datetime)):
                        value = cell.date().isoformat() if isinstance(cell, datetime) else str(cell)
                    else:
                        value = cell_text(cell)
                    raw[key] = value
                parsed = self._parse_row(raw, account_id, offset + index)
                if isinstance(parsed, Rejection):
                    rejections.append(parsed)
                else:
                    rows.append(parsed)
            offset += len(sheet.rows)
        if not names:
            return ParseResult(rejections=[Rejection(
                0, "not a Zerodha tradebook: no worksheet has the required trade columns",
            )], notes=notes)
        return ParseResult(rows=rows, rejections=rejections,
                           notes=[*notes, "Tradebook worksheets: " + ", ".join(names)])
