"""GET /investors/{id}/transactions, POST /investors/{id}/transactions,
GET /investors/{id}/lots, GET /investors/{id}/realised, GET /investors/{id}/dividends,
GET /investors/{id}/dividends/expected."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.auth.dependencies import require_owned_investor
from app.config import Settings, get_settings
from app.database import get_conn, money
from app.db.consent import require_consent
from app.db.fund_reference import valuation_routes
from app.db.fund_tax import facts_by_isin, facts_on, taxpayer_profile
from app.db.repository import account_belongs_to, load_transactions, save_transactions
from app.engine.dividends import compute_dividends, compute_expected_dividends
from app.engine.fund_tax import classify_fund_slice
from app.engine.holdings import exclude_unreconciled
from app.engine.lots import compute_lots
from app.engine.realised import compute_realised
from app.engine.tax_classification import classification_sensitive
from app.engine.types import ZERO, Transaction
from app.prices.fund_category import InstrumentKind
from app.prices.routing import Route
from app.routers import exclusions
from app.schemas.transactions import (
    DividendSummaryOut,
    ExpectedDividendsOut,
    ImportSummaryOut,
    LotOut,
    RealisedOut,
    TransactionIn,
    TransactionOut,
)

router = APIRouter(prefix="/investors", tags=["transactions", "derived"])


@router.get(
    "/{investor_id}/transactions",
    response_model=list[TransactionOut],
    dependencies=[Depends(require_owned_investor)],
)
def transactions(
    investor_id: str,
    isin: str | None = None,
    account_id: str | None = None,
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = None,
    connection: Any = Depends(get_conn),
) -> list[dict[str, Any]]:
    """List the ledger. ``from``/``to`` per contracts/openapi.yaml; ``from`` is a Python
    keyword, so the query parameter alias is applied here rather than to the field name."""
    rows = load_transactions(
        connection,
        investor_id=investor_id,
        account_id=account_id,
        isin=isin,
        date_from=from_,
        date_to=to,
    )
    return [
        {
            "id": t.id,
            "account_id": t.account_id,
            "isin": t.isin,
            "txn_date": t.txn_date,
            "txn_type": str(t.txn_type),
            "quantity": money(t.quantity),
            "price": money(t.price),
            "charges": money(t.charges),
            "stt": money(t.stt),
            "dividend_amount": money(t.dividend_amount),
            "tds_amount": money(t.tds_amount),
            "cost_transfer": money(t.cost_transfer),
            "source": str(t.source) if t.source else None,
            "source_ref": t.source_ref,
            "created_at": t.created_at,
        }
        for t in rows
    ]


@router.post(
    "/{investor_id}/transactions",
    response_model=ImportSummaryOut,
    dependencies=[Depends(require_owned_investor)],
)
def create_transactions(
    investor_id: str,
    body: list[TransactionIn],
    connection: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Append transactions -- manual entry, and the fallback path every other adapter
    needs when parsing fails. Idempotent on ``dedupe_key``: a duplicate is skipped and
    counted, never rejected as an error, per contracts/openapi.yaml.

    ``investor_id`` scopes the URL; each row still carries its own ``account_id``, which
    is what ``save_transactions`` actually writes against -- so every distinct
    ``account_id`` in the body is checked here against ``investor_id``, the same
    ``account_belongs_to`` call ``routers.ingest`` already makes before staging. An FK
    only proves the account exists, not that the caller owns it: without this check the
    path-level ``require_owned_investor`` guard would authorise the URL while the body
    let an authenticated user append ledger rows to a stranger's account.

    All logic lives in ``app.db.repository.save_transactions``; this handler only
    translates between the wire shape and ``Transaction``.
    """
    require_consent(connection, investor_id)
    for account_id in {t.account_id for t in body}:
        if not account_belongs_to(connection, account_id, investor_id):
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"no such account for this investor: {account_id}",
            )
    transactions = [
        Transaction(
            isin=t.isin,
            txn_date=t.txn_date,
            txn_type=t.txn_type,
            quantity=t.quantity,
            price=t.price,
            charges=t.charges,
            stt=t.stt,
            dividend_amount=t.dividend_amount,
            tds_amount=t.tds_amount,
            cost_transfer=t.cost_transfer,
            account_id=t.account_id,
            source=t.source,
            source_ref=t.source_ref,
            raw_payload=t.raw_payload,
        )
        for t in body
    ]
    summary = save_transactions(connection, transactions)
    return {
        "inserted": summary.inserted,
        "skipped_duplicates": summary.skipped_duplicates,
        "rejected": summary.rejected,
    }


