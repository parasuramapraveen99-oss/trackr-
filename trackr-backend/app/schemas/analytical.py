"""DTOs for GET /investors/{id}/performance, GET /investors/{id}/contribution,
GET /investors/{id}/ledger-summary,
POST /ops/prices/refresh, and POST /ops/snapshots/run."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.analytics import PerformanceWindowOut


class BenchmarkOut(BaseModel):
    name: str
    xirr: str | None = None
    terminal_value: str | None = None
    reason_code: str | None = None
    warning: str | None = None


class ExcludedOut(BaseModel):
    """A security left out of a figure because its history does not reconcile yet
    (docs/ARCHITECTURE_INVARIANTS.md rules 5, 16 and the 2026-10-08 owner amendment).
    The figure beside it is computed on everything else and must be read as such."""

    isin: str
    symbol: str | None = None
    message: str
    deficit: str | None = None
    #: Set when the reason is categorical rather than a reconciliation gap, e.g.
    #: ``MUTUAL_FUND_SCOPE``: a fund unit whose return is reported in the fund series.
    reason_code: str | None = None
    as_of: date | None = None


class PerformanceResponse(BaseModel):
    as_of: date
    # Both variants together, always -- CLAUDE.md: reporting one alone leaves a choice
    # about which number flatters the portfolio.
    xirr_all: str | None
    xirr_holdings: str | None
    # Never shown alone either (app.engine.xirr.compute_fy_xirr's docstring) -- sits
    # beside the pair above. No default: the router must always say so explicitly, even
    # when it is null because no 1-April price exists for a held ISIN.
    fy_xirr: str | None
    # Fixed-calendar trailing windows, same resolution as fy_xirr (see
    # app.routers.analytical._window_xirr) -- 1/3/5 calendar years back from as_of, via
    # years_before (app.engine.xirr). Each is independently null: a portfolio with 2
    # years of history gets a null xirr_5y alongside real xirr_1y/xirr_3y values, never
    # all blanked together and never a mislabeled xirr_all standing in for the window.
    xirr_1y: str | None = None
    xirr_3y: str | None = None
    xirr_5y: str | None = None
    # TWRR (app.engine.twrr, ENGINE_SPEC section 5) -- the adviser-skill companion to
    # the fy/1y/3y/5y XIRR windows above, never shown alone. Same fixed-calendar
    # resolution, same independent nullability -- chains the persisted daily snapshot
    # series instead of solving a single money-weighted rate.
    fy_twrr: str | None = None
    twrr_1y: str | None = None
    twrr_3y: str | None = None
    twrr_5y: str | None = None
    # Adviser-onboarding variant: since investor.onboarding_date (docs/DATA_MODEL.md),
    # resolved the same honest way as fy_xirr -- see
    # app.routers.analytical._window_xirr. period_start echoes back the date actually
    # used; both are None together, exactly when no onboarding_date is usable.
    period_xirr: str | None = None
    period_start: date | None = None
    absolute_return: str | None = None
    cagr: str | None = None
    invested: str | None = None
    market_value: str | None = None
    unrealised: str | None = None
    realised_to_date: str | None = None
    benchmark: BenchmarkOut | None = None
    warning: str | None = None
    #: Which ledger these figures cover (owner amendment 26-Oct-2026): "listed" (default,
    #: stocks and ETFs), "all" (stocks and mutual funds together), or "mutual_fund".
    scope: str = "listed"
    #: Only for scope "all": the combined XIRR's two parts, each solved over its own flows.
    xirr_listed: str | None = None
    xirr_funds: str | None = None
    #: Securities left out of EVERY figure above because their history does not reconcile
    #: yet. Empty when the whole ledger reconciles.
    excluded: list[ExcludedOut] = Field(default_factory=list)


class ContributionOut(BaseModel):
    """One ISIN's rupee contribution -- app.engine.types.Contribution, ENGINE_SPEC
    section 10. A fully-exited holding still appears here, with end_value "0"."""

    isin: str
    symbol: str | None = None
    start_value: str
    end_value: str
    net_flow: str
    gain: str
    pct_of_total: str | None = None


class ContributionResponse(BaseModel):
    as_of: date
    period_start: date
    total_gain: str
    contributions: list[ContributionOut]
    excluded: list[ExcludedOut] = Field(default_factory=list)
    benchmark: str | None = None
    performance: PerformanceWindowOut | None = None


class LedgerSummaryPeriod(BaseModel):
    """The window ``(start, end]``; ``first_day`` is ``start`` + 1 (ENGINE_SPEC s.16)."""

    start: date
    end: date
    first_day: date
    label: str
    kind: Literal["all", "fy", "custom"]
    #: Newest price date behind the closing value; null when nothing is held at ``end``.
    valuation_date: date | None = None


class LedgerSummaryFy(BaseModel):
    fy: str
    label: str
    start: date
    end: date


class PeriodXirrOut(BaseModel):
    value: str | None = None
    status: Literal["ok", "short_period", "withheld_transfers", "undefined", "incomplete"]
    reason: str | None = None


class LedgerSummaryExclusion(BaseModel):
    """A security left out of some or all figures. An empty ``affects`` means it is
    listed for completeness and does not make this window's figures incomplete."""

    isin: str
    symbol: str | None = None
    reason: str
    reason_code: str | None = None
    as_of: date | None = None
    affects: list[str] = Field(default_factory=list)
    periods: list[str] = Field(default_factory=list)


