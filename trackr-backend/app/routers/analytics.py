"""GET /investors/{id}/analytics -- the portfolio analytics surface.

READ-ONLY AND CACHE-BACKED. This endpoint never fetches from a third party. Prices come
from the bhavcopy pipe, fundamentals from whatever the cron has already stored, and
staleness is REPORTED rather than waited on. That is what keeps the page fast regardless
of what Yahoo is doing, and it is the difference between "analytics" and "a proxy".

Layering, which the lint rules enforce:
    app.engine      -- holdings, cost basis, XIRR. Decimal. Never recomputed here.
    app.analytics   -- statistics. float. Pure functions over materialised data.
    this module     -- reads the db, calls both, converts once at the edge.

Money is Decimal from the engine to the wire, where it becomes a string. Statistics are
floats and never become money: the rupee VaR below is the one crossing point, and it goes
through Decimal(str(ratio)) explicitly.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app import securities
from app.analytics import advisor, composition, factors, integration, series, verdict, windows
from app.analytics import fundamentals as fagg
from app.auth.dependencies import require_owned_investor
from app.auth.ops import require_cron_secret, require_ops_key
from app.config import Settings, get_settings
from app.database import get_conn
from app.db import identity_review
from app.db.corporate_actions import list_corporate_actions
from app.db.fundamentals import latest as latest_fundamentals
from app.db.index_levels import get_index_levels
from app.db.index_valuation import latest as latest_index_valuation
from app.db.prices import adjusted_price_series as price_series
from app.db.prices import held_isins, latest_prices, observed_listing
from app.db.repository import load_transactions
from app.db.risk_free import annualised_rate
from app.db.universe import DEFAULT_UNIVERSE, current_universe_isins
from app.engine.holdings import compute_holdings_partitioned
from app.prices.fundamentals import DEFAULT_BATCH_LIMIT, refresh_stale
from app.prices.index_sources import NIFTY_TRI_INDEX_NAME
from app.routers.analytics_performance import build_performance
from app.schemas.analytics import (
    AnalyticsResponse,
    FundamentalsRefreshResponse,
    IdentityDecisionIn,
    IdentityReviewsResponse,
)

logger = logging.getLogger("routers.analytics")

router = APIRouter(tags=["analytics"])

#: How far back the price series is pulled. Five years plus a margin, because the 12-1
#: momentum construction needs a full year BEFORE the window it reports on.
_LOOKBACK_DAYS = 366 * 6

_FACTOR_KEYS = ("momentum", "value", "quality", "low_volatility")


def _money(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _num(value: Decimal | None) -> float | None:
    """A stored ratio -> a JSON number. Ratios are not money; see the module docstring."""
    return None if value is None else float(value)


def _pct(part: Decimal, whole: Decimal) -> float:
    return float(part / whole * 100) if whole else 0.0


@router.get(
    "/investors/{investor_id}/analytics",
    response_model=AnalyticsResponse,
    dependencies=[Depends(require_owned_investor)],
)
def analytics(
    investor_id: str,
    as_of: date | None = None,
    benchmark: str = NIFTY_TRI_INDEX_NAME,
    advised_only: bool = False,
    universe: str = DEFAULT_UNIVERSE,
    connection: Any = Depends(get_conn),
    calculation_method: Literal["current", "advisor_v1", "advisor_v2"] = "current",
) -> dict[str, Any]:
    """Everything the analytics screen renders, from one replay of the ledger."""
    on = as_of or date.today()
    if calculation_method == "advisor_v2":
        from app.routers.analytics_advisor import IST, cached_analytics

        # Indian-market sessions: "today" is the IST date, the same date the warm job and
        # the current-classification rules use, so a default request can hit the cache.
        on = as_of or datetime.now(IST).date()
        return cached_analytics(connection, investor_id, on, benchmark, advised_only, universe)
    transactions = load_transactions(connection, investor_id, advised_only=advised_only, date_to=on)

    # Partitioned, not raw: a segment that does not reconcile is carried out as a named
    # exception rather than silently producing a negative holding.
    holdings, skipped = compute_holdings_partitioned(transactions, on)
    holdings = [h for h in holdings if h.quantity > 0]
    isins = [h.isin for h in holdings]
    meta = securities.lookup(connection, isins) if isins else {}

    prices = latest_prices(connection, isins, on) if isins else {}
    start = on - timedelta(days=_LOOKBACK_DAYS)
    history = price_series(connection, isins, start, on) if isins else {}
    fundamentals = latest_fundamentals(connection, isins, on) if isins else {}
    # A vendor record withheld for a name mismatch is used for THIS investor once they
    # have answered "same company" (vendor_identity_decision); never for anyone else.
    confirmed = identity_review.confirmed_overrides(connection, investor_id, isins) if isins else {}
    fundamentals.update(confirmed)

    # --- positions, and the unpriced exception -----------------------------------------
    positions: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    total_value = Decimal(0)
    total_invested = Decimal(0)

    for holding in holdings:
        info = meta.get(holding.isin, {})
        price = prices.get(holding.isin)
        if price is None:
            # CLAUDE.md rule 4: never valued at zero. Carried out of every denominator.
            unresolved.append(
                {
                    "isin": holding.isin,
                    "symbol": info.get("symbol"),
                    "name": info.get("name"),
                    "quantity": str(holding.quantity),
                    "invested": str(holding.invested),
                    "reason": "No price on or before this date within the staleness window.",
                }
            )
            continue
        value = (holding.quantity * price).quantize(Decimal("0.01"))
        total_value += value
        total_invested += holding.invested
        positions.append(
            {
                "isin": holding.isin,
                "symbol": info.get("symbol"),
                "name": info.get("name") or info.get("symbol"),
                "quantity": str(holding.quantity),
                "invested": str(holding.invested),
                "avg_cost": str(holding.avg_cost),
                "last_price": str(price),
                "market_value": value,
                "unrealised": value - holding.invested,
                "sector": info.get("sector"),
                "market_cap_bucket": info.get("market_cap_bucket"),
                "asset_class": securities.describe(meta, holding.isin)["asset_class"],
                "_holding": holding,
            }
        )

    for position in positions:
        position["weight"] = _pct(position["market_value"], total_value)
        invested = Decimal(position["invested"])
        position["unrealised_pct"] = (
            float(position["unrealised"] / invested * 100) if invested else None
        )

    weights = {p["isin"]: p["weight"] for p in positions}
    labels = {p["isin"]: (p["symbol"] or p["isin"]) for p in positions}

    # Each window selects fully observed holdings before alignment. Missing history
    # excludes that holding from that window, never everybody else's dates.
    observed_dates = sorted({d for i in weights for d, _ in history.get(i, [])})
    bench_by_date = _benchmark_levels(connection, benchmark, observed_dates)
    covered_windows = windows.portfolio_windows(
        history, weights, bench_by_date, risk_free=lambda a, b: annualised_rate(connection, a, b)
    )
    risk_windows = {label: window.stats for label, window in covered_windows.items()}
    display_window = covered_windows.get("1y") or covered_windows.get("available")
    shared_dates = display_window.dates if display_window else []
    aligned = display_window.aligned if display_window else {}
    portfolio_levels = display_window.levels if display_window else []
    bench_levels = display_window.benchmark_levels if display_window else None
    window_values = {
        label: sum(
            (p["market_value"] for p in positions if p["isin"] in window.aligned), Decimal(0)
        )
        for label, window in covered_windows.items()
    }
    risk_coverage = {
        label: {
            "start": window.dates[0],
            "end": window.dates[-1],
            "observations": window.stats.observations,
            "weight_pct": _pct(window_values[label], total_value),
            "market_value": _money(window_values[label]),
            "included_isins": list(window.aligned),
            "excluded": [
                {
                    "isin": i,
                    "symbol": labels[i],
                    "reason": "Missing price history within this window.",
                }
                for i in window.excluded
            ],
            "basis": "constant_weight_simulation",
        }
        for label, window in covered_windows.items()
    }

    # --- capital state, trend, per-holding returns ---------------------------------------
    buckets = dict.fromkeys(("STRONG", "WEAKENING", "CAUTION", "RECOVERY", "UNKNOWN"), Decimal(0))
    for position in positions:
        values = [float(c) for _, c in history.get(position["isin"], [])]
        dma50 = composition.moving_average(values, 50) if values else None
        direction = composition.trend(float(position["last_price"]), dma50)
        state = composition.capital_state(position["unrealised"] >= 0, direction)
        position["trend"] = direction
        position["capital_state"] = state
        buckets[state] += position["market_value"]
        position["return_windows"] = advisor.holding_returns(history.get(position["isin"], []), on)
        position["returns"] = {
            key: row["return_pct"] for key, row in position["return_windows"].items()
        }
        position["momentum_12_1"] = advisor.price_momentum(history.get(position["isin"], []), on)
        position["momentum_basis"] = (
            "12-calendar-month to 1-calendar-month price return; excludes cash dividends"
        )

    # --- fundamentals on to positions ---------------------------------------------------
    for position in positions:
        row = fundamentals.get(position["isin"])
        if row is None:
            continue
        for field in (
            "pe",
            "pb",
            "roe",
            "net_margin",
            "debt_to_equity",
            "interest_cover",
            "dividend_yield",
            "sales_growth_1y",
            "profit_growth_1y",
        ):
            position[field] = _num(getattr(row, field))
        position["roe_basis"] = row.roe_basis
        position["fundamentals_as_of"] = row.as_of
        position["fundamentals_source"] = row.source
        position["fiscal_period"] = row.fiscal_period
        position["fundamentals_basis"] = (
            "Vendor trailing P/E; ROE and balance-sheet periods may differ"
        )

    earnings_total = sum(p["weight"] / p["pe"] for p in positions if p.get("pe") and p["pe"] > 0)
    for position in positions:
        position["input_notes"] = []
        source = position.get("fundamentals_source", "") or ""
        if source.endswith(":investor_confirmed"):
            position["input_notes"].append(
                "Vendor ratios used because you confirmed the vendor's company is this one."
            )
        elif "identity_mismatch" in source or "identity_unverified" in source:
            position["input_notes"].append(
                "Fundamentals withheld: vendor company name differs; confirm it on Analytics."
            )
        elif source == "YFINANCE":
            position["input_notes"].append(
                "Legacy vendor ratios: company identity not independently verified."
            )
        for field, label in (
            ("pe", "P/E"),
            ("pb", "P/B"),
            ("roe", "ROE"),
            ("debt_to_equity", "debt/equity"),
            ("momentum_12_1", "12-1 history"),
        ):
            if position.get(field) is None:
                position["input_notes"].append(f"No {label} available")
        if position.get("pe") is not None and 0 < position["pe"] < 5:
            position["input_notes"].append(
                "P/E below 5: check earnings period and exceptional items"
            )
        position["earnings_yield_contribution_pct"] = (
            position["weight"] / position["pe"] / earnings_total * 100
            if position.get("pe") and position["pe"] > 0 and earnings_total
            else None
        )

    index_valuation = latest_index_valuation(connection, benchmark, on)

    performance = build_performance(
        connection,
        investor_id,
        transactions,
        prices,
        on,
        benchmark,
        advised_only,
        {s.isin for s in skipped} | {p["isin"] for p in unresolved},
    )

    factor_scores = _factor_scores(
        connection, positions, weights, universe, on, history, overrides=confirmed
    )
    for position in positions:
        position["factors"] = {
            key: factor_scores["holdings"].get(key, {}).get(position["isin"])
            for key in _FACTOR_KEYS
        }

    # --- aggregates ---------------------------------------------------------------------
    exclusions = unresolved + [{"isin": s.isin, "reason": s.message} for s in skipped]
    candidate = integration.materialize(positions, exclusions, on)
    candidate["active_method"] = calculation_method
    candidate["differences"] = []
    pe_agg = fagg.harmonic_mean([(p["weight"], p.get("pe")) for p in positions])
    pb_agg = fagg.harmonic_mean([(p["weight"], p.get("pb")) for p in positions])
    dy_agg = fagg.weighted_mean([(p["weight"], p.get("dividend_yield")) for p in positions])

    one_year = risk_windows.get("1y")
    actual_one_year = performance.get("windows", {}).get("1y", {})
    relative_pp = (
        float(actual_one_year["relative_pp"])
        if actual_one_year.get("relative_pp") is not None
        else None
    )

    bs_pairs = [
        (p["weight"], verdict.balance_sheet_score(p.get("debt_to_equity"), p.get("interest_cover")))
        for p in positions
    ]
    bs_used = [(w, s) for w, s in bs_pairs if s is not None]
    bs_weight = sum(w for w, _ in bs_used)
    balance_sheet = sum(w * s for w, s in bs_used) / bs_weight if bs_weight else None

    # Computed ONCE, here. It feeds both the health grade and the `derived` block, and the
    # frontend reads it rather than recomputing its own -- see DerivedOut.
    effective_n = composition.effective_holdings([p["weight"] for p in positions])
    old_effective_n = effective_n
    # A priced subtotal cannot establish full-book concentration, in either method.
    if exclusions or calculation_method == "advisor_v1":
        value = candidate["metrics"]["effective_holdings"]["value"]
        effective_n = float(value) if value is not None else None

    for key, old in (
        ("effective_holdings", old_effective_n),
        ("pe", pe_agg.value),
        ("pb", pb_agg.value),
        ("dividend_yield", dy_agg.value),
    ):
        value = candidate["metrics"][key]["value"]
        new = float(value) if value is not None else None
        if (old is None) != (new is None) or (
            old is not None and new is not None and abs(old - new) > 0.0001
        ):
            candidate["differences"].append(
                {
                    "metric": key,
                    "current": old,
                    "candidate": new,
                    "reason": "scope_or_precision_difference",
                }
            )

    health = verdict.compute_health(
        factor_scores=[factor_scores["portfolio"][k].score for k in _FACTOR_KEYS],
        effective_n=effective_n,
        balance_sheet=balance_sheet,
        balance_sheet_coverage=bs_weight,
        relative_1y_pp=relative_pp,
        benchmark_coverage_pct=actual_one_year.get("coverage_pct"),
    )

    sector_alloc = composition.allocation(
        [(securities.allocation_label(p, "sector"), p["market_value"]) for p in positions]
    )
    cap_alloc = composition.allocation(
        [
            (securities.allocation_label(p, "market_cap_bucket"), p["market_value"])
            for p in positions
        ]
    )

    gains = sorted((p for p in positions if p["unrealised"] > 0), key=lambda p: -p["unrealised"])
    total_gain = sum(p["unrealised"] for p in gains)
    gain_share = (
        float(sum(p["unrealised"] for p in gains[:2]) / total_gain * 100)
        if total_gain > 0 and len(gains) >= 2
        else None
    )

    insights = verdict.build_insights(
        top_weights=[
            (labels[p["isin"]], p["weight"]) for p in sorted(positions, key=lambda p: -p["weight"])
        ],
        gain_share_top2=gain_share,
        relative_1y_pp=relative_pp,
        beta=one_year.beta if one_year else None,
        quality_leaders=[labels[p["isin"]] for p in positions if (p.get("roe") or 0) > 0.30],
        top_sector=(sector_alloc[0]["label"], sector_alloc[0]["pct"]) if sector_alloc else None,
    )

    day_change, day_change_pct = _day_change(connection, positions, on)

    # --- derived scalars ----------------------------------------------------------------
    #
    # Everything below was previously recomputed by the browser from the fields already in
    # this payload. The two implementations agreed -- see DerivedOut, which says so
    # precisely -- but they agreed because of a precondition neither one stated. Computing
    # it once here is what stops that being load-bearing.
    growth = _growth_payload(shared_dates, portfolio_levels, bench_levels)
    growth_delta = None
    if growth and growth.get("benchmark"):
        growth_delta = _money(Decimal(growth["portfolio"][-1]) - Decimal(growth["benchmark"][-1]))

    # `bench_volatility` is falsy-checked rather than None-checked on purpose: a zero
    # benchmark volatility is not a 1.0 ratio, it is an unusable denominator.
    vol_ratio = None
    if one_year and one_year.volatility is not None and one_year.bench_volatility:
        vol_ratio = round(one_year.volatility / one_year.bench_volatility, 4)

    return {
        "as_of": on,
        "generated_at": datetime.now(UTC),
        "benchmark": benchmark,
        "coverage": _coverage(positions, total_value, history, shared_dates, len(exclusions)),
        "integration": candidate,
        "sources": {
            "quantity_and_cost": "Trackr+ ledger, replayed by app.engine.holdings (FIFO)",
            "prices": "NSE/BSE bhavcopy; verified split/bonus-adjusted price-return series",
            "fundamentals": "Yahoo Finance via yfinance (unofficial endpoint), cached",
            "market_cap_bucket": "AMFI half-yearly categorisation, ISIN-keyed",
            "sector": "NSE Nifty Total Market list; BSE scrip classification for names outside "
            "it (same SEBI/AMFI harmonised sector level), ISIN-keyed",
            "benchmark": f"{benchmark} total-return index",
            "risk_free_rate": "Nifty 1D Rate Index (overnight TREPS, NSE Indices), annualised "
            "over each risk window; Sharpe and Sortino are withheld where it is missing",
        },
        "kpi": {
            "invested": _money(total_invested),
            "market_value": _money(total_value),
            "unrealised": _money(total_value - total_invested),
            "unrealised_pct": (
                _pct(total_value - total_invested, total_invested) if total_invested else None
            ),
            "day_change": _money(day_change),
            "day_change_pct": day_change_pct,
        },
        "health": {
            "score": health.score,
            "grade": health.grade,
            "components": [
                {
                    "key": c.key,
                    "label": c.label,
                    "weight": c.weight,
                    "score": c.score,
                    "points": c.points,
                    "evidence": c.evidence,
                }
                for c in health.components
            ],
            "drivers": health.drivers,
            "bands": [list(b) for b in health.bands],
            "rule_version": health.rule_version,
        },
        "factors": {
            key: {
                "score": factor_scores["portfolio"][key].score,
                "universe": factor_scores["portfolio"][key].universe,
                "universe_size": factor_scores["portfolio"][key].universe_size,
                "coverage_pct": factor_scores["portfolio"][key].coverage_pct,
            }
            for key in _FACTOR_KEYS
        },
        "performance": performance,
        "characteristics": advisor.fundamental_summary(positions),
        "concentration": advisor.concentration(positions),
        "price_events": advisor.price_events(
            history,
            positions,
            since=min((w.dates[0] for w in covered_windows.values()), default=None),
            actions=_actions_by_isin(connection, list(weights)),
        ),
        "risk_contributions": {
            label: [
                {"isin": i, "symbol": labels[i], "weight_pct": weights[i], "risk_share_pct": share}
                for i, share in sorted(
                    advisor.risk_contributions(window.aligned, weights).items(),
                    key=lambda item: -weights[item[0]],
                )
            ]
            for label, window in covered_windows.items()
        },
        "risk_coverage": risk_coverage,
        "risk": {
            label: {
                **asdict(window),
                "drawdown_dates": advisor.drawdown_dates(
                    covered_windows[label].dates, covered_windows[label].levels
                ),
                "drawdown_series": advisor.drawdown_series(
                    covered_windows[label].dates, covered_windows[label].levels
                ),
                "return_histogram": advisor.return_histogram(covered_windows[label].levels),
                "volatility_1d_value": _money(
                    window_values[label] * Decimal(str(window.volatility / (252**0.5)))
                )
                if window.volatility is not None
                else None,
                "cvar_1d_95_value": _money(window_values[label] * Decimal(str(window.cvar_1d_95)))
                if window.cvar_1d_95 is not None
                else None,
                "var_1d_95_value": _money(
                    (window_values[label] * Decimal(str(window.var_1d_95))).quantize(
                        Decimal("0.01")
                    )
                )
                if window.var_1d_95 is not None
                else None,
            }
            for label, window in risk_windows.items()
        },
        "allocation": {
            "sector": [{**r, "value": _money(r["value"])} for r in sector_alloc],
            "market_cap": [{**r, "value": _money(r["value"])} for r in cap_alloc],
        },
        "correlation": _correlation_payload(aligned, labels),
        "fundamentals": {
            **{
                key: integration.aggregate_projection(
                    candidate["metrics"][key],
                    100.0
                    - sum(
                        p["weight"]
                        for p in positions
                        if p["isin"] not in candidate["metrics"][key]["coverage"]["excluded_isins"]
                    ),
                )
                if calculation_method == "advisor_v1"
                else {"value": agg.value, "excluded_weight_pct": agg.excluded_weight_pct}
                for key, agg in (("pe", pe_agg), ("pb", pb_agg), ("dividend_yield", dy_agg))
            },
            **index_valuation,
            "benchmark_comparison": "reported_separately_incompatible_aggregation",
        },
        "capital_state": {k: _money(v) for k, v in buckets.items()},
        "derived": {
            "effective_holdings": effective_n,
            "concentration_target": verdict.CONCENTRATION_TARGET,
            "position_count": len(positions),
            "vol_ratio_1y": vol_ratio,
            "growth_delta_vs_benchmark": growth_delta,
            "capital_state_pct": {k: _pct(v, total_value) for k, v in buckets.items()},
            "capital_state_total": _money(sum(buckets.values(), Decimal(0))),
            "lenses": integration.lens_projection(candidate, positions)
            if calculation_method == "advisor_v1"
            else _lens_payload(positions, index_valuation, benchmark),
        },
        "growth_of_100k": growth,
        "positions": [_position_payload(p) for p in sorted(positions, key=lambda p: -p["weight"])],
        "insights": [asdict(i) for i in insights],
        "unresolved": unresolved
        + [
            {
                "isin": s.isin,
                "symbol": meta.get(s.isin, {}).get("symbol"),
                "name": meta.get(s.isin, {}).get("name"),
                "quantity": "0",
                "invested": "0",
                "reason": s.message,
            }
            for s in skipped
        ],
    }


def _actions_by_isin(connection: Any, isins: list[str]) -> dict[str, list]:
    """Recorded corporate actions per held ISIN, to explain a large price move by name."""
    out: dict[str, list] = {}
    if not isins:
        return out
    for action in list_corporate_actions(connection, isins):
        kind = getattr(action.action_type, "value", action.action_type)
        out.setdefault(action.isin, []).append((kind, action.ex_date))
    return out


def _lens_payload(
    positions: list[dict[str, Any]],
    index_valuation: dict[str, Any] | None = None,
    benchmark: str = "",
) -> dict[str, Any]:
    """Neutral, money-weighted comparisons; a two-input lens requires both inputs.

    The valuation lens is referenced to the index publisher's own P/E and P/B when they are
    cached, as a reported reference. Its inclusion of losses differs from the positive-only
    portfolio harmonic diagnostic; it does not establish a comparable valuation spread.
    The advisor_v1 projection uses eligible-holdings means instead. Other legacy lenses
    have no published index equivalent and stay within-portfolio.
    """
    published = (index_valuation or {}).get("benchmark") or {}
    return {
        key: advisor.lens_summary(
            positions,
            key,
            reference=published if key == "valuation" else None,
            reference_label=_index_label(benchmark),
        )
        for key in fagg.LENSES
    }


def _index_label(benchmark: str) -> str:
    """NIFTY50_TRI -> "Nifty 50": the constituents, without the total-return suffix."""
    from app.prices.index_sources import TRI_INDICES

    index = TRI_INDICES.get(benchmark)
    return index.request_name if index else benchmark


def _position_payload(position: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in position.items() if not k.startswith("_")}
    for field in ("market_value", "unrealised"):
        out[field] = _money(out.get(field))
    return out


def _window_return(values: list[float], days: int) -> float | None:
    if len(values) <= days or values[-1 - days] <= 0:
        return None
    return (values[-1] / values[-1 - days] - 1.0) * 100.0


def _benchmark_levels(connection: Any, benchmark: str, dates: list[date]) -> dict[date, Any] | None:
    """Whatever index levels exist for ``dates``. The CALLER intersects.

    Returns the mapping rather than a list so the caller can restrict the portfolio series
    to the same dates. Never carries a level forward into a gap: an index level invented
    for a missing day would put a fabricated benchmark return into a beta.
    """
    if not dates:
        return None
    try:
        levels = get_index_levels(connection, benchmark, dates, max_staleness_days=0)
    except Exception:
        logger.info("benchmark levels unavailable for %s", benchmark, exc_info=True)
        return None
    return levels or None


def _correlation_payload(aligned: dict[str, list[float]], labels: dict[str, str]) -> dict[str, Any]:
    if len(aligned) < 2:
        return {"symbols": [], "matrix": [], "average": None, "tightest": None, "loosest": None}
    matrix = composition.correlation_matrix(aligned, labels)
    return {
        "symbols": matrix.symbols,
        "matrix": matrix.matrix,
        "average": matrix.average,
        "tightest": list(matrix.tightest) if matrix.tightest else None,
        "loosest": list(matrix.loosest) if matrix.loosest else None,
    }


def _growth_payload(
    dates: list[date], levels: list[float], bench: list[float] | None
) -> dict[str, Any] | None:
    """Growth of Rs 1,00,000 over the trailing year, as strings."""
    if len(levels) < 30 or len(dates) != len(levels):
        return None
    window = min(len(levels), 253)
    tail_dates = dates[-window:]
    tail = levels[-window:]
    base = tail[0] or 1.0
    portfolio = [str(Decimal(str(100000 * v / base)).quantize(Decimal("0.01"))) for v in tail]
    benchmark = None
    if bench and len(bench) >= window:
        bt = bench[-window:]
        bbase = bt[0] or 1.0
        benchmark = [str(Decimal(str(100000 * v / bbase)).quantize(Decimal("0.01"))) for v in bt]
    return {
        "dates": tail_dates,
        "portfolio": portfolio,
        "benchmark": benchmark,
        "basis": "constant_weight_simulation",
    }


def _day_change(
    connection: Any, positions: list[dict[str, Any]], on: date
) -> tuple[Decimal | None, float | None]:
    """Today's move, from the previous stored close. None when there is no previous close."""
    if not positions:
        return None, None
    isins = [p["isin"] for p in positions]
    # The day BEFORE the one the current prices actually came from -- not `on` minus a
    # calendar day. On a holiday, a weekend, or before the day's bhavcopy settles, `on`
    # and `on - 1` both resolve to the same stored close and the move reads as a flat
    # zero, which is a different claim from "the market did not move".
    latest_stored = _latest_price_date(connection, isins, on)
    if latest_stored is None:
        return None, None
    previous = latest_prices(connection, isins, latest_stored - timedelta(days=1))
    change = Decimal(0)
    base = Decimal(0)
    for position in positions:
        prior = previous.get(position["isin"])
        if prior is None:
            continue
        quantity = Decimal(position["quantity"])
        change += (Decimal(position["last_price"]) - prior) * quantity
        base += prior * quantity
    if base <= 0:
        return None, None
    return change.quantize(Decimal("0.01")), float(change / base * 100)


