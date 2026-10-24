"""GET /investors/{id}/performance, POST /ops/prices/refresh, POST /ops/snapshots/run.

``absolute_return`` is computed via ``compute_net_invested``/``compute_absolute_return``
(app/engine/returns.py), per the ``net_invested`` definition in ENGINE_SPEC section 5:
net lifetime cash contributed (BUY/IPO_ALLOT outflows minus SELL/BUYBACK inflows, across
the whole ledger). It is ``None`` when ``net_invested <= 0`` -- heavy net withdrawals, or
even a full exit at a profit, can drive lifetime contributions to zero or negative.

``cagr`` stays ``None`` permanently, by decision, not by omission: section 5 scopes CAGR
to per-holding only (its lump-sum assumption breaks for a portfolio's staggered dated
contributions), and there is no portfolio-level case that is both valid and not already
covered by ``xirr_all``.

``benchmark`` (section 6) is computed only when the ``benchmark`` query param is
supplied -- never by default, given the extra external-data dependency. MVP supports
``NIFTY50_TRI`` only (``app.prices.index_sources``); ``SENSEX_TRI`` is a valid contract
enum value with no reliable free, machine-fetchable source behind it yet, so it is
rejected with an explicit 400 rather than silently returning null or a guessed number --
see ``_benchmark``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from time import monotonic
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app import securities
from app.auth.dependencies import require_owned_investor
from app.auth.ops import require_cron_secret, require_ops_key
from app.config import Settings, get_settings
from app.database import get_conn, money, rate
from app.db import snapshots
from app.db.fund_reference import exchange_isins
from app.db.index_levels import get_index_levels, weekend_sessions
from app.db.prices import (
    held_isins,
    latest_prices,
    missing_prices,
)
from app.db.repository import (
    get_investor_onboarding_date,
    load_transactions,
)
from app.engine.benchmark import (
    benchmark_series,
    compute_benchmark,
    required_index_dates,
    required_series_index_dates,
)
from app.engine.contribution import compute_contribution
from app.engine.errors import MissingIndexLevelError, MissingPriceError, NegativeHoldingError
from app.engine.holdings import (
    MAX_PRICE_STALENESS_DAYS,
    compute_holdings,
    compute_holdings_partitioned,
    exclude_unreconciled,
)
from app.engine.returns import compute_absolute_return, compute_net_invested
from app.engine.snapshot import compute_snapshot
from app.engine.twrr import compute_twrr
from app.engine.types import ZERO, Snapshot, Transaction
from app.engine.xirr import (
    compute_period_xirr,
    compute_xirr,
    compute_xirr_holdings,
    financial_year_start,
    period_warning,
    years_before,
)
from app.exchange_reference import MissingExchangeReference
from app.prices.alerts import Notifier, get_notifier
from app.prices.current import resolve_current_prices
from app.prices.history import backfill_price_history, store_backfilled_days
from app.prices.index_sources import (
    TRI_INDICES,
    IndexSourceFetchFailed,
    IndexSourceFormatError,
    fetch_and_store,
)
from app.prices.pipe import NSE_BHAVCOPY_EARLIEST, backfill_days, fetch_day, trading_days
from app.prices.store import store_day
from app.routers import exclusions
from app.schemas.analytical import (
    BenchmarkSeriesResponse,
    ContributionResponse,
    PerformanceResponse,
    PriceBackfillResponse,
    PriceRefreshResponse,
    SnapshotOut,
    SnapshotRunResponse,
)

#: The only benchmark names the contract declares (contracts/openapi.yaml). Kept as a
#: literal here so FastAPI 422s on anything else before ``performance`` even runs --
#: SENSEX_TRI still reaches the handler (it IS a valid enum member), where it gets a
#: deliberate 400 rather than the generic validation error.
BenchmarkName = Literal["NIFTY50_TRI", "NIFTY500_TRI", "SENSEX_TRI"]

#: What the dashboard's benchmark line plots unless a caller says otherwise. NIFTY 500
#: rather than NIFTY 50: a retail equity portfolio is rarely large-cap-only, and holding
#: a mid- or small-cap book against a 50-stock index reads as skill (or failure) that is
#: really just a difference in universe. NIFTY 50 remains the default for section 6's
#: single-figure XIRR comparison, which is a different question and a different endpoint.
DEFAULT_SERIES_BENCHMARK = "NIFTY500_TRI"

#: How far back the daily index refresh re-fetches. The TRI endpoint answers a range in
#: one call for the same cost as a single day, so a trailing window closes the gap left
#: by a missed or failed run without needing a separate backfill job. Two weeks covers a
#: long exchange holiday plus a few consecutive failures.
INDEX_REFRESH_LOOKBACK_DAYS = 14

logger = logging.getLogger("app")

router = APIRouter(tags=["derived", "ops"])


def _window_xirr(
    request: Request,
    connection: Any,
    investor_id: str,
    transactions: list[Transaction],
    prices: dict[str, Decimal],
    label: str,
    target_start: date,
    earliest_txn: date | None,
    as_of: date,
    advised_only: bool,
) -> tuple[Decimal | None, date | None]:
    """Shared opening-value resolution for every trailing/period XIRR window --
    ``fy_xirr``, ``xirr_1y``, ``xirr_3y``, ``xirr_5y``, and the adviser-onboarding
    ``period_xirr`` all call this with a different ``target_start``. Generalizes what
    used to be two near-identical private helpers (``_fy_xirr``/``_period_xirr``).

    Returns ``(xirr, actual_start_used)``. ``actual_start_used`` is the date the opening
    value actually came from -- almost always ``target_start`` itself, but occasionally
    a few days earlier when a persisted snapshot stood in for it (see step 2). Callers
    that disclose their window's start date (``period_xirr``'s ``period_start``) should
    use it; callers that don't (``fy_xirr`` and the three new fixed-calendar windows,
    matching ``fy_xirr``'s existing precedent of never disclosing its own 1-April anchor)
    simply discard it.

    Step 1 -- history guard. If the investor's earliest transaction postdates
    ``target_start`` (or there are no transactions at all), the window predates the
    portfolio's own history: ``compute_holdings`` at ``target_start`` would legitimately
    be empty, ``opening_value`` would legitimately be 0, and ``compute_period_xirr``
    would then fold in *every* transaction the investor has ever made -- numerically
    ``xirr_all``, silently mislabeled with this window's name. Blank the window instead
    of computing a number under the wrong label -- the same "never guess a start date"
    discipline this function's opening-value resolution applies below, just applied to
    whether the window applies at all.

    Step 2 -- a persisted snapshot on or before ``target_start``
    (``app.db.snapshots.get_snapshot_on_or_before``, Phase 2 Session 1's table) is the
    fast path, skipping live recomputation entirely. Its own ``as_of`` -- not
    ``target_start`` -- becomes ``compute_period_xirr``'s ``start_date``: using
    ``target_start`` as the label while the value actually came from a few days earlier
    would silently drop every transaction dated in that gap from both the opening value
    and the flow list, the same failure class ``compute_period_xirr``'s docstring
    documents for mid-period deposits (inflates on a BUY in the gap, deflates on a
    SELL), just scoped to the gap instead of the whole period. A snapshot older than
    ``MAX_PRICE_STALENESS_DAYS`` (``app.engine.holdings`` -- the codebase's existing
    answer to "how old can a substituted value be and still count as current," reused
    here rather than inventing a new tolerance) is treated as a miss: a sparse backfill
    gap should fall through to a precise live value, not silently widen the window.

    Step 3 -- on a miss, the live fallback this function has always used: build holdings
    and prices AT ``target_start``, ``compute_snapshot`` there. ``MissingPriceError`` is
    caught HERE ONLY, never re-raised, so the rest of the ``/performance`` response
    stays valid -- logged with structured context, turned into ``(None, None)``.
    """
    if earliest_txn is None or earliest_txn > target_start:
        return None, None

    persisted = snapshots.get_snapshot_on_or_before(
        connection, investor_id, target_start, advised_only
    )
    if persisted is not None and (target_start - persisted.as_of).days <= MAX_PRICE_STALENESS_DAYS:
        actual_start = persisted.as_of
        opening_value = persisted.market_value
    else:
        window_isins = [h.isin for h in compute_holdings(transactions, target_start)]
        window_prices = latest_prices(connection, window_isins, target_start)
        try:
            opening_snapshot = compute_snapshot(transactions, window_prices, target_start)
        except MissingPriceError as exc:
            logger.warning(
                "%s omitted: no usable price on/before %s for %s (investor_id=%s request_id=%s)",
                label,
                target_start,
                exc.isins,
                investor_id,
                getattr(getattr(request, "state", None), "request_id", None),
                exc_info=True,
            )
            return None, None
        actual_start = target_start
        opening_value = opening_snapshot.market_value

    xirr = compute_period_xirr(transactions, prices, actual_start, opening_value, as_of)
    return xirr, actual_start


def _window_twrr(
    connection: Any,
    investor_id: str,
    transactions: list[Transaction],
    target_start: date,
    earliest_txn: date | None,
    as_of: date,
    advised_only: bool,
    closing_snapshot: Snapshot,
) -> Decimal | None:
    """Shared snapshot-chain resolution for every TWRR window -- ``fy_twrr``,
    ``twrr_1y``, ``twrr_3y``, ``twrr_5y`` all call this with a different
    ``target_start``. Mirrors ``_window_xirr``'s opening-value resolution, but TWRR
    needs the FULL sub-period snapshot series between the window's start and ``as_of``,
    not just the two endpoints -- ``app.engine.twrr.compute_twrr`` chains between
    consecutive rows (docs/ENGINE_SPEC.md section 5).

    Step 1 -- the same history guard ``_window_xirr`` uses: a window that predates the
    investor's earliest transaction blanks to ``None`` rather than silently computing a
    chain over an empty portfolio.

    Step 2 -- the anchor snapshot: the nearest persisted row on or before
    ``target_start`` (``get_snapshot_on_or_before``), same ``MAX_PRICE_STALENESS_DAYS``
    tolerance ``_window_xirr``'s opening-value resolution uses, for the same reason -- a
    sparse backfill gap should blank the window rather than silently widen it. Unlike
    ``_window_xirr``, there is no live fallback here: a missing anchor means there is
    nothing to chain FROM, and a single live ``compute_snapshot`` at ``target_start``
    would be one point, not a series.

    Step 3 -- the chain: every persisted row from the anchor's own ``as_of`` through
    ``as_of`` (``list_snapshots_range``). If the series doesn't already reach exactly
    ``as_of`` (the common case -- today's daily snapshot job hasn't run yet), the
    caller's already-computed LIVE ``closing_snapshot`` is appended, so TWRR stays "as
    of today" like every other figure on this response rather than silently trailing by
    however many days the backfill lags. ``compute_twrr`` consumes whatever consecutive
    rows result, chaining across any interior gap as one wider sub-period rather than
    filling or interpolating it (see that function's own docstring).
    """
    if earliest_txn is None or earliest_txn > target_start:
        return None

    anchor = snapshots.get_snapshot_on_or_before(
        connection, investor_id, target_start, advised_only
    )
    if anchor is None or (target_start - anchor.as_of).days > MAX_PRICE_STALENESS_DAYS:
        return None

    chain = snapshots.list_snapshots_range(
        connection, investor_id, advised_only, anchor.as_of, as_of
    )
    if not chain or chain[-1].as_of != as_of:
        chain = [*chain, closing_snapshot]

    return compute_twrr(chain, transactions)


def _reject_unsourced_benchmark(name: str) -> None:
    """400 for a contract enum member with no data source behind it.

    ``SENSEX_TRI`` is a valid contract enum member with no reliable free, machine-
    fetchable source (BSE's own archive is a JS-only page, Asia Index has no documented
    API, third-party aggregators are unfit as a system-of-record source). Rejected
    explicitly, BEFORE any database access, rather than guessing at a number or silently
    returning null -- a caller asking for a benchmark that does not exist yet needs to be
    told plainly, unlike one whose data genuinely has a gap.
    """
    if name not in TRI_INDICES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{name} is not yet available: no reliable public TRI data source exists",
        )


def _index_levels(
    connection: Any,
    name: str,
    dates_needed: list[date],
    investor_id: str,
    request_id: str | None,
) -> dict[date, Decimal] | None:
    """Levels for ``dates_needed``, fetching once on a cache miss -- or ``None`` when the
    external source is unreachable or has changed shape.

    On a cache miss, one live ``fetch_and_store`` covers the whole date range in a single
    call (the source answers a range, not one day at a time), then the same read is
    retried. A fetch failure (``IndexSourceFetchFailed``/``IndexSourceFormatError``) is
    caught HERE, logged with structured context, and turned into ``None`` so the caller
    can omit an optional field rather than 500 the whole request -- exactly like
    ``_window_xirr`` catches ``MissingPriceError``.
    """
    if not dates_needed:
        return {}
    index = TRI_INDICES[name]
    levels = get_index_levels(connection, index.storage_name, dates_needed)
    if not all(d in levels for d in dates_needed):
        try:
            fetch_and_store(connection, min(dates_needed), max(dates_needed), index=index)
        except (IndexSourceFetchFailed, IndexSourceFormatError) as exc:
            logger.warning(
                "benchmark omitted: %s fetch failed for %s (investor_id=%s request_id=%s): %s",
                name,
                dates_needed,
                investor_id,
                request_id,
                exc,
                exc_info=True,
            )
            return None
        levels = get_index_levels(connection, index.storage_name, dates_needed)
    return _carry_newest_published(levels, dates_needed)


#: The index is published with a lag of about a day. A date after the newest published
#: level takes that level, as prices do, but only for this many days: beyond that the
#: index is genuinely missing and the caller must say so.
_INDEX_CARRY_DAYS = 4


def _carry_newest_published(
    levels: dict[date, Decimal], dates_needed: list[date]
) -> dict[date, Decimal]:
    if not levels:
        return levels
    newest = max(levels)
    carried = dict(levels)
    for d in dates_needed:
        if d not in carried and newest < d and (d - newest).days <= _INDEX_CARRY_DAYS:
            carried[d] = levels[newest]
    return carried


def _benchmark(
    request: Request,
    connection: Any,
    investor_id: str,
    transactions: list[Transaction],
    name: BenchmarkName,
    as_of: date,
) -> dict[str, Any] | None:
    """``benchmark``'s value, or ``None`` -- section 6's cash-flow replication, computed
    only when a caller asked for it (the ``benchmark`` query param).

    An unsourced name 400s (``_reject_unsourced_benchmark``) and an unreachable source
    degrades to ``None`` (``_index_levels``) -- the two are deliberately different: a
    caller asking for a benchmark that does not exist yet needs telling plainly, whereas
    a transient gap should leave the rest of this response valid.

    ``MissingIndexLevelError`` -- a genuine data gap in a source that answered fine -- is
    caught HERE, exactly like ``_window_xirr`` catches ``MissingPriceError``: logged with
    structured context and turned into ``None``. An optional field's external dependency
    hiccupping must never 500 the whole request.
    """
    _reject_unsourced_benchmark(name)
    request_id = getattr(getattr(request, "state", None), "request_id", None)
    dates_needed = required_index_dates(transactions, as_of)
    try:
        levels = _index_levels(connection, name, dates_needed, investor_id, request_id)
    except MissingExchangeReference as exc:
        return {"name": name, "reason_code": exc.reason_code, "warning": str(exc)}
    if levels is None:
        return None

    try:
        result = compute_benchmark(transactions, levels, as_of, name)
    except MissingIndexLevelError as exc:
        logger.warning(
            "benchmark omitted: no usable %s level for %s (investor_id=%s request_id=%s)",
            name,
            exc.dates,
            investor_id,
            request_id,
            exc_info=True,
        )
        return None

    return {
        "name": result.name,
        "xirr": rate(result.xirr),
        "terminal_value": money(result.terminal_value),
    }


@router.get(
    "/investors/{investor_id}/performance",
    response_model=PerformanceResponse,
    dependencies=[Depends(require_owned_investor)],
)
def performance(
    request: Request,
    investor_id: str,
    as_of: date | None = None,
    advised_only: bool = False,
    benchmark: BenchmarkName | None = None,
    connection: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Both XIRR variants, always together -- CLAUDE.md: reporting one alone leaves a
    choice about which number flatters the portfolio, and that choice should not exist.
    Four fixed-calendar trailing windows sit beside them -- ``fy_xirr`` (financial-year-
    to-date), ``xirr_1y``, ``xirr_3y``, ``xirr_5y`` -- each independently omittable when
    the portfolio's history doesn't reach back that far or no usable opening price
    exists; see ``_window_xirr``. ``period_xirr`` sits beside those, scoped to the
    investor's stored ``onboarding_date`` (the adviser-onboarding case) -- same
    resolution, different start date.

    ``fy_twrr``/``twrr_1y``/``twrr_3y``/``twrr_5y`` (docs/ENGINE_SPEC.md section 5) sit
    beside their xirr counterparts, same target_start dates, never shown alone: TWRR
    chains the persisted daily snapshot series to measure the adviser (strips out when
    money moved), where XIRR measures the client's actual experience -- see
    ``_window_twrr``.

    ``advised_only`` restricts the ledger to accounts flagged ``is_advised`` (docs/
    DATA_MODEL.md, contracts/openapi.yaml), so a SEBI-registered adviser can report on
    the mandated account only.

    ``benchmark`` (section 6) is ``None`` unless a caller asks for it -- see
    ``_benchmark`` for the cash-flow replication and why ``SENSEX_TRI`` 400s.
    """
    resolved_as_of = as_of or date.today()
    transactions = load_transactions(
        connection, investor_id=investor_id, advised_only=advised_only, date_to=resolved_as_of
    )
    # Owner amendment 2026-10-08: compute on everything that reconciles and NAME what
    # does not (``excluded`` below), never 409 the whole portfolio over one stock.
    cas_gaps = exclusions.missing_cas_history(
        connection, investor_id, transactions, [], resolved_as_of, advised_only
    )
    transactions, skipped = exclude_unreconciled(transactions, resolved_as_of)
    skipped.extend(g for g in cas_gaps if g.isin not in {s.isin for s in skipped})
    # Listed securities only: mutual funds have their own series (docs/MF_ETF_PLAN.md s.8).
    transactions, skipped = exclusions.listed_scope(
        connection, transactions, skipped, resolved_as_of
    )

    isins = [h.isin for h in compute_holdings(transactions, resolved_as_of)]
    # Same price selection as /holdings and /summary (audit D1): the XIRR terminal value
    # must describe the same prices the headline market value shows.
    prices = resolve_current_prices(connection, isins, resolved_as_of).prices
    transactions, isins, skipped = exclusions.drop_unpriced(
        transactions, isins, prices, resolved_as_of, skipped
    )

    snapshot = compute_snapshot(transactions, prices, resolved_as_of)
    xirr_all = compute_xirr(transactions, prices, resolved_as_of)
    xirr_holdings = compute_xirr_holdings(transactions, prices, resolved_as_of)

    # Fixed-calendar trailing windows -- FY-to-date plus 1/3/5 calendar years back.
    # years_before is real date arithmetic (Feb-29-aware), not days=365*n, which would
    # drift by a day every leap year. earliest_txn is computed once and shared across
    # all five _window_xirr calls below (including period_xirr) rather than recomputed
    # per window.
    earliest_txn = min((t.txn_date for t in transactions), default=None)
    fy_xirr, _ = _window_xirr(
        request,
        connection,
        investor_id,
        transactions,
        prices,
        "fy_xirr",
        financial_year_start(resolved_as_of),
        earliest_txn,
        resolved_as_of,
        advised_only,
    )
    xirr_1y, _ = _window_xirr(
        request,
        connection,
        investor_id,
        transactions,
        prices,
        "xirr_1y",
        years_before(resolved_as_of, 1),
        earliest_txn,
        resolved_as_of,
        advised_only,
    )
    xirr_3y, _ = _window_xirr(
        request,
        connection,
        investor_id,
        transactions,
        prices,
        "xirr_3y",
        years_before(resolved_as_of, 3),
        earliest_txn,
        resolved_as_of,
        advised_only,
    )
    xirr_5y, _ = _window_xirr(
        request,
        connection,
        investor_id,
        transactions,
        prices,
        "xirr_5y",
        years_before(resolved_as_of, 5),
        earliest_txn,
        resolved_as_of,
        advised_only,
    )

    # TWRR windows (docs/ENGINE_SPEC.md section 5) -- the adviser-skill companion to the
    # four XIRR windows above, same target_start dates, never shown alone.
    fy_twrr = _window_twrr(
        connection,
        investor_id,
        transactions,
        financial_year_start(resolved_as_of),
        earliest_txn,
        resolved_as_of,
        advised_only,
        snapshot,
    )
    twrr_1y = _window_twrr(
        connection,
        investor_id,
        transactions,
        years_before(resolved_as_of, 1),
        earliest_txn,
        resolved_as_of,
        advised_only,
        snapshot,
    )
    twrr_3y = _window_twrr(
        connection,
        investor_id,
        transactions,
        years_before(resolved_as_of, 3),
        earliest_txn,
        resolved_as_of,
        advised_only,
        snapshot,
    )
    twrr_5y = _window_twrr(
        connection,
        investor_id,
        transactions,
        years_before(resolved_as_of, 5),
        earliest_txn,
        resolved_as_of,
        advised_only,
        snapshot,
    )

    net_invested = compute_net_invested(transactions, resolved_as_of)
    absolute_return = compute_absolute_return(
        net_invested, snapshot.market_value, snapshot.realised_to_date
    )

    # period_xirr (adviser onboarding). onboarding_date is stored independently of any
    # given request's as_of (a free query param, usable for historical replay) -- unlike
    # financial_year_start(as_of), it is not guaranteed <= as_of, so that has to be
    # checked here rather than assumed. period_start and warning stay tied to
    # period_xirr's non-null-ness: no partial state where a date is known but nothing is
    # shown. period_start echoes _window_xirr's actual_start_used -- almost always
    # onboarding_date itself, occasionally a few days earlier when only a nearby
    # persisted snapshot existed, always the honest date the opening value came from.
    onboarding_date = get_investor_onboarding_date(connection, investor_id)
    period_xirr_value: Decimal | None = None
    period_start: date | None = None
    period_warn: str | None = None
    if onboarding_date is not None and onboarding_date <= resolved_as_of:
        period_xirr_value, actual_start = _window_xirr(
            request,
            connection,
            investor_id,
            transactions,
            prices,
            "period_xirr",
            onboarding_date,
            earliest_txn,
            resolved_as_of,
            advised_only,
        )
        if period_xirr_value is not None:
            period_start = actual_start
            period_warn = period_warning(period_start, resolved_as_of)

    # benchmark (section 6): computed only when asked for -- see _benchmark's docstring
    # for why SENSEX_TRI 400s here rather than degrading to null like a genuine data gap.
    benchmark_out: dict[str, Any] | None = None
    if benchmark is not None:
        benchmark_out = _benchmark(
            request, connection, investor_id, transactions, benchmark, resolved_as_of
        )

    return {
        "as_of": resolved_as_of,
        "xirr_all": rate(xirr_all),
        "xirr_holdings": rate(xirr_holdings),
        "fy_xirr": rate(fy_xirr),
        "xirr_1y": rate(xirr_1y),
        "xirr_3y": rate(xirr_3y),
        "xirr_5y": rate(xirr_5y),
        "fy_twrr": rate(fy_twrr),
        "twrr_1y": rate(twrr_1y),
        "twrr_3y": rate(twrr_3y),
        "twrr_5y": rate(twrr_5y),
        "period_xirr": rate(period_xirr_value),
        "period_start": period_start,
        "cagr": None,  # permanent, by decision -- see module docstring, section 5
        "absolute_return": rate(absolute_return),
        "invested": money(snapshot.invested),
        "market_value": money(snapshot.market_value),
        "unrealised": money(snapshot.unrealised),
        "realised_to_date": money(snapshot.realised_to_date),
        "benchmark": benchmark_out,
        "warning": period_warn,
        "excluded": exclusions.describe(connection, skipped),
    }


def _snapshot_row(snapshot: Snapshot) -> dict[str, Any]:
    """One persisted row as JSON. Pure shaping, no computation -- the same split
    ``holdings.py``'s ``_row``/``_totals`` and ``reconcile.py``'s ``_discrepancy_row``
    use, so it is testable without a database."""
    return {
        "as_of": snapshot.as_of,
        "invested": money(snapshot.invested),
        "market_value": money(snapshot.market_value),
        "unrealised": money(snapshot.unrealised),
        "realised_to_date": money(snapshot.realised_to_date),
        "excluded_isins": list(snapshot.excluded_isins),
    }


@router.get(
    "/investors/{investor_id}/snapshots",
    response_model=list[SnapshotOut],
    dependencies=[Depends(require_owned_investor)],
)
def list_snapshots(
    investor_id: str,
    start: date,
    end: date,
    advised_only: bool = False,
    connection: Any = Depends(get_conn),
) -> list[dict[str, Any]]:
    """Persisted ``portfolio_snapshot`` rows for ``[start, end]`` -- a plain read
    against ``app.db.snapshots.list_snapshots_range``, never a live
    ``compute_snapshot()`` replay across the range (docs/ENGINE_SPEC.md section 8; the
    entire reason the table exists is to make this endpoint cheap). Powers the
    Dashboard's historical-value chart (docs/trackr_plus_phase2_overview.md section 6).

    A date with no persisted row -- before backfill's earliest reachable date, or a gap
    from a run that hit ``MissingPriceError`` -- is simply absent from the returned
    array. Never filled, never interpolated: the caller renders that as an honest gap,
    the same convention ``UnpricedBanner``/``MissingPriceNotice`` use on the frontend.

    ``advised_only`` selects the same independent series ``/performance`` and
    ``/ops/snapshots/run`` use -- accounts flagged ``is_advised`` were snapshotted as a
    genuinely different ledger, not filtered after the fact.
    """
    if start > end:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "start must be on or before end")
    rows = snapshots.list_snapshots_range(connection, investor_id, advised_only, start, end)
    result = [_snapshot_row(row) for row in rows]
    # The chart is meant to be live: until today's bhavcopy settles, today's point is
    # computed on request from live quotes. It is never stored, so the settled row the
    # job writes later replaces it by simply being the only row there is.
    if rows and rows[-1].as_of >= end:
        return result
    tail = _live_snapshot_tail(connection, investor_id, advised_only, start, end)
    if tail is not None:
        result.append(_snapshot_row(tail))
    return result


@router.get(
    "/investors/{investor_id}/snapshots/status",
    dependencies=[Depends(require_owned_investor)],
)
def snapshot_status(
    investor_id: str,
    advised_only: bool = False,
    connection: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Cheap chart progress: never replay a ledger or contact a price provider."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT count(*), max(computed_at) FROM portfolio_snapshot "
            "WHERE investor_id=%s AND advised_only=%s",
            (investor_id, advised_only),
        )
        count, computed = cur.fetchone()
        cur.execute(
            """SELECT EXISTS (
                   SELECT 1 FROM reference_sync_dependency d
                   JOIN account a ON a.id=d.account_id
                   JOIN reference_sync_task t USING (kind,task_key)
                   WHERE a.investor_id=%s AND t.state <> 'done'
                     AND t.kind IN ('REPLAY','GAP','SNAPSHOT')
               ) OR EXISTS (
                   SELECT 1 FROM reference_sync_import r JOIN account a ON a.id=r.account_id
                   WHERE a.investor_id=%s
                     AND (r.scheduled_at IS NULL OR r.requested_at > r.scheduled_at)
               )""",
            (investor_id, investor_id),
        )
        pending = bool(cur.fetchone()[0])
        cur.execute(
            "SELECT DISTINCT t.kind,t.payload->'reference_missing' "
            "FROM reference_sync_task t JOIN reference_sync_dependency d USING(kind,task_key) "
            "JOIN account a ON a.id=d.account_id WHERE a.investor_id=%s "
            "AND t.kind IN ('REPLAY','GAP','SNAPSHOT') AND t.payload ? 'reference_missing'",
            (investor_id,),
        )
        missing = [{"kind": kind, "details": details} for kind, details in cur.fetchall()]
    return {
        "version": f"{count}:{computed.isoformat() if computed else ''}",
        "pending": pending,
        "points": count,
        "missing_reference": missing,
    }


def _live_snapshot_tail(
    connection: Any,
    investor_id: str,
    advised_only: bool,
    start: date,
    end: date,
    today: date | None = None,
) -> Snapshot | None:
    """Today's provisional snapshot from live quotes, or ``None``.

    ``None`` when today is not in the requested range, is not a trading day, is already
    settled, has no ledger, or any held security has no live quote. A missing quote is
    never treated as zero (CLAUDE.md rule 4): the point is simply left off, and the
    chart keeps its last settled day.
    """
    today = today or datetime.now(_IST).date()
    if not (start <= today <= end) or not trading_days(today, today):
        return None
    transactions = load_transactions(
        connection, investor_id=investor_id, advised_only=advised_only, date_to=today
    )
    transactions, _scoped_out = exclusions.listed_scope(connection, transactions, [], today)
    if not transactions:
        return None
    try:
        reconciled, _skipped = compute_holdings_partitioned(transactions, today)
    except NegativeHoldingError:
        return None
    isins = [h.isin for h in reconciled]
    # Same selection as /holdings (app.prices.current): settled close, then live quote,
    # then carried close. One unpriced security never blanks the whole point: anything
    # with no price at all is excluded and named (owner amendment 2026-10-08, invariant
    # 16), never valued at zero.
    prices = resolve_current_prices(connection, isins, today).prices
    still_missing = {isin for isin in isins if isin not in prices}
    try:
        return compute_snapshot(transactions, prices, today, exclude_isins=still_missing)
    except (MissingPriceError, NegativeHoldingError):
        return None


@router.get(
    "/investors/{investor_id}/benchmark-series",
    response_model=BenchmarkSeriesResponse,
    dependencies=[Depends(require_owned_investor)],
)
def benchmark_series_endpoint(
    request: Request,
    investor_id: str,
    start: date,
    end: date,
    name: BenchmarkName = DEFAULT_SERIES_BENCHMARK,
    advised_only: bool = False,
    connection: Any = Depends(get_conn),
) -> dict[str, Any]:
    """The benchmark line for the Dashboard's value chart -- docs/ENGINE_SPEC.md section
    6's cash-flow replication, valued at every persisted snapshot date in ``[start, end]``
    instead of only at ``as_of``.

    Points are dated to SNAPSHOT dates, not to every calendar day in the range, so each
    one lands on the same x-position as that snapshot's market value and the two lines are
    read against each other honestly. A range with no snapshots returns no points, exactly
    as ``/snapshots`` returns no rows -- the chart has nothing to draw the line beside.

    Why replication rather than "rebase the index to invested cost on day one": a rebased
    line ignores every rupee contributed after the anchor, so a portfolio that simply kept
    saving would appear to beat the index for doing nothing. Replicating the real cash
    flows buys index units on the days money actually arrived, which is the only version
    of this line that stays comparable to market value across a contribution.

    Degrades to an empty ``points`` list rather than erroring when the index source is
    unreachable or a level is genuinely missing, for the same reason ``_benchmark`` returns
    ``None``: this is a decoration on a chart whose real subject -- the portfolio's own
    value -- is already drawn from ``/snapshots``.
    """
    if start > end:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "start must be on or before end")

    _reject_unsourced_benchmark(name)

    rows = list(snapshots.list_snapshots_range(connection, investor_id, advised_only, start, end))
    # The same provisional current-day point /snapshots appends (audit D5). Without it the
    # benchmark ends a day before the portfolio line it is read against.
    if not rows or rows[-1].as_of < end:
        tail = _live_snapshot_tail(connection, investor_id, advised_only, start, end)
        if tail is not None:
            rows.append(tail)
    if not rows:
        return {"name": name, "points": []}

    transactions = load_transactions(connection, investor_id=investor_id, advised_only=advised_only)
    # The same scope as the portfolio line it is drawn against.
    transactions, _scoped_out = exclusions.listed_scope(connection, transactions, [], end)
    dates = [row.as_of for row in rows]
    dates_needed = required_series_index_dates(transactions, dates)
    request_id = getattr(getattr(request, "state", None), "request_id", None)
    levels = _index_levels(connection, name, dates_needed, investor_id, request_id)
    if levels is None:
        return {"name": name, "points": []}

    try:
        series = benchmark_series(transactions, levels, dates, name)
    except MissingIndexLevelError as exc:
        logger.warning(
            "benchmark series omitted: no usable %s level for %s (investor_id=%s request_id=%s)",
            name,
            exc.dates,
            investor_id,
            request_id,
            exc_info=True,
        )
        return {"name": name, "points": []}

    return {
        "name": name,
        "points": [{"as_of": point_date, "value": money(value)} for point_date, value in series],
    }


