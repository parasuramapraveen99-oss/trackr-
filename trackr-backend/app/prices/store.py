"""Persisting one day of pipe output: prices, and the symbols that name them.

``app.prices.pipe`` deliberately does no storage -- ``fetch_day`` and ``backfill_days``
return results and stop, so the caller decides what "already stored" means. But there are
now TWO callers that persist a day (``pipe.main()`` for the CLI, and
``app.routers.analytical._refresh_prices`` for the cron), and the rules about WHAT to keep
have grown past "every ISIN in the ledger". Keeping that logic in one function is what
stops the two paths drifting apart -- the cron is the one that actually runs in production,
and a rule that only ever landed in the CLI would be a rule that never ran.

Two things widened here, both for the analytics module:

1. **Retention.** The bhavcopy carries every listed scrip and the pipe used to discard all
   but the held ones. Factor scores are cross-sectional PERCENTILES, which need a
   population, so prices are now also kept for the ranking universe
   (``app.db.universe``). This costs rows, not fetches -- the file was already downloaded.

2. **Symbols.** ``security.symbol`` was written once at import and never refreshed, which
   silently breaks every Yahoo lookup built from it. See ``app.db.symbols``.

What did NOT widen, deliberately: absence recording and "did this holding get priced?"
stay scoped to HELD ISINs. A universe member is reference data. It must never make a
backfill day look unsettled, and it must never appear in a "missing prices" report that a
human reads as "your portfolio is unpriced".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.db.fund_reference import sync_route_observations
from app.db.prices import held_isins, upsert_prices
from app.db.quote_mapping import upsert_listings
from app.db.symbols import SymbolChange, refresh_symbols
from app.db.traded_value import upsert as upsert_traded_values
from app.db.universe import current_universe_isins
from app.prices.pipe import PipeResult

logger = logging.getLogger("prices.store")


def _funds(isins: set[str]) -> set[str]:
    return {isin for isin in isins if isin.startswith("INF")}


@dataclass
class StoreResult:
    prices_stored: int = 0
    held_count: int = 0
    universe_count: int = 0
    symbol_changes: list[SymbolChange] = field(default_factory=list)


def store_day(conn: Any, result: PipeResult) -> StoreResult:
    """Keep the whole merged day in ``market_close``; upsert held + universe ISINs into
    ``price`` and refresh their symbols.

    Idempotent. ``upsert_prices`` is ``ON CONFLICT DO UPDATE`` and ``refresh_symbols``
    no-ops when nothing moved, so a duplicate cron invocation -- which Vercel documents as
    possible -- changes nothing.

    Symbol refresh is scoped to ``wanted`` rather than run over the whole 5,000-row file:
    the other 4,900 scrips have no ``security`` row, so refreshing them would be 4,900
    lookups that can only miss.
    """
    from app.prices.market import record_market_day

    held = set(held_isins(conn))
    universe = set(current_universe_isins(conn))
    wanted = held | universe

    # Every row of the file is kept in `market_close`; `price` stays held + universe.
    record_market_day(conn, result)
    stored = upsert_prices(conn, [p for p in result.prices if p.isin in wanted])
    # A ledger fund unit an exchange has now printed has positive listing evidence; if
    # that changes its valuation route, the series cached under the old route are redone.
    sync_route_observations(conn, {p.isin for p in result.prices if p.isin in held} & _funds(held))

    changes = refresh_symbols(
        conn,
        {isin: sym for isin, sym in result.nse_symbols.items() if isin in wanted},
        result.price_date,
    )
    # Where each ISIN's indicative quote is looked up, read from this same file.
    upsert_listings(
        conn,
        {isin: lst for isin, lst in result.listings.items() if isin in wanted},
        result.price_date,
    )
    # Liquidity is measured for holdings only, so turnover is kept for held ISINs.
    upsert_traded_values(conn, [r for r in result.traded_values if r[0] in held])

    return StoreResult(
        prices_stored=stored,
        held_count=len(held),
        universe_count=len(universe),
        symbol_changes=changes,
    )
