"""Portfolio-snapshot persistence -- docs/ENGINE_SPEC.md section 8.

Narrow and typed, like ``app/db/prices.py``: this module only reads and writes rows. It
has no opinion about WHEN a snapshot should be computed or what to do when a price is
missing -- that orchestration lives in ``app.routers.analytical._run_snapshots``, the only
caller allowed to write here. Every row is a cache of ``compute_snapshot()``, never a
source of truth, so nothing in this module ever mutates a stored total in place.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from app.engine.types import Snapshot


def upsert_snapshots(conn: Any, rows: list[tuple[str, bool, Snapshot]]) -> None:
    """One statement per completed batch, with the same NUMERIC precision as a point write."""
    if not rows:
        return
    payload = [
        {
            "investor_id": investor,
            "advised_only": advised,
            "as_of": point.as_of.isoformat(),
            "invested": str(point.invested),
            "market_value": str(point.market_value),
            "unrealised": str(point.unrealised),
            "realised_to_date": str(point.realised_to_date),
            "excluded_isins": list(point.excluded_isins),
        }
        for investor, advised, point in rows
    ]
    conn.execute(
        """INSERT INTO portfolio_snapshot
           (investor_id,advised_only,as_of,invested,market_value,unrealised,
            realised_to_date,excluded_isins)
           SELECT * FROM jsonb_to_recordset(%s::jsonb) AS p
           (investor_id uuid,advised_only boolean,as_of date,invested numeric,
            market_value numeric,unrealised numeric,realised_to_date numeric,excluded_isins text[])
           ON CONFLICT (investor_id,as_of,advised_only) DO UPDATE SET
             invested=EXCLUDED.invested,market_value=EXCLUDED.market_value,
             unrealised=EXCLUDED.unrealised,realised_to_date=EXCLUDED.realised_to_date,
             excluded_isins=EXCLUDED.excluded_isins,computed_at=clock_timestamp()
           WHERE """
        + _CHANGED,
        (json.dumps(payload),),
    )


#: An upsert's DO UPDATE applies only when a stored figure differs. Re-valuing a day to the
#: same numbers then changes no row, so it neither moves the chart's version
#: (count:max computed_at) nor invalidates any cached analytics report (migration 093).
_CHANGED = """(portfolio_snapshot.invested,portfolio_snapshot.market_value,
             portfolio_snapshot.unrealised,portfolio_snapshot.realised_to_date,
             portfolio_snapshot.excluded_isins)
           IS DISTINCT FROM (EXCLUDED.invested,EXCLUDED.market_value,EXCLUDED.unrealised,
             EXCLUDED.realised_to_date,EXCLUDED.excluded_isins)"""


def settle_rebuild_tasks(conn: Any, rows: list[tuple[str, bool, Snapshot]], read_snapshot) -> None:
    """Close queued SNAPSHOT rebuilds for days just written from inputs that already
    included the change that queued them.

    A task carries ``queued_xid``: the transaction that queued it (a replay that deleted
    the day, or an interior hole being seen). ``read_snapshot`` was taken before the
    writer read any input, so when that transaction is visible in it, the row just written
    reflects the change and the worker re-valuing the day would only reproduce it. A task
    queued by a transaction the writer could not see stays pending, and a task the worker
    has already claimed ('running') is left to the worker.
    """
    if not rows or read_snapshot is None:
        return
    keys = [f"{investor}:{point.as_of.isoformat()}:{advised}" for investor, advised, point in rows]
    conn.execute(
        """UPDATE reference_sync_task SET state='done',last_error=NULL,attempts=0,
             payload=payload-'reference_missing',
             updated_at=clock_timestamp(),last_success_at=clock_timestamp()
           WHERE kind='SNAPSHOT' AND (state IN ('pending','retry')
               OR (state='done' AND payload ? 'reference_missing')) AND task_key=ANY(%s)
             AND payload ? 'queued_xid'
             AND pg_visible_in_snapshot((payload->>'queued_xid')::xid8, %s::pg_snapshot)""",
        (keys, read_snapshot),
    )


def upsert_snapshot(conn: Any, investor_id: str, advised_only: bool, snapshot: Snapshot) -> None:
    """Store one snapshot. Idempotent: rerunning a date changes nothing.

    ``ON CONFLICT DO UPDATE`` rather than ``DO NOTHING``, same reasoning as
    ``upsert_prices`` -- a rerun (say, after a price correction) should carry the better
    number through, not be a no-op forever.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO portfolio_snapshot
                (investor_id, as_of, advised_only, invested, market_value,
                 unrealised, realised_to_date, excluded_isins)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (investor_id, as_of, advised_only) DO UPDATE
            SET invested = EXCLUDED.invested,
                market_value = EXCLUDED.market_value,
                unrealised = EXCLUDED.unrealised,
                realised_to_date = EXCLUDED.realised_to_date,
                excluded_isins = EXCLUDED.excluded_isins,
                computed_at = now()
            WHERE """
            + _CHANGED,
            (
                investor_id,
                snapshot.as_of,
                advised_only,
                snapshot.invested,
                snapshot.market_value,
                snapshot.unrealised,
                snapshot.realised_to_date,
                list(snapshot.excluded_isins),
            ),
        )


def get_snapshot(
    conn: Any, investor_id: str, as_of: date, advised_only: bool = False
) -> Snapshot | None:
    """The persisted snapshot for one investor/date/series, or ``None`` if never written.

    A miss is not an error -- it means the caller should fall back to a live
    ``compute_snapshot()`` call, exactly as ``app.engine.xirr.compute_fy_xirr`` and
    ``app.routers.analytical._window_xirr`` already do when
    this table has nothing for the date they need.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT as_of, invested, market_value, unrealised, realised_to_date, excluded_isins
            FROM portfolio_snapshot
            WHERE investor_id = %s AND as_of = %s AND advised_only = %s
            """,
            (investor_id, as_of, advised_only),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return Snapshot(
        as_of=row[0],
        invested=row[1],
        market_value=row[2],
        unrealised=row[3],
        realised_to_date=row[4],
        excluded_isins=tuple(row[5] or ()),
    )


def get_snapshot_on_or_before(
    conn: Any, investor_id: str, as_of: date, advised_only: bool = False
) -> Snapshot | None:
    """The most recent persisted snapshot at or before ``as_of``, or ``None`` if the
    series has nothing that far back yet.

    Subsumes ``get_snapshot``'s exact-match case -- when a row exists exactly on
    ``as_of``, it is necessarily the latest qualifying row, so one query answers both "is
    there a snapshot for this exact date" and "what's the closest one before it". Uses
    ``portfolio_snapshot_range_idx (investor_id, advised_only, as_of)`` as a backward
    index scan.

    Callers MUST use the returned ``Snapshot``'s own ``as_of`` -- not the requested
    ``as_of`` -- as ``compute_period_xirr``'s ``start_date``. Labelling the opening value
    with a later date than the one it actually came from would silently drop every
    transaction dated in the gap from both the opening value and the flow list; see
    ``app.routers.analytical._window_xirr``.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT as_of, invested, market_value, unrealised, realised_to_date, excluded_isins
            FROM portfolio_snapshot
            WHERE investor_id = %s AND advised_only = %s AND as_of <= %s
            ORDER BY as_of DESC
            LIMIT 1
            """,
            (investor_id, advised_only, as_of),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return Snapshot(
        as_of=row[0],
        invested=row[1],
        market_value=row[2],
        unrealised=row[3],
        realised_to_date=row[4],
        excluded_isins=tuple(row[5] or ()),
    )


def list_snapshots_range(
    conn: Any, investor_id: str, advised_only: bool, start: date, end: date
) -> list[Snapshot]:
    """Persisted rows for one investor/series between ``start`` and ``end``, inclusive,
    ascending by ``as_of`` -- exactly what the historical-value chart needs (Phase 2
    Session 2) and nothing the ledger has to be replayed for. Uses
    ``portfolio_snapshot_range_idx (investor_id, advised_only, as_of)``.

    A date with no persisted row is simply absent from the result -- never filled,
    never interpolated. The caller renders that as a gap, exactly like a miss in
    ``get_snapshot`` is a signal to fall back, not an error.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT as_of, invested, market_value, unrealised, realised_to_date, excluded_isins
            FROM portfolio_snapshot
            WHERE investor_id = %s AND advised_only = %s AND as_of BETWEEN %s AND %s
            ORDER BY as_of ASC
            """,
            (investor_id, advised_only, start, end),
        )
        rows = cur.fetchall()
    return [
        Snapshot(
            as_of=row[0],
            invested=row[1],
            market_value=row[2],
            unrealised=row[3],
            realised_to_date=row[4],
            excluded_isins=tuple(row[5] or ()),
        )
        for row in rows
    ]


def snapshot_dates_present(
    conn: Any, investor_id: str, advised_only: bool, start: date, end: date
) -> set[date]:
    """Dates already written for this investor/series, so a backfill can resume.

    Mirrors ``app.db.prices.price_dates_settled`` for the same reason: a multi-year,
    multi-investor backfill is a lot of requests, and it has to be resumable rather than
    starting over after a timeout.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT as_of FROM portfolio_snapshot
            WHERE investor_id = %s AND advised_only = %s AND as_of BETWEEN %s AND %s
            """,
            (investor_id, advised_only, start, end),
        )
        return {row[0] for row in cur.fetchall()}