@router.get(
    "/{investor_id}/lots",
    response_model=list[LotOut],
    dependencies=[Depends(require_owned_investor)],
)
def lots(
    response: Response,
    investor_id: str,
    as_of: date | None = None,
    connection: Any = Depends(get_conn),
) -> list[dict[str, Any]]:
    """Open FIFO lots. ``as_of`` enables the same historical replay every derived
    endpoint supports -- the engine takes one and today is simply the default.

    Securities whose history does not reconcile are left out and named in the
    ``X-Trackr-Excluded`` header (owner amendment 2026-10-08)."""
    rows, skipped = exclude_unreconciled(
        load_transactions(connection, investor_id=investor_id), as_of
    )
    exclusions.mark(response, skipped)
    return [
        {
            "isin": lot.isin,
            "open_date": lot.open_date,
            "quantity_open": money(lot.quantity_open),
            "cost_per_unit": money(lot.cost_per_unit),
            "acquisition_date_basis": lot.acquisition_date_basis,
        }
        for lot in compute_lots(rows, as_of=as_of)
    ]


def _financial_year_bounds(fy: str) -> tuple[date, date]:
    """``"2025-26"`` -> (2025-04-01, 2026-03-31). April-to-March, the same boundary
    ``app.engine.xirr.financial_year_start`` documents -- this only applies that existing
    rule as a filter, it does not define a new one."""
    try:
        start_year_str, end_suffix = fy.split("-", 1)
        start_year = int(start_year_str)
        end_year = (
            start_year // 100 * 100 + int(end_suffix) if len(end_suffix) == 2 else int(end_suffix)
        )
    except (ValueError, IndexError) as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"fy must look like '2025-26', got {fy!r}"
        ) from exc
    if end_year != start_year + 1:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"fy must span consecutive years, got {fy!r}"
        )
    return date(start_year, 4, 1), date(end_year, 3, 31)


@router.get(
    "/{investor_id}/realised",
    response_model=list[RealisedOut],
    dependencies=[Depends(require_owned_investor)],
)
def realised(
    response: Response,
    investor_id: str,
    fy: str | None = None,
    connection: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, Any]]:
    """Realised gains, FIFO-matched, short/long classified. ``fy`` filters to one
    Indian financial year, e.g. ``2025-26``.

    ``classification_sensitive`` flags a slice drawn from a provisionally dated bonus lot
    whose classification could change once the allotment date is known (ENGINE_SPEC
    section 3); the window is ``BONUS_ALLOTMENT_SENSITIVITY_DAYS``."""
    # Unreconciled securities are left out and named in X-Trackr-Excluded (2026-10-08).
    rows, skipped = exclude_unreconciled(load_transactions(connection, investor_id=investor_id))

    # Fund units (docs/MF_ETF_PLAN.md section 5) never take the equity-share path: an ETF
    # or mutual fund classified on the 365-day equity rule would get a confidently wrong
    # term. Equity shares below are computed exactly as before.
    routes = valuation_routes(connection, {t.isin for t in rows})
    fund_isins = {isin for isin, d in routes.items() if d.instrument_kind != InstrumentKind.STOCK}
    stock_rows = [t for t in rows if t.isin not in fund_isins]
    gains = compute_realised(stock_rows)
    fund_slices, fund_skipped = _fund_realised([t for t in rows if t.isin in fund_isins], routes)
    skipped = skipped + fund_skipped
    exclusions.mark(response, skipped)

    if fy is not None:
        start, end = _financial_year_bounds(fy)
        gains = [g for g in gains if g.sell_date is not None and start <= g.sell_date <= end]
        fund_slices = [
            f for f in fund_slices if f[1].sell_date is not None and start <= f[1].sell_date <= end
        ]

    out = [
        {
            "isin": g.isin,
            "sell_date": g.sell_date,
            "quantity": money(g.quantity),
            "proceeds": money(g.proceeds),
            "cost": money(g.cost),
            "gain": money(g.gain),
            "term": str(g.term),
            "holding_days": g.holding_days,
            "acquisition_date_basis": g.acquisition_date_basis,
            "classification_sensitive": classification_sensitive(
                g, sensitivity_days=settings.bonus_allotment_sensitivity_days
            ),
            "instrument_kind": "STOCK",
        }
        for g in gains
    ]
    if fund_slices:
        profiles = facts_by_isin(connection, sorted({g.isin for _a, g in fund_slices}))
        taxpayer = taxpayer_profile(connection, investor_id)
        fair_values = _fair_values_31jan2018(
            connection, routes, sorted({g.isin for _a, g in fund_slices})
        )
        for account_id, g in fund_slices:
            acquired = g.sell_date - timedelta(days=g.holding_days)
            if settings.mf_tax_enabled:
                result = classify_fund_slice(
                    acquired,
                    g.sell_date,
                    facts_on(profiles.get(g.isin, []), g.sell_date),
                    taxpayer,
                    # A sale or redemption carries STT by statute where it applies
                    # (Finance (No. 2) Act 2004, Chapter VII): the ledger SELL is it.
                    stt_paid_on_transfer=True,
                    fmv_31jan2018=fair_values.get(g.isin),
                )
                tax = {
                    "term": str(result.term) if result.resolved else "UNRESOLVED",
                    "tax_branch": str(result.branch) if result.branch else None,
                    "tax_term": str(result.term) if result.term else None,
                    "rate_status": str(result.rate_status) if result.rate_status else None,
                    "base_rate": str(result.base_rate) if result.base_rate is not None else None,
                    "indexation": result.indexation,
                    "tax_act": result.act,
                    "tax_section": result.section,
                    "tax_rule_version": result.rule_version,
                    "tax_unresolved_reason": result.unresolved_reason,
                }
            else:
                tax = {
                    "term": "UNRESOLVED",
                    "tax_unresolved_reason": (
                        "Fund capital-gains classification is not enabled on this deployment"
                    ),
                }
            out.append(
                {
                    "isin": g.isin,
                    "sell_date": g.sell_date,
                    "quantity": money(g.quantity),
                    "proceeds": money(g.proceeds),
                    "cost": money(g.cost),
                    "gain": money(g.gain),
                    "holding_days": g.holding_days,
                    "acquisition_date": acquired,
                    "acquisition_date_basis": g.acquisition_date_basis,
                    "classification_sensitive": False,
                    "instrument_kind": str(routes[g.isin].instrument_kind),
                    "account_id": account_id,
                    **tax,
                }
            )
    return out


