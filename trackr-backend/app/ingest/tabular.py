"""Cell readers shared by structured CSV and XLSX document adapters."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime
from itertools import islice
from typing import Any


@dataclass(frozen=True)
class Sheet:
    name: str
    rows: list[tuple[Any, ...]]


def cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def csv_rows(content: bytes, *, max_rows: int | None = None) -> list[tuple[str, ...]]:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("This CSV is not readable text.")
    # CSV exports may contain tabs instead of commas; no guesses about cell contents.
    first = next((line for line in text.splitlines() if line.strip()), "")
    delimiter = "\t" if "\t" in first else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter, strict=True)
    return [tuple(row) for row in islice(reader, max_rows)]


def workbook_sheets(content: bytes, *, max_rows: int | None = None) -> list[Sheet]:
    import openpyxl

    workbook = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    try:
        sheets = []
        for sheet in workbook:
            # Some actual broker exports declare A1:A1 despite containing 80+ rows.
            # Stream the XML instead of trusting that incorrect cached dimension.
            sheet.reset_dimensions()
            rows = sheet.iter_rows(values_only=True)
            sheets.append(Sheet(sheet.title, list(islice(rows, max_rows))))
        return sheets
    finally:
        workbook.close()
