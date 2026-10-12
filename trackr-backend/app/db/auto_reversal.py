"""Rule 20(f): an auto-resolved row answers to the investor's own documents.

Trackr+ adds a rights or IPO allotment (rule 20) only to close a gap no document explained.
When a document the investor uploads -- a contract note, or a CAS -- shows that row is no
longer needed, or that the shares never arrived, the row is reversed here, automatically;
when a CAS shows they did arrive, it is confirmed. The investor's word alone never changes
the ledger (owner decision, 2026-10-10): the screen's "Not right" only asks for a document.

A reversal is a new row (migration 076): ``reverses_id`` names the voided row, ``source``
is CORRECTION, and replay drops both (``app.db.repository.load_transactions``). The
original is never edited. ``app.db.gap_resolution`` never re-adds a reversed resolution,
so a reversed gap stays open and named instead of looping.

Runs as pass 0 of the GAP task, i.e. after every replay -- and a CAS upload now triggers
one (``app.db.imports``).
"""

from __future__ import annotations

import json
import logging
from datetime import date
from decimal import Decimal
from typing import Any

from app.engine.gap_resolution import CONFIRM, REVERSE, auto_row_verdict
from app.engine.types import Transaction, TxnType

logger = logging.getLogger(__name__)

AUTO_PREFIX = "AUTO_RESOLVED"


def is_inferred_cash_row(t: Transaction) -> bool:
    """The rows "Added for you" lists: a BUY or IPO_ALLOT rule 20 wrote, with a price."""
    return bool(
        t.source_ref
        and t.source_ref.startswith(AUTO_PREFIX)
        and t.txn_type in (TxnType.BUY, TxnType.IPO_ALLOT)
        and t.price > 0
    )


def reversed_refs(conn: Any, account_id: str) -> set[str]:
    """``source_ref`` of every auto-resolved row of the account that has been reversed --
    the resolutions ``app.db.gap_resolution`` must not write again."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT o.source_ref FROM transaction o
               JOIN transaction r ON r.reverses_id = o.id
               WHERE o.account_id = %s AND o.source_ref LIKE %s""",
            (account_id, f"{AUTO_PREFIX}%"),
        )
        return {row[0] for row in cur.fetchall()}


def _cas_counts(conn: Any, account_id: str, isin: str) -> dict[date, Decimal]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT snap_date, quantity FROM holding_snapshot WHERE account_id = %s AND isin = %s",
            (account_id, isin),
        )
        return {row[0]: Decimal(row[1]) for row in cur.fetchall()}


def reverse_transaction(conn: Any, txn_id: str, reason: str) -> None:
    """Append the reversal of one row. Idempotent: a row is reversed at most once."""
    payload = json.dumps({"resolution": "REVERSED", "rule": "20f", "reason": reason})
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO transaction (account_id, isin, txn_date, txn_type, quantity, price,
                   source, source_ref, dedupe_key, raw_payload, reverses_id)
               SELECT account_id, isin, txn_date, txn_type, quantity, price,
                   'CORRECTION', source_ref, 'REVERSAL:' || id::text, %s::jsonb, id
               FROM transaction WHERE id = %s
               ON CONFLICT DO NOTHING""",
            (payload, txn_id),
        )


def check_auto_rows(conn: Any, account_id: str) -> dict[str, list[dict[str, str]]]:
    """Reverse or confirm each inferred cash row of one account by its own documents.
    Commits once at the end; returns what it did."""
    from app.db.inferred_cash_flows import record_confirmation
    from app.db.repository import load_transactions

    transactions = load_transactions(conn, account_id=account_id)
    rows = [t for t in transactions if is_inferred_cash_row(t)]
    out: dict[str, list[dict[str, str]]] = {"reversed": [], "confirmed": []}
    if not rows:
        return out
    with conn.cursor() as cur:
        cur.execute("SELECT investor_id::text FROM account WHERE id = %s", (account_id,))
        investor = cur.fetchone()
    for row in rows:
        verdict = auto_row_verdict(transactions, row, _cas_counts(conn, account_id, row.isin))
        item = {"isin": row.isin, "date": row.txn_date.isoformat(), "reason": verdict.reason}
        if verdict.action == REVERSE and row.id:
            reverse_transaction(conn, row.id, verdict.reason)
            out["reversed"].append(item)
            logger.info("auto_row_reversed", extra=item)
            # Later rows are judged against the ledger as it now stands.
            transactions = [t for t in transactions if t.id != row.id]
        elif verdict.action == CONFIRM and investor:
            key = (row.isin, row.txn_date, row.txn_type.value, row.source_ref or "")
            record_confirmation(conn, investor[0], key, confirmed_by="CAS")
            out["confirmed"].append(item)
    conn.commit()
    return out