def _latest_price_date(connection: Any, isins: list[str], on: date) -> date | None:
    """The newest date on or before ``on`` that any of ``isins`` has a stored close for."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT max(price_date) FROM price WHERE isin = ANY(%s) AND price_date <= %s",
            (list(isins), on),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _coverage(
    positions: list[dict[str, Any]],
    total_value: Decimal,
    history: dict[str, Any],
    shared_dates: list[date],
    excluded_count: int = 0,
) -> dict[str, Any]:
    def weight_with(field: str) -> float:
        return round(sum(p["weight"] for p in positions if p.get(field) is not None), 1)

    derived = round(
        sum(p["weight"] for p in positions if p.get("roe_basis") == "derived_statements"), 1
    )
    with_history = round(sum(p["weight"] for p in positions if history.get(p["isin"])), 1)
    return {
        "priced_pct": None if excluded_count else (100.0 if positions else 0.0),
        "basis": "priced_subset" if excluded_count else "complete_priced_book",
        "priced_count_pct": 100.0 * len(positions) / (len(positions) + excluded_count)
        if positions or excluded_count
        else 0.0,
        "priced_count": len(positions),
        "total_count": len(positions) + excluded_count,
        "known_value_subtotal": _money(total_value),
        "pe_pct": weight_with("pe"),
        "pb_pct": weight_with("pb"),
        "roe_pct": weight_with("roe"),
        "debt_to_equity_pct": weight_with("debt_to_equity"),
        "interest_cover_pct": weight_with("interest_cover"),
        "sector_pct": weight_with("sector"),
        "roe_derived_weight_pct": derived,
        "price_history_pct": with_history,
    }


def _factor_scores(
    connection: Any,
    positions: list[dict[str, Any]],
    weights: dict[str, float],
    universe: str,
    on: date,
    history: dict[str, Any],
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Rank holdings against the stored universe, falling back to the holdings themselves.

    The fallback is NOT silent: ``universe`` in the response says which population was
    used, so a score ranked against fifteen holdings cannot be mistaken for a market
    percentile.
    """
    members = current_universe_isins(connection, universe)
    population = sorted({*members, *(p["isin"] for p in positions)})
    label = universe if members else f"HOLDINGS_{len(population)}"

    start = on - timedelta(days=_LOOKBACK_DAYS)
    pop_history = price_series(connection, population, start, on) if population else {}
    pop_history = {**pop_history, **history}
    pop_fundamentals = latest_fundamentals(connection, population, on) if population else {}
    pop_fundamentals.update(overrides or {})

    momentum_raw: dict[str, float] = {}
    lowvol_raw: dict[str, float] = {}
    for isin in population:
        values = [float(c) for _, c in pop_history.get(isin, [])]
        if len(values) < 60:
            continue
        rows = pop_history.get(isin, [])
        recent = [float(c) for d, c in rows if d >= on - timedelta(days=366)]
        sd_momentum = series.stdev(series.daily_returns(recent)) if len(recent) >= 239 else None
        raw_momentum = advisor.price_momentum(rows, on)
        mom = raw_momentum / sd_momentum if raw_momentum is not None and sd_momentum else None
        if mom is not None:
            momentum_raw[isin] = mom
        sd = series.stdev(series.daily_returns(recent)) if len(recent) >= 239 else None
        if sd:
            lowvol_raw[isin] = -sd

    earnings_yield: dict[str, float] = {}
    book_to_price: dict[str, float] = {}
    roe_map: dict[str, float] = {}
    margin_map: dict[str, float] = {}
    leverage_map: dict[str, float] = {}
    for isin, row in pop_fundamentals.items():
        if row.pe and row.pe > 0:
            earnings_yield[isin] = 1.0 / float(row.pe)
        if row.pb and row.pb > 0:
            book_to_price[isin] = 1.0 / float(row.pb)
        if row.roe is not None:
            roe_map[isin] = float(row.roe)
        if row.net_margin is not None:
            margin_map[isin] = float(row.net_margin)
        if row.debt_to_equity is not None:
            leverage_map[isin] = -float(row.debt_to_equity)

    reference = set(members) if members else set(population)

    def standardize(mapping):
        return factors.reference_zscore(mapping, reference)

    # Require the same component set per holding; averaging arbitrary missing parts
    # would compare two different factors under one name.
    def complete_composite(*maps):
        complete = set.intersection(*(set(m) for m in maps))
        return {i: sum(m[i] for m in maps) / len(maps) for i in complete}

    raw = {
        "momentum": momentum_raw,
        "value": complete_composite(standardize(earnings_yield), standardize(book_to_price)),
        "quality": complete_composite(
            standardize(roe_map), standardize(margin_map), standardize(leverage_map)
        ),
        "low_volatility": lowvol_raw,
    }
    holding_scores = {}
    portfolio = {}
    for key, values in raw.items():
        scores, valid_peers = factors.reference_percentiles(values, reference)
        holding_scores[key] = scores
        reading = factors.score_portfolio(scores, weights, label, valid_peers)
        # Within-book ranks never enter the portfolio health input.
        portfolio[key] = factors.FactorScore(
            reading.score if members else None,
            reading.universe,
            valid_peers,
            reading.coverage_pct,
        )
    return {"holdings": holding_scores, "portfolio": portfolio}