class LedgerSummaryRow(BaseModel):
    date: date
    isin: str
    symbol: str | None = None
    txn_type: str
    quantity: str
    #: The engine figure this row adds to ``figure``; null for a type with no flow, or a
    #: transfer with no close on its date.
    amount: str | None = None
    figure: Literal[
        "purchases", "proceeds", "dividends", "transfers_in", "transfers_out", "none"
    ]
    excluded: bool = False
    auto_resolved: bool = False
    source_ref: str | None = None


class LedgerSummaryHolding(BaseModel):
    isin: str
    symbol: str | None = None
    quantity: str
    price: str
    price_date: date | None = None
    price_source: str
    market_value: str


class LedgerSummaryResponse(BaseModel):
    """GET /investors/{id}/ledger-summary -- ENGINE_SPEC section 16. Every money value is
    a Decimal string; the client does no arithmetic on them."""

    period: LedgerSummaryPeriod
    scope: Literal["all", "listed", "mutual_fund"]
    available_fys: list[LedgerSummaryFy]
    opening: str
    purchases: str
    proceeds: str
    dividends: str
    transfers_in: str
    transfers_out: str
    closing: str
    period_gain: str
    sales_and_dividends: str
    realised: str
    realised_sales: int
    xirr: PeriodXirrOut
    xirr_all_time: PeriodXirrOut
    exclusions: list[LedgerSummaryExclusion] = Field(default_factory=list)
    rows: list[LedgerSummaryRow] = Field(default_factory=list)
    holdings_end: list[LedgerSummaryHolding] = Field(default_factory=list)
    auto_resolved_count: int = 0


class SymbolChangeOut(BaseModel):
    """A trading symbol that moved under an UNCHANGED ISIN.

    Reported rather than left in the logs because it is a corporate-action signal: NSE
    renaming a scrip usually means something happened to the company. Distinct from an
    ISIN change (``security_alias``) -- here the key did not move, only the label.
    """

    isin: str
    old_symbol: str | None
    new_symbol: str


class PriceRefreshResponse(BaseModel):
    prices_stored: int
    missing_isins: list[str]
    price_date: date
    #: Rows upserted into ``index_level`` per TRI series on this run, keyed by storage
    #: name. A series whose fetch failed is absent rather than zero: nothing was stored
    #: AND nothing is known, which is not the same as "the source had no new rows".
    index_levels_stored: dict[str, int] = {}
    #: Series whose fetch or parse failed this run, with the reason. Reported rather than
    #: raised: the benchmark line is a decoration on a chart whose subject is priced from
    #: bhavcopy, and failing the whole price refresh over it would be the tail wagging
    #: the dog. Present and non-empty is the signal to go and look.
    index_failures: dict[str, str] = {}
    #: Empty on almost every run, which is the point. A non-empty list means a scrip was
    #: renamed today and every ticker-keyed lookup built from the old symbol -- Yahoo
    #: quotes, fundamentals -- was about to break silently.
    symbol_changes: list[SymbolChangeOut] = []


class SnapshotOut(BaseModel):
    """One persisted ``portfolio_snapshot`` row -- the shape ``schema/snapshot.schema.json``
    already defines. ``GET /investors/{id}/snapshots`` returns a bare array of these
    (mirrors ``holding.schema.json`` reused as ``holdings``' array items), so no separate
    top-level schema file was added for the range-read response itself."""

    as_of: date
    invested: str
    market_value: str
    unrealised: str | None = None
    realised_to_date: str | None = None
    #: ISINs these totals do NOT include, because their ledger segment did not reconcile
    #: on this date. Empty on a clean replay. A client showing these figures should say
    #: so when this is non-empty -- that is the whole reason the field is on the wire.
    excluded_isins: list[str] = Field(default_factory=list)


class BenchmarkPointOut(BaseModel):
    """One point on the dashboard's benchmark line: what the portfolio's OWN cash flows
    would be worth in the index on that date (docs/ENGINE_SPEC.md section 6, evaluated
    per date -- see ``app.engine.benchmark.benchmark_series``).

    Dated to a persisted snapshot's ``as_of`` so it lands on the same x-position as that
    snapshot's market value. A snapshot date whose index level is missing has no point
    here at all, rather than a null or a carried-forward one -- the chart draws the gap.
    """

    as_of: date
    value: str


class BenchmarkSeriesResponse(BaseModel):
    name: str
    points: list[BenchmarkPointOut]


class SkippedSnapshotOut(BaseModel):
    investor_id: str
    advised_only: bool
    as_of: date
    isins: list[str]


class PriceBackfillResponse(BaseModel):
    """One bounded price-history backfill run -- see ``app.prices.history``. ``pending`` is
    how many trading days in the ledger's history still lacked an answer after
    ``hydrated_days`` were answered from the full-market store (``market_close``); it
    reaches 0 once history is complete."""

    pending: int
    hydrated_days: int = 0
    fetched_days: int
    closed_days: int
    failed_days: int


class SnapshotRunResponse(BaseModel):
    snapshots_written: int
    price_dates_fetched: int
    skipped_missing_price: list[SkippedSnapshotOut]
    # False when a time-boxed run stopped before the target day; the rest resumes next run.
    complete: bool = True
