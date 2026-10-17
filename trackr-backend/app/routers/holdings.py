"""GET /investors/{id}/holdings, GET /investors/{id}/summary.

The shaping logic (``_portfolio``, ``_row``, ``_totals``, ``_unpriced``, ``_allocation``)
is moved verbatim from ``api/main.py`` -- see docs/BACKEND_REUSE_AND_GAP_ANALYSIS.md
section 1. There is no calculation here and there must never be one: a number computed
in this file could not be reproduced by replaying the ledger, which is CLAUDE.md's one
architectural rule.

``_sector_allocation`` groups the same valued holdings by ``security.sector`` instead of
``asset_class``, sharing ``_group_holdings``'s weight_pct arithmetic (ENGINE_SPEC section
2) so the two groupings can never drift apart mathematically. ``_market_cap_allocation``
is the same mechanism again, keyed by ``security.market_cap_bucket``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app import securities
from app.auth.dependencies import require_owned_investor
from app.database import get_conn, money, rate
from app.db.fund_reference import latest_fund_navs
from app.db.inferred_cash_flows import confirmed_by, record_confirmation
from app.db.prices import latest_prices
from app.db.repository import load_transactions
from app.engine.holdings import compute_holdings_partitioned
from app.engine.types import ZERO, Holding, Transaction, TxnType
from app.engine.valuation import value_holdings
from app.prices.current import SOURCE_NAV, resolve_current_prices
from app.prices.live import ist_now
from app.schemas.holdings import HoldingsResponse, SummaryResponse

#: What an unclassified sector group is labelled. security.sector is NULL for any ISIN
#: NSE's classification source (app.prices.sector_sources) does not cover -- shown as
#: its own honest group, never merged into a real sector or silently dropped.
UNCLASSIFIED_SECTOR_LABEL = "Unclassified"

#: What an unclassified market-cap group is labelled. security.market_cap_bucket is NULL
#: for any ISIN AMFI's classification (app.prices.market_cap_sources) does not cover --
#: shown as its own honest group, never merged into a real bucket or silently dropped.
UNCLASSIFIED_MARKET_CAP_LABEL = "Unclassified"

router = APIRouter(prefix="/investors", tags=["derived"])


def _as_of(value: date | None) -> date:
    """Defaults to today. The parameter exists because the engine takes one and
    historical replay is the same call with an earlier date."""
    return value or date.today()


def value_partition(
    holdings: list[Holding],
    prices: dict[str, Decimal],
    as_of: date,
    previous_closes: dict[str, Decimal] | None = None,
) -> tuple[list[Holding], list[Holding]]:
    """Value the holdings that have a price; return the rest, unvalued, by name.

    The one place the dashboard's "priced vs unpriced" split lives, shared by ``/summary``
    and the clients overview (``app.routers.investors.overview``) so the two always agree.
    An unpriced holding is never valued at cost or zero -- it is excluded from totals.
    """
    priced = [h for h in holdings if h.isin in prices]
    unpriced = [h for h in holdings if h.isin not in prices]
    valued = (
        value_holdings(priced, prices, as_of=as_of, previous_closes=previous_closes or {})
        if priced
        else []
    )
    return valued, unpriced


def _portfolio(
    connection: Any, investor_id: str, as_of: date, advised_only: bool = False
) -> dict[str, Any]:
    """Holdings, valued where a price exists, with the unpriced ones kept separate.

    THE PARTITION IS THE POINT. ``value_holdings`` is all-or-nothing: one unpriced ISIN
    raises ``MissingPriceError`` naming every one of them, because CLAUDE.md rule 4
    forbids substituting a number for a price nobody has. That is right for the engine
    and wrong for a dashboard, which would go blank over one delisted scrip.

    So the split happens here instead. Priced holdings are valued normally. Unpriced ones
    are returned by name with their invested cost and are EXCLUDED from every total --
    they are not valued at their cost, which would silently report a P&L of zero on them.

    weight_pct therefore sums to 1 across the priced holdings, not across everything held.

    ``advised_only`` restricts the ledger to accounts flagged ``is_advised`` (docs/
    DATA_MODEL.md, contracts/openapi.yaml) -- so a SEBI-registered adviser can report on
    the mandated account only.
    """
    transactions = load_transactions(connection, investor_id=investor_id,
                                     advised_only=advised_only, date_to=as_of)
    holdings, skipped = compute_holdings_partitioned(transactions, as_of=as_of)

    isins = [h.isin for h in holdings]
    # One price selection shared with /summary's siblings, /performance and the
    # provisional snapshot (app.prices.current). Precedence and labels live there.
    current = resolve_current_prices(connection, isins, as_of)
    prices = current.prices
    live_isins = current.live_isins

    # The previous close, for each holding's day move. Walked back from the day BEFORE
    # as_of (``latest_prices`` handles a preceding weekend or holiday by returning the
    # last real trading day's close), so this is "the last close before the one shown"
    # whether the current price is a live intraday quote or today's settled bhavcopy.
    has_priced = any(h.isin in prices for h in holdings)
    # A fund's previous value is its previous NAV, never an exchange close: a listed fund
    # unit can have both, and mixing them would make a day move out of two sources.
    nav_quotes = {i: q for i, q in current.quotes.items() if q.source == SOURCE_NAV}
    previous_closes = (
        latest_prices(
            connection, [i for i in isins if i not in nav_quotes], as_of - timedelta(days=1)
        )
        if has_priced
        else {}
    )
    for isin, quote in nav_quotes.items():
        before = latest_fund_navs(connection, [isin], quote.price_date - timedelta(days=1))
        if isin in before:
            previous_closes[isin] = before[isin].nav
    valued, unpriced = value_partition(holdings, prices, as_of, previous_closes)
    meta = securities.lookup(connection, [h.isin for h in holdings] + [s.isin for s in skipped])

    return {
        "as_of": as_of,
        "valued": valued,
        "unpriced": unpriced,
        "skipped": skipped,
        "meta": meta,
        "invested": sum((h.invested for h in valued), ZERO),
        "market_value": sum((h.market_value for h in valued), ZERO),
        "live_isins": live_isins,
        "live_as_of": ist_now().isoformat() if live_isins else None,
        "quotes": current.quotes,
        "unpriced_reasons": current.unpriced_reasons,
    }


def _row(book: dict[str, Any], holding: Holding) -> dict[str, Any]:
    """One holding as JSON, with its security metadata joined on."""
    described = securities.describe(book["meta"], holding.isin)
    quote = book.get("quotes", {}).get(holding.isin)
    return {
        "isin": holding.isin,
        "symbol": described["symbol"],
        "name": securities.display_name(book["meta"], holding.isin),
        "quantity": money(holding.quantity),
        "avg_cost": money(holding.avg_cost),
        "invested": money(holding.invested),
        "last_price": money(holding.last_price),
        "market_value": money(holding.market_value),
        "unrealised": money(holding.unrealised),
        "unrealised_pct": rate(holding.unrealised_pct),
        "weight_pct": rate(holding.weight_pct),
        "cagr": rate(holding.cagr),
        "day_change_pct": rate(holding.day_change_pct),
        "cost_estimated": holding.cost_estimated,
        "live": holding.isin in book.get("live_isins", set()),
        "price_date": quote.price_date.isoformat() if quote and quote.price_date else None,
        "price_source": quote.source if quote else None,
        "instrument_kind": described.get("instrument_kind"),
        "fund_category": described.get("fund_category"),
    }


def _totals(book: dict[str, Any]) -> dict[str, Any]:
    """Portfolio totals over the PRICED holdings only."""
    invested, market_value = book["invested"], book["market_value"]
    unrealised = market_value - invested
    return {
        "invested": money(invested),
        "market_value": money(market_value),
        "unrealised": money(unrealised),
        "unrealised_pct": rate(unrealised / invested) if invested != ZERO else None,
        "cost_estimated": any(h.cost_estimated for h in book["valued"]),
    }


def _unpriced(book: dict[str, Any]) -> dict[str, Any]:
    """The excluded holdings, named. "Cannot value your portfolio" is unactionable;
    "no price for KDL-SM since 6 August" is something a person can go and fix."""
    return {
        "count": len(book["unpriced"]),
        "invested": money(sum((h.invested for h in book["unpriced"]), ZERO)),
        "holdings": [
            {
                "isin": h.isin,
                "symbol": securities.describe(book["meta"], h.isin)["symbol"],
                "name": securities.display_name(book["meta"], h.isin),
                "invested": money(h.invested),
                "reason": book.get("unpriced_reasons", {}).get(h.isin),
            }
            for h in book["unpriced"]
        ],
    }


def _skipped(book: dict[str, Any]) -> list[dict[str, Any]]:
    """ISINs excluded by ``compute_holdings_partitioned`` because their own ledger
    segment didn't reconcile -- named individually, same "actionable, not a dead end"
    posture as ``_unpriced`` above, so the dashboard can point at exactly what's wrong
    with each one instead of the whole screen going blank over it."""
    return [
        {
            "isin": s.isin,
            "symbol": securities.describe(book["meta"], s.isin)["symbol"],
            "name": securities.display_name(book["meta"], s.isin),
            "message": s.message,
            "deficit": money(s.deficit) if s.deficit is not None else None,
            "as_of": s.as_of,
        }
        for s in book["skipped"]
    ]


def _group_holdings(
    book: dict[str, Any], key_fn: Callable[[Holding], str | None]
) -> list[tuple[str | None, Decimal, list[dict[str, Any]]]]:
    """Market value grouped by whatever ``key_fn`` returns for each holding -- the shared
    mechanism behind both ``_allocation`` (by asset_class) and ``_sector_allocation`` (by
    sector). ``None`` is a valid, distinct key (an unclassified group), never coerced into
    an existing group or dropped.

    Returns ``(key, group_value, children_rows)`` tuples, sorted by value descending --
    the caller shapes this into its own response DTO, since asset_class and sector groups
    carry different field names.
    """
    groups: dict[str | None, list[Holding]] = {}
    for holding in book["valued"]:
        groups.setdefault(key_fn(holding), []).append(holding)

    out = [
        (
            key,
            sum((h.market_value for h in holdings), ZERO),
            sorted(
                (_row(book, h) for h in holdings),
                key=lambda r: Decimal(r["market_value"]),
                reverse=True,
            ),
        )
        for key, holdings in groups.items()
    ]
    return sorted(out, key=lambda g: g[1], reverse=True)


def _allocation(book: dict[str, Any]) -> list[dict[str, Any]]:
    """Market value grouped by asset class, each group carrying its own holdings."""
    total = book["market_value"]
    groups = _group_holdings(
        book, lambda h: securities.describe(book["meta"], h.isin)["asset_class"]
    )
    return [
        {
            "asset_class": asset_class,
            "label": securities.label(asset_class),
            "market_value": money(group_value),
            "weight_pct": rate(group_value / total) if total != ZERO else None,
            "children": children,
        }
        for asset_class, group_value, children in groups
    ]


def _sector_allocation(book: dict[str, Any]) -> list[dict[str, Any]]:
    """Market value grouped by security.sector -- ENGINE_SPEC section 2's weight_pct
    formula, reused verbatim via ``_group_holdings``, just keyed differently.

    A holding whose sector is NULL (docs/DATA_MODEL.md -- NSE's classification source
    does not cover every ISIN) groups under its own ``sector: None`` entry rather than
    being merged into a real sector or excluded from the response.
    """
    return _classification_allocation(book, "sector")


def _market_cap_allocation(book: dict[str, Any]) -> list[dict[str, Any]]:
    """Market value grouped by security.market_cap_bucket -- ENGINE_SPEC section 2's
    weight_pct formula, reused verbatim via ``_group_holdings``, just keyed differently.

    A holding whose bucket is NULL (docs/DATA_MODEL.md -- AMFI's classification source
    does not cover every ISIN) groups under its own ``market_cap_bucket: None`` entry
    rather than being merged into a real bucket or excluded from the response.
    """
    return _classification_allocation(book, "market_cap_bucket")


def _classification_allocation(book: dict[str, Any], field: str) -> list[dict[str, Any]]:
    """Company classifications plus explicit fund groups, preserving native fields."""
    total = book["market_value"]
    groups = _group_holdings(
        book,
        lambda h: securities.allocation_label(securities.describe(book["meta"], h.isin), field),
    )
    return [
        {
            field: securities.describe(book["meta"], children[0]["isin"]).get(field),
            "label": group_label,
            "market_value": money(group_value),
            "weight_pct": rate(group_value / total) if total != ZERO else None,
            "children": children,
        }
        for group_label, group_value, children in groups
    ]


@router.get(
    "/{investor_id}/summary",
    response_model=SummaryResponse,
    dependencies=[Depends(require_owned_investor)],
)
def summary(
    investor_id: str, as_of: date | None = None, connection: Any = Depends(get_conn)
) -> dict[str, Any]:
    book = _portfolio(connection, investor_id, _as_of(as_of))
    return {
        "as_of": book["as_of"],
        **_totals(book),
        "unpriced": _unpriced(book),
        "skipped": _skipped(book),
        "allocation": _allocation(book),
        "allocation_by_sector": _sector_allocation(book),
        "allocation_by_market_cap": _market_cap_allocation(book),
        "live_as_of": book["live_as_of"],
    }


@router.get(
    "/{investor_id}/holdings",
    response_model=HoldingsResponse,
    dependencies=[Depends(require_owned_investor)],
)
def holdings(
    investor_id: str,
    as_of: date | None = None,
    asset_class: str | None = Query(default=None),
    advised_only: bool = False,
    connection: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Implements contracts/openapi.yaml's ``/investors/{id}/holdings``, plus an
    ``asset_class`` filter so the equities screen asks for equities server-side.

    ``advised_only`` restricts to accounts flagged ``is_advised`` -- per
    contracts/openapi.yaml and docs/DATA_MODEL.md's account table."""
    book = _portfolio(connection, investor_id, _as_of(as_of), advised_only=advised_only)
    rows = [
        h
        for h in book["valued"]
        if asset_class is None
        or securities.describe(book["meta"], h.isin)["asset_class"] == asset_class
    ]
    subtotal_invested = sum((h.invested for h in rows), ZERO)
    subtotal_value = sum((h.market_value for h in rows), ZERO)
    unrealised = subtotal_value - subtotal_invested
    return {
        "as_of": book["as_of"],
        "holdings": sorted(
            (_row(book, h) for h in rows),
            key=lambda r: Decimal(r["market_value"]),
            reverse=True,
        ),
        "totals": {
            "invested": money(subtotal_invested),
            "market_value": money(subtotal_value),
            "unrealised": money(unrealised),
            "unrealised_pct": (
                rate(unrealised / subtotal_invested) if subtotal_invested != ZERO else None
            ),
        },
        "unpriced": _unpriced(book),
        "skipped": _skipped(book),
        "live_as_of": book["live_as_of"],
    }


