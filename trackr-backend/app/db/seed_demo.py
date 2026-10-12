"""Seed a realistic demo portfolio into Postgres, for local development and screenshots.

    DATABASE_URL=postgresql:///portfolio python -m app.db.seed_demo

Loads ``fixtures/demo_portfolio.json`` -- one investor, a demo login, two accounts (one
advised, one not, so the "Advised accounts only" filter has something to show), 18
securities and their transaction history -- and generates a fresh closing price for each
security. Every screen that needs live data (dashboard, holdings, FIFO lots, performance,
allocation) is populated after one run; nothing here writes an empty state.

WHY PRICES ARE GENERATED HERE, NOT IN THE FIXTURE. Dates in the fixture don't go stale --
a BUY from 2023 is still a valid BUY next year. A price is different: ``engine/holdings.py``
refuses anything older than ``MAX_PRICE_STALENESS_DAYS`` (5), so a price baked into a
committed JSON file would work today and quietly turn every holding "unpriced" a week from
now. Generating a short recent window at seed time, anchored to each security's own
computed average cost (via the real ``compute_holdings``, not a guess), is what keeps
re-running this script on any day always render a fully priced portfolio.

Not a general importer -- same caveat as ``app/db/load_golden.py``: the real ingestion
path is ``app/ingest/`` plus the staging flow in ``docs/INGESTION.md``.

IDEMPOTENT. investor and security rows upsert on their natural keys (``code``, ``isin``);
account rows get a deterministic id derived from ``investor.code`` and the fixture's
account key, since an account has no other unique column to upsert on; transactions rely
on ``dedupe_key``'s UNIQUE INDEX (``ON CONFLICT DO NOTHING`` in ``save_transactions``);
the demo login is created once and left alone on every later run. Re-running this script
is always safe, and always refreshes prices to stay within the staleness window.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from random import Random
from typing import Any

from app.auth.service import EmailAlreadyRegisteredError, register
from app.db import prices as price_db
from app.db.connection import connect
from app.db.repository import save_transactions
from app.engine.holdings import compute_holdings
from app.engine.types import Holding, Source, Transaction, TxnType
from app.prices.pipe import MergedPrice

#: fixtures/ stays at the repo root -- four parents: db -> app -> trackr-backend -> root.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
FIXTURE = _REPO_ROOT / "fixtures" / "demo_portfolio.json"

#: Fixed, not random -- so re-seeding the same investor code always regenerates the same
#: account ids instead of minting duplicates. ``account`` has no unique column of its own
#: to upsert on (unlike investor.code or security.isin), so the id has to be the stable
#: key, and a uuid5 of (investor code, account key) is exactly that.
_UUID_NAMESPACE = uuid.UUID("6f1e6a9e-2f2b-4c39-9f0a-9b3f7f5a9e11")

#: Trading days of price history to seed per security. Comfortably covers
#: MAX_PRICE_STALENESS_DAYS (5) regardless of which weekday the seed happens to run on.
PRICE_LOOKBACK_DAYS = 10

SOURCE_LABEL = "SEED_DEMO"


def _dec(value: Any) -> Decimal:
    """Via str, always -- see ``load_golden._dec``: a JSON number converted straight to
    Decimal reintroduces exactly the binary approximation this codebase avoids."""
    return Decimal(str(value))


def _account_id(investor_code: str, account_key: str) -> str:
    return str(uuid.uuid5(_UUID_NAMESPACE, f"{investor_code}:{account_key}"))


def _to_transaction(row: dict[str, Any], account_id: str) -> Transaction:
    return Transaction(
        isin=row["isin"],
        txn_date=date.fromisoformat(row["txn_date"]),
        txn_type=TxnType(row["txn_type"]),
        quantity=_dec(row["quantity"]),
        price=_dec(row["price"]),
        charges=_dec(row.get("charges", "0")),
        stt=_dec(row.get("stt", "0")),
        dividend_amount=_dec(row.get("dividend_amount", "0")),
        account_id=account_id,
        source=Source(row["source"]) if row.get("source") else None,
        source_ref=row.get("source_ref"),
    )


def _trading_days_back(end: date, count: int) -> list[date]:
    """The ``count`` most recent Mon-Fri dates on or before ``end``, oldest first."""
    days: list[date] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(days))


def _synthetic_prices(holdings: list[Holding], as_of: date) -> list[MergedPrice]:
    """A short, deterministic recent close series per open holding.

    Seeded off the ISIN, so the *shape* of each stock's story (up, down, choppy) is
    stable across runs even as the absolute dates slide forward with ``as_of``.
    Anchored to the security's own average buy cost from ``compute_holdings`` -- a real
    number already in the ledger -- rather than an arbitrary price, so unrealised P&L
    reads like a real holding's instead of noise. ``float`` is used only for the random
    walk itself and is converted back to ``Decimal`` via ``str()`` immediately, the same
    guard ``_dec`` applies everywhere else -- see CLAUDE.md rule 1; this module sits
    outside ``engine/``, where ``make lint`` enforces the ban structurally.
    """
    prices: list[MergedPrice] = []
    trading_days = _trading_days_back(as_of, PRICE_LOOKBACK_DAYS)

    for holding in holdings:
        rng = Random(holding.isin)
        total_return = Decimal(str(round(rng.uniform(-0.22, 0.48), 4)))
        price = (holding.avg_cost * (Decimal("1") + total_return)).quantize(Decimal("0.01"))

        # Walk backwards from today's close with small daily noise, so the stored series
        # looks like a recent close history rather than a flat line repeated ten times.
        for day in reversed(trading_days):
            prices.append(
                MergedPrice(isin=holding.isin, price_date=day, close=price, source=SOURCE_LABEL)
            )
            drift = Decimal(str(round(rng.uniform(-0.012, 0.012), 4)))
            price = (price / (Decimal("1") + drift)).quantize(Decimal("0.01"))

    return prices


def main() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    investor = fixture["investor"]
    demo_user = fixture["demo_user"]

    account_ids = {a["key"]: _account_id(investor["code"], a["key"]) for a in fixture["accounts"]}
    transactions = [
        _to_transaction(row, account_ids[row["account"]]) for row in fixture["transactions"]
    ]

    print(
        f"fixture: {len(transactions)} transactions, {len(fixture['securities'])} securities,"
        f" {len(fixture['accounts'])} accounts"
    )

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO investor (code, display_name) VALUES (%s, %s)
                ON CONFLICT (code) DO UPDATE SET display_name = EXCLUDED.display_name
                RETURNING id
                """,
                (investor["code"], investor["display_name"]),
            )
            investor_id = cur.fetchone()[0]

            for account in fixture["accounts"]:
                cur.execute(
                    """
                    INSERT INTO account
                        (id, investor_id, broker, dp_id, client_id, is_advised, label)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE
                        SET broker = EXCLUDED.broker, dp_id = EXCLUDED.dp_id,
                            client_id = EXCLUDED.client_id, is_advised = EXCLUDED.is_advised,
                            label = EXCLUDED.label
                    """,
                    (
                        account_ids[account["key"]],
                        investor_id,
                        account["broker"],
                        account.get("dp_id"),
                        account.get("client_id"),
                        account.get("is_advised", True),
                        account.get("label", account["broker"]),
                    ),
                )

            for security in fixture["securities"]:
                cur.execute(
                    """
                    INSERT INTO security (isin, symbol, name, exchange, asset_class)
                    VALUES (%s, %s, %s, %s, 'EQ')
                    ON CONFLICT (isin) DO UPDATE
                        SET symbol = EXCLUDED.symbol, name = EXCLUDED.name,
                            exchange = EXCLUDED.exchange
                    """,
                    (security["isin"], security["symbol"], security["name"], security["exchange"]),
                )

        summary = save_transactions(conn, transactions, actor_uid=None)

        # Real engine, not a re-derivation: whatever /holdings would show is exactly what
        # anchors the synthetic prices, so the two can never quietly disagree.
        holdings = compute_holdings(transactions)
        as_of = date.today()
        prices = _synthetic_prices(holdings, as_of)
        stored = price_db.upsert_prices(conn, prices)

        try:
            register(conn, demo_user["email"], demo_user["password"], demo_user.get("display_name"))
            login_note = f"demo login created: {demo_user['email']} / {demo_user['password']}"
        except EmailAlreadyRegisteredError:
            login_note = f"demo login already exists: {demo_user['email']}"

    print(f"investor {investor_id} ({investor['code']})")
    print(
        f"transactions: inserted={summary.inserted} skipped_duplicates={summary.skipped_duplicates}"
    )
    print(
        f"prices stored: {stored}"
        f" ({len(holdings)} securities x up to {PRICE_LOOKBACK_DAYS} trading days)"
    )
    print(login_note)
    # NOT "set NEXT_PUBLIC_DEFAULT_INVESTOR_ID": that build-time env var is gone.
    # lib/auth-context.tsx takes the investor from the login response alone, precisely so
    # two accounts in one browser cannot end up reading the same portfolio.
    print(f"\nLog in and paste {investor_id} into Settings to view this portfolio.")


if __name__ == "__main__":
    if not os.environ.get("DATABASE_URL"):
        raise SystemExit("DATABASE_URL is not set")
    main()
