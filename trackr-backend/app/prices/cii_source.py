"""Where cost_inflation_index rows come from -- docs/TAX_ENGINE.md's "Indexation":
"Needs a static CII table (CBDT values, hardcoded, one row per FY)." "Hardcoded" there
means "a static published fact", not "buried in engine code" -- CLAUDE.md's "these
change annually -- keep them in data, never in code" rule applies to the values
themselves the same way it applies to the 1 April cutoff
(``app.engine.indexation.INDEXATION_CUTOFF_FY``).

CBDT (the Central Board of Direct Taxes) publishes exactly one CII number per Indian
financial year, by notification, typically mid-year for the year already under way.
Like ``fmv_31jan2018_source.py``, there is no single stable URL that reliably re-serves
the whole historical series today, so this takes a locally-sourced CSV rather than a
live fetch -- the loader pattern docs/PHASE3_PROMPTS.md's Session 4 prompt says to reuse
from Session 3 rather than invent a second one: required-columns check, per-row skip on
blanks, a FormatError raised only on a genuine shape break.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

_FY_COLUMN = "financial_year"
_CII_COLUMN = "cii_value"
_SOURCE_COLUMN = "source"

#: The three columns this parser actually depends on. Missing any is the clearest signal
#: the source file isn't shaped the way this loader expects.
_REQUIRED_COLUMNS = {_FY_COLUMN, _CII_COLUMN, _SOURCE_COLUMN}

#: Same shape migration 013's CHECK constraint requires on
#: ``cost_inflation_index.financial_year`` -- kept in sync deliberately so a malformed FY
#: label is rejected here rather than failing later as an opaque database error.
_FY_PATTERN = re.compile(r"^\d{4}-\d{2}$")


@dataclass(frozen=True, slots=True)
class CIIRow:
    financial_year: str
    cii_value: Decimal
    source: str


class CIISourceFormatError(Exception):
    """The file parsed as CSV but is missing a required column, a row's FY label isn't
    ``"YYYY-YY"``, or a row's CII value is present but not a positive number --
    migration 013's CHECK constraints require the same, so a bad value is rejected here
    rather than failing later as an opaque database error."""


def parse_cii_csv(payload: bytes) -> list[CIIRow]:
    """financial_year -> (cii_value, source) rows from a locally-sourced CSV.

    A row with a blank financial_year, cii_value, or source is skipped (footer/note
    artefacts are common in hand-compiled reference files), matching
    fmv_31jan2018_source.py's convention -- only a missing COLUMN, a malformed FY label,
    or a non-positive CII value is a format break, never a missing individual value.

    Raises:
        CIISourceFormatError: header is missing ``financial_year``/``cii_value``/
            ``source``, a row's FY isn't ``"YYYY-YY"``, or a row's CII value isn't a
            positive decimal.
    """
    text = payload.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = set(reader.fieldnames or [])
    missing = _REQUIRED_COLUMNS - fieldnames
    if missing:
        raise CIISourceFormatError(
            f"CII source: missing column(s) {sorted(missing)} in header {reader.fieldnames}"
        )

    rows: list[CIIRow] = []
    for record in reader:
        fy = (record.get(_FY_COLUMN) or "").strip()
        raw_cii = (record.get(_CII_COLUMN) or "").strip()
        source = (record.get(_SOURCE_COLUMN) or "").strip()
        if not fy or not raw_cii or not source:
            continue
        if not _FY_PATTERN.match(fy):
            raise CIISourceFormatError(f"CII source: financial_year {fy!r} is not 'YYYY-YY'")
        try:
            cii_value = Decimal(raw_cii)
        except InvalidOperation as exc:
            raise CIISourceFormatError(
                f"CII source: unreadable CII value {raw_cii!r} for FY {fy}"
            ) from exc
        if cii_value <= 0:
            raise CIISourceFormatError(
                f"CII source: non-positive CII value {cii_value} for FY {fy}"
            )
        rows.append(CIIRow(financial_year=fy, cii_value=cii_value, source=source))
    return rows
