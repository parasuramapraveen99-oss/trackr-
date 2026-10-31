"""How a figure computed WITHOUT some securities says so.

docs/ARCHITECTURE_INVARIANTS.md rule 16 and the 2026-10-08 owner amendment: performance,
lots, realised and contribution compute on every security that reconciles and name the
ones they left out, instead of one unreconciled stock returning 409 for the whole
portfolio. Object responses carry ``excluded``; list responses (lots, realised), whose
shape is a public contract, carry the same ISINs in the ``X-Trackr-Excluded`` header.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal

from fastapi import Response

from app import securities
from app.engine.holdings import SkippedHolding
from app.engine.types import Transaction

#: Comma-separated ISINs left out of a list response. Exposed for CORS in app.main.
EXCLUDED_HEADER = "X-Trackr-Excluded"


def describe(connection: Any, skipped: list[SkippedHolding]) -> list[dict[str, Any]]:
    if not skipped:
        return []
    meta = securities.lookup(connection, [s.isin for s in skipped])
    return [
        {
            "isin": s.isin,
            "symbol": securities.describe(meta, s.isin)["symbol"],
            "message": s.message,
            "deficit": str(s.deficit) if s.deficit is not None else None,
            "as_of": s.as_of,
            "reason_code": s.reason_code,
        }
        for s in skipped
    ]


def mark(response: Response, skipped: list[SkippedHolding]) -> None:
    if skipped:
        response.headers[EXCLUDED_HEADER] = ",".join(s.isin for s in skipped)


#: Reason code on a fund unit left out of a listed-securities figure.
FUND_SCOPE_REASON = "MUTUAL_FUND_SCOPE"

#: Which part of the ledger a figure covers. Owner amendment 26-Oct-2026: the Dashboard and
#: Holdings show stocks and mutual funds together ("all", their default) or either one
#: alone; returns are then computed over that scope. "listed" stays the default wherever a
#: figure predates the amendment (/performance, analytics), so none of those moves.
Scope = Literal["all", "listed", "mutual_fund"]


def in_scope(
    connection: Any, transactions: list[Transaction], scope: Scope
) -> list[Transaction]:
    """The ledger restricted to ``scope`` -- applied to the LEDGER, before holdings, FIFO
    and cash flows, the same rule ``listed_scope`` follows (docs/MF_ETF_PLAN.md s.8).

    A fund is a unit on the AMFI route; everything else (stocks, ETFs, units whose route
    is unresolved) is listed. Out-of-scope rows are left out by the user's choice, so
    unlike ``listed_scope`` this names nothing as excluded.
    """
    if scope == "all" or not transactions:
        return transactions
    from app.db.fund_reference import isins_on_route
    from app.prices.routing import Route

    funds = set(isins_on_route(connection, {t.isin for t in transactions}, Route.AMFI))
    if scope == "mutual_fund":
        return [t for t in transactions if t.isin in funds]
    return [t for t in transactions if t.isin not in funds]


def listed_scope(
    connection: Any,
    transactions: list[Transaction],
    skipped: list[SkippedHolding],
    on: date,
) -> tuple[list[Transaction], list[SkippedHolding]]:
    """Restrict a ledger to the listed-securities scope (stocks and ETFs), naming the
    mutual-fund units left out.

    docs/MF_ETF_PLAN.md section 8: scope is applied to the LEDGER, before holdings, FIFO
    and cash flows -- never by filtering a finished percentage. A NAV-valued fund's return
    is reported in its own series (``/investors/{id}/funds/performance``); blending it
    into this one would make neither number mean anything. Units whose route is not yet
    resolved stay in, and are named as unpriced by ``drop_unpriced`` like any other
    holding without a price.
    """
    from app.db.fund_reference import isins_on_route
    from app.prices.routing import Route

    funds = set(isins_on_route(connection, {t.isin for t in transactions}, Route.AMFI))
    if not funds:
        return transactions, skipped
    skipped = skipped + [
        SkippedHolding(
            isin=isin,
            message=(
                "Mutual fund units are reported in the fund series, not in listed-security returns"
            ),
            as_of=on,
            reason_code=FUND_SCOPE_REASON,
        )
        for isin in sorted(funds)
    ]
    return [t for t in transactions if t.isin not in funds], skipped


def drop_unpriced(
    transactions: list[Transaction],
    isins: list[str],
    prices: dict[str, Decimal],
    on: date,
    skipped: list[SkippedHolding],
    reasons: dict[str, str] | None = None,
) -> tuple[list[Transaction], list[str], list[SkippedHolding]]:
    """Leave out every held ISIN with no usable price on ``on``, and say so.

    ``reasons`` (``CurrentPrices.unpriced_reasons``) replaces the generic message for an
    ISIN whose route explains why it has no price, e.g. fund valuation switched off.

    CLAUDE.md rule 4 forbids VALUING a missing price at zero; it does not require one
    unpriced stock to blank the portfolio. Dropping the ISIN's rows entirely (buys and
    sells) and naming it in ``excluded`` is the same partition-and-label posture as an
    unreconciled history (docs/ARCHITECTURE_INVARIANTS.md rule 16, owner amendment
    2026-10-08). The real case: GUJTHEM's retired ISIN, whose new ISIN carries every
    price after the 2023 ISIN change but whose identity link is not yet resolved.
    """
    missing = [isin for isin in isins if isin not in prices]
    if not missing:
        return transactions, isins, skipped
    gone = set(missing)
    skipped = skipped + [
        SkippedHolding(
            isin=isin,
            message=(reasons or {}).get(isin, f"No usable price for {isin} on {on}"),
            as_of=on,
        )
        for isin in missing
    ]
    return (
        [t for t in transactions if t.isin not in gone],
        [isin for isin in isins if isin not in gone],
        skipped,
    )


def price_transfers(
    connection: Any,
    transactions: list[Transaction],
    start: date,
    end: date,
    skipped: list[SkippedHolding],
    *,
    live: bool = False,
    resolve: Any = None,
) -> tuple[list[Transaction], dict[tuple[str, date], Decimal], list[SkippedHolding]]:
    """Market prices for every TRANSFER_IN/OUT dated in ``(start, end]``, the input the
    period bridge (ENGINE_SPEC s.16) and contribution (s.10, amendment 2026-10-30) value
    transfers with: quantity x the Holdings price selection on the transfer date.

    A transfer with no usable price there drops its ISIN's rows entirely and names it,
    with the price lookup's own reason when it has one (a fund with valuation off was
    never priced, which is not the same as "no close found"). Never valued at zero or at
    recorded cost. Returns ``(transactions, transfer_prices, skipped)``.
    """
    from app.engine.types import TxnType
    from app.prices.current import resolve_current_prices

    resolve = resolve or resolve_current_prices  # a caller's own seam, else the default

    kinds = (TxnType.TRANSFER_IN, TxnType.TRANSFER_OUT)
    dates: dict[date, set[str]] = {}
    for t in transactions:
        if start < t.txn_date <= end and t.txn_type in kinds:
            dates.setdefault(t.txn_date, set()).add(t.isin)
    prices: dict[tuple[str, date], Decimal] = {}
    reasons: dict[tuple[str, date], str] = {}
    for on, isins in sorted(dates.items()):
        quoted = resolve(connection, sorted(isins), on, live=live)
        prices.update({(isin, on): price for isin, price in quoted.prices.items()})
        reasons.update({(isin, on): why for isin, why in quoted.unpriced_reasons.items()})
    unpriced: dict[str, date] = {}
    for t in transactions:
        if start < t.txn_date <= end and t.txn_type in kinds and (t.isin, t.txn_date) not in prices:
            unpriced.setdefault(t.isin, t.txn_date)
    if not unpriced:
        return transactions, prices, skipped
    skipped = skipped + [
        SkippedHolding(
            isin=isin,
            message=reasons.get((isin, on))
            or f"no usable price on or shortly before transfer date {on}",
            as_of=on,
        )
        for isin, on in sorted(unpriced.items())
    ]
    return [t for t in transactions if t.isin not in unpriced], prices, skipped


def missing_cas_history(
    connection: Any,
    investor_id: str,
    transactions: list[Transaction],
    skipped: list[SkippedHolding],
    on: date,
    advised_only: bool = False,
) -> list[SkippedHolding]:
    """Name CAS holdings that cannot contribute returns because acquisition history
    is absent. Reading only the ledger concealed these assets entirely. Snapshots remain
    checking evidence: this function neither adds holdings nor invents cost/cash flows.
    """
    from app.db.security_alias import verified_map
    from app.engine.types import ACQUIRING_TYPES

    with connection.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT ON (h.account_id, h.isin)
                      h.account_id, h.isin, h.quantity, h.snap_date
               FROM holding_snapshot h JOIN account a ON a.id = h.account_id
               WHERE a.investor_id = %s AND h.snap_date <= %s
                 AND (%s = FALSE OR a.is_advised = TRUE)
               ORDER BY h.account_id, h.isin, h.snap_date DESC""",
            (investor_id, on, advised_only),
        )
        rows = cur.fetchall()
    aliases = verified_map(connection, on)
    acquired = {
        (t.account_id, t.isin)
        for t in transactions
        if t.txn_date <= on and t.txn_type in ACQUIRING_TYPES
    }
    named = {s.isin for s in skipped}
    gaps = []
    for account_id, isin, quantity, snapshot_date in rows:
        current = aliases.get(isin, isin)
        if quantity <= 0 or current in named or (str(account_id), current) in acquired:
            continue
        named.add(current)
        gaps.append(
            SkippedHolding(
                isin=current,
                message=(
                    f"CAS declares {quantity} units on {snapshot_date}; acquisition history "
                    "is missing in this account. Cost basis and returns are unavailable."
                ),
                as_of=snapshot_date,
                reason_code="CAS_HISTORY_MISSING",
            )
        )
    return skipped + gaps
