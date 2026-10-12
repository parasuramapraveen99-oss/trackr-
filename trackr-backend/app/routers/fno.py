"""GET /investors/{id}/fno/positions, /fno/realised, /fno/tax -- equity F&O.

docs/FNO_PLAN.md "Surface", docs/ARCHITECTURE_INVARIANTS.md rule 21. A thin layer:
``app.db.fno_reference.load_fno_book`` reads the ``fno_trade`` ledger,
``app.engine.fno.compute_fno`` and ``app.engine.fno_tax.fno_tax_report`` compute, and the
functions here only shape the result. The one sum here, ``unrealised_total``, adds engine
figures and nothing else.

F&O is never blended with equity (OD-F8): no endpoint here returns an XIRR, and no equity
route reads ``fno_trade``. Every response names what it leaves out (rule 16): an unpriced
open position is ``excluded`` (never valued at 0, CLAUDE.md rule 4), and an expired
position the engine will not close is ``expired_unresolved``.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.auth.dependencies import get_current_user, require_owned_investor
from app.database import get_conn, money
from app.db.consent import require_consent
from app.db.fno_corrections import (
    FnoTradeNotFoundError,
    FnoTradeNotReversibleError,
    list_fno_trades,
    reverse_fno_trade,
)
from app.db.fno_reference import load_fno_book
from app.engine.fno import compute_fno, financial_year, realised_by_fy, realised_by_underlying
from app.engine.fno_tax import FnoTaxReport, LossAmount
from app.engine.fno_tax import fno_tax_report as compute_tax_report
from app.engine.fno_types import (
    FnoCloseKind,
    FnoContract,
    FnoExcluded,
    FnoExpiredUnresolved,
    FnoLot,
    FnoMatch,
    FnoOpenPosition,
    FnoResult,
    FnoTotals,
)
from app.engine.types import ZERO
from app.routers.transactions import _financial_year_bounds
from app.schemas.fno import (
    FnoPositionsResponse,
    FnoRealisedResponse,
    FnoReverseIn,
    FnoTaxResponse,
    FnoTradeOut,
)

router = APIRouter(prefix="/investors", tags=["derived", "fno"])

#: Shown on every engine-derived EXPIRY_SETTLE leg (ENGINE_SPEC section 15 rule 5). Such a
#: leg is provisional: no contract-note expiry line confirmed it. An EXCHANGE_SETTLEMENT
#: match came from the note itself and is not provisional.
PROVISIONAL_NOTE = (
    "Derived from the exchange's settlement price. No expiry line from your contract note "
    "has been imported, so exercise charges are not included and the ledger may be "
    "incomplete up to expiry. Import the expiry-day contract note to confirm."
)


def _provisional(m: FnoMatch) -> bool:
    return m.close_kind is FnoCloseKind.EXPIRY_SETTLE


def _receipt(text: str | None) -> dict[str, Any] | None:
    """``load_fno_book`` hands the engine a receipt as JSON text; it leaves as an object."""
    return json.loads(text) if text else None


def _contract(contract: FnoContract) -> dict[str, Any]:
    return {
        "contract_id": contract.id,
        "exchange": contract.exchange,
        "instrument_type": str(contract.instrument_type),
        "underlying": contract.underlying,
        "expiry": contract.expiry,
        "strike": money(contract.strike),
        "option_type": str(contract.option_type) if contract.option_type else None,
        "lot_size": contract.lot_size,
    }


def _lots(lots: tuple[FnoLot, ...]) -> list[dict[str, Any]]:
    return [
        {
            "open_trade_id": lot.open_trade_id,
            "open_date": lot.open_date,
            "quantity": money(lot.quantity),
            "price": money(lot.price),
            "charges": money(lot.charges),
        }
        for lot in lots
    ]


def _position(p: FnoOpenPosition, contracts: dict[str, FnoContract]) -> dict[str, Any]:
    return {
        **_contract(contracts[p.contract_id]),
        "account_id": p.account_id,
        "side": str(p.side),
        "quantity": money(p.quantity),
        "lots": _lots(p.lots),
        "mark": money(p.mark),
        "mark_date": p.mark_date,
        "mark_receipt": _receipt(p.mark_receipt),
        "unrealised": money(p.unrealised),
    }


def _unresolved(
    p: FnoExcluded | FnoExpiredUnresolved, contracts: dict[str, FnoContract]
) -> dict[str, Any]:
    return {
        **_contract(contracts[p.contract_id]),
        "account_id": p.account_id,
        "side": str(p.side),
        "quantity": money(p.quantity),
        "lots": _lots(p.lots),
        "reason": str(p.reason),
        "detail": getattr(p, "detail", None),
    }


def _match(m: FnoMatch) -> dict[str, Any]:
    return {
        "account_id": m.account_id,
        "contract_id": m.contract_id,
        "underlying": m.underlying,
        "side": str(m.side),
        "open_trade_id": m.open_trade_id,
        "open_date": m.open_date,
        "close_trade_id": m.close_trade_id,
        "close_date": m.close_date,
        "close_kind": str(m.close_kind),
        "quantity": money(m.quantity),
        "open_price": money(m.open_price),
        "close_price": money(m.close_price),
        "pnl": money(m.pnl),
        "charges": money(m.charges),
        "stt": money(m.stt),
        "financial_year": m.financial_year,
        "settlement_receipt": _receipt(m.settlement_receipt),
        "provisional": _provisional(m),
        "note": PROVISIONAL_NOTE if _provisional(m) else None,
    }


def _provisional_count(matches: list[FnoMatch], key, value: str) -> int:
    return sum(1 for m in matches if key(m) == value and _provisional(m))


def _totals(
    groups: dict[str, FnoTotals], matches: list[FnoMatch], key
) -> list[dict[str, Any]]:
    out = []
    for k, t in groups.items():
        provisional = _provisional_count(matches, key, k)
        out.append(
            {
                "key": k,
                "pnl": money(t.pnl),
                "charges": money(t.charges),
                "matches": t.matches,
                "includes_provisional": provisional > 0,
                "provisional_matches": provisional,
            }
        )
    return out


def _loss(loss: LossAmount) -> dict[str, Any]:
    return {
        "origin_fy": loss.origin_fy,
        "amount": money(loss.amount),
        "last_set_off_fy": loss.last_set_off_fy,
    }


def positions_body(
    result: FnoResult, contracts: list[FnoContract], as_of: date, has_trades: bool
) -> dict[str, Any]:
    by_id = {c.id: c for c in contracts}
    return {
        "as_of": as_of,
        "has_trades": has_trades,
        "positions": [_position(p, by_id) for p in result.open_positions],
        "unrealised_total": money(sum((p.unrealised for p in result.open_positions), ZERO)),
        "excluded": [_unresolved(p, by_id) for p in result.excluded],
        "expired_unresolved": [_unresolved(p, by_id) for p in result.expired_unresolved],
    }


def realised_body(
    result: FnoResult, contracts: list[FnoContract], as_of: date, fy: str | None
) -> dict[str, Any]:
    by_id = {c.id: c for c in contracts}
    matches = list(result.matches)
    if fy is not None:
        start, end = _financial_year_bounds(fy)
        matches = [m for m in matches if start <= m.close_date <= end]
    return {
        "as_of": as_of,
        "fy": fy,
        "matches": [_match(m) for m in matches],
        "by_fy": _totals(realised_by_fy(matches), matches, lambda m: m.financial_year),
        "by_underlying": _totals(
            realised_by_underlying(matches), matches, lambda m: m.underlying
        ),
        "expired_unresolved": [_unresolved(p, by_id) for p in result.expired_unresolved],
    }


def tax_body(
    result: FnoResult, contracts: list[FnoContract], as_of: date, fy: str | None
) -> dict[str, Any]:
    """Losses carry forward across years, so the report is always computed over every
    match; ``fy`` selects which year is returned (and extends the report to it). With no
    ``fy`` the report runs through the FY of ``as_of``, so a loss's expiry after the last
    match is still visible."""
    by_id = {c.id: c for c in contracts}
    label = financial_year(_financial_year_bounds(fy)[0]) if fy is not None else None
    report: FnoTaxReport = compute_tax_report(
        result.matches, through_fy=label or financial_year(as_of)
    )
    years = [y for y in report.years if label is None or y.financial_year == label]
    matches = list(result.matches)
    return {
        "as_of": as_of,
        "fy": fy,
        "unconfirmed_by_ca": report.unconfirmed_by_ca,
        "years": [
            {
                "financial_year": y.financial_year,
                "matches": y.matches,
                "includes_provisional": _provisional_count(
                    matches, lambda m: m.financial_year, y.financial_year
                )
                > 0,
                "provisional_matches": _provisional_count(
                    matches, lambda m: m.financial_year, y.financial_year
                ),
                "turnover": money(y.turnover),
                "realised_pnl": money(y.realised_pnl),
                "expenses": money(y.expenses),
                "stt": money(y.stt),
                "net_business_income": money(y.net_business_income),
                "set_off": [_loss(x) for x in y.set_off],
                "income_after_set_off": money(y.income_after_set_off),
                "expired": [_loss(x) for x in y.expired],
                "carried_forward": [_loss(x) for x in y.carried_forward],
                "flags": {
                    "turnover_above_1_crore": y.flags.turnover_above_1_crore,
                    "turnover_above_10_crore": y.flags.turnover_above_10_crore,
                    "net_profit_below_6_percent": y.flags.net_profit_below_6_percent,
                    "cash_within_5_percent": y.flags.cash_within_5_percent,
                    "income_above_exemption_limit": y.flags.income_above_exemption_limit,
                    "return_filed_by_due_date": y.flags.return_filed_by_due_date,
                    "unconfirmed_by_ca": y.flags.unconfirmed_by_ca,
                },
                "unconfirmed_by_ca": y.unconfirmed_by_ca,
            }
            for y in years
        ],
        "expired_unresolved": [_unresolved(p, by_id) for p in result.expired_unresolved],
    }


def _book(connection: Any, investor_id: str, as_of: date) -> tuple[FnoResult, Any]:
    book = load_fno_book(connection, investor_id, as_of)
    return compute_fno(book.trades, book.prices, book.contracts, as_of), book


@router.get(
    "/{investor_id}/fno/positions",
    response_model=FnoPositionsResponse,
    dependencies=[Depends(require_owned_investor)],
)
def fno_positions(
    investor_id: str, as_of: date | None = None, connection: Any = Depends(get_conn)
) -> dict[str, Any]:
    """Open F&O positions marked at the latest exchange price on or before ``as_of``
    (ENGINE_SPEC section 15 rule 8). ``has_trades`` says whether the investor has any
    F&O trade at all, so a client can show the F&O view only when there is one."""
    as_of = as_of or date.today()
    result, book = _book(connection, investor_id, as_of)
    return positions_body(result, book.contracts, as_of, bool(book.trades))


@router.get(
    "/{investor_id}/fno/realised",
    response_model=FnoRealisedResponse,
    dependencies=[Depends(require_owned_investor)],
)
def fno_realised(
    investor_id: str,
    fy: str | None = None,
    as_of: date | None = None,
    connection: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Realised F&O matches, FIFO per contract (OD-F3), each in the FY of its closing
    date; ``fy`` (e.g. ``2024-25``) filters them."""
    as_of = as_of or date.today()
    result, book = _book(connection, investor_id, as_of)
    return realised_body(result, book.contracts, as_of, fy)


