"""Populate security.fmv_31jan2018 from a locally-sourced CSV of exchange-archive FMV
values. Developer-local, run once per newly-sourced batch -- this is a one-time
historical constant, not something that changes, so there is no schedule to run this
on the way there is for load_sectors.py/load_market_cap.py (see
app.prices.fmv_31jan2018_source's module docstring for why there's no URL to re-hit):

    DATABASE_URL=postgresql:///portfolio python -m app.db.load_fmv_31jan2018 path/to/fmv.csv

Expects two columns: ``ISIN`` and ``fmv_31jan2018``.

ENRICHMENT ONLY -- never inserts a security row. ``transaction.isin`` is what decides
which ISINs exist in ``security``; this script only fills in ``fmv_31jan2018`` for rows
that are already there, the same way ``load_sectors.py`` only fills in ``sector``.

Re-running with the same file is a no-op change-wise for any ISIN it already set the
same value for -- an UPDATE with the same value, not a second insert. Safe to run again
with a second, different source file (e.g. an NSE list, then a BSE list covering
different ISINs) since each run only touches the ISINs its own file lists.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from app.db.connection import connect
from app.prices.fmv_31jan2018_source import parse_fmv_31jan2018_csv


def main(csv_path: Path) -> None:
    print(f"reading {csv_path}...")
    payload = csv_path.read_bytes()
    rows = parse_fmv_31jan2018_csv(payload)
    fmv_by_isin = {row.isin: row.fmv_31jan2018 for row in rows}
    print(f"  {len(fmv_by_isin)} ISINs with an FMV value in the source file")

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT isin FROM security")
            held_isins = {row[0] for row in cur.fetchall()}

            matched = [isin for isin in held_isins if isin in fmv_by_isin]
            for isin in matched:
                cur.execute(
                    "UPDATE security SET fmv_31jan2018 = %s WHERE isin = %s",
                    (fmv_by_isin[isin], isin),
                )

    unmatched = sorted(held_isins - set(matched))
    print(f"securities in DB: {len(held_isins)}")
    print(f"matched and updated: {len(matched)}")
    print(f"remain unsourced: {len(unmatched)}")
    if unmatched:
        print(f"  {unmatched}")


if __name__ == "__main__":
    if not os.environ.get("DATABASE_URL"):
        raise SystemExit("DATABASE_URL is not set")
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m app.db.load_fmv_31jan2018 path/to/fmv.csv")
    main(Path(sys.argv[1]))
