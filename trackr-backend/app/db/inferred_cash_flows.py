"""The investor's confirmations of inferred cash flows (migration 068).

Writes only ``inferred_cash_flow_confirmation``. Confirming records that the investor has
looked at an inferred rights or IPO cash flow. It never changes a transaction or any figure.
"""

from __future__ import annotations

from datetime import date
from typing import Any

FlowKey = tuple[str, date, str, str]


def record_confirmation(
    conn: Any, investor_id: str, key: FlowKey, confirmed_by: str = "INVESTOR"
) -> None:
    """Confirm one inferred flow. Idempotent: confirming twice keeps the first timestamp.
    ``confirmed_by`` is INVESTOR (their click) or CAS (a matching statement, rule 20f)."""
    isin, txn_date, txn_type, source_ref = key
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO inferred_cash_flow_confirmation
                   (investor_id, isin, txn_date, txn_type, source_ref, confirmed_by)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT DO NOTHING""",
            (investor_id, isin, txn_date, txn_type, source_ref, confirmed_by),
        )


def confirmed_keys(conn: Any, investor_id: str) -> set[FlowKey]:
    """Every flow this investor has confirmed."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT isin, txn_date, txn_type, source_ref
               FROM inferred_cash_flow_confirmation WHERE investor_id = %s""",
            (investor_id,),
        )
        return {(row[0], row[1], row[2], row[3]) for row in cur.fetchall()}


def confirmed_by(conn: Any, investor_id: str) -> dict[FlowKey, str]:
    """Every confirmed flow of this investor, with who confirmed it (INVESTOR or CAS)."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT isin, txn_date, txn_type, source_ref, confirmed_by
               FROM inferred_cash_flow_confirmation WHERE investor_id = %s""",
            (investor_id,),
        )
        return {(r[0], r[1], r[2], r[3]): r[4] for r in cur.fetchall()}
