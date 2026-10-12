"""Which facts a given surface is allowed to cite. Retrieval, by schema rather than by
embedding.

THIS IS THE ANSWER TO "DO WE NEED RAG". Every number in this system already has a name,
so finding the relevant ones is a lookup, not a search. A hand-written list per surface
is auditable, deterministic, and cannot return the wrong neighbour the way a nearest-
vector match can. There is no index to rebuild and nothing to re-embed when a metric is
added -- an unlisted fact is simply not citable, which is the safe direction.

It is also the second half of a verifier rule. A claim on the Risk tab that cites a
fundamentals figure is describing something the reader cannot see, and V5 rejects it by
checking membership here. So these lists are not a token-budget optimisation; widening
one widens what a surface may assert.

TWO THINGS EVERY SURFACE GETS. ``coverage.*`` because a claim resting on 61% of the book
has to be able to say so, and ``unresolved.*`` because invariant 16 requires a total that
excludes something to name the exclusion.
"""

from __future__ import annotations

from fnmatch import fnmatchcase

from app.narrative.factpack import FactPack, FactSet

#: Granted to every surface. See the module docstring.
_ALWAYS = ("coverage.*", "unresolved.*", "health.grade", "health.score", "delta.since_date")

#: Surface -> the fnmatch patterns whose facts that surface may cite.
SURFACES: dict[str, tuple[str, ...]] = {
    "analytics.overview": (
        "kpi.*", "health.*", "insight.*", "capital_state.*",
        "derived.capital_state_pct.*", "derived.effective_holdings",
        "derived.concentration_target", "derived.position_count",
        "risk.1y.*", "risk.available.*",
    ),
    "analytics.factor": (
        "factor.*", "position.*.factor.*.score", "position.*.symbol",
        "position.*.name", "position.*.weight",
    ),
    "analytics.risk": (
        "risk.*", "derived.vol_ratio_1y", "kpi.market_value",
    ),
    "analytics.diversification": (
        "allocation.*", "correlation.*", "derived.effective_holdings",
        "derived.concentration_target", "derived.position_count",
        "position.*.weight", "position.*.sector", "position.*.market_cap_bucket",
        "position.*.symbol", "position.*.name",
    ),
    "analytics.fundamentals": (
        "fundamentals.*", "position.*.pe", "position.*.pb", "position.*.roe",
        "position.*.net_margin", "position.*.debt_to_equity",
        "position.*.interest_cover", "position.*.dividend_yield",
        "position.*.roe_basis", "position.*.symbol", "position.*.name",
        "position.*.weight",
    ),
    "analytics.performance": (
        "derived.growth_delta_vs_benchmark", "risk.*.cum_return", "risk.*.cagr",
        "risk.*.bench_cum_return", "risk.*.bench_cagr", "risk.*.sharpe",
        "risk.*.bench_sharpe", "kpi.*",
        "risk.*.coverage.*", "risk.*.excluded.*",
    ),
    "analytics.valuation": (
        "derived.lens.*", "position.*.pe", "position.*.pb", "position.*.roe",
        "position.*.net_margin", "position.*.debt_to_equity",
        "position.*.interest_cover", "position.*.sales_growth_1y",
        "position.*.profit_growth_1y", "position.*.symbol", "position.*.name",
        "position.*.weight",
    ),
    "dashboard": (
        "kpi.*", "health.*", "allocation.*", "derived.position_count",
        "derived.effective_holdings", "insight.*",
    ),
    #: Per-holding. Scoped further by ``select()``'s ``isin`` argument -- the pattern
    #: below would otherwise let a holding's page cite every OTHER holding's numbers.
    "holding": (
        "position.*", "kpi.market_value", "allocation.sector.*",
    ),
}


class UnknownSurface(Exception):
    """Fail closed rather than defaulting to "everything"."""


def patterns_for(surface: str) -> tuple[str, ...]:
    try:
        return SURFACES[surface] + _ALWAYS
    except KeyError:
        raise UnknownSurface(
            f"{surface!r} is not a known surface; known: {sorted(SURFACES)}"
        ) from None


def select(pack: FactPack, surface: str, isin: str | None = None) -> FactPack:
    """A NEW pack holding only what ``surface`` may cite.

    Returns a pack rather than a set of ids so that nothing downstream ever holds a
    selected id list alongside the full pack and resolves against the wrong one. The
    selected pack is the only pack the generator and the verifier ever see.

    ``isin`` narrows the ``holding`` surface to one security. Passing it for any other
    surface is a caller error, not a filter: it would silently drop the portfolio-level
    facts that surface is about.
    """
    patterns = patterns_for(surface)
    if surface == "holding":
        if not isin:
            raise ValueError("the 'holding' surface needs an isin to scope to")
        # Exact substitution, not a prefix replace: `position.*` must become
        # `position.<isin>.*` and keep its trailing wildcard, or the surface selects the
        # single fact literally named `position.<isin>` -- which does not exist -- and
        # silently yields a pack with no position facts at all.
        patterns = tuple(
            f"position.{isin}.*" if pattern == "position.*" else pattern
            for pattern in patterns
        )
    elif isin is not None:
        raise ValueError(f"isin is only meaningful for the 'holding' surface, not {surface!r}")

    facts = {
        fact_id: fact
        for fact_id, fact in pack.facts.items()
        if any(fnmatchcase(fact_id, pattern) for pattern in patterns)
    }

    # A set survives only if EVERY member it ranks over survived. A partially-present
    # set is worse than an absent one: "the weakest component" recomputed over half the
    # components is a true statement about a set the reader cannot see.
    sets: dict[str, FactSet] = {
        set_id: factset
        for set_id, factset in pack.sets.items()
        if all(fact_id in facts for fact_id in factset.fact_ids())
    }

    return FactPack(
        as_of=pack.as_of,
        benchmark=pack.benchmark,
        facts=facts,
        sets=sets,
        # History is narrowed with the facts: a surface that may not cite a figure may
        # not talk about its movement either.
        previous={k: v for k, v in pack.previous.items() if k in facts},
        previous_as_of=pack.previous_as_of,
    )
