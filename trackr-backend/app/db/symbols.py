"""Keeps ``security.symbol`` true to what the exchange currently calls a scrip.

``security.symbol`` carries the comment "Display only. Tickers get renamed and reused; the
ISIN never moves" (``001_core.sql``), and that is exactly right -- but until now nothing
ever re-read it. It was written once at import (``app/db/imports.py``) and left to rot.

That is not cosmetic. ``app.prices.yfinance_live.resolve_ticker`` builds its Yahoo ticker
out of this column, so a stale symbol is a dead lookup. INE155A01022 is the live example:
Tata Motors Ltd became Tata Motors Passenger Vehicles Ltd on the October-2026 demerger of
the commercial-vehicle business, NSE moved it from TATAMOTORS to TMPV, and TATAMOTORS was
freed for the demerged CV entity. A lookup on the stale symbol returns nothing today and
will return THE WRONG COMPANY once the ticker is reassigned -- silently, with no error to
catch, poisoning every fundamental and factor score derived from it.

The bhavcopy already carries ISIN and symbol side by side for every listed scrip, every
day, so keeping this column current costs one comparison per row and no extra fetch.

A change is RECORDED as well as applied (``security_symbol_change``), because a symbol
moving is a corporate-action signal worth a human's attention, not a silent UPDATE.

Deliberately NOT ``security_alias``: that table maps ISIN -> ISIN when an identifier is
superseded, and demands verified evidence because it rewrites identity. Nothing here
rewrites identity. The ISIN is unchanged and remains the key; only the label moved.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

logger = logging.getLogger("db.symbols")


@dataclass(frozen=True, slots=True)
class SymbolChange:
    isin: str
    old_symbol: str | None
    new_symbol: str
    observed_on: date


def refresh_symbols(
    conn: Any,
    observed: dict[str, str],
    observed_on: date,
) -> list[SymbolChange]:
    """Point ``security.symbol`` at what the exchange called each ISIN on ``observed_on``.

    ``observed`` is ISIN -> trading symbol, straight off a bhavcopy. Only ISINs already in
    ``security`` are touched: this function maintains existing rows, it does not decide
    which securities are worth knowing about (``app.db.universe.ensure_universe_securities``
    does that, explicitly).

    An empty or whitespace symbol is ignored rather than written. A feed that omits the
    column for a row is telling us nothing about that scrip, and overwriting a good symbol
    with a blank would be treating silence as a fact.

    Returns the changes applied, so the caller can log or alert on them. Idempotent: a
    second call with the same input changes nothing and returns an empty list.
    """
    if not observed:
        return []

    candidates = {
        isin: symbol.strip()
        for isin, symbol in observed.items()
        if symbol and symbol.strip()
    }
    if not candidates:
        return []

    changes: list[SymbolChange] = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT isin, symbol FROM security WHERE isin = ANY(%s)",
            (list(candidates),),
        )
        current = {row[0]: row[1] for row in cur.fetchall()}

        for isin, new_symbol in candidates.items():
            if isin not in current:
                continue  # not a security this system tracks; not ours to create here
            old_symbol = current[isin]
            if old_symbol == new_symbol:
                continue
            changes.append(SymbolChange(isin, old_symbol, new_symbol, observed_on))

        for change in changes:
            cur.execute(
                "UPDATE security SET symbol = %s WHERE isin = %s",
                (change.new_symbol, change.isin),
            )
            # ON CONFLICT DO NOTHING: re-running a day's pipe must not fail on the audit
            # row it already wrote. The (isin, observed_on) grain means one recorded change
            # per scrip per day, which is the finest grain the daily feed can support.
            cur.execute(
                """
                INSERT INTO security_symbol_change
                    (isin, old_symbol, new_symbol, observed_on)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (isin, observed_on) DO NOTHING
                """,
                (change.isin, change.old_symbol, change.new_symbol, change.observed_on),
            )

    for change in changes:
        # WARNING, not INFO: a ticker moving usually means a corporate action, and the
        # first person to notice should not have to be reading DEBUG logs.
        logger.warning(
            "security.symbol changed for %s: %s -> %s (observed %s)",
            change.isin,
            change.old_symbol,
            change.new_symbol,
            change.observed_on,
        )
    return changes


def list_symbol_changes(conn: Any, isin: str | None = None) -> list[SymbolChange]:
    """The recorded symbol-change history, newest first. Whole table when ``isin`` is None."""
    sql = (
        "SELECT isin, old_symbol, new_symbol, observed_on FROM security_symbol_change"
    )
    params: tuple[Any, ...] = ()
    if isin is not None:
        sql += " WHERE isin = %s"
        params = (isin,)
    sql += " ORDER BY observed_on DESC, isin"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [SymbolChange(r[0], r[1], r[2], r[3]) for r in cur.fetchall()]