@router.get(
    "/investors/{investor_id}/contribution",
    response_model=ContributionResponse,
    dependencies=[Depends(require_owned_investor)],
)
def contribution(
    investor_id: str,
    period_start: date,
    as_of: date | None = None,
    advised_only: bool = False,
    connection: Any = Depends(get_conn),
    benchmark: Literal["NIFTY50_TRI", "NIFTY500_TRI"] | None = None,
    price_basis: Literal["current", "stored_close"] = "current",
) -> dict[str, Any]:
    """Per-security gain attribution over (period_start, as_of], using current prices."""
    return build_contribution(
        connection, investor_id, period_start, as_of, advised_only, benchmark,
        cached_prices_only=price_basis == "stored_close",
    )


def build_contribution(
    connection: Any,
    investor_id: str,
    period_start: date,
    as_of: date | None,
    advised_only: bool,
    benchmark: str | None = None,
    *,
    cached_prices_only: bool = False,
) -> dict[str, Any]:
    """Per-ISIN rupee attribution of the portfolio's gain over
    ``(period_start, as_of]`` -- docs/ENGINE_SPEC.md section 10, the first
    Intelligence-layer feature per ``docs/PRODUCT_VISION.md``.

    Unlike the XIRR/TWRR windows above, there is no persisted-snapshot fast path here
    (``_window_xirr``'s step 2): ``portfolio_snapshot`` carries only portfolio-level
    totals, not a per-ISIN breakdown, and this needs the latter at BOTH ``period_start``
    and ``as_of``. So both ends are always resolved live, the same way
    ``_window_xirr``'s step 3 fallback works -- ``compute_holdings`` at that date against
    prices looked up for it -- via ``compute_contribution`` itself, which raises
    ``MissingPriceError`` (caught by the global handler, 409) naming any held ISIN
    missing a price at either end, never substituting zero.

    ``period_start`` has no default the way ``fy_xirr``'s 1-April anchor does: the
    caller states the window explicitly, same as ``/snapshots``' ``start``/``end``.
    """
    resolved_as_of = as_of or date.today()
    if period_start > resolved_as_of:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "period_start must be on or before as_of")

    transactions = load_transactions(
        connection,
        investor_id=investor_id,
        advised_only=advised_only,
        date_to=resolved_as_of,
    )
    # Same exclusion as /performance (owner amendment 2026-10-08), named in ``excluded``.
    cas_gaps = exclusions.missing_cas_history(
        connection, investor_id, transactions, [], resolved_as_of, advised_only
    )
    transactions, skipped = exclude_unreconciled(transactions, resolved_as_of)
    skipped.extend(g for g in cas_gaps if g.isin not in {s.isin for s in skipped})
    transactions, skipped = exclusions.listed_scope(
        connection, transactions, skipped, resolved_as_of
    )

    start_isins = [h.isin for h in compute_holdings(transactions, period_start)]
    start_prices = latest_prices(connection, start_isins, period_start)
    transactions, start_isins, skipped = exclusions.drop_unpriced(
        transactions, start_isins, start_prices, period_start, skipped
    )
    end_isins = [h.isin for h in compute_holdings(transactions, resolved_as_of)]
    end_prices = (
        latest_prices(connection, end_isins, resolved_as_of)
        if cached_prices_only
        else resolve_current_prices(connection, end_isins, resolved_as_of).prices
    )
    transactions, end_isins, skipped = exclusions.drop_unpriced(
        transactions, end_isins, end_prices, resolved_as_of, skipped
    )

    results = compute_contribution(
        transactions, start_prices, end_prices, period_start, resolved_as_of
    )
    meta = securities.lookup(connection, [c.isin for c in results])
    total_gain = sum((c.gain for c in results), ZERO)

    performance = None
    if benchmark:
        from app.routers.analytics_performance import build_performance

        performance = build_performance(
            connection, investor_id, transactions, end_prices, resolved_as_of,
            benchmark, advised_only, {s.isin for s in skipped},
            requested_windows=[("custom", period_start)],
        )["windows"].get("custom")

    return {
        "as_of": resolved_as_of,
        "period_start": period_start,
        "total_gain": money(total_gain),
        "benchmark": benchmark,
        "performance": performance,
        "contributions": sorted(
            (
                {
                    "isin": c.isin,
                    "symbol": securities.describe(meta, c.isin)["symbol"],
                    "start_value": money(c.start_value),
                    "end_value": money(c.end_value),
                    "net_flow": money(c.net_flow),
                    "gain": money(c.gain),
                    "pct_of_total": rate(c.pct_of_total),
                }
                for c in results
            ),
            key=lambda row: Decimal(row["gain"]),
            reverse=True,
        ),
        "excluded": exclusions.describe(connection, skipped),
    }


