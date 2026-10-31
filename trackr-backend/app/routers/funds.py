"""Mutual funds: the fund series, the taxpayer profile, and AMFI operations.

docs/MF_ETF_PLAN.md sections 7, 8 and 10. Investor routes are owner-scoped like every
other ``/investors/{id}`` route. Operator routes take ``X-Ops-Key``; the daily job takes
``CRON_SECRET`` (Vercel Cron's convention, GET only).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app import securities
from app.analytics import fund_checks, fund_nav, fund_portfolio
from app.auth.dependencies import get_current_user, require_owned_investor
from app.auth.ops import require_cron_secret, require_ops_key
from app.config import Settings, get_settings
from app.database import get_conn, money, rate
from app.db import cas_folios, consent, fund_tax
from app.db import fund_reference as ref
from app.db.index_levels import get_index_levels
from app.db.risk_free import annualised_rate
from app.engine.contribution import compute_contribution
from app.engine.fund_tax import FundTaxFacts
from app.engine.holdings import compute_holdings, exclude_unreconciled
from app.engine.lots import compute_lots
from app.engine.snapshot import compute_snapshot
from app.engine.twrr import compute_twrr
from app.engine.types import Holding, Snapshot, Transaction, TxnType
from app.engine.xirr import compute_xirr, compute_xirr_holdings
from app.funds import series
from app.ingest import mf_cas_pdf
from app.prices import amfi, amfi_jobs
from app.prices.current import resolve_current_prices
from app.routers import exclusions
from app.schemas.analytical import ExcludedOut

logger = logging.getLogger(__name__)
router = APIRouter(tags=["funds"])

_DISABLED = "Mutual fund valuation is not enabled on this deployment (MF_VALUATION_ENABLED)"


class FundHoldingOut(BaseModel):
    isin: str
    name: str | None
    scheme_code: int | None
    category: str | None
    plan: str | None
    option: str | None
    quantity: str
    invested: str
    nav: str | None
    nav_date: date | None
    market_value: str | None
    reason: str | None = None


class FundPerformanceOut(BaseModel):
    as_of: date
    scope: Literal["MUTUAL_FUND"] = "MUTUAL_FUND"
    xirr_all: str | None
    xirr_holdings: str | None
    xirr_unavailable_reason: str | None
    twrr: str | None
    twrr_unavailable_reason: str | None
    invested: str
    market_value: str
    unrealised: str
    realised_to_date: str
    complete: bool
    latest_final_snapshot: date | None
    holdings: list[FundHoldingOut]
    excluded: list[ExcludedOut]


class FundPointOut(BaseModel):
    as_of: date
    invested: str
    market_value: str
    unrealised: str
    realised_to_date: str
    final: bool
    pending_isins: list[str]
    excluded_isins: list[str]


class FundStatementHoldingOut(BaseModel):
    folio_key: str
    folio_label: str
    isin: str
    name: str
    amc_name: str
    as_of: date
    quantity: str | None
    nav: str | None
    market_value: str | None
    status: Literal['DECLARED', 'CONFLICT']
    reason: str | None
    source_batch_ids: list[str]


@router.get('/investors/{investor_id}/funds/statements',
            response_model=list[FundStatementHoldingOut],
            dependencies=[Depends(require_owned_investor)])
def fund_statement_holdings(investor_id: str, as_of: date | None = None,
                           connection: Any = Depends(get_conn)) -> list[dict[str, Any]]:
    """Dated CAS balances, available even when NAV-based return valuation is disabled."""
    return cas_folios.latest(connection, investor_id, as_of or date.today())


class MfTransactionOut(BaseModel):
    date: str
    kind: str
    description: str
    units: str | None
    nav: str | None
    amount: str | None
    balance: str | None


class MfSchemeOut(BaseModel):
    #: Masked ("…6789"), like /funds/statements. The full folio number is never returned.
    folio_label: str
    amc: str
    scheme: str
    isin: str | None
    registrar: str
    opening_units: str
    closing_units: str
    calculated_units: str
    nav: str | None
    nav_date: str | None
    value: str | None
    checks_passed: bool
    issues: list[str]
    evidence_rows: int
    transactions: list[MfTransactionOut]
    #: Whether "Add to your portfolio" would import this fund, and if not, why -- the same
    #: decision staging makes (app.db.imports.mf_sections).
    importable: bool = False
    blocked_reason: str | None = None
    #: Ledger rows it would become; charge rows fold into their parent transaction.
    ledger_rows: int = 0


class MfStatementPreviewOut(BaseModel):
    #: Always false in this version: the preview is never written to the ledger.
    #: docs/MF_ETF_PLAN.md P5 -- see app/ingest/mf_cas_pdf.py's module docstring.
    saved: Literal[False] = False
    issuer: str
    period_from: str
    period_to: str
    investor_name: str
    folio_count: int
    scheme_count: int
    transaction_count: int
    schemes_needing_attention: int
    #: False when the reader skipped or doubted part of the statement (see ``warnings``).
    complete: bool
    #: Funds with no NAV/value printed; left out of ``total_value``, never counted as 0.
    unvalued_count: int
    total_value: str
    warnings: list[str]
    schemes: list[MfSchemeOut]
    #: Fingerprint of exactly this preview. The import call sends it back, and is
    #: refused if the file no longer parses to what was reviewed.
    preview_digest: str = ""


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


@router.post(
    "/investors/{investor_id}/funds/statement-preview",
    response_model=MfStatementPreviewOut,
    dependencies=[Depends(require_owned_investor)],
)
async def preview_fund_statement(
    investor_id: str,
    file: UploadFile = File(...),
    password: str = Form(...),
    connection: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Read a CAMS/KFintech DETAILED CAS and say what is in it. Writes nothing.

    The "Import Mutual Funds" flow's upload step. Ownership and consent are checked
    before the file is opened (MF_ETF_PLAN section 6: "Account ownership and consent
    checks apply before staging"). The file is read in memory and not stored; the
    password is passed to the PDF library once and dropped -- never saved, unlike
    /ingest/files, and never logged.
    """
    consent.require_consent(connection, investor_id)
    content = await file.read(mf_cas_pdf.MAX_BYTES + 1)
    try:
        preview = await run_in_threadpool(mf_cas_pdf.parse_statement, content, password)
    except mf_cas_pdf.MfStatementError as exc:
        code = (
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
            if exc.code == "too_large"
            else status.HTTP_422_UNPROCESSABLE_ENTITY
        )
        raise HTTPException(code, str(exc)) from exc
    finally:
        del password

    from app.db import imports
    from app.ingest.mf_statement_rows import statement_digest

    sections = imports.mf_sections(connection, investor_id, preview)
    return {
        "saved": False,
        "preview_digest": statement_digest(preview),
        "issuer": preview.issuer,
        "period_from": preview.period_from,
        "period_to": preview.period_to,
        "investor_name": preview.investor_name,
        "folio_count": preview.folio_count,
        "scheme_count": len(preview.schemes),
        "transaction_count": preview.transaction_count,
        "schemes_needing_attention": preview.schemes_needing_attention,
        "complete": preview.complete,
        "unvalued_count": preview.unvalued_count,
        "total_value": str(preview.total_value),
        "warnings": preview.warnings,
        "schemes": [
            {
                "folio_label": s.folio_label,
                "amc": s.amc,
                "scheme": s.scheme,
                "isin": s.isin,
                "registrar": s.registrar,
                "opening_units": str(s.opening_units),
                "closing_units": str(s.closing_units),
                "calculated_units": str(s.calculated_units),
                "nav": _text(s.nav),
                "nav_date": s.nav_date,
                "value": _text(s.value),
                "checks_passed": s.checks_passed,
                "issues": s.issues,
                "evidence_rows": s.evidence_rows,
                "importable": sections[i].importable,
                "blocked_reason": sections[i].blocked_reason,
                "ledger_rows": len(sections[i].rows),
                "transactions": [
                    {
                        "date": t.date,
                        "kind": t.kind,
                        "description": t.description,
                        "units": _text(t.units),
                        "nav": _text(t.nav),
                        "amount": _text(t.amount),
                        "balance": _text(t.balance),
                    }
                    for t in s.transactions
                ],
            }
            for i, s in enumerate(preview.schemes)
        ],
    }