def _fair_values_31jan2018(connection: Any, routes: dict[str, Any], isins: list[str]) -> dict:
    """31-Jan-2018 fair value per unit for grandfathering: an unlisted fund unit's NAV that
    day (s.55(2)(ac)); for an exchange-traded unit, the loaded ``security.fmv_31jan2018``."""
    from app.routers.capital_gains import _nav_31jan2018

    navs = _nav_31jan2018(connection, [i for i in isins if routes[i].route == Route.AMFI])
    with connection.cursor() as cur:
        cur.execute(
            "SELECT isin, fmv_31jan2018 FROM security WHERE isin = ANY(%s) "
            "AND fmv_31jan2018 IS NOT NULL",
            ([i for i in isins if routes[i].route != Route.AMFI],),
        )
        navs.update(dict(cur.fetchall()))
    return navs


def _fund_realised(
    rows: list[Transaction], routes: dict[str, Any]
) -> tuple[list[tuple[str | None, Any]], list[Any]]:
    """FIFO per ACCOUNT, then per ISIN (docs/MF_ETF_PLAN.md section 5.5): a sale in one
    demat account or folio can never consume another's lots. An account segment that does
    not reconcile on its own (units moved in without an evidenced transfer) is left out
    and named, never matched against another account's purchases."""
    by_account: dict[str | None, list[Transaction]] = {}
    for t in rows:
        by_account.setdefault(t.account_id, []).append(t)
    slices: list[tuple[str | None, Any]] = []
    skipped: list[Any] = []
    for account_id, account_rows in by_account.items():
        usable, broken = exclude_unreconciled(account_rows)
        skipped.extend(broken)
        slices.extend((account_id, g) for g in compute_realised(usable))
    return slices, skipped


@router.get(
    "/{investor_id}/dividends",
    response_model=list[DividendSummaryOut],
    dependencies=[Depends(require_owned_investor)],
)
def dividends(investor_id: str, connection: Any = Depends(get_conn)) -> list[dict[str, Any]]:
    """Dividend income, per ISIN and per financial year (app.engine.dividends).

    DIVIDEND is already a correct ledger row and already a positive XIRR cash flow
    (docs/ENGINE_SPEC.md section 4) -- this is the display aggregation over those same
    rows, not a new data source. Only ISINs with at least one DIVIDEND row appear, same
    convention as ``lots``/``realised`` above.
    """
    rows = load_transactions(connection, investor_id=investor_id)
    return [
        {
            "isin": d.isin,
            "total": money(d.total),
            "by_year": [{"fy": y.fy, "amount": money(y.amount)} for y in d.by_year],
        }
        for d in compute_dividends(rows)
    ]


