"""How a figure computed WITHOUT some securities says so.

docs/ARCHITECTURE_INVARIANTS.md rule 16 and the 2026-10-08 owner amendment: performance,
lots, realised and contribution compute on every security that reconciles and name the
ones they left out, instead of one unreconciled stock returning 409 for the whole
portfolio. Object responses carry ``excluded``; list responses (lots, realised), whose
shape is a public contract, carry the same ISINs in the ``X-Trackr-Excluded`` header.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import Response

from app import securities
from app.engine.holdings import SkippedHolding
from app.engine.types import Transaction

#: Comma-separated ISINs left out of a list response. Exposed for CORS in app.main.
EXCLUDED_HEADER = "X-Trackr-Excluded"


def describe(connection: Any, skipped: list[SkippedHolding]) -> list[dict[str, Any]]:
    if not skipped:
        return []
    meta = securities.lookup(connection, [s.isin for s in skipped])
    return [
        {
            "isin": s.isin,
            "symbol": securities.describe(meta, s.isin)["symbol"],
            "message": s.message,
            "deficit": str(s.deficit) if s.deficit is not None else None,
            "as_of": s.as_of,
        }
        for s in skipped
    ]


def mark(response: Response, skipped: list[SkippedHolding]) -> None:
    if skipped:
        response.headers[EXCLUDED_HEADER] = ",".join(s.isin for s in skipped)


def drop_unpriced(
    transactions: list[Transaction],
    isins: list[str],
    prices: dict[str, Decimal],
    on: date,
    skipped: list[SkippedHolding],
) -> tuple[list[Transaction], list[str], list[SkippedHolding]]:
    """Leave out every held ISIN with no usable price on ``on``, and say so.

    CLAUDE.md rule 4 forbids VALUING a missing price at zero; it does not require one
    unpriced stock to blank the portfolio. Dropping the ISIN's rows entirely (buys and
    sells) and naming it in ``excluded`` is the same partition-and-label posture as an
    unreconciled history (docs/ARCHITECTURE_INVARIANTS.md rule 16, owner amendment
    2026-10-08). The real case: GUJTHEM's retired ISIN, whose new ISIN carries every
    price after the 2023 ISIN change but whose identity link is not yet resolved.
    """
    missing = [isin for isin in isins if isin not in prices]
    if not missing:
        return transactions, isins, skipped
    gone = set(missing)
    skipped = skipped + [
        SkippedHolding(isin=isin, message=f"No usable price for {isin} on {on}", as_of=on)
        for isin in missing
    ]
    return (
        [t for t in transactions if t.isin not in gone],
        [isin for isin in isins if isin not in gone],
        skipped,
    )
