"""The F&O contract master and price store (migration 063), plus the refresh / backfill
that fill them from ``app.prices.fno_sources``.

docs/ARCHITECTURE_INVARIANTS.md rule 21. Raw SQL, no ORM. A contract is identified by
(exchange, underlying, instrument_type, expiry, strike, option_type): a contract seen on
NSE and on BSE is two rows, and the exchange token is stored but never matched on.

``find_contracts`` returns EVERY match and decides nothing. Piece 1 (contract-note
resolution) accepts a candidate only when exactly one row comes back.

``load_fno_book`` is the read boundary into ``app.engine.fno``: one investor's
``fno_trade`` rows (migration 064) with the contracts and prices they need, as engine
objects. It reads; it computes nothing.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from time import monotonic
from typing import Any

from app.engine import fno_types
from app.prices.alerts import Notifier
from app.prices.fno_sources import (
    SOURCE_LABEL,
    FnoDayResult,
    FnoRow,
    bse_fo_source,
    fetch_fo_day,
    nse_fo_source,
)
from app.prices.pipe import NSE_BHAVCOPY_EARLIEST, trading_days

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FnoContract:
    id: str
    exchange: str
    instrument_type: str
    underlying: str
    expiry: date
    strike: Decimal | None
    option_type: str | None
    lot_size: int | None
    exchange_token: str | None
    first_seen: date
    last_seen: date
    #: Bhavcopy ``FinInstrmNm`` (migration 065); a resolution tie-breaker, never the key.
    fin_instrm_nm: str | None = None


@dataclass(frozen=True, slots=True)
class FnoPrice:
    contract_id: str
    price_date: date
    close: Decimal | None
    settlement_price: Decimal | None
    underlying_close: Decimal | None
    open_interest: Decimal | None
    lot_size: int | None
    source: str
    receipt: dict


_CONTRACT_COLUMNS = (
    "id, exchange, instrument_type, underlying, expiry, strike, option_type, lot_size, "
    "exchange_token, first_seen, last_seen, fin_instrm_nm"
)
_PRICE_COLUMNS = (
    "contract_id, price_date, close, settlement_price, underlying_close, open_interest, "
    "lot_size, source, receipt"
)


def _contract(row: tuple) -> FnoContract:
    return FnoContract(str(row[0]), *row[1:])


def _price(row: tuple) -> FnoPrice:
    return FnoPrice(str(row[0]), *row[1:])


def _identity_sort_key(row: FnoRow) -> tuple:
    """The unique index's own order: (exchange, underlying, instrument_type, expiry,
    COALESCE(strike, -1), COALESCE(option_type, ''))."""
    return (
        row.exchange,
        row.underlying,
        row.instrument_type,
        row.expiry,
        row.strike if row.strike is not None else Decimal("-1"),
        row.option_type or "",
    )


def upsert_contracts(conn: Any, rows: list[FnoRow], seen_on: date) -> dict[tuple, str]:
    """Insert or refresh one day's contracts; return identity -> contract id.

    first_seen / last_seen widen to include ``seen_on``. lot_size and exchange_token move
    only when ``seen_on`` is not older than the stored last_seen (a backfill of an older
    day must not overwrite the latest values), and a None (legacy tier) never erases one.
    fin_instrm_nm (migration 065) follows the same rule, so a backfill of an older UDiFF
    day fills a name the contract lacks and a legacy-tier day never erases one.
    """
    if not rows:
        return {}
    # SAME LOCK ORDER FOR EVERY WRITER. Two processes upserting one day's contracts in
    # different row orders take the unique-index row locks in different orders and
    # deadlock (seen live 2026-10-08, two post-import cycles healing the same day). Sorted
    # here and ORDER BY in the statement, so every writer locks in identity order.
    rows = sorted(rows, key=_identity_sort_key)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO fno_contract (exchange, instrument_type, underlying, expiry, strike,
                                      option_type, lot_size, exchange_token, first_seen,
                                      last_seen, fin_instrm_nm)
            SELECT u.exchange, u.instrument_type, u.underlying, u.expiry, u.strike,
                   u.option_type, u.lot_size, u.exchange_token, %s, %s, u.fin_instrm_nm
            FROM unnest(%s::text[], %s::text[], %s::text[], %s::date[], %s::numeric[],
                        %s::text[], %s::integer[], %s::text[], %s::text[])
                 AS u(exchange, instrument_type, underlying, expiry, strike, option_type,
                      lot_size, exchange_token, fin_instrm_nm)
            ORDER BY u.exchange, u.underlying, u.instrument_type, u.expiry,
                     COALESCE(u.strike, -1), COALESCE(u.option_type, '')
            ON CONFLICT (exchange, underlying, instrument_type, expiry,
                         COALESCE(strike, -1), COALESCE(option_type, ''))
            DO UPDATE SET
                lot_size = CASE WHEN EXCLUDED.last_seen >= fno_contract.last_seen
                                THEN COALESCE(EXCLUDED.lot_size, fno_contract.lot_size)
                                ELSE COALESCE(fno_contract.lot_size, EXCLUDED.lot_size) END,
                exchange_token = CASE WHEN EXCLUDED.last_seen >= fno_contract.last_seen
                                THEN COALESCE(EXCLUDED.exchange_token, fno_contract.exchange_token)
                                ELSE COALESCE(fno_contract.exchange_token, EXCLUDED.exchange_token)
                                END,
                fin_instrm_nm = CASE WHEN EXCLUDED.last_seen >= fno_contract.last_seen
                                THEN COALESCE(EXCLUDED.fin_instrm_nm, fno_contract.fin_instrm_nm)
                                ELSE COALESCE(fno_contract.fin_instrm_nm, EXCLUDED.fin_instrm_nm)
                                END,
                first_seen = LEAST(fno_contract.first_seen, EXCLUDED.first_seen),
                last_seen = GREATEST(fno_contract.last_seen, EXCLUDED.last_seen)
            RETURNING {_CONTRACT_COLUMNS}
            """,
            (
                seen_on,
                seen_on,
                [r.exchange for r in rows],
                [r.instrument_type for r in rows],
                [r.underlying for r in rows],
                [r.expiry for r in rows],
                [r.strike for r in rows],
                [r.option_type for r in rows],
                [r.lot_size for r in rows],
                [r.exchange_token for r in rows],
                [r.fin_instrm_nm for r in rows],
            ),
        )
        out: dict[tuple, str] = {}
        for record in cur.fetchall():
            c = _contract(record)
            key = (c.exchange, c.underlying, c.instrument_type, c.expiry, c.strike, c.option_type)
            out[key] = c.id
    return out


