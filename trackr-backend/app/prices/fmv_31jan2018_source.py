"""Where security.fmv_31jan2018 comes from -- docs/TAX_ENGINE.md's "Grandfathering":
"a one-time historical constant per ISIN. Source it once from exchange archives."

Unlike ``sector_sources.py`` and ``market_cap_sources.py``, this is not a live,
refetchable feed. NSE and BSE each published one static "highest traded price as on
31 Jan 2018" list at the time the 2018 LTCG amendment took effect; there is no current
URL that reliably re-serves that data today, and pinning one down is exactly the kind
of financial-data sourcing docs/PHASE3_PROMPTS.md's Session 3 prompt puts out of scope
for this session's code ("Sourcing real fmv_31jan2018 values is explicitly out of scope
for the *code* in this session ... it's data acquisition").

So this module takes a locally-sourced CSV instead of a URL: a human downloads the
exchange archive (or a compiled equivalent) once and drops it on disk; this parses
whatever is there. Same shape as the two live-feed sources in every other respect --
required-columns check, per-row skip on blanks, a FormatError raised only on a genuine
shape break -- mirrored on purpose: docs/PHASE3_PROMPTS.md's Session 4 (CII table) is
told to "reuse whatever loader pattern Session 3 established" rather than invent a
second one, and this is that pattern with its `fetch()` half dropped.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

_ISIN_COLUMN = "ISIN"
_FMV_COLUMN = "fmv_31jan2018"

#: The two columns this parser actually depends on. Missing either is the clearest
#: signal the source file isn't shaped the way this loader expects.
_REQUIRED_COLUMNS = {_ISIN_COLUMN, _FMV_COLUMN}


@dataclass(frozen=True, slots=True)
class FmvRow:
    isin: str
    fmv_31jan2018: Decimal


class FmvSourceFormatError(Exception):
    """The file parsed as CSV but is missing a required column, or a row's FMV value
    is present but not a positive number -- migration 013's CHECK constraint requires
    the same (``fmv_31jan2018 IS NULL OR fmv_31jan2018 > 0``), so a bad value is
    rejected here rather than failing later as an opaque database error."""


def parse_fmv_31jan2018_csv(payload: bytes) -> list[FmvRow]:
    """ISIN -> fmv_31jan2018 rows from a locally-sourced CSV.

    A row with a blank ISIN or FMV value is skipped (footer/note artefacts are common
    in these hand-compiled files), matching sector_sources.py/market_cap_sources.py's
    convention -- only a missing COLUMN, or a non-positive FMV value, is a format
    break, never a missing individual value.

    Raises:
        FmvSourceFormatError: header is missing ``ISIN``/``fmv_31jan2018``, or a row's
            FMV isn't a positive decimal.
    """
    text = payload.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = set(reader.fieldnames or [])
    missing = _REQUIRED_COLUMNS - fieldnames
    if missing:
        raise FmvSourceFormatError(
            f"fmv_31jan2018 source: missing column(s) {sorted(missing)} "
            f"in header {reader.fieldnames}"
        )

    rows: list[FmvRow] = []
    for record in reader:
        isin = (record.get(_ISIN_COLUMN) or "").strip()
        raw_fmv = (record.get(_FMV_COLUMN) or "").strip()
        if not isin or not raw_fmv:
            continue
        try:
            fmv = Decimal(raw_fmv)
        except InvalidOperation as exc:
            raise FmvSourceFormatError(
                f"fmv_31jan2018 source: unreadable FMV value {raw_fmv!r} for ISIN {isin}"
            ) from exc
        if fmv <= 0:
            raise FmvSourceFormatError(
                f"fmv_31jan2018 source: non-positive FMV {fmv} for ISIN {isin}"
            )
        rows.append(FmvRow(isin=isin, fmv_31jan2018=fmv))
    return rows
