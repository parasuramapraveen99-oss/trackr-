"""What a fact IS: its unit, the precision it renders at, and how it may be compared.

A ``fact_id`` on its own is a name. This module is what makes it a measurement, and it
does three jobs that each prevent a specific defect:

1. UNIT. ``format.ts`` is explicit that guessing between a fraction and an
   already-a-percentage "is how a screen ends up showing 3000% volatility". The same
   guess made here would put 3000% into a sentence, where it is harder to spot. So every
   fact declares which it is and nothing infers it from the value.

2. PRECISION. ``digits`` is the number of decimals the figure will actually be rendered
   at. It is not cosmetic: the cache key quantises to exactly this, so that two days whose
   figures render identically produce the same prose instead of two paraphrases of one
   fact.

3. COMPARABILITY. Two facts may only be compared when their units match. "Your Sharpe is
   above your beta" is grammatical, arithmetically evaluable, and meaningless. ``kind``
   is what lets the verifier refuse it.

MONEY IS A STRING AND STAYS ONE. Decimal from the engine, ``str`` on the wire, ``str``
here -- see ``app/database.py::money``. Nothing in this package converts money to float,
including "just for a comparison": a comparison that needs it can use Decimal.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Unit vocabulary. Deliberately small -- a unit nobody can name is a unit nobody can
#: compare safely.
#:
#:   FRACTION  0.0295 means 2.95%. Volatility, CAGR, VaR, ROE, growth rates.
#:   PERCENT   34.5 already means 34.5%. Weights, allocation shares, factor scores.
#:   RATIO     a pure number. P/E, beta, Sharpe, effective holdings, correlation.
#:   MONEY     rupees, as a decimal STRING. Never a float, never compared as one.
#:   COUNT     a whole number of things.
#:   ENUM      a value from a closed set (capital_state, trend, grade).
#:   LABEL     free text naming something (a sector, a security, a universe).
FRACTION = "fraction"
PERCENT = "percent"
RATIO = "ratio"
MONEY = "money"
COUNT = "count"
ENUM = "enum"
LABEL = "label"

#: Units whose values are numerically comparable to each other. MONEY is excluded on
#: purpose: it is a string here, and a comparison that wants it must go through Decimal.
NUMERIC_UNITS = frozenset({FRACTION, PERCENT, RATIO, COUNT})


@dataclass(frozen=True, slots=True)
class Spec:
    """How one metric renders and whether it may be compared."""

    unit: str
    digits: int
    #: False for anything whose ordering is meaningless even though it is a number --
    #: a universe size, a rule version. Prevents "your universe is larger than your beta".
    comparable: bool = True


#: Metric name -> Spec, keyed by the LEAF of a fact id rather than the whole path, so
#: ``risk.1y.beta`` and ``risk.3y.beta`` cannot disagree about what a beta is.
#:
#: Precision follows what the screen actually renders (trackr-frontend/app/analytics/
#: format.ts and the panels), not what the number carries.
SPECS: dict[str, Spec] = {
    # --- money ---------------------------------------------------------------------
    "invested": Spec(MONEY, 0),
    "market_value": Spec(MONEY, 0),
    "known_value_subtotal": Spec(MONEY, 0),
    "unrealised": Spec(MONEY, 0),
    "day_change": Spec(MONEY, 0),
    "realised_to_date": Spec(MONEY, 0),
    "var_1d_95_value": Spec(MONEY, 0),
    "growth_delta_vs_benchmark": Spec(MONEY, 0),
    "avg_cost": Spec(MONEY, 2),
    "last_price": Spec(MONEY, 2),
    "value": Spec(MONEY, 0),

    # --- already-a-percentage --------------------------------------------------------
    "unrealised_pct": Spec(PERCENT, 2),
    "day_change_pct": Spec(PERCENT, 2),
    "bench_day_change_pct": Spec(PERCENT, 2),
    "weight": Spec(PERCENT, 1),
    "pct": Spec(PERCENT, 1),
    "score": Spec(PERCENT, 0),
    "points": Spec(PERCENT, 1),
    "coverage_pct": Spec(PERCENT, 1),
    "excluded_weight_pct": Spec(PERCENT, 1),
    "plottable_pct": Spec(PERCENT, 1),
    "relative_1y_pp": Spec(PERCENT, 1),
    "gap_pp": Spec(PERCENT, 1),
    "top1_pct": Spec(PERCENT, 1),
    "top2_pct": Spec(PERCENT, 1),
    "combined_pct": Spec(PERCENT, 1),
    "sector_pct": Spec(PERCENT, 1),
    "gain_share_top2_pct": Spec(PERCENT, 1),
    "roe_floor_pct": Spec(PERCENT, 1),
    "single_name_threshold": Spec(PERCENT, 1),
    "top_two_threshold": Spec(PERCENT, 1),
    "priced_pct": Spec(PERCENT, 1),
    "priced_count_pct": Spec(PERCENT, 1),
    "pe_pct": Spec(PERCENT, 1),
    "pb_pct": Spec(PERCENT, 1),
    "roe_pct": Spec(PERCENT, 1),
    "debt_to_equity_pct": Spec(PERCENT, 1),
    "interest_cover_pct": Spec(PERCENT, 1),
    "sector_pct_coverage": Spec(PERCENT, 1),
    "roe_derived_weight_pct": Spec(PERCENT, 1),
    "price_history_pct": Spec(PERCENT, 1),

    # --- fractions -------------------------------------------------------------------
    "cum_return": Spec(FRACTION, 2),
    "cagr": Spec(FRACTION, 2),
    "volatility": Spec(FRACTION, 2),
    "var_1d_95": Spec(FRACTION, 2),
    "var_1d_99": Spec(FRACTION, 2),
    "cvar_1d_95": Spec(FRACTION, 2),
    "max_drawdown": Spec(FRACTION, 2),
    "risk_free_rate": Spec(FRACTION, 2),
    "bench_cum_return": Spec(FRACTION, 2),
    "bench_cagr": Spec(FRACTION, 2),
    "bench_volatility": Spec(FRACTION, 2),
    "roe": Spec(FRACTION, 2),
    "net_margin": Spec(FRACTION, 2),
    "dividend_yield": Spec(FRACTION, 2),
    "sales_growth_1y": Spec(FRACTION, 2),
    "profit_growth_1y": Spec(FRACTION, 2),

    # --- ratios ----------------------------------------------------------------------
    "sharpe": Spec(RATIO, 2),
    "sortino": Spec(RATIO, 2),
    "beta": Spec(RATIO, 2),
    "bench_sharpe": Spec(RATIO, 2),
    "pe": Spec(RATIO, 2),
    "pb": Spec(RATIO, 2),
    "debt_to_equity": Spec(RATIO, 2),
    "interest_cover": Spec(RATIO, 2),
    "effective_holdings": Spec(RATIO, 1),
    "concentration_target": Spec(RATIO, 0),
    "vol_ratio_1y": Spec(RATIO, 2),
    "average": Spec(RATIO, 2),
    "weight_share": Spec(RATIO, 2),
    "target": Spec(RATIO, 0),
    "band_low": Spec(RATIO, 2),
    "band_high": Spec(RATIO, 2),
    "x_average": Spec(RATIO, 2),
    "y_average": Spec(RATIO, 2),

    # --- counts ----------------------------------------------------------------------
    "position_count": Spec(COUNT, 0),
    "priced_count": Spec(COUNT, 0),
    "total_count": Spec(COUNT, 0),
    "observations": Spec(COUNT, 0),
    "universe_size": Spec(COUNT, 0, comparable=False),
    "count": Spec(COUNT, 0),
    "plotted": Spec(COUNT, 0),
    "factors_scored": Spec(COUNT, 0),
    "factors_offered": Spec(COUNT, 0),
    "unresolved_count": Spec(COUNT, 0),

    # --- non-numeric -----------------------------------------------------------------
    "grade": Spec(ENUM, 0, comparable=False),
    "capital_state": Spec(ENUM, 0, comparable=False),
    "trend": Spec(ENUM, 0, comparable=False),
    "roe_basis": Spec(ENUM, 0, comparable=False),
    "severity": Spec(ENUM, 0, comparable=False),
    "var_method": Spec(ENUM, 0, comparable=False),
    # The Sortino downside target, carried as text ("0") because it names a convention
    # rather than measuring anything -- a 0 here is not a reading of zero.
    "sortino_target": Spec(ENUM, 0, comparable=False),
    "label": Spec(LABEL, 0, comparable=False),
    "symbol": Spec(LABEL, 0, comparable=False),
    "name": Spec(LABEL, 0, comparable=False),
    "sector": Spec(LABEL, 0, comparable=False),
    "market_cap_bucket": Spec(LABEL, 0, comparable=False),
    "universe": Spec(LABEL, 0, comparable=False),
    "benchmark": Spec(LABEL, 0, comparable=False),
    "basis": Spec(LABEL, 0, comparable=False),
    "rule_version": Spec(LABEL, 0, comparable=False),
    # The date a DELTA_SINCE is measured FROM. A label, not a date type: it is rendered
    # verbatim and never compared, and a change measured from an unnamed day is
    # unfalsifiable.
    "since_date": Spec(LABEL, 0, comparable=False),
}


class UnregisteredFact(Exception):
    """A fact was built whose leaf has no Spec.

    FAIL CLOSED. The alternative -- default to "ratio, 2 decimals" -- is how a fraction
    reaches a reader as 0.03% instead of 3%. A new field on AnalyticsResponse should
    break this loudly and be given a unit by a human who knows which one it is.
    """


def spec_for(fact_id: str) -> Spec:
    """Spec for a dotted fact id, resolved on its LEAF.

    ``health.component.concentration.evidence.effective_holdings`` resolves on
    ``effective_holdings``, which is why the same metric cannot mean two things in two
    places.
    """
    leaf = fact_id.rsplit(".", 1)[-1]
    try:
        return SPECS[leaf]
    except KeyError:
        raise UnregisteredFact(
            f"{fact_id!r} has no unit: add {leaf!r} to app.narrative.registry.SPECS. "
            "Guessing a unit is how a fraction gets rendered as a percentage."
        ) from None
