"""``holding_snapshot`` writes -- CAS/broker-declared quantities, used to CHECK the
engine, never to display. See app/db/migrations/001_core.sql's comment on the table and
app/engine/reconcile.py, which is what actually compares these against
compute_holdings().

Nothing here is the ledger. This table carries no review/promote gate -- it is a
checking dataset, not a source of truth (the table's own comment says so), so a
snapshot lands as soon as an adapter produces one. That is deliberate: unlike a
Transaction, a wrong declared quantity here cannot corrupt a client's cost basis, it
can only make a reconciliation check less useful.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from decimal import Decimal
from typing import Any


def snapshot_date(conn: Any, account_id: str, as_of=None):
    """One date, bounded by the requested cutoff. Never a per-ISIN mixture."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT MAX(snap_date) FROM holding_snapshot WHERE account_id=%s "
            "AND (%s::date IS NULL OR snap_date <= %s::date)",
            (account_id, as_of, as_of),
        )
        return cur.fetchone()[0]


def snapshot_at(conn: Any, account_id: str, on) -> dict[str, Decimal]:
    """Newest individual observation on exactly one date; legacy fallback is partial."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT quantities FROM reconciliation_statement "
            "WHERE account_id=%s AND snap_date=%s ORDER BY seq DESC LIMIT 1",
            (account_id, on),
        )
        row = cur.fetchone()
        if row:
            return {isin: Decimal(qty) for isin, qty in row[0].items()}
        cur.execute(
            "SELECT isin, quantity FROM holding_snapshot WHERE account_id=%s AND snap_date=%s",
            (account_id, on),
        )
        return {isin: quantity for isin, quantity in cur.fetchall()}


def selected_statement(conn, account_id, on, statement_id=None):
    """Resolve one immutable source; refuse ambiguous same-date imports."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, quantities, source, batch_id FROM reconciliation_statement "
            "WHERE account_id=%s AND snap_date=%s "
            "AND (%s::uuid IS NULL OR id=%s::uuid) ORDER BY seq DESC",
            (account_id, on, statement_id, statement_id),
        )
        rows = cur.fetchall()
    if len(rows) > 1:
        raise ValueError("Multiple observations on this date; select one statement")
    if rows:
        sid, quantities, source, batch = rows[0]
        return (
            {isin: Decimal(qty) for isin, qty in quantities.items()},
            {"id": str(sid), "source": source, "batch_id": str(batch) if batch else None},
        )
    if statement_id:
        raise ValueError("Statement does not belong to this account and date")
    return snapshot_at(conn, account_id, on), {"id": None, "source": "LEGACY_PARTIAL"}


def latest_snapshot(conn: Any, account_id: str) -> dict[str, Decimal]:
    """Partial observations from one most recent date. Absence is not a zero."""
    on = snapshot_date(conn, account_id)
    return snapshot_at(conn, account_id, on) if on else {}


def statement_dates(conn: Any, investor_id: str, account_id: str):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT s.id,s.snap_date,s.quantities,s.source FROM reconciliation_statement s "
            "JOIN account a ON a.id=s.account_id WHERE a.investor_id=%s AND s.account_id=%s "
            "ORDER BY s.snap_date DESC,s.seq DESC LIMIT 100",
            (investor_id, account_id),
        )
        result = [
            {
                "id": str(sid),
                "as_of": on.isoformat(),
                "positions": len(qty),
                "sources": [source],
                "completeness": "UNVERIFIED",
            }
            for sid, on, qty, source in cur.fetchall()
        ]
        cur.execute(
            "SELECT h.snap_date,COUNT(*),array_agg(DISTINCT h.source) "
            "FROM holding_snapshot h JOIN account a ON a.id=h.account_id "
            "WHERE a.investor_id=%s AND h.account_id=%s AND NOT EXISTS "
            "(SELECT 1 FROM reconciliation_statement s WHERE s.account_id=h.account_id "
            "AND s.snap_date=h.snap_date) GROUP BY h.snap_date "
            "ORDER BY h.snap_date DESC LIMIT 100",
            (investor_id, account_id),
        )
        result.extend(
            {
                "id": None,
                "as_of": on.isoformat(),
                "positions": count,
                "sources": sources,
                "completeness": "LEGACY_PARTIAL",
            }
            for on, count, sources in cur.fetchall()
        )
        return sorted(result, key=lambda r: r["as_of"], reverse=True)[:100]


