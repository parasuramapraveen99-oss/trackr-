"""Populate cost_inflation_index from a locally-sourced CSV of CBDT-published CII
values. Developer-local, run once per newly-sourced batch -- like
load_fmv_31jan2018.py, this is static annual reference data, not a live feed with a
schedule to re-hit:

    DATABASE_URL=postgresql:///portfolio python -m app.db.load_cii path/to/cii.csv

Expects three columns: financial_year, cii_value, source.

UNLIKE load_fmv_31jan2018.py and load_market_cap.py, this does not enrich an existing
security row -- cost_inflation_index has no ISIN and no FK to security (migration 013),
so every row is its own reference fact, upserted by financial_year (the table's primary
key) rather than matched against ISINs already in the database. Re-running with the same
file is a no-op change-wise for any FY it already set the same value for -- an UPDATE
with the same value, not a second insert (ON CONFLICT DO UPDATE, the same idempotency
posture as app.db.index_levels.upsert_index_levels).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from app.db.connection import connect
from app.prices.cii_source import parse_cii_csv


def main(csv_path: Path) -> None:
    print(f"reading {csv_path}...")
    payload = csv_path.read_bytes()
    rows = parse_cii_csv(payload)
    print(f"  {len(rows)} financial years in the source file")

    with connect() as conn:
        with conn.cursor() as cur:
            for row in rows:
                cur.execute(
                    """
                    INSERT INTO cost_inflation_index (financial_year, cii_value, source)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (financial_year) DO UPDATE
                    SET cii_value = EXCLUDED.cii_value, source = EXCLUDED.source
                    """,
                    (row.financial_year, row.cii_value, row.source),
                )

    print(f"upserted: {len(rows)}")


if __name__ == "__main__":
    if not os.environ.get("DATABASE_URL"):
        raise SystemExit("DATABASE_URL is not set")
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m app.db.load_cii path/to/cii.csv")
    main(Path(sys.argv[1]))