def _refresh_index_levels(
    connection: Any, price_date: date
) -> tuple[dict[str, int], dict[str, str]]:
    """Keep every fetchable TRI series current, on the same daily beat as bhavcopy.

    Without this the only thing that ever wrote ``index_level`` was a benchmark request
    finding a cache miss and fetching inline. That works, and it is why the chart
    self-heals, but it puts an uncached external HTTP call on a dashboard load and makes
    the benchmark line's presence depend on whether niftyindices happens to be reachable
    at the moment somebody opens the page. Refreshing on a schedule means the read path
    almost always hits rows that are already there.

    ``INDEX_REFRESH_LOOKBACK_DAYS`` rather than just ``price_date`` because the endpoint
    answers a range as cheaply as a day, and a short trailing window closes any gap left
    by a missed or failed run without a separate backfill. Upserts, so re-fetching days
    already stored costs nothing but re-writing the same numbers.

    Never raises. A TRI failure is recorded and returned; bhavcopy is what the ledger is
    actually priced from, and the benchmark line going stale must not fail the job that
    keeps every valuation in the product correct.
    """
    start = price_date - timedelta(days=INDEX_REFRESH_LOOKBACK_DAYS)
    stored: dict[str, int] = {}
    failures: dict[str, str] = {}
    for name, index in TRI_INDICES.items():
        try:
            stored[name] = fetch_and_store(connection, start, price_date, index=index)
        except (IndexSourceFetchFailed, IndexSourceFormatError) as exc:
            failures[name] = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "index level refresh failed for %s over %s..%s: %s",
                name,
                start,
                price_date,
                exc,
                exc_info=True,
            )
    return stored, failures


