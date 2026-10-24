"""DTOs for GET /investors/{id}/analytics and the fundamentals ops routes.

Money and quantity cross the wire as STRINGS, never JSON numbers -- the convention
``app/schemas/analytical.py`` and ``trackr-frontend/lib/api-types.ts`` already share. A
JSON number is an IEEE double by the time a browser parses it, and paise are gone.

Statistics (volatility, correlation, a factor percentile) cross as numbers, because they
are not money and never become money. The rupee figure beside a VaR is computed server-side
in Decimal and sent as a string.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel


class CoverageOut(BaseModel):
    """What share of the book each section could actually answer for.

    First-class, not a footnote: the existing liquidity module already proves the value of
    saying "66% of this is an estimate" out loud. A section below 95% gets a badge.
    """

    priced_pct: float | None
    pe_pct: float
    pb_pct: float
    roe_pct: float
    debt_to_equity_pct: float
    interest_cover_pct: float
    sector_pct: float
    #: Share of weight whose ROE exists only because we derived it from the annual
    #: statements. High is normal for Indian scrips and is the headline caveat on the page.
    roe_derived_weight_pct: float
    price_history_pct: float
    #: Other percentages describe the priced subset when full-book value is unknown.
    basis: str = "complete_priced_book"
    priced_count_pct: float = 0
    priced_count: int = 0
    total_count: int = 0
    known_value_subtotal: str = "0"


class KpiOut(BaseModel):
    invested: str | None
    market_value: str | None
    unrealised: str | None
    unrealised_pct: float | None
    realised_to_date: str | None = None
    xirr_all: float | None = None
    xirr_holdings: float | None = None
    day_change: str | None = None
    day_change_pct: float | None = None
    bench_day_change_pct: float | None = None


class HealthComponentOut(BaseModel):
    key: str
    label: str
    weight: float
    score: float | None
    points: float | None
    #: The numbers the score was computed from, keyed by name. Replaces a ``detail``
    #: sentence -- see ``app.analytics.verdict.Component`` for why a sentence was the
    #: wrong container for a figure that existed nowhere else.
    evidence: dict[str, float | None] = {}
    #: advisor_v2 only: the component's own availability, never imputed.
    status: str | None = None
    reasons: list[str] = []


class HealthExclusionOut(BaseModel):
    isin: str
    name: str | None = None
    reason: str


class HealthOut(BaseModel):
    score: int | None
    grade: str | None
    components: list[HealthComponentOut]
    drivers: list[str]
    bands: list[list]
    rule_version: str
    # advisor_v2 (portfolio-health-v1) fields. Absent on the legacy method.
    status: str | None = None
    reasons: list[str] = []
    #: Exact Decimal score as a string; ``score`` is its HALF_UP display integer.
    score_exact: str | None = None
    #: Missing-information bound when components are unavailable; not a confidence interval.
    score_bounds: dict[str, float] | None = None
    data_status: dict[str, Any] | None = None
    limitations: list[str] = []
    #: ISIN -> rubric profile, or the named reason it has none.
    profile_status: dict[str, str] = {}
    #: Covariance, recovery and sensitivity diagnostics, projected to plain numbers.
    evidence: dict[str, Any] = {}
    #: Holdings the score could not include (unpriced or unreconciled), with the reason.
    excluded: list[HealthExclusionOut] = []


class FactorOut(BaseModel):
    status: str | None = None
    reasons: list[str] = []
    eligible_count: int = 0
    score: float | None
    universe: str
    universe_size: int
    coverage_pct: float


class DrawdownSeriesOut(BaseModel):
    """Fraction below the running peak at each date of the risk window."""

    dates: list[date]
    values: list[float]


class ReturnHistogramOut(BaseModel):
    """Daily-return histogram of the risk window; ``edges`` has one more entry than
    ``counts``. Fractions, the same unit as ``var_1d_95``."""

    edges: list[float]
    counts: list[int]


class RiskWindowOut(BaseModel):
    status: str | None = None
    reasons: list[str] = []
    metrics: dict[str, dict] = {}
    period: dict[str, str | None] = {}
    basis: str | None = None
    tracking_error: float | None = None
    information_ratio: float | None = None
    observations: int
    cum_return: float | None = None
    cagr: float | None = None
    volatility: float | None = None
    sharpe: float | None = None
    sortino: float | None = None
    beta: float | None = None
    var_1d_95: float | None = None
    var_1d_99: float | None = None
    cvar_1d_95: float | None = None
    max_drawdown: float | None = None
    bench_cum_return: float | None = None
    bench_cagr: float | None = None
    bench_volatility: float | None = None
    bench_sharpe: float | None = None
    risk_free_rate: float | None
    var_method: str
    sortino_target: str
    #: The rupee value of a 1-day 95% VaR. Decimal server-side, string on the wire.
    var_1d_95_value: str | None = None
    cvar_1d_95_value: str | None = None
    volatility_1d_value: str | None = None
    drawdown_dates: dict[str, date | None] = {}
    drawdown_series: DrawdownSeriesOut | None = None
    return_histogram: ReturnHistogramOut | None = None


class AllocationRowOut(BaseModel):
    label: str
    value: str
    pct: float


class HistoryExclusionOut(BaseModel):
    isin: str
    symbol: str
    reason: str


class RiskCoverageOut(BaseModel):
    start: date
    end: date
    observations: int
    weight_pct: float | None
    market_value: str
    included_isins: list[str]
    excluded: list[HistoryExclusionOut]
    basis: str


class CorrelationOut(BaseModel):
    isins: list[str] = []
    pairs: list[dict] = []
    symbols: list[str]
    matrix: list[list[float | None]]
    average: float | None = None
    tightest: list | None = None
    loosest: list | None = None


class PositionOut(BaseModel):
    isin: str
    symbol: str | None = None
    name: str | None = None
    quantity: str
    invested: str
    avg_cost: str | None = None
    last_price: str | None = None
    market_value: str | None = None
    unrealised: str | None = None
    unrealised_pct: float | None = None
    weight: float | None = None
    sector: str | None = None
    market_cap_bucket: str | None = None
    capital_state: str | None = None
    #: advisor_v2: why a holding's capital state is UNKNOWN (e.g. MISSING_PRICE_HISTORY).
    capital_state_reason: str | None = None
    trend: str | None = None
    #: Fundamentals. Null means UNKNOWN and must render as n/a, never as zero.
    pe: float | None = None
    pb: float | None = None
    roe: float | None = None
    net_margin: float | None = None
    debt_to_equity: float | None = None
    interest_cover: float | None = None
    dividend_yield: float | None = None
    sales_growth_1y: float | None = None
    profit_growth_1y: float | None = None
    roe_basis: str | None = None
    fundamentals_as_of: date | None = None
    fundamentals_source: str | None = None
    fundamentals_basis: str | None = None
    fiscal_period: date | None = None
    momentum_12_1: float | None = None
    momentum_basis: str | None = None
    input_notes: list[str] = []
    earnings_yield_contribution_pct: float | None = None
    factors: dict[str, float | None] = {}
    returns: dict[str, float | None] = {}
    return_windows: dict[str, dict] = {}


class InsightOut(BaseModel):
    key: str
    title: str
    severity: str
    #: Every number the rule weighed, including thresholds that did NOT fire.
    evidence: dict[str, float | None] = {}
    #: The securities the chip is about, in the order the rule ranked them.
    subjects: list[str] = []
    value: float | None = None
    threshold: float | None = None
    rule_version: str


class AggregateOut(BaseModel):
    value: float | None
    excluded_weight_pct: float
    method_version: str | None = None
    status: str | None = None
    reasons: list[str] = []
    excluded_isins: list[str] = []


class FundamentalsOut(BaseModel):
    pe: AggregateOut
    pb: AggregateOut
    dividend_yield: AggregateOut
    benchmark: dict[str, float | None] = {}
    benchmark_source: str | None = None
    benchmark_as_of: date | None = None
    benchmark_comparison: str = "unverified"


class GrowthSeriesOut(BaseModel):
    dates: list[date]
    portfolio: list[str]
    benchmark: list[str] | None = None
    #: Stated on the chart, not buried: this applies TODAY's weights backwards. It is not
    #: what the portfolio returned -- that is TWRR/XIRR from the ledger.
    basis: str = "constant_weight_simulation"


class UnresolvedOut(BaseModel):
    isin: str
    symbol: str | None = None
    name: str | None = None
    quantity: str
    invested: str
    reason: str


class LensDerivedOut(BaseModel):
    """One Valuation Map lens, scored server-side.

    ``lens_status`` grades each holding against the PORTFOLIO's own averages, so the
    averages are part of the answer rather than a detail of how it was reached: without
    them "Expensive" is an unfalsifiable label.
    """

    label: str
    x_field: str
    y_field: str
    x_average: float | None
    y_average: float | None
    #: Status name -> how many holdings landed in it. Keys come from the lens's own
    #: ``statuses`` triple, never from a shared good/ok/bad vocabulary -- "Attractive"
    #: and "Efficient" are different claims.
    status_counts: dict[str, int] = {}
    status_weight_pct: dict[str, float] = {}
    position_statuses: dict[str, str | None] = {}
    unrated_weight_pct: float = 0
    reference_basis: str = "market_value_weighted_median_of_complete_pairs"
    #: Holdings with BOTH axes known, and that count as a share of all positions. A
    #: scatter that silently drops half the book is the coverage failure this names.
    plotted: int
    plottable_pct: float
    excluded: dict[str, str] = {}
    method_version: str | None = None


class AdvisorMetricOut(BaseModel):
    value: str | None
    status: Literal["available", "provisional", "unavailable"]
    reasons: list[str]
    unit: str
    method: str
    coverage: dict = {}
    period: dict = {}
    source_refs: list[str] = []


class AdvisorIntegrationOut(BaseModel):
    active_method: Literal["current", "advisor_v1", "advisor_v2"]
    method_version: str
    source_version: str
    as_of: date
    metrics: dict[str, AdvisorMetricOut]
    #: Candidate results are available for independent shadow comparison.
    lenses: dict[str, dict]
    differences: list[dict] = []


class DerivedOut(BaseModel):
    """Scalars the SCREEN used to compute for itself, moved server-side.

    Every figure here was previously recomputed in ``trackr-frontend/app/analytics/
    panels.tsx`` from the payload's other fields.

    THE TWO IMPLEMENTATIONS AGREED, and that is worth stating precisely rather than
    claiming a bug that was not there. Effective holdings was
    ``composition.effective_holdings`` here, which normalises by the actual sum of
    weights, and ``1 / Σ(weight/100)²`` there, which divides by a hard-coded 100. Those
    are the same number for exactly as long as the weights sum to 100 -- which they do,
    because every weight is ``market_value / total_value`` over the same set of priced
    positions. Verified to the last float bit by
    ``tests/test_analytics_derived.py::test_the_two_effective_holdings_formulas_agree_today``.

    The reason to move it anyway is that the agreement rests on a precondition neither
    side states. Add an unpriced holding to the numerator, round a weight for display, or
    let a filter drop a position from one list and not the other, and the hard-coded 100
    becomes wrong silently, in the renderer, where no test looks. The rule this block
    enforces: a number rendered anywhere is computed exactly once, here, and every
    renderer reads it. Nothing in this block is new financial logic -- each value is a
    projection of figures the endpoint already produced.
    """

    #: 1/HHI. The single authoritative value; the frontend must not recompute it.
    effective_holdings: float | None
    #: The effective-holdings count that scores full marks, published so the ratio behind
    #: the concentration component can be checked rather than taken on trust.
    concentration_target: float | None
    position_count: int
    #: Portfolio volatility / benchmark volatility over 1Y. None when either is missing --
    #: never 1.0, which would read as "moves exactly with the index".
    vol_ratio_1y: float | None
    #: Growth-of-1,00,000 endpoint difference, portfolio minus benchmark. Signed money, so
    #: a renderer switches its wording on the sign instead of guessing.
    growth_delta_vs_benchmark: str | None
    #: Capital-state bucket -> share of portfolio worth. The buckets sum to worth by
    #: construction; this is that reconciliation expressed as percentages.
    capital_state_pct: dict[str, float] = {}
    capital_state_total: str | None
    lenses: dict[str, LensDerivedOut] = {}


class ReadingOut(BaseModel):
    value: float | None
    coverage_pct: float
    #: What the reading leaves out by construction, named -- e.g. financials from D/E.
    note: str | None = None


class PerformanceWindowOut(BaseModel):
    start: date
    end: date
    xirr: str | None
    benchmark_xirr: str | None
    relative_pp: str | None
    market_value: str
    benchmark_terminal_value: str | None
    terminal_gap: str | None
    coverage_pct: float
    excluded: list[HistoryExclusionOut]
    benchmark_missing_dates: list[date]
    basis: str
    annualised: bool
    #: The window's own money-weighted return (XIRR de-annualised, ACT/365). The headline
    #: for windows under a year, where ``annualised`` is false.
    period_return: str | None = None
    benchmark_period_return: str | None = None


class CashFlowPerformanceOut(BaseModel):
    windows: dict[str, PerformanceWindowOut] = {}
    xirr_holdings: str | None = None
    recorded_dividend_count: int = 0
    auto_resolved_cash_flow_count: int = 0
    basis: str | None = None


class ExposureOut(BaseModel):
    isin: str
    symbol: str
    weight_pct: float
    risk_share_pct: float | None = None


class ConcentrationOut(BaseModel):
    top1_pct: float
    top3_pct: float
    top5_pct: float
    leaders: list[ExposureOut]


class PriceEventOut(BaseModel):
    isin: str
    symbol: str
    weight_pct: float
    date: date
    previous_date: date
    change_pct: float
    threshold_pct: float
    reason: str


class MilestoneSectionsOut(BaseModel):
    overview: dict[str, Any]
    factor_score: dict[str, Any]
    risk_return: dict[str, Any]
    diversification: dict[str, Any]
    fundamentals: dict[str, Any]
    performance: dict[str, Any]
    valuation_map: dict[str, Any]


class MilestoneReportOut(BaseModel):
    schema_version: str
    method_version: str
    input_digest: str
    as_of: date
    generated_at: datetime
    sources: list[dict[str, Any]]
    sections: MilestoneSectionsOut
    exclusions: dict[str, Any]
    status: str
    reasons: list[str]
    integration: dict[str, Any]
    compatibility: dict[str, Any]
    illustrations: dict[str, Any] = {}
    scope: dict[str, Any] = {}
    reported_inputs: dict[str, Any] = {}


class AnalyticsResponse(BaseModel):
    as_of: date
    generated_at: datetime
    benchmark: str
    coverage: CoverageOut
    sources: dict[str, str]
    kpi: KpiOut
    health: HealthOut
    factors: dict[str, FactorOut]
    risk: dict[str, RiskWindowOut]
    risk_coverage: dict[str, RiskCoverageOut] = {}
    risk_contributions: dict[str, list[ExposureOut]] = {}
    performance: CashFlowPerformanceOut = CashFlowPerformanceOut()
    characteristics: dict[str, ReadingOut] = {}
    concentration: ConcentrationOut | None = None
    price_events: list[PriceEventOut] = []
    allocation: dict[str, list[AllocationRowOut]]
    correlation: CorrelationOut
    fundamentals: FundamentalsOut
    capital_state: dict[str, str]
    derived: DerivedOut
    growth_of_100k: GrowthSeriesOut | None = None
    positions: list[PositionOut]
    insights: list[InsightOut]
    #: Holdings carried OUT of every denominator because they could not be priced. Never
    #: valued at zero -- CLAUDE.md rule 4.
    unresolved: list[UnresolvedOut] = []
    integration: AdvisorIntegrationOut | None = None
    advisor: MilestoneReportOut | None = None
    #: Basic-view findings (app.analytics.basic_slots): rule_version, facts, and per tab
    #: the fired candidates and the findings that could not be measured.
    basic: dict[str, Any] = {}
    #: Figures the revamped page adds (app.analytics.revamp): header cards, betas, 1Y
    #: correlation, concentration impact, index shock, liquidity, fundamentals cards and
    #: the Valuation Map against the Nifty 500 median, holding-return reasons, coverage.
    revamp: dict[str, Any] = {}


class FundamentalsRefreshResponse(BaseModel):
    requested: int
    stored: int
    with_data: int
    as_of: date
    skipped_no_symbol: list[str] = []


class IdentityReviewOut(BaseModel):
    """One withheld vendor record: our name for the ISIN against the vendor's."""

    isin: str
    symbol: str | None
    our_name: str | None
    provider: str
    ticker: str
    vendor_name: str
    reason: str
    observed_on: date
    decision: Literal["SAME", "DIFFERENT"] | None = None
    decided_at: datetime | None = None


class IdentityReviewsResponse(BaseModel):
    items: list[IdentityReviewOut]


class IdentityDecisionIn(BaseModel):
    isin: str
    provider: str
    ticker: str
    vendor_name: str
    decision: Literal["SAME", "DIFFERENT"]
