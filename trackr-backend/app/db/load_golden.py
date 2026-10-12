"""Load the golden fixture into Postgres and price it. Developer-local, run once.

    DATABASE_URL=postgresql:///portfolio python -m db.load_golden

Not a general importer -- the real ingestion path is ``ingest/`` plus the staging flow in
``docs/INGESTION.md``, and nothing here should grow into a substitute for it. This exists
so the dashboard has the one portfolio whose answers are independently verified, which is
the only way to tell a screen showing real numbers from a screen showing plausible ones.

Securities come from the bhavcopy, not from the fixture: it is the only source in the
system carrying symbol AND name for an ISIN, and it is already being fetched for prices.
An ISIN the exchanges no longer list (delisted, or an MF folio) gets a row with whatever
the fixture knows and NULL for the rest -- a security row must exist either way, because
``transaction.isin`` is a foreign key.
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.db import prices as price_db
from app.db.connection import connect
from app.db.repository import save_transactions
from app.engine.types import Source, Transaction, TxnType
from app.prices.pipe import merge
from app.prices.sources import FetchFailed, NotPublished, bse_source, nse_source
from app.prices.udiff import BhavcopyFormatError, parse

#: fixtures/ stays at the repo root -- shared by the root package's tests and this one,
#: not duplicated per package. Four parents: db -> app -> trackr-backend -> repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
FIXTURE = _REPO_ROOT / "fixtures" / "golden_portfolio.json"

#: How many calendar days back to fetch. Five trading days covers the staleness window in
#: db/prices.latest_prices, so every ISIN either has a usable price or genuinely has none.
PRICE_DAYS = 7


def _dec(value: Any) -> Decimal:
    """Via str, always. A JSON number converted straight to Decimal reintroduces exactly
    the binary approximation this codebase spends its precision discipline avoiding."""
    return Decimal(str(value))


def _to_transaction(row: dict[str, Any]) -> Transaction:
    return Transaction(
        isin=row["isin"],
        txn_date=date.fromisoformat(row["txn_date"]),
        txn_type=TxnType(row["txn_type"]),
        quantity=_dec(row["quantity"]),
        price=_dec(row["price"]),
        charges=_dec(row.get("charges", "0")),
        stt=_dec(row.get("stt", "0")),
        dividend_amount=_dec(row.get("dividend_amount", "0")),
        account_id=row.get("account_id"),
        source=Source(row["source"]) if row.get("source") else None,
        source_ref=row.get("source_ref"),
        dedupe_key=row.get("dedupe_key"),
        raw_payload=row.get("raw_payload"),
    )


def fetch_reference(
    isins: set[str], as_of: date
) -> tuple[dict[str, tuple[str, str, str]], list[Any]]:
    """Walk the recent window once. Returns (isin -> symbol/name/exchange, prices to store).

    Each (day, exchange) file is fetched exactly once and used for both purposes, which is
    why this does not call ``prices.pipe.fetch_day`` -- that would re-fetch for the
    metadata pass. Merging still uses ``pipe.merge``, so NSE-wins-on-conflict stays in the
    one place that is tested.

    Oldest day first, so a newer file simply overwrites an older one and each ISIN ends up
    described by the most recent exchange file that lists it.

    What this gives up: ``fetch_day``'s notifier. A one-off loader printing its own failures
    to a terminal someone is watching does not need an alerting path; the daily pipe does,
    and still has one.
    """
    meta: dict[str, tuple[str, str, str]] = {}
    keep: list[Any] = []

    for offset in range(PRICE_DAYS, -1, -1):
        day = as_of - timedelta(days=offset)
        if day.weekday() >= 5:
            continue

        by_exchange: dict[str, list[Any]] = {}
        missing: list[str] = []
        for source in (nse_source(), bse_source()):
            try:
                rows = parse(source.fetch(day), source.exchange)
            except (NotPublished, FetchFailed, BhavcopyFormatError) as exc:
                missing.append(f"{source.exchange}({type(exc).__name__})")
                continue
            by_exchange[source.exchange] = rows
            for row in rows:
                if row.isin in isins:
                    meta[row.isin] = (row.symbol, row.name, row.exchange)

        counts = {k: len(v) for k, v in by_exchange.items()}
        print(f"  {day}  fetched={counts or '-'}  unavailable={missing or '-'}")
        keep.extend(p for p in merge(by_exchange) if p.isin in isins)

    return meta, keep


def main() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    transactions = [_to_transaction(row) for row in fixture["transactions"]]
    isins = {t.isin for t in transactions}
    as_of = date.today()

    # Symbols the fixture knows for currently-held positions. Used only where the
    # exchanges no longer list the ISIN.
    fixture_symbols = {h["isin"]: h.get("symbol", "") for h in fixture["expected"]["holdings"]}

    print(f"fixture: {len(transactions)} transactions, {len(isins)} distinct ISINs")
    print("price pipe:")
    meta, merged_prices = fetch_reference(isins, as_of)
    print(
        f"  metadata for {len(meta)}/{len(isins)} ISINs, {len(merged_prices)} price rows to store"
    )

    with connect() as conn:
        with conn.cursor() as cur:
            investor = fixture["investor"]
            cur.execute(
                """
                INSERT INTO investor (code, display_name) VALUES (%s, %s)
                ON CONFLICT (code) DO UPDATE SET display_name = EXCLUDED.display_name
                RETURNING id
                """,
                (investor["code"], investor["display_name"]),
            )
            investor_id = cur.fetchone()[0]

            account = fixture["account"]
            cur.execute(
                """
                INSERT INTO account (id, investor_id, broker, dp_id, client_id, is_advised, label)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    account["id"],
                    investor_id,
                    account["broker"],
                    account.get("dp_id"),
                    account.get("client_id"),
                    account.get("is_advised", True),
                    account.get("label", account["broker"]),
                ),
            )

            for isin in sorted(isins):
                symbol, name, exchange = meta.get(isin, (fixture_symbols.get(isin, ""), "", ""))
                cur.execute(
                    """
                    INSERT INTO security (isin, symbol, name, exchange, asset_class)
                    VALUES (%s, %s, %s, %s, 'EQ')
                    ON CONFLICT (isin) DO UPDATE
                        SET symbol = COALESCE(NULLIF(EXCLUDED.symbol, ''), security.symbol),
                            name   = COALESCE(NULLIF(EXCLUDED.name, ''),   security.name),
                            exchange = COALESCE(EXCLUDED.exchange, security.exchange)
                    """,
                    (isin, symbol or None, name or None, exchange or None),
                )

        summary = save_transactions(conn, transactions, actor_uid=None)
        stored = price_db.upsert_prices(conn, merged_prices)

    print(f"investor {investor_id}")
    print(
        f"transactions: inserted={summary.inserted} skipped_duplicates={summary.skipped_duplicates}"
    )
    print(f"prices stored: {stored}")


if __name__ == "__main__":
    if not os.environ.get("DATABASE_URL"):
        raise SystemExit("DATABASE_URL is not set")
    main()