class MfImportSectionOut(BaseModel):
    folio_label: str
    amc: str
    scheme: str
    isin: str | None
    importable: bool
    blocked_reason: str | None
    ledger_rows: int


class MfStatementImportOut(BaseModel):
    #: The staged batch. Nothing is in the ledger until it is promoted through
    #: POST /ingest/stage/{batch_id}/promote -- the same attributed second step as every
    #: other import.
    batch_id: str
    staged_rows: int
    funds_importable: int
    funds_blocked: int
    sections: list[MfImportSectionOut]


@router.post(
    "/investors/{investor_id}/funds/statement-import",
    response_model=MfStatementImportOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_owned_investor)],
)
async def import_fund_statement(
    investor_id: str,
    file: UploadFile = File(...),
    password: str = Form(...),
    preview_digest: str = Form(...),
    connection: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Stage the statement the user just reviewed. Writes nothing to the ledger.

    The file is parsed again (the preview kept nothing) and must match ``preview_digest``
    -- 409 otherwise. Each fund imports whole or is staged as a named rejection. The PDF
    is kept in private document storage, still locked by its own password; the password
    itself is used once and dropped, never stored or logged.
    """
    from app.db import imports
    from app.routers.ingest import _investor_code
    from app.storage.factory import get_store

    consent.require_consent(connection, investor_id)
    content = await file.read(mf_cas_pdf.MAX_BYTES + 1)
    try:
        preview = await run_in_threadpool(mf_cas_pdf.parse_statement, content, password)
    except mf_cas_pdf.MfStatementError as exc:
        code = (
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
            if exc.code == "too_large"
            else status.HTTP_422_UNPROCESSABLE_ENTITY
        )
        raise HTTPException(code, str(exc)) from exc
    finally:
        del password
    try:
        staged = imports.stage_mf_statement(
            connection,
            investor_id=investor_id,
            content=content,
            preview=preview,
            reviewed_digest=preview_digest,
            filename=file.filename or "",
            store=get_store(settings),
            investor_code=_investor_code(connection, investor_id),
            actor_uid=str(current_user["id"]),
        )
    except imports.StatementChangedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except imports.DuplicateUploadError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This statement was already imported. Nothing was added twice.",
        ) from exc
    sections = staged["sections"]
    return {
        "batch_id": staged["batch_id"],
        "staged_rows": staged["staged_rows"],
        "funds_importable": sum(1 for s in sections if s.importable),
        "funds_blocked": sum(1 for s in sections if not s.importable),
        "sections": [
            {
                "folio_label": s.folio_label,
                "amc": s.amc,
                "scheme": s.scheme,
                "isin": s.isin,
                "importable": s.importable,
                "blocked_reason": s.blocked_reason,
                "ledger_rows": len(s.rows),
            }
            for s in sections
        ],
    }


class TaxProfileIn(BaseModel):
    taxpayer_type: Literal["INDIVIDUAL", "HUF", "COMPANY", "FIRM", "TRUST", "OTHER"]
    residency: Literal["RESIDENT", "NON_RESIDENT"]


class TaxProfileOut(TaxProfileIn):
    supported_for_fund_tax: bool


class FundTaxProfileIn(BaseModel):
    isin: str
    valid_from: date
    valid_to: date
    equity_oriented: bool | None
    specified_fund: bool | None
    listed: bool | None
    domiciled_in_india: bool = True
    evidence_url: str
    evidence_note: str
    evidence_sha256: str | None = None


def _require_valuation(settings: Settings) -> None:
    if not settings.mf_valuation_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _DISABLED)


@dataclass(frozen=True)
class _FundBook:
    """One replay of the MUTUAL_FUND scope, shared by every fund read so the overview,
    the analytics and a scheme page cannot disagree about what is held or excluded.

    ``transactions`` and ``prices`` are AFTER exclusions: only what reconciles and has a
    NAV. ``holdings`` is BEFORE the unpriced drop, so an unpriced position is still listed
    and named rather than vanishing."""

    transactions: list[Transaction]
    holdings: list[Holding]
    current: Any
    prices: dict[str, Decimal]
    snapshot: Snapshot
    skipped: list[Any]


def _fund_book(connection: Any, investor_id: str, advised_only: bool, on: date) -> _FundBook:
    transactions = series.fund_ledger(connection, investor_id, advised_only, date_to=on)
    from app.db.fund_reference import isins_on_route
    from app.db.repository import load_transactions
    from app.prices.routing import Route

    all_tx = load_transactions(connection, investor_id=investor_id,
                               advised_only=advised_only, date_to=on)
    cas_gaps = exclusions.missing_cas_history(
        connection, investor_id, all_tx, [], on, advised_only
    )
    fund_gaps = set(isins_on_route(connection, {g.isin for g in cas_gaps}, Route.AMFI))
    transactions, skipped = exclude_unreconciled(transactions, on)
    skipped.extend(g for g in cas_gaps if g.isin in fund_gaps
                   and g.isin not in {s.isin for s in skipped})
    holdings = compute_holdings(transactions, on)
    isins = [h.isin for h in holdings]
    current = resolve_current_prices(connection, isins, on)
    transactions, isins, skipped = exclusions.drop_unpriced(
        transactions, isins, current.prices, on, skipped
    )
    prices = {i: p for i, p in current.prices.items() if i in isins}
    snapshot = compute_snapshot(transactions, prices, on)
    return _FundBook(transactions, holdings, current, prices, snapshot, skipped)


@router.get(
    "/investors/{investor_id}/funds/performance",
    response_model=FundPerformanceOut,
    dependencies=[Depends(require_owned_investor)],
)
def fund_performance(
    investor_id: str,
    as_of: date | None = None,
    advised_only: bool = False,
    connection: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """The MUTUAL_FUND scope: XIRR and TWRR over fund units only, valued at AMFI NAVs.

    Never blended with the listed-securities figures on ``/performance`` (plan section
    1). An undefined XIRR -- too few dates, no sign change -- is null with its reason,
    never 0%. What could not be valued is named in ``excluded``.
    """
    _require_valuation(settings)
    on = as_of or date.today()
    book = _fund_book(connection, investor_id, advised_only, on)
    transactions, holdings, current = book.transactions, book.holdings, book.current
    skipped, prices, snapshot = book.skipped, book.prices, book.snapshot
    xirr_all = compute_xirr(transactions, prices, on) if transactions else None
    xirr_holdings = compute_xirr_holdings(transactions, prices, on) if transactions else None
    xirr_reason = None
    if not transactions:
        xirr_reason = "No fund transactions that can be valued"
    elif xirr_all is None:
        xirr_reason = "XIRR is undefined for these cash flows (too few dates or no sign change)"

    points = series.list_points(connection, investor_id, advised_only, date(1990, 1, 1), on)
    chain = [
        Snapshot(
            as_of=p["as_of"],
            invested=p["invested"],
            market_value=p["market_value"],
            unrealised=p["unrealised"],
            realised_to_date=p["realised_to_date"],
        )
        for p in points
    ]
    if not chain or chain[-1].as_of != on:
        chain.append(snapshot)
    twrr = compute_twrr(chain, transactions) if transactions else None
    twrr_reason = None if twrr is not None else "Not enough fund snapshots to chain a TWRR"
    latest_final = max((p["as_of"] for p in points if p["final"]), default=None)

    routes = ref.valuation_routes(connection, [h.isin for h in holdings])
    meta = securities.lookup(connection, [h.isin for h in holdings])
    out_holdings = []
    for h in holdings:
        quote = current.quotes.get(h.isin)
        d = routes.get(h.isin)
        out_holdings.append(
            {
                "isin": h.isin,
                "name": (d.scheme_name if d else None) or securities.display_name(meta, h.isin),
                "scheme_code": d.scheme_code if d else None,
                "category": d.category if d else None,
                "plan": d.plan if d else None,
                "option": d.option if d else None,
                "quantity": money(h.quantity),
                "invested": money(h.invested),
                "nav": str(quote.price) if quote else None,
                "nav_date": quote.price_date if quote else None,
                "market_value": money(h.quantity * quote.price) if quote else None,
                "reason": current.unpriced_reasons.get(h.isin) if not quote else None,
            }
        )
    return {
        "as_of": on,
        "xirr_all": rate(xirr_all),
        "xirr_holdings": rate(xirr_holdings),
        "xirr_unavailable_reason": xirr_reason,
        "twrr": rate(twrr),
        "twrr_unavailable_reason": twrr_reason,
        "invested": money(snapshot.invested),
        "market_value": money(snapshot.market_value),
        "unrealised": money(snapshot.unrealised),
        "realised_to_date": money(snapshot.realised_to_date),
        "complete": not skipped,
        "latest_final_snapshot": latest_final,
        "holdings": out_holdings,
        "excluded": exclusions.describe(connection, skipped),
    }


@router.get(
    "/investors/{investor_id}/funds/snapshots",
    response_model=list[FundPointOut],
    dependencies=[Depends(require_owned_investor)],
)
def fund_snapshots(
    investor_id: str,
    start: date,
    end: date,
    advised_only: bool = False,
    connection: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, Any]]:
    """The stored fund series in ``[start, end]``. ``final`` false means a NAV the row
    depends on is still awaited, or a holding is excluded -- never a finished figure."""
    _require_valuation(settings)
    if start > end:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "start must be on or before end")
    return [
        {
            **p,
            **{
                k: money(p[k])
                for k in ("invested", "market_value", "unrealised", "realised_to_date")
            },
        }
        for p in series.list_points(connection, investor_id, advised_only, start, end)
    ]


# --- one scheme: what it is, its NAV, the investor's units -----------------------------
#
# Read-only over the same `_fund_book` replay as /funds/performance; nothing is stored. The
# holding page (/holdings/{isin}) calls this for a fund. Fund returns and risk here are the
# SCHEME's, from its published NAV -- never the investor's return.

#: ELSS units are locked for three years from each allotment (ELSS scheme rules; every SIP
#: instalment is its own lot), so the lock is read off FIFO lots, never the first buy.
ELSS_LOCK_YEARS = 3

#: Display names for the exposure the AMFI heading implies (app/prices/fund_category.py).
#: UNKNOWN is shown as such -- a sectoral fund may hold foreign stocks, and calling it
#: "Equity" would assert a domestic exposure the heading does not.
_ASSET_CLASS = {
    "DOMESTIC_EQUITY": "Equity",
    "DEBT": "Debt",
    "MIXED": "Hybrid",
    "COMMODITY": "Commodity",
    "INTERNATIONAL": "International",
    "UNKNOWN": "Unclassified by heading",
}


def _split_category(d: Any) -> tuple[str | None, str | None]:
    """(asset class, category) for a route decision; both None outside the catalogue."""
    if d is None:
        return None, None
    exposure = getattr(d.exposure, "value", d.exposure)
    return _ASSET_CLASS.get(exposure) if exposure else None, d.category


def _is_elss(category: str | None) -> bool:
    return bool(category) and "ELSS" in category.upper()


def _unlock_date(opened: date) -> date:
    try:
        return opened.replace(year=opened.year + ELSS_LOCK_YEARS)
    except ValueError:  # allotted on 29 February
        return opened.replace(year=opened.year + ELSS_LOCK_YEARS, day=28)


class FundTrailingOut(BaseModel):
    years: int
    start_date: date | None
    end_date: date | None
    cumulative: str | None
    annualised: str | None
    reason: str | None


class FundRiskOut(BaseModel):
    months: int
    start_date: date | None
    end_date: date | None
    volatility: str | None
    max_drawdown: str | None
    drawdown_peak: date | None
    drawdown_trough: date | None
    reason: str | None


class FundLotOut(BaseModel):
    open_date: date
    units: str
    cost: str
    unlock_date: date | None


class FundNavPointOut(BaseModel):
    date: date
    nav: str


class FundDetailOut(BaseModel):
    as_of: date
    isin: str
    name: str | None
    scheme_code: int | None
    amc: str | None
    asset_class: str | None
    category: str | None
    plan: str | None
    option: str | None
    route_reason: str
    held: bool
    quantity: str | None
    invested: str | None
    nav: str | None
    nav_date: date | None
    market_value: str | None
    unrealised: str | None
    elss: bool
    lots: list[FundLotOut]
    #: Why the NAV-based figures below are withheld, when they are (an IDCW option).
    nav_basis_note: str | None
    trailing: list[FundTrailingOut]
    risk: FundRiskOut
    nav_history: list[FundNavPointOut]


def _ratio(value: float | None) -> str | None:
    return None if value is None else rate(Decimal(str(value)))


def _is_idcw(option: str | None) -> bool:
    text = (option or "").upper()
    return "IDCW" in text or "DIVIDEND" in text


@router.get(
    "/investors/{investor_id}/funds/schemes/{isin}",
    response_model=FundDetailOut,
    dependencies=[Depends(require_owned_investor)],
)
def fund_detail(
    investor_id: str,
    isin: str,
    as_of: date | None = None,
    advised_only: bool = False,
    connection: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """One scheme: identity, the investor's position and lots, and what its NAV says.

    Trailing returns and risk are the FUND's, from its published NAV -- not the investor's
    return. They are withheld for an IDCW option, whose NAV drops at every payout.
    """
    _require_valuation(settings)
    on = as_of or date.today()
    from app.prices.routing import Route

    d = ref.valuation_routes(connection, [isin]).get(isin)
    if d is None or d.route is not Route.AMFI:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            d.reason if d else f"{isin} is not a mutual fund valued at its AMFI NAV",
        )
    book = _fund_book(connection, investor_id, advised_only, on)
    holding = next((h for h in book.holdings if h.isin == isin), None)
    quote = book.current.quotes.get(isin) if isin in book.prices else None
    value = holding.quantity * quote.price if holding and quote else None
    elss = _is_elss(d.category)
    asset_class, category = _split_category(d)

    history = ref.nav_history(connection, isin, date(on.year - 6, 1, 1), on)
    note = None
    if _is_idcw(d.option):
        note = ("IDCW option: the NAV falls by every payout, so its change understates the "
                "return. Fund returns and risk are shown for growth options only.")
        trailing = [
            {"years": y, "start_date": None, "end_date": None, "cumulative": None,
             "annualised": None, "reason": "Withheld for an IDCW option"}
            for y in fund_nav.TRAILING_YEARS
        ]
        risk = {"months": fund_nav.RISK_MONTHS, "start_date": None, "end_date": None,
                "volatility": None, "max_drawdown": None, "drawdown_peak": None,
                "drawdown_trough": None, "reason": "Withheld for an IDCW option"}
    else:
        trailing = []
        for y in fund_nav.TRAILING_YEARS:
            t = fund_nav.trailing_return(history, on, y)
            trailing.append({"years": y, "start_date": t.start_date, "end_date": t.end_date,
                             "cumulative": _ratio(t.cumulative),
                             "annualised": _ratio(t.annualised), "reason": t.reason})
        r = fund_nav.monthly_risk(history, on)
        risk = {"months": r.months, "start_date": r.start_date, "end_date": r.end_date,
                "volatility": _ratio(r.volatility), "max_drawdown": _ratio(r.max_drawdown),
                "drawdown_peak": r.drawdown_peak, "drawdown_trough": r.drawdown_trough,
                "reason": r.reason}

    lots = [lot for lot in compute_lots(book.transactions, on) if lot.isin == isin]
    return {
        "as_of": on,
        "isin": isin,
        "name": d.scheme_name,
        "scheme_code": d.scheme_code,
        "amc": d.amc_name,
        "asset_class": asset_class,
        "category": category,
        "plan": d.plan,
        "option": d.option,
        "route_reason": d.reason,
        "held": holding is not None,
        "quantity": money(holding.quantity) if holding else None,
        "invested": money(holding.invested) if holding else None,
        "nav": str(quote.price) if quote else None,
        "nav_date": quote.price_date if quote else None,
        "market_value": money(value),
        "unrealised": money(value - holding.invested) if value is not None and holding else None,
        "elss": elss,
        "lots": [
            {"open_date": lot.open_date, "units": money(lot.quantity_open),
             "cost": money(lot.quantity_open * lot.cost_per_unit),
             "unlock_date": _unlock_date(lot.open_date) if elss else None}
            for lot in lots
        ],
        "nav_basis_note": note,
        "trailing": trailing,
        "risk": risk,
        "nav_history": [{"date": on_, "nav": str(nav)} for on_, nav in history],
    }


# --- the Mutual fund analytics tabs: Overview, Diversification, Risk & return, Performance,
# Diagnostics -----------------------------------------------------------------------------
#
# One read over the same `_fund_book` replay as /funds/performance, so the tabs and the fund
# header cannot disagree. Only what Trackr+ holds data for is here: plan, fund house,
# category and asset class come from the AMFI scheme list; risk and return from published
# NAVs and the Nifty 50 TRI. Expense ratios, holdings look-through, overlap, category peers
# and exit loads need sources Trackr+ does not have, and are absent rather than estimated.

#: The index the fund risk figures are compared with (a total-return series).
FUND_BENCHMARK = "NIFTY50_TRI"


class FundGroupOut(BaseModel):
    label: str
    value: str
    share: str | None
    count: int


class FundRowOut(BaseModel):
    isin: str
    name: str
    amc: str | None
    category: str | None
    asset_class: str | None
    plan: str
    option: str | None
    units: str
    nav: str | None
    nav_date: date | None
    invested: str
    value: str | None
    unrealised: str | None
    unrealised_pct: str | None
    weight: str | None
    #: The SCHEME's own NAV return, not the investor's: 1 year, and 3 years a year.
    fund_return_1y: str | None
    fund_return_3y: str | None
    fund_return_reason: str | None


class FundUnvaluedOut(BaseModel):
    isin: str
    name: str
    reason: str


class FundStatsOut(BaseModel):
    annual_return: str | None
    volatility: str | None
    sharpe: str | None
    sortino: str | None
    beta: str | None
    alpha: str | None
    max_drawdown: str | None


class FundRiskPointOut(BaseModel):
    isin: str
    name: str
    weight: str | None
    stats: FundStatsOut


class FundLevelOut(BaseModel):
    date: date
    mix: str
    benchmark: str | None


class FundWindowOut(BaseModel):
    years: int
    months: int
    start: date | None
    end: date | None
    reason: str | None
    #: Share of today's fund value that has this window's full NAV history.
    covered_weight: str | None
    excluded: list[FundUnvaluedOut]
    risk_free_rate: str | None
    benchmark_name: str
    benchmark_reason: str | None
    mix: FundStatsOut | None
    benchmark: FundStatsOut | None
    funds: list[FundRiskPointOut]
    levels: list[FundLevelOut]


class FundContributionOut(BaseModel):
    isin: str
    name: str
    gain: str
    share_of_total: str | None
    held: bool


class FundCheckOut(BaseModel):
    key: str
    title: str
    status: Literal["pass", "warn", "info", "not_assessable"]
    summary: str
    rule: str
    items: list[str]


class FundAnalyticsOut(BaseModel):
    as_of: date
    market_value: str
    invested: str
    unrealised: str
    realised_to_date: str
    xirr: str | None
    xirr_reason: str | None
    plan_split: list[FundGroupOut]
    asset_classes: list[FundGroupOut]
    fund_houses: list[FundGroupOut]
    categories: list[FundGroupOut]
    funds: list[FundRowOut]
    unvalued: list[FundUnvaluedOut]
    windows: list[FundWindowOut]
    contributions: list[FundContributionOut]
    checks: list[FundCheckOut]
    rule_version: str
    excluded: list[ExcludedOut]


def _stats_out(s: fund_portfolio.MonthlyStats | None) -> dict[str, Any] | None:
    if s is None:
        return None
    return {k: _ratio(getattr(s, k)) for k in
            ("annual_return", "volatility", "sharpe", "sortino", "beta", "alpha", "max_drawdown")}


def _groups(rows: list[tuple[str, Decimal]], total: Decimal) -> list[dict[str, Any]]:
    """Value per label, largest first; ``rows`` is (label, value) per fund."""
    sums: dict[str, Decimal] = {}
    counts: dict[str, int] = {}
    for label, value in rows:
        sums[label] = sums.get(label, Decimal(0)) + value
        counts[label] = counts.get(label, 0) + 1
    return [
        {"label": label, "value": money(value), "share": rate(value / total) if total > 0 else None,
         "count": counts[label]}
        for label, value in sorted(sums.items(), key=lambda kv: -kv[1])
    ]


def _fund_window(
    connection: Any,
    years: int,
    on: date,
    histories: dict[str, list[tuple[date, Decimal]]],
    weights: dict[str, Decimal],
    names: dict[str, str],
    ruled_out: dict[str, str],
) -> dict[str, Any]:
    months = 12 * years
    mix = fund_portfolio.current_mix(histories, weights, on, months, ruled_out)
    keys = fund_portfolio.month_keys(on, months)
    start, end = fund_portfolio.month_end(keys[0]), fund_portfolio.month_end(keys[-1])
    ends = [fund_portfolio.month_end(k) for k in keys]
    index = get_index_levels(connection, FUND_BENCHMARK, ends, max_staleness_days=7)
    bench = fund_portfolio.monthly_series(sorted(index.items()), on, months)
    bench_reason = bench if isinstance(bench, str) else None
    bench_returns = None if isinstance(bench, str) else bench.returns
    risk_free = annualised_rate(connection, start, end)
    total = sum(weights.values(), Decimal(0))

    funds = []
    for isin in mix.included:
        found = fund_portfolio.monthly_series(histories[isin], on, months)
        if isinstance(found, str):
            continue
        funds.append({
            "isin": isin, "name": names[isin],
            "weight": rate(weights[isin] / total) if total > 0 else None,
            "stats": _stats_out(fund_portfolio.stats(found.returns, bench_returns, risk_free)),
        })
    levels = []
    if mix.series is not None:
        bench_levels = (
            dict(fund_portfolio.levels_base_100(bench)) if not isinstance(bench, str) else {}
        )
        levels = [
            {"date": d, "mix": f"{v:.4f}",
             "benchmark": f"{bench_levels[d]:.4f}" if d in bench_levels else None}
            for d, v in fund_portfolio.levels_base_100(mix.series)
        ]
    return {
        "years": years,
        "months": months,
        "start": start,
        "end": end,
        "reason": mix.reason,
        "covered_weight": rate(mix.covered_weight),
        "excluded": [{"isin": i, "name": names.get(i, i), "reason": why}
                     for i, why in sorted(mix.excluded.items())],
        "risk_free_rate": _ratio(risk_free),
        "benchmark_name": "Nifty 50 TRI",
        "benchmark_reason": bench_reason,
        "mix": _stats_out(fund_portfolio.stats(mix.series.returns, bench_returns, risk_free))
        if mix.series else None,
        "benchmark": _stats_out(fund_portfolio.stats(bench_returns, None, risk_free))
        if bench_returns else None,
        "funds": funds,
        "levels": levels,
    }


@router.get(
    "/investors/{investor_id}/funds/analytics",
    response_model=FundAnalyticsOut,
    dependencies=[Depends(require_owned_investor)],
)
def fund_analytics(
    investor_id: str,
    as_of: date | None = None,
    advised_only: bool = False,
    connection: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Everything the Mutual fund analytics tabs show, over the funds held on ``as_of``.

    Money is summed in Decimal here; the browser formats it. Risk figures are the CURRENT
    MIX's (app.analytics.fund_portfolio): today's funds at today's weights through past
    months, labelled as such and never presented as the investor's own return.
    """
    _require_valuation(settings)
    on = as_of or date.today()
    from app.prices.routing import Route

    book = _fund_book(connection, investor_id, advised_only, on)
    held = [h for h in book.holdings if h.quantity > 0]
    routes = ref.valuation_routes(connection, [h.isin for h in held])
    meta = securities.lookup(connection, [h.isin for h in held])

    names: dict[str, str] = {}
    values: dict[str, Decimal] = {}
    rows, positions, unvalued = [], [], []
    histories: dict[str, list[tuple[date, Decimal]]] = {}
    for h in held:
        d = routes.get(h.isin)
        names[h.isin] = ((d.scheme_name if d else None)
                         or securities.display_name(meta, h.isin) or h.isin)
        quote = book.current.quotes.get(h.isin) if h.isin in book.prices else None
        if quote is not None:
            values[h.isin] = h.quantity * quote.price
        else:
            unvalued.append({"isin": h.isin, "name": names[h.isin],
                             "reason": book.current.unpriced_reasons.get(h.isin)
                             or "Left out until its history reconciles"})
        if d is not None and d.route is Route.AMFI:
            histories[h.isin] = ref.nav_history(connection, h.isin, date(on.year - 6, 1, 1), on)
    total = sum(values.values(), Decimal(0))

    for h in held:
        d = routes.get(h.isin)
        asset_class, category = _split_category(d)
        value = values.get(h.isin)
        idcw = _is_idcw(d.option if d else None)
        history = histories.get(h.isin, [])
        one = fund_nav.trailing_return(history, on, 1) if history and not idcw else None
        three = fund_nav.trailing_return(history, on, 3) if history and not idcw else None
        reason = ("Withheld for an IDCW option" if idcw else
                  None if three and three.annualised is not None else
                  (three.reason if three else "No NAV history"))
        rows.append({
            "isin": h.isin, "name": names[h.isin],
            "amc": d.amc_name if d else None, "category": category, "asset_class": asset_class,
            "plan": fund_checks.plan_kind(d.plan if d else None),
            "option": d.option if d else None,
            "units": money(h.quantity),
            "nav": str(book.prices[h.isin]) if h.isin in values else None,
            "nav_date": book.current.quotes[h.isin].price_date if h.isin in values else None,
            "invested": money(h.invested),
            "value": money(value),
            "unrealised": money(value - h.invested) if value is not None else None,
            "unrealised_pct": rate((value - h.invested) / h.invested)
            if value is not None and h.invested > 0 else None,
            "weight": rate(value / total) if value is not None and total > 0 else None,
            "fund_return_1y": _ratio(one.cumulative) if one else None,
            "fund_return_3y": _ratio(three.annualised) if three else None,
            "fund_return_reason": reason,
        })
        positions.append(fund_checks.FundPosition(
            h.isin, names[h.isin], d.amc_name if d else None, category,
            d.plan if d else None, value,
        ))
    rows.sort(key=lambda r: -(values.get(r["isin"]) or Decimal(-1)))

    valued = [(r, values[r["isin"]]) for r in rows if r["isin"] in values]
    plan_split = _groups([(r["plan"], v) for r, v in valued], total)
    asset_classes = _groups([(r["asset_class"] or "Not classified", v) for r, v in valued], total)
    fund_houses = _groups([(r["amc"] or "Fund house not stated", v) for r, v in valued], total)
    categories = _groups([(r["category"] or "Category not stated", v) for r, v in valued], total)

    # Risk & return: IDCW NAVs are not total return; unvalued funds have no weight.
    weights = {i: v for i, v in values.items()}
    ruled_out = {r["isin"]: "IDCW option: its NAV is not a total-return series"
                 for r in rows if r["isin"] in weights and _is_idcw(r["option"])}
    windows = [
        _fund_window(connection, y, on, histories, weights, names, ruled_out)
        for y in fund_portfolio.WINDOW_YEARS
    ]

    # Performance: each fund's rupee gain since its first purchase, exited funds included.
    contributions = []
    contribution_skipped: list[Any] = []
    if book.transactions:
        first = min(t.txn_date for t in book.transactions)
        # Transfers (CAS movements) flow at the NAV on their date (ENGINE_SPEC s.10,
        # amendment 2026-10-30); a fund with no NAV there is left out of this table, named.
        contribution_tx, transfer_prices, contribution_skipped = exclusions.price_transfers(
            connection, book.transactions, first - timedelta(days=1), on, []
        )
        gone = {t.isin for t in book.transactions} - set(names)
        gone_meta = securities.lookup(connection, list(gone)) if gone else {}
        gone_routes = ref.valuation_routes(connection, list(gone)) if gone else {}
        for c in compute_contribution(contribution_tx, {}, book.prices,
                                      first - timedelta(days=1), on, transfer_prices):
            name = names.get(c.isin) or (
                gone_routes[c.isin].scheme_name if c.isin in gone_routes else None
            ) or securities.display_name(gone_meta, c.isin) or c.isin
            contributions.append({"isin": c.isin, "name": name, "gain": money(c.gain),
                                  "share_of_total": rate(c.pct_of_total),
                                  "held": c.isin in names})
        contributions.sort(key=lambda c: -Decimal(c["gain"]))

    # Diagnostics.
    reinvest = {(t.isin, t.txn_date) for t in book.transactions if t.txn_type is TxnType.DIVIDEND}
    purchases: dict[str, list[date]] = {}
    for t in book.transactions:
        if t.txn_type is TxnType.BUY and (t.isin, t.txn_date) not in reinvest:
            purchases.setdefault(t.isin, []).append(t.txn_date)
    locked = []
    for lot in compute_lots(book.transactions, on):
        d = routes.get(lot.isin)
        if lot.isin in names and d is not None and _is_elss(d.category):
            unlock = _unlock_date(lot.open_date)
            if unlock > on:
                price = book.prices.get(lot.isin)
                locked.append(fund_checks.LockedLot(
                    lot.isin, names[lot.isin], lot.quantity_open,
                    lot.quantity_open * price if price is not None else None, unlock,
                ))
    checks = fund_checks.run_checks(
        positions, purchases, locked, {u["name"]: u["reason"] for u in unvalued}, on
    )

    xirr = compute_xirr(book.transactions, book.prices, on) if book.transactions else None
    xirr_reason = None
    if not book.transactions:
        xirr_reason = "No fund transactions that can be valued"
    elif xirr is None:
        xirr_reason = "XIRR is undefined for these cash flows (too few dates or no sign change)"
    snap = book.snapshot
    return {
        "as_of": on,
        "market_value": money(snap.market_value),
        "invested": money(snap.invested),
        "unrealised": money(snap.unrealised),
        "realised_to_date": money(snap.realised_to_date),
        "xirr": rate(xirr),
        "xirr_reason": xirr_reason,
        "plan_split": plan_split,
        "asset_classes": asset_classes,
        "fund_houses": fund_houses,
        "categories": categories,
        "funds": rows,
        "unvalued": unvalued,
        "windows": windows,
        "contributions": contributions,
        "checks": [{"key": c.key, "title": c.title, "status": c.status, "summary": c.summary,
                    "rule": c.rule, "items": c.items} for c in checks],
        "rule_version": fund_checks.RULE_VERSION,
        "excluded": exclusions.describe(connection, book.skipped + contribution_skipped),
    }


@router.get(
    "/investors/{investor_id}/tax-profile",
    response_model=TaxProfileOut,
    dependencies=[Depends(require_owned_investor)],
)
def get_tax_profile(investor_id: str, connection: Any = Depends(get_conn)) -> dict[str, Any]:
    profile = fund_tax.taxpayer_profile(connection, investor_id)
    if profile is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No taxpayer type/residency recorded; fund tax classification needs it",
        )
    return _profile_out(profile.taxpayer_type, profile.residency)


