"""The mutual-fund return series: its ledger scope, calendar, snapshots and repair.

docs/MF_ETF_PLAN.md sections 7.4 and 8. A separate series from the listed-securities one
(stocks and ETFs, ``portfolio_snapshot``), computed from the same immutable ledger
restricted to units on the AMFI route, valued at AMFI NAVs only.

THE CALENDAR IS THE FUNDS' OWN. A snapshot date is a date on which AMFI published a NAV
for some scheme the investor held -- never the exchange trading calendar. A held scheme
with no NAV that day carries its newest NAV (at most 7 days old):

* FINAL when that carry is evidenced -- AMFI has since published a later NAV for the
  scheme, so it published none for this date;
* PROVISIONAL otherwise -- the NAV may simply not be out yet. The row is rebuilt when it
  lands, and is never reported as final meanwhile.

A held scheme with no NAV within 7 days is EXCLUDED from that row and named in it;
the row is then not final either. Nothing is valued at zero.

REPAIR IS EXPLICIT, NOT INFERRED FROM WHAT EXISTS. Every run compares the full set of
required dates with the stored ones (so an interior hole is found, not just a short
tail), rebuilds provisional rows and dates marked dirty by a new or corrected NAV, and
rebuilds an investor's whole fund series when its fund ledger changed (fingerprint).
A dirty marker is cleared only if its version is unchanged since the rebuild read it, so
a correction landing mid-rebuild is never lost.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.db import fund_reference as ref
from app.db.repository import load_transactions
from app.engine.errors import MissingPriceError, NegativeHoldingError
from app.engine.holdings import compute_holdings_partitioned
from app.engine.snapshot import compute_snapshot
from app.engine.types import Snapshot, Transaction
from app.prices.routing import Route

logger = logging.getLogger(__name__)


def fund_ledger(
    conn: Any,
    investor_id: str,
    advised_only: bool = False,
    date_to: date | None = None,
) -> list[Transaction]:
    """The investor's ledger restricted to NAV-valued fund units -- scope applied to the
    ledger itself, before holdings, FIFO or cash flows (plan section 8)."""
    rows = load_transactions(
        conn, investor_id=investor_id, advised_only=advised_only, date_to=date_to
    )
    funds = set(ref.isins_on_route(conn, {t.isin for t in rows}, Route.AMFI))
    return [t for t in rows if t.isin in funds]


def fingerprint(transactions: list[Transaction]) -> str:
    digest = hashlib.sha256()
    for t in sorted(
        transactions,
        key=lambda t: (t.txn_date, str(t.id or t.dedupe_key or ""), str(t.txn_type)),
    ):
        digest.update(
            "|".join(
                str(v)
                for v in (
                    t.id or t.dedupe_key,
                    t.isin,
                    t.txn_date,
                    t.txn_type,
                    t.quantity,
                    t.price,
                    t.charges,
                    t.dividend_amount,
                    t.account_id,
                )
            ).encode()
        )
        digest.update(b"\n")
    return digest.hexdigest()


@dataclass(frozen=True)
class FundPoint:
    snapshot: Snapshot
    final: bool
    pending_isins: tuple[str, ...]
    excluded_isins: tuple[str, ...]


def compute_point(conn: Any, transactions: list[Transaction], on: date) -> FundPoint | None:
    """One fund-series point at ``on``, or None when nothing held then could be valued."""
    day = [t for t in transactions if t.txn_date <= on]
    if not day:
        return None
    reconciled, skipped = compute_holdings_partitioned(day, on)
    held = [h.isin for h in reconciled if h.quantity > 0]
    navs = ref.latest_fund_navs(conn, held, on)
    if held and not navs:
        return None
    missing = {isin for isin in held if isin not in navs}
    carried = [isin for isin in held if isin in navs and navs[isin].nav_date != on]
    evidenced = ref.has_nav_after(conn, carried, on)
    pending = sorted(set(carried) - evidenced)
    prices = {isin: quote.nav for isin, quote in navs.items()}
    snapshot = compute_snapshot(day, prices, on, exclude_isins=missing)
    excluded = tuple(sorted(set(snapshot.excluded_isins) | {s.isin for s in skipped}))
    return FundPoint(
        snapshot=snapshot,
        final=not pending and not excluded,
        pending_isins=tuple(pending),
        excluded_isins=excluded,
    )


def required_dates(conn: Any, transactions: list[Transaction], end: date) -> list[date]:
    """The fund series' calendar: every date AMFI published a NAV for a scheme the
    investor transacted in, from their first fund transaction to ``end``."""
    if not transactions:
        return []
    first = min(t.txn_date for t in transactions)
    if first > end:
        return []
    calendars = ref.fund_nav_dates(conn, sorted({t.isin for t in transactions}), first, end)
    return sorted({d for dates in calendars.values() for d in dates})


def upsert_point(conn: Any, investor_id: str, advised_only: bool, point: FundPoint) -> None:
    s = point.snapshot
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO fund_snapshot (investor_id, as_of, advised_only, invested,
                   market_value, unrealised, realised_to_date, final, pending_isins,
                   excluded_isins)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (investor_id, as_of, advised_only) DO UPDATE SET
                   invested=EXCLUDED.invested, market_value=EXCLUDED.market_value,
                   unrealised=EXCLUDED.unrealised, realised_to_date=EXCLUDED.realised_to_date,
                   final=EXCLUDED.final, pending_isins=EXCLUDED.pending_isins,
                   excluded_isins=EXCLUDED.excluded_isins, computed_at=now()""",
            (
                investor_id,
                s.as_of,
                advised_only,
                s.invested,
                s.market_value,
                s.unrealised,
                s.realised_to_date,
                point.final,
                list(point.pending_isins),
                list(point.excluded_isins),
            ),
        )


