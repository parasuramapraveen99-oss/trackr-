"""Populate security.market_cap_bucket from AMFI's large/mid/small-cap classification.
Developer-local, run whenever the classification should be refreshed (roughly every six
months -- see app.prices.market_cap_sources's module docstring for why there's no
"latest" URL to just re-hit):

    DATABASE_URL=postgresql:///portfolio python -m app.db.load_market_cap

ENRICHMENT ONLY -- never inserts a security row. ``transaction.isin`` is what decides
which ISINs exist in ``security``; this script only fills in ``market_cap_bucket`` for
rows that are already there, the same way ``load_sectors.py`` only fills in ``sector``.

Re-running is a no-op change-wise for any ISIN AMFI's list already classified the same
way last time -- an UPDATE with the same value, not a second insert.
"""

from __future__ import annotations

import os

from app.db.connection import connect
from app.prices.market_cap_sources import amfi_market_cap_source, parse_amfi_market_cap_list


def main() -> None:
    print("fetching AMFI large/mid/small-cap classification...")
    payload = amfi_market_cap_source().fetch()
    rows = parse_amfi_market_cap_list(payload)
    bucket_by_isin = {row.isin: row.bucket for row in rows}
    print(f"  {len(bucket_by_isin)} classified ISINs in the source list")

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT isin FROM security")
            held_isins = {row[0] for row in cur.fetchall()}

            matched = [isin for isin in held_isins if isin in bucket_by_isin]
            for isin in matched:
                cur.execute(
                    "UPDATE security SET market_cap_bucket = %s WHERE isin = %s",
                    (bucket_by_isin[isin], isin),
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