def record_snapshot(
    conn: Any, holdings: list[Any], source: str = "CAS", batch_id: str | None = None
) -> int:
    """Upsert one row per ``DeclaredHolding`` (app.ingest.cas_pdf.DeclaredHolding, or
    any object with the same ``isin``/``account_id``/``quantity``/``snap_date``
    attributes).

    ``ON CONFLICT (account_id, isin, snap_date) DO UPDATE`` on the table's own primary
    key: re-processing the same statement period overwrites cleanly, which is exactly
    what should happen when the same CAS is forwarded twice (docs/INGESTION.md
    "Idempotency: assume every message may arrive twice") -- no separate dedupe key is
    needed here the way transaction.dedupe_key is needed for the ledger, because this
    table only ever holds the LATEST declared quantity for a given account/ISIN/date,
    never a history of corrections.

    Each new observation set is also retained in reconciliation_statement with its
    source/batch and a stable fingerprint. Same-date documents remain separate;
    only the legacy compatibility table is overwritten.

    Returns the number of rows written. Holdings with no resolved ``account_id`` (the
    empty string a caller passes when it has none to give) are skipped, not written
    with a null account -- ``holding_snapshot.account_id`` is NOT NULL.

    ``holding_snapshot.isin`` is a foreign key into ``security`` (001_core.sql) --
    ``promote_batch`` (app/db/imports.py) satisfies the same constraint for
    ``transaction`` by inserting a bare security row before any ledger write, since an
    import is how a new holding is first discovered, not something the master can be
    pre-populated for. A declared holding needs the identical treatment and for the
    identical reason: a CAS's Holdings table lists every ISIN it holds -- equity AND
    fund-in-demat entries alike (``ISIN_PATTERN`` matches both INE and INF) -- and a
    fund-in-demat ISIN will never appear in a promoted transaction (mutual funds are
    out of Phase 1 scope), so without this it could never get a ``security`` row any
    other way. Found against a real statement: ``INF247L01BQ9`` failed with
    ``ForeignKeyViolation`` before this existed.
    """
    isins = {h.isin for h in holdings if h.account_id}
    with conn.cursor() as cur:
        for isin in sorted(isins):
            cur.execute(
                "INSERT INTO security (isin) VALUES (%s) ON CONFLICT (isin) DO NOTHING",
                (isin,),
            )

    grouped = defaultdict(dict)
    for h in holdings:
        if h.account_id:
            group = grouped[(h.account_id, h.snap_date)]
            if h.isin in group:
                raise ValueError("Duplicate ISIN in one account statement")
            group[h.isin] = str(h.quantity)
    with conn.cursor() as cur:
        for (account_id, on), quantities in grouped.items():
            # Identical retries are no-ops. Changed reparses create a new observation,
            # preserving the old evidence for saved runs and explicit selection.
            fingerprint = hashlib.sha256(
                json.dumps(
                    [account_id, batch_id, str(on), source, quantities], sort_keys=True
                ).encode()
            ).hexdigest()
            cur.execute(
                "INSERT INTO reconciliation_statement "
                "(account_id,batch_id,snap_date,source,quantities,fingerprint) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (fingerprint) DO NOTHING",
                (account_id, batch_id, on, source, json.dumps(quantities), fingerprint),
            )

    written = 0
    with conn.cursor() as cur:
        for h in holdings:
            if not h.account_id:
                continue
            cur.execute(
                """
                INSERT INTO holding_snapshot (account_id, isin, snap_date, quantity, source)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (account_id, isin, snap_date)
                DO UPDATE SET quantity = EXCLUDED.quantity, source = EXCLUDED.source
                """,
                (h.account_id, h.isin, h.snap_date, h.quantity, source),
            )
            written += 1
    return written