@router.get(
    "/{investor_id}/fno/tax",
    response_model=FnoTaxResponse,
    dependencies=[Depends(require_owned_investor)],
)
def fno_tax(
    investor_id: str,
    fy: str | None = None,
    as_of: date | None = None,
    connection: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Per-FY F&O business-income figures for the user's CA. DRAFT, UNCONFIRMED BY CA:
    ``unconfirmed_by_ca`` is always true. Trackr+ does not file."""
    as_of = as_of or date.today()
    result, book = _book(connection, investor_id, as_of)
    return tax_body(result, book.contracts, as_of, fy)


def _trade_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "quantity": money(row["quantity"]),
        "price": money(row["price"]),
        "charges": money(row["charges"]),
        "stt": money(row["stt"]),
        "strike": money(row.get("strike")),
    }


@router.get(
    "/{investor_id}/fno/trades",
    response_model=list[FnoTradeOut],
    dependencies=[Depends(require_owned_investor)],
)
def fno_trades(investor_id: str, connection: Any = Depends(get_conn)) -> list[dict[str, Any]]:
    """The investor's F&O ledger as written, REVERSAL rows included, each with its
    contract identity and ``reversed`` -- so a trade to correct can be found."""
    return [_trade_out(r) for r in list_fno_trades(connection, investor_id)]


@router.post(
    "/{investor_id}/fno/trades/{trade_id}/reverse",
    response_model=FnoTradeOut,
    dependencies=[Depends(require_owned_investor)],
)
def reverse_trade(
    investor_id: str,
    trade_id: str,
    body: FnoReverseIn,
    response: Response,
    connection: Any = Depends(get_conn),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Void one F&O trade with an appended REVERSAL row (ENGINE_SPEC section 15 rule 15);
    nothing is edited. 201 when written, 200 when the trade was already reversed (the
    existing reversal is returned). 404 for a trade not in this investor's accounts, 409
    for a trade that is itself a reversal."""
    require_consent(connection, investor_id)
    try:
        row, created = reverse_fno_trade(
            connection, investor_id, trade_id, body.reason, actor_uid=current_user["id"]
        )
    except FnoTradeNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except FnoTradeNotReversibleError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    connection.commit()
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    row["reversed"] = False
    return _trade_out(row)