def _refresh_prices(connection: Any, settings: Settings) -> dict[str, Any]:
    """Fetch NSE + BSE bhavcopy and upsert prices for every ISIN the ledger holds.

    Mirrors ``app.prices.pipe.main()``'s logic exactly (both exchanges mandatory, stores
    only ISINs already in the ledger, and the same ``get_notifier`` selection so a
    failure reaches a human here too). Shared by both ``POST /ops/prices/refresh``
    (``X-Ops-Key``, a human or a non-Vercel scheduler calling directly) and
    ``GET /ops/cron/prices-refresh`` (``CRON_SECRET``, Vercel Cron's own convention --
    it can only call GET). Idempotent: ``upsert_prices`` is ``ON CONFLICT DO UPDATE``,
    so a duplicate cron invocation (Vercel's own documented "best effort, can invoke
    more than once" delivery) changes nothing beyond re-storing the same values.
    """
    price_date = date.today()
    result = fetch_day(price_date, notifier=get_notifier(settings))
    stored = store_day(connection, result)
    # Today's official closes are on file: check each quote mapping against them now
    # (app.prices.quote_check), so tomorrow's indicative quotes use only proven tickers.
    try:
        from app.prices.quote_check import verify_quote_mappings

        connection.commit()
        verify_quote_mappings(connection)
    except Exception:
        connection.rollback()
        logger.warning("quote_mapping_check_failed", exc_info=True)
    # Scoped to HELD ISINs, never the ranking universe. This list is read by a human as
    # "your portfolio is unpriced"; padding it with reference securities nobody owns would
    # turn a real alarm into noise.
    # Exchange-priced holdings only: a NAV-valued fund is never in a bhavcopy, and its
    # freshness is reported by the AMFI refresh (/ops/cron/mf-nav-refresh).
    missing = missing_prices(
        connection, exchange_isins(connection, held_isins(connection)), price_date
    )
    index_stored, index_failures = _refresh_index_levels(connection, price_date)

    return {
        "prices_stored": stored.prices_stored,
        "missing_isins": missing,
        "price_date": price_date,
        "index_levels_stored": index_stored,
        "index_failures": index_failures,
        "symbol_changes": [
            {"isin": c.isin, "old_symbol": c.old_symbol, "new_symbol": c.new_symbol}
            for c in stored.symbol_changes
        ],
    }


