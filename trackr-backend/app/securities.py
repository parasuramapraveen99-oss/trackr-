"""ISIN -> symbol, name, asset_class, sector. The join the engine deliberately does not do.

``Holding`` carries no ``asset_class`` and its ``symbol`` is never populated --
``compute_holdings`` replays a ledger and has no reason to know what a security is called.
Those three fields are ``security`` table columns, so they are attached here, at the web
boundary, rather than pushed into ``app/engine/types.py`` where they would mean editing
the dataclass, ``schema/holding.schema.json``, and the test that asserts the two agree,
all to carry a display concern into a layer that has no use for it.

The consequence, stated rather than hidden: ``Holding.symbol`` stays ``None`` for anyone
consuming the engine directly. That gap predates this module; it is not closed by it.
"""

from __future__ import annotations

from typing import Any

#: What a security with no row resolves to. ISIN is the identity and is always known, so
#: a missing name degrades to showing the ISIN rather than to showing nothing.
UNKNOWN = {
    "symbol": None,
    "name": None,
    "asset_class": "EQ",
    "sector": None,
    "market_cap_bucket": None,
    "exchange": None,
}

#: Display labels for the asset classes actually built. Phase 1 is EQ alone -- the other
#: 29 codes in schema/asset_classes.json are seeded so the enum never migrates, not
#: built, so anything else falling through to its raw code is correct and visible.
ASSET_CLASS_LABELS = {"EQ": "Stocks", "MF_EQUITY": "Mutual Funds", "MF_DEBT": "Mutual Funds (Debt)"}


def lookup(conn: Any, isins: list[str]) -> dict[str, dict[str, Any]]:
    """Metadata for the given ISINs, keyed by ISIN. Absent ISINs are simply absent."""
    if not isins:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT isin, symbol, name, asset_class, sector, market_cap_bucket, exchange "
            "FROM security WHERE isin = ANY(%s)",
            (list(isins),),
        )
        return {
            row[0]: {
                "symbol": row[1],
                "name": row[2],
                "asset_class": row[3],
                "sector": row[4],
                "market_cap_bucket": row[5],
                "exchange": row[6],
            }
            for row in cur.fetchall()
        }


def label(asset_class: str) -> str:
    return ASSET_CLASS_LABELS.get(asset_class, asset_class)


def describe(meta: dict[str, dict[str, Any]], isin: str) -> dict[str, Any]:
    """One security's display fields, with the unknown case handled once."""
    return meta.get(isin, UNKNOWN)


def display_name(meta: dict[str, dict[str, Any]], isin: str) -> str:
    """Best available human label: name, else symbol, else the ISIN itself."""
    row = describe(meta, isin)
    return row["name"] or row["symbol"] or isin