# --- ops ---------------------------------------------------------------------------------


def _refresh_fundamentals(connection: Any, limit: int) -> dict[str, Any]:
    """Refresh the stalest slice of held + universe ISINs. Never raises."""
    try:
        # Keep the ranking universe on NSE's current Nifty 500 list (rebalanced
        # semi-annually); an unchanged list changes nothing.
        from app.db.universe import sync_index_universe
        from app.prices.sector_sources import nse_nifty500_source, parse_nse_sector_list

        sync_index_universe(
            connection,
            DEFAULT_UNIVERSE,
            parse_nse_sector_list(nse_nifty500_source().fetch()),
            date.today(),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        logger.warning("universe sync failed", exc_info=True)
    candidates = sorted({*held_isins(connection), *current_universe_isins(connection)})
    meta = securities.lookup(connection, candidates) if candidates else {}
    symbols = {i: m.get("symbol") for i, m in meta.items() if m.get("symbol")}
    exchanges = {i: m.get("exchange") for i, m in meta.items()}
    # A NULL listing would default to NSE; use the exchange whose bhavcopy priced it.
    observed = observed_listing(
        connection,
        [i for i in candidates if not exchanges.get(i)],
        date.today() - timedelta(days=400),
    )
    exchanges = {i: exchanges.get(i) or observed.get(i) for i in candidates}
    names = {i: m.get("name") for i, m in meta.items()}
    result = refresh_stale(connection, candidates, symbols, exchanges, limit=limit, names=names)
    # Commit before the optional refreshes below: their failure handlers roll back, and
    # must not take the fundamentals just stored with them.
    connection.commit()
    from app.db.index_valuation import upsert as store_valuation
    from app.db.risk_free import upsert as store_rate
    from app.prices.index_sources import TRI_INDICES
    from app.prices.index_valuation import fetch as fetch_valuation
    from app.prices.risk_free import fetch as fetch_rate

    try:
        from app.db.sectors import refresh_sectors

        refresh_sectors(connection, candidates)
    except Exception:
        connection.rollback()
        logger.warning("sector refresh failed", exc_info=True)
    try:
        store_rate(connection, fetch_rate(date.today() - timedelta(days=14), date.today()))
    except Exception:
        logger.warning("risk-free rate refresh failed", exc_info=True)
    for index in TRI_INDICES:
        try:
            store_valuation(
                connection, fetch_valuation(index, date.today() - timedelta(days=7), date.today())
            )
        except Exception:
            logger.warning("index valuation refresh failed for %s", index, exc_info=True)
    return result


@router.post(
    "/ops/fundamentals/refresh",
    response_model=FundamentalsRefreshResponse,
    dependencies=[Depends(require_ops_key)],
)
def refresh_fundamentals(
    limit: int = Query(DEFAULT_BATCH_LIMIT, ge=1, le=500),
    connection: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Gated by ``X-Ops-Key``: this mutates shared reference data, not one caller's view."""
    return _refresh_fundamentals(connection, limit)


@router.get(
    "/ops/cron/fundamentals-refresh",
    response_model=FundamentalsRefreshResponse,
    dependencies=[Depends(require_cron_secret)],
)
def refresh_fundamentals_cron(
    connection: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """The Vercel-Cron counterpart: GET, no query params, fixed batch size."""
    return _refresh_fundamentals(connection, DEFAULT_BATCH_LIMIT)


@router.get(
    "/investors/{investor_id}/identity-reviews",
    response_model=IdentityReviewsResponse,
    dependencies=[Depends(require_owned_investor)],
)
def identity_reviews(
    investor_id: str,
    advised_only: bool = False,
    connection: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Holdings whose vendor ratios are withheld because the vendor names a different
    company, with this investor's answer. Asked, never silently dropped."""
    transactions = load_transactions(connection, investor_id, advised_only=advised_only)
    holdings, _ = compute_holdings_partitioned(transactions, date.today())
    items = identity_review.reviews_for(connection, investor_id, [h.isin for h in holdings])
    return {"items": sorted(items, key=lambda r: r["symbol"] or r["isin"])}


@router.post(
    "/investors/{investor_id}/identity-reviews/decide",
    dependencies=[Depends(require_owned_investor)],
)
def decide_identity_review(
    investor_id: str,
    body: IdentityDecisionIn,
    connection: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Record SAME (use the vendor's ratios in this investor's analytics) or DIFFERENT
    (keep them withheld). Refuses (404) a question that does not exist."""
    if not identity_review.decide(
        connection,
        investor_id,
        body.isin,
        body.provider,
        body.ticker,
        body.vendor_name,
        body.decision,
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no open identity question matches")
    connection.commit()
    return {"recorded": body.decision}
