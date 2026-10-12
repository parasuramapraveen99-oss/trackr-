"""Membership of the cross-sectional ranking universe -- see
``app/db/migrations/032_analytics_and_universe.sql``.

A factor score is a PERCENTILE, and a percentile without a stated population is not a
number, it is a decoration. "Momentum 66/100" means nothing until you know 66th out of
what. Ranking a portfolio only against its own holdings is the specific failure this
exists to prevent: with fifteen positions, every percentile is an artefact of the fifteen.

Membership is stored rather than inferred because the population must stay explicable
after the fact. Index constituents change; a score computed last quarter was computed
against last quarter's population, and `removed_on` is what keeps that answerable instead
of quietly re-writing history every time the index is rebalanced.

The universe costs no extra fetch. The bhavcopy this system already downloads every day
carries every listed scrip -- ``app.prices.pipe.main`` simply discards all but the held
ones. Widening that filter to include universe members buys a real population for the
price of rows.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

logger = logging.getLogger("db.universe")

#: The default ranking population. Named and stored on every score that uses it, because
#: a mid-cap book ranked against a large-cap index is as misleading as no universe at all.
#:
#: The NAME changed from "TRACKED" to "NIFTY500" on 10 Oct 2026, in the same change that
#: loads NSE's Nifty 500 constituent list into it (``sync_index_universe``) -- the rule
#: was: never name a population after an index it does not hold. With no members loaded,
#: the router still falls back to an explicitly labelled HOLDINGS_n population.
DEFAULT_UNIVERSE = "NIFTY500"


@dataclass(frozen=True, slots=True)
class UniverseMember:
    isin: str
    universe: str
    added_on: date
    removed_on: date | None


def current_universe_isins(conn: Any, universe: str | None = None) -> list[str]:
    """ISINs currently in ``universe`` (all universes when None). Sorted, for determinism."""
    sql = "SELECT isin FROM universe_member WHERE removed_on IS NULL"
    params: tuple[Any, ...] = ()
    if universe is not None:
        sql += " AND universe = %s"
        params = (universe,)
    sql += " ORDER BY isin"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [row[0] for row in cur.fetchall()]


def list_members(conn: Any, universe: str, include_removed: bool = False) -> list[UniverseMember]:
    sql = "SELECT isin, universe, added_on, removed_on FROM universe_member WHERE universe = %s"
    if not include_removed:
        sql += " AND removed_on IS NULL"
    sql += " ORDER BY isin"
    with conn.cursor() as cur:
        cur.execute(sql, (universe,))
        return [UniverseMember(r[0], r[1], r[2], r[3]) for r in cur.fetchall()]


def ensure_universe_securities(conn: Any, rows: dict[str, dict[str, Any]]) -> int:
    """Create ``security`` rows for universe members that have none yet.

    ``rows`` is ISIN -> {"symbol", "name", "exchange"}, taken from a bhavcopy. Needed
    because ``universe_member.isin`` and ``price.isin`` are both foreign keys to
    ``security``: a universe member with no security row can be neither enrolled nor priced.

    ``ON CONFLICT DO NOTHING`` -- this only fills gaps. It must never overwrite a security
    the ledger already knows about, whose symbol is maintained by
    ``app.db.symbols.refresh_symbols`` and whose sector and cap bucket are loaded from NSE
    and AMFI. Returns the number of rows actually created.
    """
    if not rows:
        return 0
    created = 0
    with conn.cursor() as cur:
        for isin, meta in rows.items():
            cur.execute(
                """
                INSERT INTO security (isin, symbol, name, exchange, asset_class)
                VALUES (%s, %s, %s, %s, 'EQ')
                ON CONFLICT (isin) DO NOTHING
                """,
                (isin, meta.get("symbol"), meta.get("name"), meta.get("exchange")),
            )
            created += cur.rowcount or 0
    return created


def set_universe(conn: Any, universe: str, isins: list[str], on: date) -> dict[str, int]:
    """Make ``isins`` the exact current membership of ``universe`` as of ``on``.

    Additive and subtractive in one call, which is what a rebalance actually is. An ISIN
    already a current member is left alone -- its ``added_on`` is not bumped, because the
    date it joined is a fact and re-stating it on every sync would erase the tenure the
    table exists to record. A current member absent from ``isins`` is marked
    ``removed_on``, never deleted.

    A previously-removed ISIN that returns is re-admitted by clearing ``removed_on`` and
    re-dating ``added_on``: that genuinely is a new tenure.
    """
    wanted = set(isins)
    with conn.cursor() as cur:
        # ``isin`` alone is the key, so an ISIN can belong to one universe at a time. A row
        # held under another universe (the pre-2026-10-10 "TRACKED" set) moves here as a
        # new tenure through the same UPDATE a returning member takes.
        cur.execute(
            "SELECT isin, removed_on, universe FROM universe_member "
            "WHERE universe = %s OR isin = ANY(%s)",
            (universe, list(wanted)),
        )
        found = cur.fetchall()
        known = {row[0]: row[1] for row in found}

        current = {isin for isin, removed, owner in found if removed is None and owner == universe}
        to_add = wanted - current
        to_remove = current - wanted

        for isin in sorted(to_add):
            if isin in known:  # returning member: a new tenure, not a resurrection
                cur.execute(
                    "UPDATE universe_member SET universe = %s, added_on = %s, "
                    "removed_on = NULL WHERE isin = %s",
                    (universe, on, isin),
                )
            else:
                cur.execute(
                    "INSERT INTO universe_member (isin, universe, added_on) VALUES (%s, %s, %s)",
                    (isin, universe, on),
                )
        for isin in sorted(to_remove):
            cur.execute(
                "UPDATE universe_member SET removed_on = %s WHERE isin = %s AND universe = %s",
                (on, isin, universe),
            )

    if to_add or to_remove:
        logger.info("universe %s updated on %s: +%d -%d", universe, on, len(to_add), len(to_remove))
    return {"added": len(to_add), "removed": len(to_remove), "current": len(wanted)}


def sync_index_universe(conn: Any, universe: str, rows: list[Any], on: date) -> dict[str, int]:
    """Load an NSE constituent list (``SectorRow``s) as ``universe``'s membership.

    Creates missing ``security`` rows from the list's own symbol and company name, fills
    a missing sector from the list's Industry column (never overwriting one), then makes
    the list the exact current membership. Idempotent for an unchanged list.
    """
    # NSE's index files carry placeholder rows while a demerger settles (seen 2026-10-10:
    # "DUM545A01024, Dummy HEG Ltd."). Not a security: no ISIN-shaped code from NSDL's
    # "INE"/"INF" issuer space, so it is skipped, counted, and never enrolled.
    real = [r for r in rows if re.fullmatch(r"IN[EF][A-Z0-9]{9}", r.isin)]
    placeholders = len(rows) - len(real)
    rows = real
    created = ensure_universe_securities(
        conn,
        {r.isin: {"symbol": r.symbol, "name": r.company_name, "exchange": "NSE"} for r in rows},
    )
    with conn.cursor() as cur:
        for r in rows:
            cur.execute(
                "UPDATE security SET sector=%s, sector_source='NSE_INDEX_LIST' "
                "WHERE isin=%s AND sector IS NULL",
                (r.sector, r.isin),
            )
    changed = set_universe(conn, universe, [r.isin for r in rows], on)
    return {
        "created_securities": created,
        "members": len(rows),
        "placeholders_skipped": placeholders,
        **(changed or {}),
    }