@router.put(
    "/investors/{investor_id}/tax-profile",
    response_model=TaxProfileOut,
    dependencies=[Depends(require_owned_investor)],
)
def put_tax_profile(
    investor_id: str, body: TaxProfileIn, connection: Any = Depends(get_conn)
) -> dict[str, Any]:
    fund_tax.set_taxpayer_profile(connection, investor_id, body.taxpayer_type, body.residency)
    connection.commit()
    return _profile_out(body.taxpayer_type, body.residency)


def _profile_out(taxpayer_type: str, residency: str) -> dict[str, Any]:
    return {
        "taxpayer_type": taxpayer_type,
        "residency": residency,
        "supported_for_fund_tax": residency == "RESIDENT"
        and taxpayer_type in ("INDIVIDUAL", "HUF"),
    }


# --- operations ----------------------------------------------------------------------------


def _run_daily(connection: Any, settings: Settings, allow_catalogue_drop: bool) -> dict[str, Any]:
    if not settings.mf_reference_enabled:
        return {"skipped": "MF_REFERENCE_ENABLED is false on this deployment"}
    if not settings.amfi_fetch_enabled:
        return {
            "skipped": "AMFI_FETCH_ENABLED is false on this host; run the reference worker "
            "on a host that can reach AMFI"
        }
    began = time.monotonic()
    daily = amfi_jobs.refresh_daily(
        connection,
        timeout_seconds=settings.amfi_http_timeout_seconds,
        allow_catalogue_drop=allow_catalogue_drop,
    )
    out: dict[str, Any] = {"daily": daily.as_dict()}
    remaining = settings.amfi_job_budget_seconds - (time.monotonic() - began)
    if daily.status in ("PUBLISHED", "QUARANTINED") and remaining > 10:
        snapshots = series.catch_up_fund_snapshots(
            connection, today=date.today(), time_budget_seconds=remaining - 5
        )
        out["fund_snapshots"] = snapshots.__dict__
    if daily.status == "FAILED":
        # A failed fetch is a failed job: the cron's own status shows it, not only a log.
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, out)
    return out