@router.post(
    "/ops/prices/refresh",
    response_model=PriceRefreshResponse,
    dependencies=[Depends(require_ops_key)],
)
def refresh_prices(
    connection: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Gated by ``X-Ops-Key`` (``app.auth.ops``), not a user login: this mutates stored
    prices for every investor, not just one caller's own view, and per the
    pre-deployment audit (2026-08-30) any signed-up alpha user could otherwise reach it
    with nothing more than a valid session."""
    return _refresh_prices(connection, settings)


@router.get("/ops/cron/live-prices-refresh", dependencies=[Depends(require_cron_secret)])
def refresh_live_prices_cron(connection: Any = Depends(get_conn)) -> dict[str, Any]:
    """Keep the intraday yfinance cache warm for every ledger ISIN, every 15 minutes in
    NSE hours (owner request 2026-10-08), so a dashboard never waits on yfinance and
    every user sees a quote at most one cycle old. Scheduled by
    .github/workflows/live-prices.yml -- Vercel Hobby crons are daily-only. Outside
    09:00-16:00 IST on weekdays it does nothing: the daily bhavcopy is the price then.

    Same cache, same rule as the on-demand path (``app.prices.live``): consulted only for
    today, only where bhavcopy has not settled. Bhavcopy remains the price of record."""
    from app.prices.live import ist_now, refresh_and_get

    now = ist_now()
    if now.weekday() >= 5 or not (9 <= now.hour < 16):
        return {"skipped": "outside NSE hours", "at": now.isoformat()}
    # Exchange-listed only: there is no intraday quote for a NAV-valued fund.
    isins = exchange_isins(connection, held_isins(connection))
    quotes = refresh_and_get(connection, isins)
    connection.commit()
    return {"requested": len(isins), "priced": len(quotes), "at": now.isoformat()}


@router.get(
    "/ops/cron/prices-refresh",
    response_model=PriceRefreshResponse,
    dependencies=[Depends(require_cron_secret)],
)
def refresh_prices_cron(
    connection: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Same job as ``refresh_prices``, reachable by Vercel Cron specifically: GET,
    gated by ``CRON_SECRET`` (``app.auth.ops.require_cron_secret``) rather than
    ``X-Ops-Key``, since that is the only credential Vercel's scheduler sends and the
    only HTTP method it calls with. See docs/DEPLOYMENT.md."""
    return _refresh_prices(connection, settings)


#: How far back the daily F&O refresh looks for exchange-days not yet stored.
FNO_REFRESH_LOOKBACK_DAYS = 7


@router.get("/ops/cron/fno-prices-refresh", dependencies=[Depends(require_cron_secret)])
def refresh_fno_prices_cron(
    connection: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Today's NSE + BSE equity F&O bhavcopy into ``fno_contract`` / ``fno_price``
    (docs/FNO_PLAN.md Piece 2). Separate from ``prices-refresh`` so an F&O file problem
    can never block the equity prices every valuation depends on.

    A trailing ``FNO_REFRESH_LOOKBACK_DAYS`` window rather than today alone: the F&O file
    can be published after this cron fires (NSE's 2026-09-30 file is stamped 17:56 IST),
    and an exchange-day already stored is skipped, so a late or missed file is picked up
    on the next run at the cost of one request per missing day."""
    from app.db.fno_reference import backfill_expiry_settlements, backfill_fno_prices

    today = date.today()
    days = backfill_fno_prices(
        connection,
        today - timedelta(days=FNO_REFRESH_LOOKBACK_DAYS),
        today,
        notifier=get_notifier(settings),
    )
    # Plus any older expiry day a traded contract still has no price for: the 7-day
    # window above can never reach one, and its settlement price exists only there.
    expiries = backfill_expiry_settlements(
        connection, notifier=get_notifier(settings), time_budget_seconds=150.0
    )
    return {"days": days, "expiry_settlements": expiries}


@router.post(
    "/ops/prices/backfill",
    response_model=PriceBackfillResponse,
    dependencies=[Depends(require_ops_key)],
)
def backfill_prices(
    connection: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """``X-Ops-Key``-gated run of the automatic price-history backfill."""
    return backfill_price_history(connection, notifier=get_notifier(settings))


@router.get(
    "/ops/cron/prices-backfill",
    response_model=PriceBackfillResponse,
    dependencies=[Depends(require_cron_secret)],
)
def backfill_prices_cron(
    connection: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Vercel Cron counterpart. Runs often and is bounded per run, so a new user's
    imported history is priced within hours with no human step; once history is
    complete each run is one query and returns ``pending: 0``."""
    return backfill_price_history(connection, notifier=get_notifier(settings))


def _run_snapshots(connection: Any, start: date, end: date, notifier: Notifier) -> dict[str, Any]:
    """Value missing points from one prefetched, date-effective range of inputs."""
    from app.db.snapshot_batch import snapshot_batch

    written = fetched = 0
    skipped = []
    points = []
    with snapshot_batch(connection, start, end) as batch:
        sessions = weekend_sessions(connection, start, end)
        results = backfill_days(
            start, end, already_present=batch.settled, notifier=notifier, sessions=sessions
        )
        fetched = store_backfilled_days(connection, results)
        if fetched:
            batch.prices_changed()
        days = trading_days(start, end, sessions)
        batch.prefetch({scope for on in days for scope in batch.missing_scopes(on)})
        for on in days:
            for investor_id, advised_only in batch.missing_scopes(on):
                try:
                    snap = batch.build(investor_id, advised_only, on)
                except (MissingPriceError, NegativeHoldingError) as exc:
                    logger.warning(
                        "snapshot omitted: %s on/before %s (investor_id=%s advised_only=%s)",
                        exc,
                        on,
                        investor_id,
                        advised_only,
                    )
                    skipped.append(
                        {
                            "investor_id": investor_id,
                            "advised_only": advised_only,
                            "as_of": on,
                            "isins": getattr(exc, "isins", None) or [getattr(exc, "isin", None)],
                        }
                    )
                    continue
                points.append((investor_id, advised_only, snap))
                batch.saved(investor_id, advised_only, on)
                written += 1
        snapshots.upsert_snapshots(connection, points)
        # The days just filled may also be queued for the worker (a replay deleted them, or
        # a hole was seen). Close those this batch's reads already covered, so the worker
        # does not value the same days again.
        snapshots.settle_rebuild_tasks(connection, points, batch.read_snapshot)
    return {
        "snapshots_written": written,
        "price_dates_fetched": fetched,
        "skipped_missing_price": skipped,
    }


@router.post(
    "/ops/snapshots/run",
    response_model=SnapshotRunResponse,
    dependencies=[Depends(require_ops_key)],
)
def run_snapshots(
    start: date | None = None,
    end: date | None = None,
    connection: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Persist ``portfolio_snapshot`` rows via ``_run_snapshots``.

    No ``start``/``end`` -> both default to today: the cheap daily catch-up, run
    alongside ``/ops/prices/refresh``. A wider range is a resumable historical backfill,
    fetching any bhavcopy it needs first. Gated by ``X-Ops-Key`` like
    ``/ops/prices/refresh``: this writes stored totals for every investor, not just one
    caller's own view of one.
    """
    resolved_start = start or date.today()
    resolved_end = end or date.today()
    return _run_snapshots(connection, resolved_start, resolved_end, get_notifier(settings))


@router.get(
    "/ops/cron/snapshots-run",
    response_model=SnapshotRunResponse,
    dependencies=[Depends(require_cron_secret)],
)
def run_snapshots_cron(
    connection: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Vercel-Cron-reachable counterpart to ``run_snapshots``: always today only (Cron
    calls a fixed path with no query params), gated by ``CRON_SECRET`` instead of
    ``X-Ops-Key``. A wider historical backfill still goes through the POST route
    directly. See docs/DEPLOYMENT.md. ``_run_snapshots`` skips any (investor, date)
    already snapshotted, so a duplicate cron invocation is a no-op, not a re-write.

    Time-boxed under Vercel's 300 s limit: unbounded, a fresh investor's catch-up was
    killed every day before saving anything. Left incomplete after progress, it hands
    the rest to the self-continuing catch-up chain (``app.continuation``)."""
    from app.continuation import continue_if_needed

    result = catch_up_snapshots(
        connection, get_notifier(settings), time_budget_seconds=SNAPSHOT_CRON_BUDGET_SECONDS
    )
    continue_if_needed(
        settings,
        more_work=not result["complete"],
        progress=bool(result["snapshots_written"] or result["price_dates_fetched"]),
        hop=0,
    )
    return result


#: Leaves headroom inside Vercel's 300 s function limit for the day already in progress.
SNAPSHOT_CRON_BUDGET_SECONDS = 200.0


#: Indian market time. The day's bhavcopy is not reliably published before this, so a
#: snapshot taken earlier in the evening would price the day with the previous close.
_IST = ZoneInfo("Asia/Kolkata")
_BHAVCOPY_READY = time(18, 30)


def last_closed_trading_day(now: datetime | None = None) -> date:
    """The latest day whose closing prices should exist by ``now`` (IST). Before the
    bhavcopy is ready it is yesterday. Weekends and holidays are left to
    ``trading_days``, which yields nothing for them."""
    local = (now or datetime.now(_IST)).astimezone(_IST)
    return local.date() if local.time() >= _BHAVCOPY_READY else local.date() - timedelta(days=1)


SNAPSHOT_BATCH_DAYS = 32


def catch_up_snapshots(
    connection: Any,
    notifier: Notifier,
    now: datetime | None = None,
    time_budget_seconds: float | None = None,
    clock: Callable[[], float] = monotonic,
) -> dict[str, Any]:
    """Bring ``portfolio_snapshot`` up to the last closed trading day, from the day after
    the newest stored row.

    Why not just today: the daily job used to snapshot only today, so a missed cron run
    (a failed deploy, a paused worker) left a permanent hole in every investor's value
    history, and the chart then never drew a line. Catching up from the newest row closes
    any hole on the next run. A day whose bhavcopy is not yet published is retried on the
    next run automatically: it stays after the newest row until it can be priced.

    ``time_budget_seconds`` is for serverless callers, which are killed at a hard limit.
    One unbounded call for a fresh investor downloads every missing bhavcopy back to
    the ledger's first supported trading date before saving anything, so on Vercel it
    was killed at 300 s,
    saved nothing, and the next run started over. With a budget the range is worked
    oldest first. Cached dates share inputs and are committed in bounded chunks;
    unsettled source dates are fetched and committed individually. The run stops
    claiming chunks once the budget is spent and ``complete`` says whether it reached
    the target. The next run resumes after the newest stored row. Each day is computed by the
    same ``_run_snapshots`` either way, so the stored values do not depend on the budget.
    """
    target = last_closed_trading_day(now)
    _catch_up_index_levels(connection, target)
    # Interior holes are repaired separately, date by date, through the reference queue:
    # moving ``start`` back to an old hole would make every budgeted run re-walk the
    # whole range after it and never reach today.
    holes = enqueue_interior_holes(connection)
    start = _catch_up_start(connection, target)
    if start is None:
        return {
            "snapshots_written": 0,
            "price_dates_fetched": 0,
            "skipped_missing_price": [],
            "complete": True,
            "holes_queued": holes,
        }
    if time_budget_seconds is None:
        result = _run_snapshots(connection, start, target, notifier)
        connection.commit()
        return {**result, "complete": True}

    from app.db.snapshot_batch import snapshot_batch

    began = clock()

    totals = {
        "snapshots_written": 0,
        "price_dates_fetched": 0,
        "skipped_missing_price": [],
        "complete": True,
    }
    days = trading_days(start, target, weekend_sessions(connection, start, target))
    with snapshot_batch(connection, start, target) as batch:
        offset = 0
        while offset < len(days):
            if clock() - began > time_budget_seconds:
                totals["complete"] = False
                break
            # Never start a multi-day download before saving a checkpoint. Cached
            # history is valued/written in ranges; an unsettled archive date is one
            # bounded source item and is committed before attempting the next.
            stop = offset + 1
            while stop < min(len(days), offset + SNAPSHOT_BATCH_DAYS) and all(
                day in batch.settled for day in days[offset : stop + 1]
            ):
                stop += 1
            result = _run_snapshots(connection, days[offset], days[stop - 1], notifier)
            connection.commit()
            for field in ("snapshots_written", "price_dates_fetched"):
                totals[field] += result[field]
            totals["skipped_missing_price"].extend(result["skipped_missing_price"])
            offset = stop
    return totals


def _catch_up_index_levels(connection: Any, target: date) -> None:
    """Pull the published NIFTY TRI levels up to ``target``. Without this the benchmark
    line only ever gets as far as whatever a page request last happened to fetch, so it
    stops at the last request, not at the last trading day. A source failure is logged
    and skipped: the snapshots do not depend on it.

    Only when a level is actually missing (newest stored < ``target``), and at most once
    per INDEX_RETRY_INTERVAL while it stays missing (recorded in ``feed_poll_state``): this
    runs on every catch-up cycle, the browser repeats cycles every few seconds while an
    import is processing, and a weekend or holiday ``target`` has no level to find. Weekend
    special sessions are still discovered, one attempt per interval. The five-day overlap
    still re-reads recent levels for corrections each time a new day is fetched."""
    for index in TRI_INDICES.values():
        try:
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT max(level_date) FROM index_level WHERE index_name = %s",
                    (index.storage_name,),
                )
                newest = cur.fetchone()[0]
            if newest is not None and newest >= target:
                continue
            feed = f"index_level_catch_up:{index.storage_name}"
            with connection.cursor() as cur:
                cur.execute(
                    """INSERT INTO feed_poll_state (feed, last_attempt_at) VALUES (%s, now())
                       ON CONFLICT (feed) DO UPDATE SET last_attempt_at = now()
                       WHERE feed_poll_state.last_attempt_at IS NULL
                          OR feed_poll_state.last_attempt_at < now() - %s::interval
                       RETURNING feed""",
                    (feed, INDEX_RETRY_INTERVAL),
                )
                due = cur.fetchone() is not None
            connection.commit()
            if not due:
                continue
            start = (newest - timedelta(days=5)) if newest else NSE_BHAVCOPY_EARLIEST
            fetch_and_store(connection, start, target, index=index)
            with connection.cursor() as cur:
                cur.execute(
                    "UPDATE feed_poll_state SET last_success_at = now(), consecutive_failures = 0 "
                    "WHERE feed = %s",
                    (feed,),
                )
        except (IndexSourceFetchFailed, IndexSourceFormatError):
            logger.warning("index refresh skipped for %s", index.storage_name, exc_info=True)
    connection.commit()


#: While an index level stays missing, the catch-up asks the source at most this often.
INDEX_RETRY_INTERVAL = "30 minutes"

#: Interior holes queued per catch-up run; the rest wait for the next run.
HOLE_REPAIR_BATCH = 500
#: A hole that could not be priced is tried again after this long, not every run.
HOLE_RETRY_INTERVAL = "7 days"


def enqueue_interior_holes(connection: Any, limit: int = HOLE_REPAIR_BATCH) -> int:
    """Queue a SNAPSHOT rebuild for every session missing BETWEEN an investor's first and
    last stored row (``_catch_up_start`` only sees a short head or tail).

    A session is a weekday, or a weekend day that traded (an index close exists, see
    ``weekend_sessions``) AND whose closes are already stored: the repair builds from
    stored prices, so offering a Sunday before its bhavcopy lands would value it at
    Friday's closes and keep that flat day permanently.

    Each is a ``repair`` task: one that still cannot be priced is recorded on the task
    (``unrepairable``) and finished, not failed -- and offered again only after
    ``HOLE_RETRY_INTERVAL``, so a permanently unpriceable day neither loops nor turns every
    queue run into a failure.
    """
    with connection.cursor() as cur:
        cur.execute(
            """WITH b AS (
                   SELECT investor_id, advised_only, min(as_of) lo, max(as_of) hi
                   FROM portfolio_snapshot GROUP BY 1, 2
               ), d AS (
                   SELECT b.investor_id, b.advised_only, g::date AS on_
                   FROM b, generate_series(greatest(b.lo, %s), b.hi, interval '1 day') g
                   WHERE extract(isodow FROM g) < 6
                      OR (EXISTS (SELECT 1 FROM index_level l WHERE l.level_date = g::date)
                          AND EXISTS (SELECT 1 FROM price p WHERE p.price_date = g::date))
               )
               SELECT d.investor_id::text, d.on_, d.advised_only FROM d
               WHERE NOT EXISTS (
                   SELECT 1 FROM portfolio_snapshot p WHERE p.investor_id = d.investor_id
                     AND p.advised_only = d.advised_only AND p.as_of = d.on_)
               ORDER BY d.on_ DESC LIMIT %s""",
            (SNAPSHOT_HISTORY_FLOOR, limit),
        )
        holes = cur.fetchall()
        queued = 0
        for investor_id, on, advised in holes:
            cur.execute(
                """INSERT INTO reference_sync_task (kind, task_key, payload)
                   VALUES ('SNAPSHOT', %s, %s::jsonb
                           || jsonb_build_object('queued_xid', pg_current_xact_id()::text))
                   ON CONFLICT (kind, task_key) DO UPDATE SET
                       payload = EXCLUDED.payload, state = 'pending', available_at = now()
                   WHERE reference_sync_task.state = 'done'
                     AND (reference_sync_task.payload->>'unrepairable' IS NULL
                          OR reference_sync_task.updated_at < now() - %s::interval)""",
                (
                    f"{investor_id}:{on}:{advised}",
                    json.dumps(
                        {
                            "investor_id": investor_id,
                            "on": on.isoformat(),
                            "advised_only": advised,
                            "repair": True,
                        }
                    ),
                    HOLE_RETRY_INTERVAL,
                ),
            )
            queued += cur.rowcount
    connection.commit()
    return queued


#: Use the same archive boundary as price fetching. An arbitrary recent-year cutoff
#: discarded investors' older history even when its exchange prices were already stored.
SNAPSHOT_HISTORY_FLOOR = NSE_BHAVCOPY_EARLIEST


def _catch_up_start(connection: Any, target: date) -> date | None:
    """The first day the catch-up must rebuild, or ``None`` when every investor is current.

    Two reasons to rebuild, and the earlier one wins:

    * the routine day after the newest stored row, for everybody; and
    * per investor, the history back to their first trade (clamped to the earliest date
      the price archive covers). An investor whose stored snapshots start after that
      point is missing history, however recent the newest row across all investors is.
      A single global start date could never recover it: a newly imported investor with
      trades from years ago got only the days since the global newest row.

    Days before SNAPSHOT_HISTORY_FLOOR are never rebuilt, so nothing is invented for them.
    """
    with connection.cursor() as cur:
        cur.execute(
            """SELECT a.investor_id, min(t.txn_date) FROM "transaction" t
               JOIN account a ON a.id = t.account_id GROUP BY a.investor_id"""
        )
        first_trade = dict(cur.fetchall())
        cur.execute(
            "SELECT investor_id, min(as_of), max(as_of) FROM portfolio_snapshot "
            "GROUP BY investor_id"
        )
        stored = {inv: (first, last) for inv, first, last in cur.fetchall()}
        cur.execute("SELECT max(as_of) FROM portfolio_snapshot")
        latest = cur.fetchone()[0]

    starts: list[date] = [latest + timedelta(days=1) if latest else target]
    sessions = weekend_sessions(connection, SNAPSHOT_HISTORY_FLOOR, target)
    for investor_id, first in first_trade.items():
        need_from = max(first, SNAPSHOT_HISTORY_FLOOR)
        days = trading_days(need_from, target, sessions) if need_from <= target else []
        if not days:
            continue
        have_first, have_last = stored.get(investor_id, (None, None))
        if have_first is None or have_first > days[0]:
            starts.append(days[0])
        elif have_last < days[-1]:
            # This investor's own series stopped short -- a budgeted catch-up ran out of
            # time partway (2026-10-13: a new account stuck at 27 Feb 2025 while another
            # account's rows were current, so the global newest row hid the gap forever).
            later = [d for d in days if d > have_last]
            if later:
                starts.append(later[0])
    start = min(starts)
    return start if start <= target else None