@router.get(
    "/{investor_id}/dividends/expected",
    response_model=ExpectedDividendsOut,
    dependencies=[Depends(require_owned_investor)],
)
def expected_dividends(
    investor_id: str,
    as_of: date | None = None,
    connection: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Declared dividends sized to this investor's ledger -- PROJECTIONS, not income.

    A SEPARATE ENDPOINT FROM ``/dividends``, DELIBERATELY, and the separation is the
    product decision rather than a routing convenience. ``/dividends`` reports money that
    ARRIVED, aggregated from ``DIVIDEND`` ledger rows, and feeds the same figures XIRR,
    TWRR and contribution consume. This reports money a company SAID IT WOULD PAY, sized
    by what the ledger says was held at each ex-date. Returning them in one envelope would
    invite a caller to add them, and the sum of those two numbers is not a quantity that
    means anything.

    ``as_of`` bounds the projection to dividends whose ex-date has passed. Omit it to
    include upcoming declarations, which is what an "upcoming dividends" view wants and
    why the default is to include them.

    Anything that could not be sized comes back in ``unprojectable`` with a reason code --
    an unverified declaration, or a bonus/split close enough before the ex-date that the
    declared amount per share and the held quantity may be on different bases. Withheld
    and named, never silently dropped.
    """
    from app.db.corporate_actions import list_corporate_actions
    from app.db.dividend_events import list_dividend_events
    from app.db.security_alias import verified_map

    rows = load_transactions(connection, investor_id=investor_id)
    current_isins = {t.isin for t in rows}

    # ALIAS-EXPANDED, not just the current ISINs. `load_transactions` above already
    # resolved every row to its TERMINAL isin (the GUJTHEM case: a ledger holding both
    # INE942C01029 and INE942C01045 rows reads as INE942C01045 only). A dividend
    # declared while the security still traded under the RETIRED identifier is stored
    # in `dividend_event` under THAT identifier -- an automated feed has no reason to
    # know about a rename that happened later, and a historic yfinance/NSE lookup
    # reports whatever ISIN was current at the time. Querying dividend_event with only
    # the resolved ISINs would silently miss every such event: not an error, not an
    # entry in `unprojectable`, simply absent -- the exact failure mode this system
    # already hit once for holdings (docs/DATA_MODEL.md's security_alias section) and
    # must not repeat here. `verified_map()` gives old_isin -> terminal_isin for every
    # VERIFIED alias; inverting it recovers every retired identifier that resolves into
    # a security this investor currently holds.
    aliases = verified_map(connection)
    retired_isins = {old for old, current in aliases.items() if current in current_isins}
    isins = sorted(current_isins | retired_isins)

    raw_events = list_dividend_events(connection, isins=isins) if isins else []
    actions = list_corporate_actions(connection, isins) if isins else []

    # RESOLVED TO THE SAME IDENTITY SPACE AS `rows`. Fetching the retired-ISIN events
    # above is only half the fix: `compute_expected_dividends` matches `event.isin`
    # against these already-alias-resolved transactions via `quantity_held_before`, so
    # an event still carrying the RETIRED isin would look up a security nobody's ledger
    # mentions any more and find zero quantity -- "not a holder", when the investor
    # plainly is one under the identifier the ledger now uses. Both sides must speak the
    # same identity before they are compared, exactly as `load_transactions` already
    # ensures for the ledger itself.
    from dataclasses import replace

    from app.engine.dividends import drop_unverified_twins

    events = drop_unverified_twins(
        [replace(e, isin=aliases.get(e.isin, e.isin)) for e in raw_events]
    )
    # ^ ONE DIVIDEND, TWO ROWS: the rule lives in app.engine.dividends
    # (drop_unverified_twins), shared with the corporate-action timeline so the two
    # cannot disagree about how many declarations exist.

    from app.engine.dividends import split_fund_units

    events, excluded_fund_units = split_fund_units(events)
    expected, unprojectable = compute_expected_dividends(rows, events, actions, as_of=as_of)

    return {
        "expected": [
            {
                "isin": e.isin,
                "ex_date": e.ex_date,
                "kind": e.kind,
                "amount_per_share": money(e.amount_per_share),
                "eligible_quantity": money(e.eligible_quantity),
                "gross_expected": money(e.gross_expected),
                "record_date": e.record_date,
                "payment_date": e.payment_date,
                "event_id": e.event_id,
            }
            for e in expected
        ],
        "unprojectable": [
            {"isin": u.isin, "ex_date": u.ex_date, "code": u.code, "detail": u.detail}
            for u in unprojectable
        ],
        "excluded_fund_units": excluded_fund_units,
        "total_gross_expected": money(sum((e.gross_expected for e in expected), ZERO)),
    }
