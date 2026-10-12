"""The investor's recorded answer to a corporate-action notice (migration 067).

Writes only ``reference_notice_outcome``. It never writes a transaction: a notice carries
no price, and a transaction without one is not a transaction (CLAUDE.md rule 4). An
investor who tendered or applied records the trade through the normal entry path, and the
outcome here only takes the notice off the reference-status list.
"""

from __future__ import annotations

from typing import Any

#: The outcomes that take a notice off the reference-status list. UNSURE does not: the
#: investor has not decided, so the notice stays visible.
CLEARING_OUTCOMES = ("NOT_PARTICIPATED", "PARTICIPATED")
OUTCOMES = (*CLEARING_OUTCOMES, "UNSURE")


class UnknownOutcomeError(ValueError):
    """The outcome is not one of ``OUTCOMES``."""


def record_outcome(
    conn: Any,
    investor_id: str,
    *,
    isin: str,
    ex_date: str,
    subject: str,
    outcome: str,
    note: str | None = None,
) -> None:
    """Create or replace the investor's answer for one notice. Does not commit."""
    if outcome not in OUTCOMES:
        raise UnknownOutcomeError(f"outcome must be one of {', '.join(OUTCOMES)}")
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO reference_notice_outcome
                   (investor_id, isin, ex_date, subject, outcome, note)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (investor_id, isin, ex_date, subject) DO UPDATE SET
                   outcome = EXCLUDED.outcome,
                   note = EXCLUDED.note,
                   recorded_at = now()""",
            (investor_id, isin, ex_date, subject, outcome, note),
        )


def cleared_notice_clause() -> str:
    """SQL, for embedding in a notice query, that is true for a notice the investor has
    already answered with a clearing outcome. Parameters: investor id, then the notice's
    own isin, ex_date and subject columns are referenced by alias ``n``."""
    return (
        "EXISTS (SELECT 1 FROM reference_notice_outcome o"
        " WHERE o.investor_id = %s AND o.isin = n.isin AND o.ex_date = n.ex_date"
        " AND o.subject = n.subject AND o.outcome IN ('NOT_PARTICIPATED', 'PARTICIPATED'))"
    )
