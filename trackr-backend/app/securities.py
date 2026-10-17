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
    "instrument_kind": None,
    "exposure": None,
    "fund_category": None,
}

#: Display labels for the asset classes actually built. Phase 1 is EQ alone -- the other
#: 29 codes in schema/asset_classes.json are seeded so the enum never migrates, not
#: built, so anything else falling through to its raw code is correct and visible.
ASSET_CLASS_LABELS = {
    "EQ": "Stocks",
    "MF_EQUITY": "Mutual Funds",
    "MF_DEBT": "Mutual Funds (Debt)",
    "MF_OTHER": "Mutual Funds (Other)",
    "ETF": "ETFs",
    "FUND_UNRESOLVED": "Fund units (unidentified)",
}

#: Display class for a fund unit, from its evidenced route and AMFI exposure
#: (docs/MF_ETF_PLAN.md section 4). ``security.asset_class`` is NOT consulted for fund
#: units: it defaults to EQ for every row ever inserted, which is a column default, not a
#: classification. These codes are display-only and never written back.
_FUND_DISPLAY = {"DOMESTIC_EQUITY": "MF_EQUITY", "DEBT": "MF_DEBT"}


def _fund_overlay(conn: Any, rows: dict[str, dict[str, Any]]) -> None:
    from app.db.fund_reference import valuation_routes
    from app.prices.fund_category import InstrumentKind

    funds = [isin for isin in rows if isin.startswith("INF")]
    if not funds:
        return
    for isin, decision in valuation_routes(conn, funds).items():
        row = rows[isin]
        row["instrument_kind"] = str(decision.instrument_kind)
        row["exposure"] = str(decision.exposure) if decision.exposure else None
        row["fund_category"] = decision.category
        if not row.get("name") and decision.scheme_name:
            row["name"] = decision.scheme_name
        if decision.instrument_kind == InstrumentKind.ETF:
            row["asset_class"] = "ETF"
        elif decision.instrument_kind == InstrumentKind.MUTUAL_FUND:
            row["asset_class"] = _FUND_DISPLAY.get(row["exposure"] or "", "MF_OTHER")
        else:
            row["asset_class"] = "FUND_UNRESOLVED"
        # Fund units are not companies: no sector or market-cap bucket applies to them.
        row["sector"] = None
        row["market_cap_bucket"] = None


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
        rows = {
            row[0]: {
                "symbol": row[1],
                "name": row[2],
                "asset_class": row[3],
                "sector": row[4],
                "market_cap_bucket": row[5],
                "exchange": row[6],
                "instrument_kind": "STOCK" if not row[0].startswith("INF") else None,
                "exposure": None,
                "fund_category": None,
            }
            for row in cur.fetchall()
        }
    _fund_overlay(conn, rows)
    return rows


def label(asset_class: str) -> str:
    return ASSET_CLASS_LABELS.get(asset_class, asset_class)


def allocation_label(info: dict[str, Any], field: str) -> str:
    """Label fund wrappers explicitly where company sector/size does not apply.

    This is a display group, not a claim about a fund's underlying sector or size.
    Unidentified fund units stay unidentified; missing stock metadata stays visible.
    """
    asset_class = info.get("asset_class", "EQ")
    if asset_class == "ETF":
        return label(asset_class)
    if asset_class.startswith("MF_"):
        return "Mutual Funds"
    if asset_class == "FUND_UNRESOLVED":
        return label(asset_class)
    return info.get(field) or "Unclassified"


def describe(meta: dict[str, dict[str, Any]], isin: str) -> dict[str, Any]:
    """One security's display fields, with the unknown case handled once. An unknown
    fund unit is never displayed as an equity by default."""
    if isin in meta:
        return meta[isin]
    return {**UNKNOWN, "asset_class": "FUND_UNRESOLVED"} if isin.startswith("INF") else UNKNOWN


def display_name(meta: dict[str, dict[str, Any]], isin: str) -> str:
    """Best available human label: name, else symbol, else the ISIN itself."""
    row = describe(meta, isin)
    return row["name"] or row["symbol"] or isin