def upsert_prices(
    conn: Any, rows: list[FnoRow], ids: dict[tuple, str], receipt: dict[str, str]
) -> int:
    """Store one exchange-day of prices. Rows with neither a close nor a settlement price
    are skipped (the contract is still recorded); nothing is ever stored as zero.
    ``ON CONFLICT DO UPDATE``: a republished file carries its correction through."""
    priced = [r for r in rows if r.close is not None or r.settlement_price is not None]
    if not priced:
        return 0
    # Same reason as upsert_contracts: one lock order for every writer.
    priced.sort(key=lambda r: (ids[r.identity], r.price_date))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO fno_price ({_PRICE_COLUMNS})
            SELECT u.contract_id, u.price_date, u.close, u.settlement_price,
                   u.underlying_close, u.open_interest, u.lot_size, u.source, %s::jsonb
            FROM unnest(%s::uuid[], %s::date[], %s::numeric[], %s::numeric[], %s::numeric[],
                        %s::numeric[], %s::integer[], %s::text[])
                 AS u(contract_id, price_date, close, settlement_price, underlying_close,
                      open_interest, lot_size, source)
            ORDER BY u.contract_id, u.price_date
            ON CONFLICT (contract_id, price_date) DO UPDATE SET
                close = EXCLUDED.close,
                settlement_price = EXCLUDED.settlement_price,
                underlying_close = EXCLUDED.underlying_close,
                open_interest = EXCLUDED.open_interest,
                lot_size = EXCLUDED.lot_size,
                source = EXCLUDED.source,
                receipt = EXCLUDED.receipt
            """,
            (
                json.dumps(receipt),
                [ids[r.identity] for r in priced],
                [r.price_date for r in priced],
                [r.close for r in priced],
                [r.settlement_price for r in priced],
                [r.underlying_close for r in priced],
                [r.open_interest for r in priced],
                [r.lot_size for r in priced],
                [SOURCE_LABEL[r.exchange] for r in priced],
            ),
        )
    return len(priced)


def store_fno_day(conn: Any, result: FnoDayResult) -> dict[str, int]:
    """Persist one ``fetch_fo_day`` result; exchange -> price rows stored. Idempotent."""
    stored: dict[str, int] = {}
    for exchange, rows in result.rows.items():
        ids = upsert_contracts(conn, rows, result.price_date)
        stored[exchange] = upsert_prices(conn, rows, ids, result.receipts[exchange])
    return stored


def _summary(result: FnoDayResult, stored: dict[str, int]) -> dict[str, Any]:
    return {
        "price_date": result.price_date.isoformat(),
        "stored": stored,
        "not_published": result.not_published,
        "not_covered": result.not_covered,
        "failures": result.failures,
    }


def refresh_fno_prices(
    conn: Any, on: date, sources: list[Any] | None = None, notifier: Notifier | None = None
) -> dict[str, Any]:
    """The daily job: fetch both exchanges' F&O bhavcopy for ``on`` and store it."""
    result = fetch_fo_day(on, sources=sources, notifier=notifier)
    stored = store_fno_day(conn, result)
    conn.commit()
    return _summary(result, stored)