@router.get("/ops/cron/mf-nav-refresh", dependencies=[Depends(require_cron_secret)])
def mf_nav_refresh_cron(
    connection: Any = Depends(get_conn), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Daily AMFI NAV refresh (plan section 7.2): fetch NAVAll.txt once, validate,
    publish relevant NAVs and the catalogue, repair, schedule history, then bring the
    fund series forward within the job budget. Independent of every exchange job."""
    return _run_daily(connection, settings, allow_catalogue_drop=False)


@router.post("/ops/funds/nav-refresh", dependencies=[Depends(require_ops_key)])
def mf_nav_refresh(
    allow_catalogue_drop: bool = False,
    connection: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """The same refresh, operator-triggered. ``allow_catalogue_drop`` is the reasoned
    override for a daily file more than 20% smaller than the last published one."""
    return _run_daily(connection, settings, allow_catalogue_drop=allow_catalogue_drop)


@router.post("/ops/funds/snapshots/catch-up", dependencies=[Depends(require_ops_key)])
def fund_snapshot_catch_up(
    connection: Any = Depends(get_conn), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    _require_valuation(settings)
    return series.catch_up_fund_snapshots(
        connection, today=date.today(), time_budget_seconds=settings.amfi_job_budget_seconds
    ).__dict__


@router.get("/ops/funds/status", dependencies=[Depends(require_ops_key)])
def fund_status(connection: Any = Depends(get_conn)) -> dict[str, Any]:
    """Health measured by held coverage and finalised dates, not HTTP success (plan
    section 10)."""
    if not ref.fund_tables_present(connection):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "migration 081_fund_reference.sql is not applied"
        )
    with connection.cursor() as cur:
        cur.execute(
            """SELECT id, layout, status, reason, rows_parsed, rows_rejected, conflicts,
                      navs_published, navs_revised, bytes, elapsed_ms, fetched_at
               FROM amfi_batch ORDER BY fetched_at DESC LIMIT 10"""
        )
        cols = [c.name for c in cur.description]
        batches = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
        cur.execute(
            "SELECT max(fetched_at) FROM amfi_batch WHERE layout='DAILY' AND status='PUBLISHED'"
        )
        last_daily = cur.fetchone()[0]
        cur.execute(
            """SELECT kind, state, count(*), min(available_at), max(attempts)
               FROM reference_sync_task WHERE kind = ANY(%s) GROUP BY kind, state""",
            (list(amfi_jobs.AMFI_TASK_KINDS),),
        )
        queue = [
            {"kind": k, "state": s, "count": n, "oldest_due": due, "max_attempts": a}
            for k, s, n, due, a in cur.fetchall()
        ]
        cur.execute("SELECT count(*), min(marked_at) FROM fund_snapshot_dirty")
        dirty, oldest_dirty = cur.fetchone()
        cur.execute("SELECT count(*) FROM fund_snapshot WHERE NOT final")
        provisional = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM amfi_amc")
        amcs = cur.fetchone()[0]
    held, stale = amfi_jobs.held_freshness(connection, date.today())
    return {
        "last_published_daily": last_daily,
        "batches": batches,
        "held_funds": held,
        "stale_or_missing": stale,
        "queue": queue,
        "dirty_snapshot_dates": dirty,
        "oldest_dirty_marked_at": oldest_dirty,
        "provisional_snapshots": provisional,
        "amc_ids_known": amcs,
    }


@router.get("/ops/amfi/probe", dependencies=[Depends(require_ops_key)])
def amfi_probe(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """Can THIS host reach AMFI, and does a full parse fit? (plan section 7.1, P0).

    Fixed URLs only -- never a caller-supplied one. Fetches the daily file and a three-day
    single-AMC history window, parses both fully, and writes nothing.
    """
    out: dict[str, Any] = {}
    today = date.today()
    probes = {
        "daily": amfi.DAILY_URL,
        # mf=21 answered as Nippon India Mutual Fund on 13-Oct-2026.
        "history": amfi.history_url(21, today - timedelta(days=4), today - timedelta(days=2)),
    }
    for name, url in probes.items():
        began = time.monotonic()
        try:
            body = amfi.fetch(url, settings.amfi_http_timeout_seconds)
            parsed = amfi.parse(body, today)
            out[name] = {
                "ok": True,
                "bytes": len(body),
                "elapsed_ms": int((time.monotonic() - began) * 1000),
                "layout": str(parsed.layout),
                "rows": parsed.rows,
                "schemes": len(parsed.schemes),
                "navs": len(parsed.navs),
                "rejects": len(parsed.rejects),
                "amcs": sorted(parsed.amc_names)[:5],
                "sha256": parsed.sha256,
            }
        except (amfi.AmfiFetchError, amfi.AmfiFormatError) as exc:
            out[name] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_ms": int((time.monotonic() - began) * 1000),
            }
    return out


@router.post("/ops/funds/tax-profile", dependencies=[Depends(require_ops_key)])
def record_fund_tax_profile(
    body: FundTaxProfileIn, connection: Any = Depends(get_conn)
) -> dict[str, Any]:
    """Record evidenced tax facts for one fund over an interval (plan section 5.1).
    Evidence is mandatory; an overlapping interval with different facts is refused."""
    try:
        version = fund_tax.record_profile(
            connection,
            body.isin,
            body.valid_from,
            body.valid_to,
            FundTaxFacts(
                equity_oriented=body.equity_oriented,
                specified_fund=body.specified_fund,
                listed=body.listed,
                domiciled_in_india=body.domiciled_in_india,
            ),
            body.evidence_url,
            body.evidence_note,
            body.evidence_sha256,
        )
    except fund_tax.ConflictingProfile as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    connection.commit()
    return {"isin": body.isin, "valid_from": body.valid_from, "profile_version": version}