@dataclass
class CatchUpResult:
    enabled: bool = True
    investors: int = 0
    written: int = 0
    rebuilt_series: int = 0
    skipped: list[dict[str, Any]] = field(default_factory=list)
    complete: bool = True


def catch_up_fund_snapshots(
    conn: Any,
    *,
    today: date,
    time_budget_seconds: float | None = None,
    clock: Any = time.monotonic,
) -> CatchUpResult:
    """Bring every investor's fund series up to ``today``, oldest gap first, bounded."""
    from app.config import get_settings

    if not get_settings().mf_valuation_enabled or not ref.fund_tables_present(conn):
        return CatchUpResult(enabled=False)
    result = CatchUpResult()
    began = clock()
    holders = ref.investors_holding(
        conn, ref.isins_on_route(conn, ref.relevant_isins(conn), Route.AMFI)
    )
    result.investors = len(holders)
    for investor_id in sorted(holders):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT as_of, version FROM fund_snapshot_dirty WHERE investor_id=%s",
                (investor_id,),
            )
            dirty = dict(cur.fetchall())
        finished_all = True
        for advised_only in (False, True):
            done = _catch_up_one(
                conn,
                investor_id,
                advised_only,
                today,
                dirty,
                result,
                began,
                time_budget_seconds,
                clock,
            )
            finished_all = finished_all and done
            if not done:
                result.complete = False
                break
        if finished_all and dirty:
            with conn.cursor() as cur:
                cur.executemany(
                    "DELETE FROM fund_snapshot_dirty WHERE investor_id=%s AND as_of=%s "
                    "AND version=%s",
                    [(investor_id, on, version) for on, version in dirty.items()],
                )
            conn.commit()
        if not result.complete:
            break
    return result


def _catch_up_one(
    conn: Any,
    investor_id: str,
    advised_only: bool,
    today: date,
    dirty: dict[date, int],
    result: CatchUpResult,
    began: float,
    budget: float | None,
    clock: Any,
) -> bool:
    transactions = fund_ledger(conn, investor_id, advised_only)
    print_ = fingerprint(transactions)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fingerprint FROM fund_snapshot_ledger WHERE investor_id=%s AND advised_only=%s",
            (investor_id, advised_only),
        )
        row = cur.fetchone()
        if row is None or row[0] != print_:
            # The fund ledger changed (or this series was never built): every stored row
            # may be wrong. Rebuild from scratch rather than guess which survive.
            cur.execute(
                "DELETE FROM fund_snapshot WHERE investor_id=%s AND advised_only=%s",
                (investor_id, advised_only),
            )
            cur.execute(
                "DELETE FROM fund_snapshot_ledger WHERE investor_id=%s AND advised_only=%s",
                (investor_id, advised_only),
            )
            # Identify this rebuild BEFORE committing any points. Otherwise a budget
            # expiry leaves no fingerprint and the next call deletes its progress.
            cur.execute(
                """INSERT INTO fund_snapshot_ledger (investor_id, advised_only, fingerprint)
                   VALUES (%s,%s,%s)""",
                (investor_id, advised_only, print_),
            )
            result.rebuilt_series += 1
        cur.execute(
            "SELECT as_of, final FROM fund_snapshot WHERE investor_id=%s AND advised_only=%s",
            (investor_id, advised_only),
        )
        stored = dict(cur.fetchall())
    conn.commit()
    if not transactions:
        return True
    todo = [
        on
        for on in required_dates(conn, transactions, today)
        if on not in stored or not stored[on] or on in dirty
    ]
    for on in todo:
        if budget is not None and clock() - began > budget:
            return False
        try:
            point = compute_point(conn, transactions, on)
        except (MissingPriceError, NegativeHoldingError) as exc:
            result.skipped.append(
                {
                    "investor_id": investor_id,
                    "advised_only": advised_only,
                    "as_of": on,
                    "reason": str(exc),
                }
            )
            logger.warning(
                "fund snapshot omitted: %s on %s (investor_id=%s advised_only=%s)",
                exc,
                on,
                investor_id,
                advised_only,
            )
            continue
        if point is None:
            continue
        upsert_point(conn, investor_id, advised_only, point)
        conn.commit()
        result.written += 1
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO fund_snapshot_ledger (investor_id, advised_only, fingerprint)
               VALUES (%s,%s,%s) ON CONFLICT (investor_id, advised_only) DO UPDATE SET
               fingerprint=EXCLUDED.fingerprint, computed_at=now()""",
            (investor_id, advised_only, print_),
        )
    conn.commit()
    return True


def list_points(
    conn: Any, investor_id: str, advised_only: bool, start: date, end: date
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT as_of, invested, market_value, unrealised, realised_to_date, final,
                      pending_isins, excluded_isins
               FROM fund_snapshot WHERE investor_id=%s AND advised_only=%s
                 AND as_of BETWEEN %s AND %s ORDER BY as_of""",
            (investor_id, advised_only, start, end),
        )
        return [
            {
                "as_of": r[0],
                "invested": r[1],
                "market_value": r[2],
                "unrealised": r[3],
                "realised_to_date": r[4],
                "final": r[5],
                "pending_isins": list(r[6]),
                "excluded_isins": list(r[7]),
            }
            for r in cur.fetchall()
        ]