def fno_price_days_present(conn: Any, start: date, end: date) -> set[tuple[str, date]]:
    """(exchange, price_date) pairs that already have stored F&O prices."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT c.exchange, p.price_date
            FROM fno_price p JOIN fno_contract c ON c.id = p.contract_id
            WHERE p.price_date BETWEEN %s AND %s
            """,
            (start, end),
        )
        return {(r[0], r[1]) for r in cur.fetchall()}


def backfill_fno_prices(
    conn: Any,
    start: date,
    end: date,
    sources: list[Any] | None = None,
    notifier: Notifier | None = None,
) -> list[dict[str, Any]]:
    """On-demand backfill of ``[start, end]`` (e.g. an investor's F&O trade range).

    Resumable: an exchange-day already stored is not fetched again. ``start`` is clamped
    to ``NSE_BHAVCOPY_EARLIEST`` (the legacy NSE F&O archive was verified there).
    Commits per day so a timeout keeps what was done.
    """
    sources = sources if sources is not None else [nse_fo_source(), bse_fo_source()]
    start = max(start, NSE_BHAVCOPY_EARLIEST)
    present = fno_price_days_present(conn, start, end)
    out: list[dict[str, Any]] = []
    for day in trading_days(start, end):
        wanted = [s for s in sources if (s.exchange, day) not in present]
        if wanted:
            out.append(refresh_fno_prices(conn, day, sources=wanted, notifier=notifier))
    return out


def find_contracts(
    conn: Any,
    exchange: str,
    underlying: str,
    expiry: date,
    strike: Decimal | None,
    option_type: str | None,
    as_of: date,
    expiry_to: date | None = None,
) -> list[FnoContract]:
    """Every contract matching the identity (any instrument type) that was listed on
    ``as_of``: first seen on or before it and not yet expired. ``strike``/``option_type``
    None means a future. ``expiry_to`` widens the expiry to ``[expiry, expiry_to]`` -- a
    monthly contract-note symbol names a month, not a day (``app.ingest.fno_symbol``).
    Returns all rows; the caller requires exactly one."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_CONTRACT_COLUMNS} FROM fno_contract
            WHERE exchange = %s AND underlying = %s AND expiry BETWEEN %s AND %s
              AND strike IS NOT DISTINCT FROM %s::numeric
              AND option_type IS NOT DISTINCT FROM %s::text
              AND first_seen <= %s AND expiry >= %s
            ORDER BY instrument_type, id
            """,
            (
                exchange,
                underlying,
                expiry,
                expiry_to or expiry,
                strike,
                option_type,
                as_of,
                as_of,
            ),
        )
        return [_contract(r) for r in cur.fetchall()]


def latest_price(conn: Any, contract_id: str, as_of: date) -> FnoPrice | None:
    """The newest stored price on or before ``as_of``, or None. The caller names the
    missing price; it is never read as zero."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_PRICE_COLUMNS} FROM fno_price
            WHERE contract_id = %s AND price_date <= %s
            ORDER BY price_date DESC LIMIT 1
            """,
            (contract_id, as_of),
        )
        row = cur.fetchone()
    return _price(row) if row else None


def price_range(conn: Any, contract_id: str, start: date, end: date) -> list[FnoPrice]:
    """Stored prices for one contract in ``[start, end]``, oldest first."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_PRICE_COLUMNS} FROM fno_price
            WHERE contract_id = %s AND price_date BETWEEN %s AND %s
            ORDER BY price_date
            """,
            (contract_id, start, end),
        )
        return [_price(r) for r in cur.fetchall()]


@dataclass(frozen=True, slots=True)
class FnoBook:
    """One investor's F&O ledger as engine inputs (``app.engine.fno.compute_fno``)."""

    trades: list[fno_types.FnoTrade]
    contracts: list[fno_types.FnoContract]
    prices: list[fno_types.FnoPrice]



