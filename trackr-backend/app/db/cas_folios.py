"""Owner-scoped, dated CAS fund observations. Never consumed by the ledger engine."""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Any

from app.ingest.cas_folios import DeclaredFundFolio


def tables_present(conn: Any) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('cas_fund_snapshot') IS NOT NULL")
        return bool(cur.fetchone()[0])


def record(conn: Any, investor_id: str, batch_id: str, rows: list[DeclaredFundFolio]) -> int:
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.execute("SELECT investor_id FROM import_batch WHERE id=%s", (batch_id,))
        batch = cur.fetchone()
        if not batch or str(batch[0]) != str(investor_id):
            raise ValueError("The CAS batch does not belong to this investor.")
        for row in rows:
            identity = "\0".join((str(investor_id), row.amc_name.casefold(), row.folio_number))
            key = hashlib.sha256(identity.encode()).hexdigest()
            label = "…" + row.folio_number[-4:]
            cur.execute(
                "INSERT INTO security (isin) VALUES (%s) ON CONFLICT DO NOTHING", (row.isin,)
            )
            cur.execute(
                """INSERT INTO cas_fund_snapshot
                   (investor_id,batch_id,folio_key,folio_label,isin,amc_name,scheme_name,
                    snap_date,quantity,nav,market_value)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (batch_id,folio_key,isin) DO UPDATE SET
                       snap_date=EXCLUDED.snap_date, quantity=EXCLUDED.quantity,
                       nav=EXCLUDED.nav, market_value=EXCLUDED.market_value,
                       scheme_name=EXCLUDED.scheme_name""",
                (
                    investor_id,
                    batch_id,
                    key,
                    label,
                    row.isin,
                    row.amc_name,
                    row.scheme_name,
                    row.snap_date,
                    row.quantity,
                    row.nav,
                    row.market_value,
                ),
            )
    return len(rows)


def latest(conn: Any, investor_id: str, on: date) -> list[dict[str, Any]]:
    """Latest declaration per folio/ISIN, with conflicts visible and exits retained.

    Repeated uploads of the same balance are one observation. Conflicting declarations
    on the same latest date have no display valuation until the evidence is resolved.
    The actual statement date stays on every row, even when older than ``on``.
    """
    if not tables_present(conn):
        return []
    with conn.cursor() as cur:
        cur.execute(
            """WITH eligible AS (
                   SELECT s.*, max(snap_date) OVER (PARTITION BY folio_key,isin) AS latest_date
                   FROM cas_fund_snapshot s JOIN import_batch b ON b.id=s.batch_id
                   WHERE s.investor_id=%s AND s.snap_date<=%s AND b.status<>'DISCARDED'
               )
               SELECT folio_key,isin,min(folio_label),min(scheme_name),min(amc_name),snap_date,
                      min(quantity),min(nav),min(market_value),
                      count(DISTINCT (quantity,nav,market_value)),array_agg(batch_id::text)
               FROM eligible WHERE snap_date=latest_date
               GROUP BY folio_key,isin,snap_date ORDER BY min(scheme_name),folio_key""",
            (investor_id, on),
        )
        rows = cur.fetchall()
        return [
            dict(
                folio_key=key,
                isin=isin,
                folio_label=label,
                name=name,
                amc_name=amc,
                as_of=day,
                quantity=str(qty) if versions == 1 else None,
                nav=str(nav) if versions == 1 else None,
                market_value=str(value) if versions == 1 else None,
                status="DECLARED" if versions == 1 else "CONFLICT",
                reason=None
                if versions == 1
                else "Conflicting CAS balances for this folio and date.",
                source_batch_ids=sorted(batches),
            )
            for key, isin, label, name, amc, day, qty, nav, value, versions, batches in rows
        ]