#: Source-reference prefix ``app.db.gap_resolution`` writes on every ledger row it infers
#: from public issue terms (owner-authorised rule 20, docs/ARCHITECTURE_INVARIANTS.md).
AUTO_RESOLVED_PREFIX = "AUTO_RESOLVED"


def _inferred_cash_rows(connection: Any, investor_id: str, as_of: date) -> list[Transaction]:
    """The one rule for what counts as an inferred cash flow: a BUY or IPO_ALLOT that
    AUTO_RESOLVED wrote, with a price. Shared by the list and by the confirm check, so a
    confirmation can only ever attach to a row the list actually shows."""
    transactions = load_transactions(connection, investor_id=investor_id, date_to=as_of)
    return [
        t
        for t in transactions
        if t.source_ref
        and t.source_ref.startswith(AUTO_RESOLVED_PREFIX)
        and t.txn_type in (TxnType.BUY, TxnType.IPO_ALLOT)
        and t.price > ZERO
    ]


def _flow_key(t: Transaction) -> tuple[str, date, str, str]:
    return (t.isin, t.txn_date, t.txn_type.value, t.source_ref or "")


@router.get(
    "/{investor_id}/inferred-cash-flows",
    dependencies=[Depends(require_owned_investor)],
)
def inferred_cash_flows(
    investor_id: str, as_of: date | None = None, connection: Any = Depends(get_conn)
) -> dict[str, Any]:
    """Cash flows the pipeline inferred rather than read from a contract note (audit D4).

    Each item says whether the investor has confirmed it. Confirming is a record only: it
    changes no transaction and no figure. Bonus and split rows move shares, not cash, so
    they are not listed.
    """
    resolved = _as_of(as_of)
    confirmed = confirmed_by(connection, investor_id)
    rows = _inferred_cash_rows(connection, investor_id, resolved)
    meta = securities.lookup(connection, sorted({t.isin for t in rows}))
    items = [
        {
            "isin": t.isin,
            "name": (meta.get(t.isin) or {}).get("name"),
            "symbol": (meta.get(t.isin) or {}).get("symbol"),
            "txn_type": t.txn_type.value,
            "date": t.txn_date.isoformat(),
            "quantity": money(t.quantity),
            "price": money(t.price),
            "amount": money(t.quantity * t.price),
            "source_ref": t.source_ref,
            "confirmed": _flow_key(t) in confirmed,
            # INVESTOR (their click) or CAS (a matching statement, rule 20f); None if not yet.
            "confirmed_by": confirmed.get(_flow_key(t)),
        }
        for t in rows
    ]
    return {"as_of": resolved, "items": sorted(items, key=lambda r: r["date"])}


class InferredFlowConfirmIn(BaseModel):
    isin: str
    date: date
    txn_type: str
    source_ref: str


@router.post(
    "/{investor_id}/inferred-cash-flows/confirm",
    dependencies=[Depends(require_owned_investor)],
)
def confirm_inferred_cash_flow(
    investor_id: str,
    body: InferredFlowConfirmIn,
    connection: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Record that the investor has confirmed one inferred cash flow. Confirm only.

    Refuses (404) anything the list does not show, so a confirmation cannot be filed
    against a row that was never inferred. Writes the confirmation, nothing else.
    """
    key = (body.isin, body.date, body.txn_type, body.source_ref)
    listed = {_flow_key(t) for t in _inferred_cash_rows(connection, investor_id, _as_of(None))}
    if key not in listed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no inferred cash flow matches that row")
    record_confirmation(connection, investor_id, key)
    connection.commit()
    return {"confirmed": True}