def backfill_expiry_settlements(
    conn: Any,
    today: date | None = None,
    sources_by_exchange: dict[str, Any] | None = None,
    notifier: Notifier | None = None,
    time_budget_seconds: float | None = None,
    clock: Callable[[], float] = monotonic,
) -> dict[str, int]:
    """Fetch the exchange F&O file of every past expiry day a traded contract has no
    price for -- the only place its final settlement price exists (ENGINE_SPEC section 15).

    WHY. The heal fetches a contract's TRADE days and the daily cron looks back 7 days,
    so an option bought before expiry and held through it -- its contract note ending
    before the expiry -- had no expiry-day row, and stayed "expired, settlement price not
    available" for ever (NIFTY 08-Sep-2026 PE, found 12 Oct 2026). Resumable: an
    exchange-day already stored is skipped by ``backfill_fno_prices``; a failed day is
    counted and retried next run. Stops claiming days once ``time_budget_seconds`` is spent.
    """
    today = today or date.today()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT c.exchange, c.expiry
            FROM fno_trade t JOIN fno_contract c ON c.id = t.contract_id
            WHERE c.expiry < %s
              AND NOT EXISTS (SELECT 1 FROM fno_price p
                              WHERE p.contract_id = c.id AND p.price_date = c.expiry)
            ORDER BY c.expiry, c.exchange
            """,
            (today,),
        )
        missing = [(r[0], r[1]) for r in cur.fetchall()]
    conn.commit()
    if sources_by_exchange is None:
        sources_by_exchange = {"NSE": nse_fo_source(), "BSE": bse_fo_source()}

    fetched = failed = done = 0
    began = clock()
    for exchange, expiry in missing:
        if time_budget_seconds is not None and clock() - began > time_budget_seconds:
            break
        done += 1
        source = sources_by_exchange.get(exchange)
        if source is None:
            failed += 1
            continue
        try:
            backfill_fno_prices(conn, expiry, expiry, sources=[source], notifier=notifier)
            fetched += 1
        except Exception:  # an archive outage must not stop the other days
            conn.rollback()
            failed += 1
            logger.warning("fno_expiry_settlement_failed %s %s", exchange, expiry, exc_info=True)
    return {
        "missing": len(missing),
        "fetched_days": fetched,
        "deferred": len(missing) - done,
        **({"failed_days": failed} if failed else {}),
    }

def load_fno_book(conn: Any, investor_id: str, as_of: date) -> FnoBook:
    """Every ``fno_trade`` row of the investor's accounts dated on or before ``as_of``,
    ordered ``(trade_date, seq)``, plus the contracts those trades reference and every
    stored price for them on or before ``as_of``.

    NUMERIC arrives as Decimal and is passed through untouched. ``seq`` is the ledger's
    created order (ENGINE_SPEC section 15 rule 2); ``reverses_id`` is set on a REVERSAL
    row (rule 15, migration 066). A price receipt crosses as JSON text
    (``FnoPrice.receipt`` is a string) carrying ``source`` alongside the stored provenance.
    ``lot_size`` is passed through as stored and may be None for a contract known only
    from the legacy NSE archive; the engine does not read it (rule 1).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.id, t.account_id, t.contract_id, t.trade_date, t.side, t.quantity,
                   t.price, t.charges, t.stt, t.kind, t.seq, t.reverses_id
            FROM fno_trade t JOIN account a ON a.id = t.account_id
            WHERE a.investor_id = %s AND t.trade_date <= %s
            ORDER BY t.trade_date, t.seq
            """,
            (investor_id, as_of),
        )
        trades = [
            fno_types.FnoTrade(
                id=str(r[0]),
                account_id=str(r[1]),
                contract_id=str(r[2]),
                trade_date=r[3],
                side=fno_types.FnoSide(r[4]),
                quantity=r[5],
                price=r[6],
                charges=r[7],
                stt=r[8],
                kind=fno_types.FnoTradeKind(r[9]),
                seq=r[10],
                reverses_id=str(r[11]) if r[11] is not None else None,
            )
            for r in cur.fetchall()
        ]
        ids = sorted({t.contract_id for t in trades})
        if not ids:
            return FnoBook([], [], [])
        cur.execute(
            f"SELECT {_CONTRACT_COLUMNS} FROM fno_contract WHERE id = ANY(%s::uuid[])",
            (ids,),
        )
        contracts = [
            fno_types.FnoContract(
                id=c.id,
                exchange=c.exchange,
                instrument_type=fno_types.FnoInstrumentType(c.instrument_type),
                underlying=c.underlying,
                expiry=c.expiry,
                strike=c.strike,
                option_type=fno_types.FnoOptionType(c.option_type) if c.option_type else None,
                lot_size=c.lot_size,
            )
            for c in (_contract(r) for r in cur.fetchall())
        ]
        cur.execute(
            f"""
            SELECT {_PRICE_COLUMNS} FROM fno_price
            WHERE contract_id = ANY(%s::uuid[]) AND price_date <= %s
            ORDER BY contract_id, price_date
            """,
            (ids, as_of),
        )
        prices = [
            fno_types.FnoPrice(
                contract_id=p.contract_id,
                price_date=p.price_date,
                close=p.close,
                settlement_price=p.settlement_price,
                underlying_close=p.underlying_close,
                receipt=json.dumps({"source": p.source, **p.receipt}, sort_keys=True),
            )
            for p in (_price(r) for r in cur.fetchall())
        ]
    return FnoBook(trades, contracts, prices)
