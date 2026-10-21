"""Complete single-day contract-note CSV exports, including charges and note totals."""

from __future__ import annotations

import csv

from app.ingest.base import DocumentIdentity, ParseResult, Rejection
from app.ingest.tabular import cell_text, csv_rows
from app.ingest.zerodha_contract_note import CLIENT_PAN, CLIENT_UCC, parse_identity
from app.ingest.zerodha_contract_note_xlsx import (
    IDENTITY_MAX_ROW,
    ZerodhaContractNoteXlsxAdapter,
)


class ZerodhaContractNoteCsvAdapter(ZerodhaContractNoteXlsxAdapter):
    name = "zerodha_contract_note_csv"

    def parse(self, content: bytes, account_id: str, filename: str = "") -> ParseResult:
        import openpyxl

        try:
            rows = csv_rows(content)
        except (ValueError, csv.Error) as exc:
            return ParseResult(rejections=[
                Rejection(0, f"could not read contract-note CSV: {exc}")
            ])
        headers = sum(
            any("order no" in cell.lower() for cell in row)
            and any("trade no" in cell.lower() for cell in row) for row in rows
        )
        header = self._find_bulk_header(rows)
        header_index = header[0] if header else 0
        # A broker's own PAN may appear in the footer. Only the client header block
        # and explicitly labelled client identities elsewhere describe the owner.
        text = "\n".join(" ".join(row) for index, row in enumerate(rows) if (
            index < header_index
            or any("pan of client" in cell.lower() or "ucc of client" in cell.lower()
                   for cell in row)
        ))
        identities = [
            {match[1].upper() for match in pattern.finditer(text)}
            for pattern in (CLIENT_PAN, CLIENT_UCC)
        ]
        dates = {
            self._bulk_trade_date([row], 0, "") for row in rows
            if any("trade date" in cell.lower() for cell in row)
        } - {None}
        if headers != 1 or any(len(values) > 1 for values in identities) or len(dates) > 1:
            return ParseResult(rejections=[Rejection(
                0, "Upload one complete contract note per CSV, for one client and one trade date.",
            )])
        # Reuse the cell-native note reader and all three charges/turnover checks. A
        # Trades-only export lacks its obligation/net amount and is explicitly rejected.
        workbook = openpyxl.Workbook()
        try:
            sheet = workbook.active
            sheet.title = "CSV contract note"
            for row in rows:
                sheet.append(row)
            return self._parse_bulk(workbook, account_id)
        finally:
            workbook.close()

    def identity(self, content: bytes) -> DocumentIdentity:
        try:
            rows = csv_rows(content)
        except (ValueError, csv.Error):
            return DocumentIdentity()
        return parse_identity("\n".join(
            " ".join(cell_text(cell) for cell in row) for row in rows[:IDENTITY_MAX_ROW]
        ))
