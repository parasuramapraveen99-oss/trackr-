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

from decimal import Decimal
from typing import Any


def latest_snapshot(conn: Any, account_id: str) -> dict[str, Decimal]:
    """The most recently declared quantity per ISIN for one account -- the declared side
    of ``app.engine.reconcile.reconcile``, read back out of the rows ``record_snapshot``
    wrote.

    Per ISIN, not per statement: a CAS lists only the ISINs held on that statement date,
    so an account with two statements of different ages has a genuinely different
    "latest" date for different ISINs, and taking a single global MAX(snap_date) would
    silently drop every holding absent from the newest file. DISTINCT ON gives each ISIN
    its own newest row instead.

    An ISIN that has since been fully exited will keep reporting its last declared
    quantity, which is correct for reconciliation: ``reconcile()`` treats a declared
    quantity the ledger no longer shows as a discrepancy to explain, not as a holding.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (isin) isin, quantity FROM holding_snapshot "
            "WHERE account_id = %s ORDER BY isin, snap_date DESC",
            (account_id,),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def record_snapshot(conn: Any, holdings: list[Any], source: str = "CAS") -> int:
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
