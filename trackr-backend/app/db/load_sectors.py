"""Populate security.sector from NSE's Industry classification. Developer-local, run
whenever the classification should be refreshed:

    DATABASE_URL=postgresql:///portfolio python -m app.db.load_sectors

ENRICHMENT ONLY -- never inserts a security row. ``transaction.isin`` is what decides
which ISINs exist in ``security``; this script only fills in ``sector`` for rows that are
already there, the same way ``load_golden.py`` only fills in symbol/name for ISINs the
fixture already introduced.

Re-running is a no-op change-wise for any ISIN NSE's list already classified the same way
last time -- an UPDATE with the same value, not a second insert.
"""

from __future__ import annotations

import os

from app.db.connection import connect
from app.prices.sector_sources import nse_sector_source, parse_nse_sector_list


def main() -> None:
    print("fetching NSE NIFTY Total Market sector classification...")
    payload = nse_sector_source().fetch()
    rows = parse_nse_sector_list(payload)
    sector_by_isin = {row.isin: row.sector for row in rows}
    print(f"  {len(sector_by_isin)} classified ISINs in the source list")

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT isin FROM security")
            held_isins = {row[0] for row in cur.fetchall()}

            matched = [isin for isin in held_isins if isin in sector_by_isin]
            for isin in matched:
                cur.execute(
                    "UPDATE security SET sector = %s WHERE isin = %s",
                    (sector_by_isin[isin], isin),
                )

    unmatched = sorted(held_isins - set(matched))
    print(f"securities in DB: {len(held_isins)}")
    print(f"matched and updated: {len(matched)}")
    print(f"remain unclassified: {len(unmatched)}")
    if unmatched:
        print(f"  {unmatched}")


if __name__ == "__main__":
    if not os.environ.get("DATABASE_URL"):
        raise SystemExit("DATABASE_URL is not set")
    main()
