"""AnalyticsResponse -> {fact_id: Fact}. A projection, never a computation.

EVERY VALUE HERE WAS ALREADY COMPUTED. This module re-keys the analytics payload into
flat, addressable facts and attaches each one's unit, precision and provenance. It does
no arithmetic beyond that, and it must not start: a number that exists only because this
module derived it would be a number no test in ``tests/test_engine_cases.py`` or
``tests/test_golden_portfolio.py`` covers, sitting in a sentence shown to an investor.
If a figure is worth saying, it is worth adding to ``AnalyticsResponse`` first -- which
is what Phase 0 did for the scalars the browser used to compute for itself.

WHY FLAT AND WHY DOTTED. A claim has to be able to point at a number. Nested JSON has no
stable way to say "that one", so the model would have to quote the value to identify it
-- which is exactly the thing it is not allowed to do. ``health.component.concentration.
score`` is a name the model can use without ever seeing 21.

A ``None`` VALUE IS A FACT. It means the figure could not be measured, and it is kept
rather than dropped so that "there is no 3-year window yet" is sayable. A dropped fact
is indistinguishable from a fact nobody thought to include, and CLAUDE.md rule 4 is that
absence is never quietly turned into a number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.narrative.registry import MONEY, NUMERIC_UNITS, Spec, spec_for


@dataclass(frozen=True, slots=True)
class Fact:
    """One addressable figure, with everything needed to render or check it."""

    id: str
    value: Any
    unit: str
    digits: int
    comparable: bool
    as_of: str
    #: The provenance sentence from ``AnalyticsResponse.sources`` -- "NSE/BSE bhavcopy via
    #: app.prices.pipe", not "the database". Carried per fact so a claim can disclose
    #: where its evidence came from without the renderer guessing.
    source: str | None = None
    #: The published threshold this figure is judged against, where one exists.
    threshold: float | None = None
    #: Share of the book this figure actually covers. Below 95 the claim needs a caveat.
    coverage_pct: float | None = None

    @property
    def known(self) -> bool:
        return self.value is not None

    @property
    def numeric(self) -> bool:
        """True when this can enter an arithmetic comparison. MONEY is excluded: it is a
        decimal string, and float() on it is the paise-losing conversion CLAUDE.md rule 1
        exists to prevent."""
        return self.unit in NUMERIC_UNITS and self.value is not None


@dataclass(frozen=True, slots=True)
class FactSet:
    """An ordered family of comparable facts, so "the weakest" can be RECOMPUTED.

    ``metric`` is a fact-id template with one ``{}``. Ranking a set means resolving that
    template for each member and sorting -- never parsing an adjective out of a sentence.
    """

    id: str
    members: tuple[str, ...]
    metric: str

    def fact_ids(self) -> tuple[str, ...]:
        return tuple(self.metric.format(m) for m in self.members)


@dataclass(frozen=True, slots=True)
class FactPack:
    """One investor's facts, for one as_of, built inside one request.

    Deliberately a value object with no lookup-by-id-alone entry point: see this
    package's docstring on why a global resolver would be a tenancy hole.
    """

    as_of: str
    benchmark: str
    facts: dict[str, Fact]
    sets: dict[str, FactSet]
    #: The QUANTISED pack from the most recent earlier date, if one is stored, and the
    #: date it belongs to. Empty by default, which is the state that matters: with no
    #: stored yesterday there are no deltas, so DELTA_SINCE cannot evaluate and change
    #: language is unsayable rather than unchecked.
    #:
    #: Quantised on both sides on purpose -- a difference here is one a reader could
    #: actually have seen, not a move in a decimal nobody renders.
    previous: dict[str, Any] = field(default_factory=dict)
    previous_as_of: str | None = None

    def __contains__(self, fact_id: object) -> bool:
        return fact_id in self.facts

    def get(self, fact_id: str) -> Fact | None:
        return self.facts.get(fact_id)

    def require(self, fact_id: str) -> Fact:
        try:
            return self.facts[fact_id]
        except KeyError:
            raise KeyError(f"no such fact in this pack: {fact_id!r}") from None


# --- provenance --------------------------------------------------------------------

#: Fact-id fragment -> the ``sources`` key that explains where it came from. Checked in
#: order, first match wins, so the specific entries must precede the general ones.
_SOURCE_RULES: tuple[tuple[str, str], ...] = (
    ("risk_free_rate", "risk_free_rate"),
    (".sector", "sector"),
    ("allocation.sector", "sector"),
    ("market_cap", "market_cap_bucket"),
    ("bench_", "benchmark"),
    ("benchmark", "benchmark"),
    ("last_price", "prices"),
    ("risk.", "prices"),
    ("correlation.", "prices"),
    ("growth", "prices"),
    ("vol_ratio", "prices"),
    ("fundamentals.", "fundamentals"),
    ("pe", "fundamentals"),
    ("pb", "fundamentals"),
    ("roe", "fundamentals"),
    ("net_margin", "fundamentals"),
    ("debt_to_equity", "fundamentals"),
    ("interest_cover", "fundamentals"),
    ("dividend_yield", "fundamentals"),
    ("growth_1y", "fundamentals"),
    ("kpi.", "quantity_and_cost"),
    ("position.", "quantity_and_cost"),
    ("capital_state", "quantity_and_cost"),
)


def _source_for(fact_id: str, sources: dict[str, str]) -> str | None:
    for fragment, key in _SOURCE_RULES:
        if fragment in fact_id:
            return sources.get(key)
    return None


# --- the builder ---------------------------------------------------------------------


class _Builder:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self._as_of = str(payload.get("as_of"))
        self._sources: dict[str, str] = payload.get("sources") or {}
        self.facts: dict[str, Fact] = {}

    def add(
        self,
        fact_id: str,
        value: Any,
        *,
        threshold: float | None = None,
        coverage_pct: float | None = None,
        spec: Spec | None = None,
    ) -> None:
        resolved = spec or spec_for(fact_id)
        if resolved.unit == MONEY and value is not None:
            # Money arrives from the router already stringified by ``_money``. Anything
            # else reaching here means a Decimal or a float slipped past that boundary.
            value = str(value)
        self.facts[fact_id] = Fact(
            id=fact_id,
            value=value,
            unit=resolved.unit,
            digits=resolved.digits,
            comparable=resolved.comparable,
            as_of=self._as_of,
            source=_source_for(fact_id, self._sources),
            threshold=threshold,
            coverage_pct=coverage_pct,
        )


def build_pack(payload: dict[str, Any]) -> FactPack:
    """Project one ``GET /investors/{id}/analytics`` payload into a fact pack.

    ``payload`` is the plain dict the router returns, not the Pydantic model -- this
    package may not import ``app.schemas`` and does not need to.
    """
    b = _Builder(payload)
    coverage: dict[str, float] = payload.get("coverage") or {}
    priced = coverage.get("priced_pct")

    # --- headline ---------------------------------------------------------------
    for key, value in (payload.get("kpi") or {}).items():
        b.add(f"kpi.{key}", value, coverage_pct=priced)

    # --- coverage, which is itself a fact and the caveat trigger -----------------
    for key, value in coverage.items():
        # `sector_pct` collides with the insight chip's own `sector_pct`; the coverage
        # one is a different measurement and gets its own leaf.
        leaf = "sector_pct_coverage" if key == "sector_pct" else key
        b.add(f"coverage.{leaf}", value)

    # --- the grade and its decomposition -----------------------------------------
    health = payload.get("health") or {}
    b.add("health.score", health.get("score"), spec=spec_for("score"))
    b.add("health.grade", health.get("grade"))
    b.add("health.rule_version", health.get("rule_version"))
    component_keys: list[str] = []
    for component in health.get("components") or []:
        key = component["key"]
        component_keys.append(key)
        base = f"health.component.{key}"
        b.add(f"{base}.score", component.get("score"))
        b.add(f"{base}.points", component.get("points"))
        # A weight is a 0-1 share here, not a percentage of the book. Registered as a
        # ratio rather than reusing `weight`, which means a position's share.
        b.add(f"{base}.weight_share", component.get("weight"))
        b.add(f"{base}.label", component.get("label"))
        for name, value in (component.get("evidence") or {}).items():
            b.add(f"{base}.evidence.{name}", value)

    # --- factor tilts -------------------------------------------------------------
    factor_keys: list[str] = []
    for key, factor in (payload.get("factors") or {}).items():
        factor_keys.append(key)
        b.add(f"factor.{key}.score", factor.get("score"),
              coverage_pct=factor.get("coverage_pct"))
        b.add(f"factor.{key}.universe", factor.get("universe"))
        b.add(f"factor.{key}.universe_size", factor.get("universe_size"))
        b.add(f"factor.{key}.coverage_pct", factor.get("coverage_pct"))

    # --- risk windows ---------------------------------------------------------------
    for window, metrics in (payload.get("risk") or {}).items():
        window_coverage = (payload.get("risk_coverage") or {}).get(window) or {}
        for name, value in metrics.items():
            b.add(f"risk.{window}.{name}", value,
                  coverage_pct=window_coverage.get("weight_pct"))
        for name in ("start", "end", "observations", "weight_pct", "market_value", "basis"):
            if name in window_coverage:
                value = window_coverage[name]
                if name in ("start", "end"):
                    value = str(value)
                b.add(f"risk.{window}.coverage.{name}", value,
                      spec=spec_for("coverage_pct") if name == "weight_pct" else None)
        for index, excluded in enumerate(window_coverage.get("excluded") or []):
            for name in ("isin", "symbol", "reason"):
                b.add(f"risk.{window}.excluded.{index}.{name}", excluded.get(name))

    # --- allocation, indexed by rank so a label never becomes part of an id ---------
    allocation = payload.get("allocation") or {}
    sector_indices: list[str] = []
    for group, rows in allocation.items():
        for index, row in enumerate(rows):
            base = f"allocation.{group}.{index}"
            b.add(f"{base}.label", row.get("label"))
            b.add(f"{base}.pct", row.get("pct"))
            b.add(f"{base}.value", row.get("value"))
            if group == "sector":
                sector_indices.append(str(index))

    # --- correlation ------------------------------------------------------------------
    correlation = payload.get("correlation") or {}
    b.add("correlation.average", correlation.get("average"))
    for slot in ("tightest", "loosest"):
        pair = correlation.get(slot)
        b.add(f"correlation.{slot}.value", pair[2] if pair else None,
              spec=spec_for("average"))
        b.add(f"correlation.{slot}.label", f"{pair[0]} / {pair[1]}" if pair else None)

    # --- portfolio fundamentals -------------------------------------------------------
    for name, aggregate in (payload.get("fundamentals") or {}).items():
        if not isinstance(aggregate, dict) or "value" not in aggregate:
            continue
        b.add(f"fundamentals.{name}.{name}", aggregate.get("value"))
        b.add(f"fundamentals.{name}.excluded_weight_pct",
              aggregate.get("excluded_weight_pct"))

    # --- capital state and the derived block ------------------------------------------
    for bucket, value in (payload.get("capital_state") or {}).items():
        b.add(f"capital_state.{bucket}.value", value)

    derived = payload.get("derived") or {}
    for name in ("effective_holdings", "concentration_target", "position_count",
                 "vol_ratio_1y", "growth_delta_vs_benchmark"):
        b.add(f"derived.{name}", derived.get(name))
    for bucket, value in (derived.get("capital_state_pct") or {}).items():
        b.add(f"derived.capital_state_pct.{bucket}.pct", value)
    for key, lens in (derived.get("lenses") or {}).items():
        base = f"derived.lens.{key}"
        b.add(f"{base}.label", lens.get("label"))
        b.add(f"{base}.x_average", lens.get("x_average"))
        b.add(f"{base}.y_average", lens.get("y_average"))
        b.add(f"{base}.plotted", lens.get("plotted"))
        b.add(f"{base}.plottable_pct", lens.get("plottable_pct"))

    # --- positions --------------------------------------------------------------------
    position_isins: list[str] = []
    _POSITION_FIELDS = (
        "quantity", "invested", "avg_cost", "last_price", "market_value", "unrealised",
        "unrealised_pct", "weight", "sector", "market_cap_bucket", "capital_state",
        "trend", "pe", "pb", "roe", "net_margin", "debt_to_equity", "interest_cover",
        "dividend_yield", "sales_growth_1y", "profit_growth_1y", "roe_basis",
        "symbol", "name",
    )
    for position in payload.get("positions") or []:
        isin = position["isin"]
        position_isins.append(isin)
        base = f"position.{isin}"
        for name in _POSITION_FIELDS:
            if name == "quantity":
                # Quantity is a decimal string like money and must not become a float.
                b.add(f"{base}.quantity", position.get("quantity"),
                      spec=spec_for("invested"))
                continue
            b.add(f"{base}.{name}", position.get(name))
        for name, value in (position.get("factors") or {}).items():
            b.add(f"{base}.factor.{name}.score", value)

    # --- insight chips ------------------------------------------------------------------
    for insight in payload.get("insights") or []:
        key = insight["key"]
        base = f"insight.{key}"
        b.add(f"{base}.severity", insight.get("severity"))
        b.add(f"{base}.label", insight.get("title"))
        for name, value in (insight.get("evidence") or {}).items():
            b.add(f"{base}.evidence.{name}", value, threshold=insight.get("threshold"))
        # Subjects are indexed rather than stored as a list, for the same reason the
        # allocation rows are: a claim has to be able to point at ONE of them, and a
        # list-valued fact can only be quoted whole.
        for index, subject in enumerate(insight.get("subjects") or []):
            b.add(f"{base}.subject.{index}.label", subject)

    # --- exclusions, which invariant 16 requires be sayable -------------------------
    b.add("unresolved.unresolved_count", float(len(payload.get("unresolved") or [])))

    # --- the sets a claim may rank over ----------------------------------------------
    sets: dict[str, FactSet] = {}

    def _add_set(set_id: str, members: list[str], metric: str) -> None:
        if members:
            sets[set_id] = FactSet(set_id, tuple(members), metric)

    _add_set("SET.positions_by_weight", position_isins, "position.{}.weight")
    _add_set("SET.health_components_by_score", component_keys,
             "health.component.{}.score")
    _add_set("SET.factors_by_score", factor_keys, "factor.{}.score")
    _add_set("SET.sectors_by_pct", sector_indices, "allocation.sector.{}.pct")

    return FactPack(
        as_of=b._as_of,
        benchmark=str(payload.get("benchmark") or ""),
        facts=b.facts,
        sets=sets,
    )


def attach_previous(
    pack: FactPack, previous: dict[str, Any], previous_as_of: str
) -> FactPack:
    """Give a pack its stored yesterday, so DELTA_SINCE has something to evaluate against.

    Separate from ``build_pack`` because building is pure and this reaches for history
    the caller had to fetch. Separate from ``select`` because a surface's facts and its
    history are different questions.
    """
    facts = dict(pack.facts)
    # The baseline date becomes a fact so a change can RENDER which day it is measured
    # from. Without it the only way to say so would be model-written text, which cannot
    # contain a date -- it has digits.
    facts["delta.since_date"] = Fact(
        id="delta.since_date",
        value=previous_as_of,
        unit="label",
        digits=0,
        comparable=False,
        as_of=pack.as_of,
    )
    return FactPack(
        as_of=pack.as_of,
        benchmark=pack.benchmark,
        facts=facts,
        sets=pack.sets,
        previous=previous,
        previous_as_of=previous_as_of,
    )
