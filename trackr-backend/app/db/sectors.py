"""Fill ``security.sector`` from NSE's list, then BSE for what NSE omits. Enrichment only.

Never inserts a security and never overwrites an NSE classification with BSE's. An ISIN
neither exchange classifies stays NULL and is named on the page as unclassified.
"""

from __future__ import annotations

import re
from typing import Any

NSE_SOURCE = "NSE_TOTAL_MARKET"
BSE_SOURCE = "BSE_SCRIP"


def _key(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", label.casefold())


def normalise(label: str, known: set[str]) -> str:
    """BSE's spelling -> NSE's, when they differ only in punctuation or case."""
    by_key = {_key(k): k for k in known}
    return by_key.get(_key(label), label)


def refresh_sectors(conn: Any, wanted: list[str], nse_fetch=None, bse_fetch=None) -> dict:
    from app.prices.bse_classification import sectors as bse_sectors
    from app.prices.sector_sources import nse_sector_source, parse_nse_sector_list

    nse = {
        r.isin: r.sector for r in parse_nse_sector_list((nse_fetch or nse_sector_source().fetch)())
    }
    known = set(nse.values())
    nse_set = 0
    with conn.cursor() as cur:
        cur.execute("SELECT isin FROM security WHERE isin = ANY(%s)", (list(nse),))
        for (isin,) in cur.fetchall():
            cur.execute(
                "UPDATE security SET sector=%s, sector_source=%s WHERE isin=%s",
                (nse[isin], NSE_SOURCE, isin),
            )
            nse_set += 1
        cur.execute(
            "SELECT isin FROM security WHERE isin = ANY(%s) AND NOT (isin = ANY(%s))",
            (list(wanted), list(nse)),
        )
        remaining = [r[0] for r in cur.fetchall()]
    bse = (bse_fetch or bse_sectors)(remaining)
    with conn.cursor() as cur:
        for isin, label in bse.items():
            cur.execute(
                "UPDATE security SET sector=%s, sector_source=%s WHERE isin=%s",
                (normalise(label, known), BSE_SOURCE, isin),
            )
    conn.commit()
    return {"nse": nse_set, "bse": len(bse), "unclassified": sorted(set(remaining) - set(bse))}
