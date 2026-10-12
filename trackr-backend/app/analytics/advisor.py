"""Inspectable exposure measures, without turning relative ranks into investment advice."""

from datetime import timedelta
from statistics import median

from app.analytics import fundamentals, series
from app.engine.xirr import years_before


def concentration(positions: list[dict]) -> dict:
    ranked = sorted(positions, key=lambda p: -p["weight"])
    return {
        "top1_pct": sum(p["weight"] for p in ranked[:1]),
        "top3_pct": sum(p["weight"] for p in ranked[:3]),
        "top5_pct": sum(p["weight"] for p in ranked[:5]),
        "leaders": [
            {"isin": p["isin"], "symbol": p.get("symbol") or p["isin"], "weight_pct": p["weight"]}
            for p in ranked[:5]
        ],
    }


def weighted_median(pairs: list[tuple[float, float]]) -> float | None:
    ordered = sorted((v, w) for w, v in pairs if w > 0)
    half = sum(w for _, w in ordered) / 2
    if not ordered:
        return None
    cumulative = 0.0
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= half:
            return value
    return ordered[-1][0]


def fundamental_summary(positions: list[dict]) -> dict:
    """Market-value-weighted company characteristics, not consolidated company ratios.

    ROE and D/E here describe exposures to company ratios. Combining companies' actual
    earnings, debt and equity would require their denominators on comparable dates.
    """
    out = {}
    for field in ("roe", "interest_cover", "momentum_12_1"):
        result = fundamentals.weighted_mean([(p["weight"], p.get(field)) for p in positions])
        out[field] = {"value": result.value, "coverage_pct": 100.0 - result.excluded_weight_pct}
    # Leverage excludes financial-services companies, the convention index providers use:
    # a lender's or broker's D/E (Edelweiss 3.2x) is its funding model, not balance-sheet
    # risk, and averaging it with an operating company's 0.1x describes neither. The
    # exclusion is named, and coverage is stated against the remaining weight.
    operating = [p for p in positions if not _is_financial(p)]
    excluded = [p for p in positions if _is_financial(p)]
    result = fundamentals.weighted_mean([(p["weight"], p.get("debt_to_equity")) for p in operating])
    out["debt_to_equity"] = {
        "value": result.value,
        "coverage_pct": 100.0 - result.excluded_weight_pct if operating else 0.0,
        "note": (
            "Excludes financial-services companies: "
            + ", ".join(f"{p.get('symbol') or p['isin']}" for p in excluded)
            if excluded
            else None
        ),
    }
    usable = [(p["weight"], p["pe"]) for p in positions if p.get("pe") is not None and p["pe"] > 0]
    total = sum(p["weight"] for p in positions)
    coverage = sum(w for w, _ in usable) / total * 100 if total else 0.0
    out["median_pe"] = {
        "value": median([v for _, v in usable]) if usable else None,
        "coverage_pct": coverage,
    }
    out["weighted_median_pe"] = {"value": weighted_median(usable), "coverage_pct": coverage}
    return out


def _is_financial(position: dict) -> bool:
    return (position.get("sector") or "").casefold() == "financial services"


def lens_summary(
    positions: list[dict], key: str, reference: dict | None = None, reference_label: str = ""
) -> dict:
    """Neutral comparisons, requiring both inputs, to ``reference`` when one is given.

    ``reference`` is an external benchmark ({x_field: value, y_field: value}), e.g. the
    index publisher's own P/E and P/B. Without one, the reference is the value-weighted
    median of the holdings themselves -- a within-portfolio description, labelled as such.
    """
    lens = fundamentals.LENSES[key]
    x, y = lens["x"][0], lens["y"][0]
    usable = [
        p
        for p in positions
        if p.get(x) is not None
        and p.get(y) is not None
        and (key != "valuation" or (p[x] > 0 and p[y] > 0))
    ]
    external = bool(reference) and all(reference.get(f) is not None for f in (x, y))
    if external:
        reference = {f: reference[f] for f in (x, y)}
        labels = (
            f"Below {reference_label} on both",
            "Mixed / at index",
            f"Above {reference_label} on both",
        )
        basis = f"external:{reference_label}"
    else:
        reference = {
            field: weighted_median([(p["weight"], p[field]) for p in usable]) for field in (x, y)
        }
        labels = ("Below both medians", "Mixed / at median", "Above both medians")
        basis = "market_value_weighted_median_of_complete_pairs"
    counts = dict.fromkeys(labels, 0)
    weights = dict.fromkeys(labels, 0.0)
    statuses = dict.fromkeys(p["isin"] for p in positions)
    for p in usable:
        below = p[x] < reference[x] and p[y] < reference[y]
        above = p[x] > reference[x] and p[y] > reference[y]
        label = labels[0] if below else labels[2] if above else labels[1]
        counts[label] += 1
        weights[label] += p["weight"]
        statuses[p["isin"]] = label
    covered = sum(p["weight"] for p in usable)
    return {
        "label": lens["label"],
        "x_field": x,
        "y_field": y,
        "x_average": reference[x],
        "y_average": reference[y],
        "reference_basis": basis,
        "status_counts": counts,
        "status_weight_pct": weights,
        "position_statuses": statuses,
        "unrated_weight_pct": sum(p["weight"] for p in positions) - covered,
        "plotted": len(usable),
        "plottable_pct": covered,
    }


def risk_contributions(aligned: dict[str, list[float]], weights: dict[str, float]) -> dict:
    """Euler shares of portfolio variance/volatility: w_i Cov(r_i,r_p)/Var(r_p).

    Shares sum to one. A negative share indicates a historical diversification effect;
    it is retained rather than clipped. Uses the same constant weights and sample
    covariance as the reported current-exposure simulation.
    """
    total = sum(weights[i] for i in aligned)
    if total <= 0:
        return {}
    returns = {i: series.daily_returns(levels) for i, levels in aligned.items()}
    portfolio = series.daily_returns(series.portfolio_series(aligned, weights))
    variance = series.covariance(portfolio, portfolio)
    if not variance:
        return {}
    return {
        i: weights[i] / total * series.covariance(rows, portfolio) / variance * 100
        for i, rows in returns.items()
    }


#: NSE/BSE daily price bands. A close within 0.1pp of one is a circuit-limit session.
PRICE_BANDS = (0.05, 0.10, 0.20)


def _event_reason(isin: str, on, change: float, actions: dict) -> str:
    nearby = [
        (kind, ex_date) for kind, ex_date in actions.get(isin, []) if abs((ex_date - on).days) <= 3
    ]
    if nearby:
        kind, ex_date = nearby[0]
        return (
            f"{kind} on record with ex-date {ex_date}; only verified splits and bonuses are "
            "adjusted in the price series, so this move may be the action, not a return."
        )
    band = next((b for b in PRICE_BANDS if abs(abs(change) - b) <= 0.001), None)
    if band is not None:
        return f"Consistent with the exchange's {band:.0%} daily price band (circuit limit)."
    return (
        "No corporate action on record within three days. Check exchange prices and "
        "announcements (bonus, split, demerger) before trusting risk figures for it."
    )


def price_events(
    history: dict,
    positions: list[dict],
    threshold: float = 0.15,
    since=None,
    actions: dict | None = None,
) -> list[dict]:
    """Flag observed extreme adjusted-price moves; never assume they are data errors.

    ``since`` is the start of the longest risk window shown: a move before it enters no
    figure on the page, and listing a 2022 move of a 0.0%-weight holding beside today's
    risk buries the ones that do. Largest position first, then the largest move.
    """
    positions_by_isin = {p["isin"]: p for p in positions}
    out = []
    for isin, rows in history.items():
        position = positions_by_isin.get(isin)
        if position is None:
            continue
        for (previous_date, previous), (on, current) in zip(rows, rows[1:], strict=False):
            # A one-day move needs consecutive sessions: a quote gap of years is missing
            # history (already excluded from risk), not a price move.
            if previous <= 0 or (since is not None and on <= since):
                continue
            if (on - previous_date).days > 7:
                continue
            change = float(current / previous - 1)
            if abs(change) > threshold:
                out.append(
                    {
                        "isin": isin,
                        "symbol": position.get("symbol") or isin,
                        "weight_pct": position["weight"],
                        "date": on,
                        "previous_date": previous_date,
                        "change_pct": change * 100,
                        "threshold_pct": threshold * 100,
                        "reason": _event_reason(isin, on, change, actions or {}),
                    }
                )
    return sorted(out, key=lambda e: (-e["weight_pct"], -abs(e["change_pct"])))


def drawdown_dates(dates: list, levels: list[float]) -> dict:
    if len(levels) < 2:
        return {"peak": None, "trough": None, "recovered": None}
    peak = worst_peak = worst_trough = 0
    worst = 0.0
    for i, value in enumerate(levels):
        if value >= levels[peak]:
            peak = i
        decline = value / levels[peak] - 1
        if decline < worst:
            worst, worst_peak, worst_trough = decline, peak, i
    if worst == 0:
        return {"peak": None, "trough": None, "recovered": None}
    recovered = next(
        (dates[i] for i in range(worst_trough + 1, len(levels)) if levels[i] >= levels[worst_peak]),
        None,
    )
    return {"peak": dates[worst_peak], "trough": dates[worst_trough], "recovered": recovered}


def holding_returns(rows: list, on) -> dict:
    """Observed, action-adjusted price returns over calendar periods, never synthetic.

    Endpoint closes must fall within five calendar days of each endpoint. Missing
    intermediate closes do not invalidate a price ratio; they DO invalidate daily risk.
    Each return carries its actual quote dates. No dividends are assumed.
    """
    out = {}
    targets = {
        "1w": on - timedelta(days=7),
        "1m": on - timedelta(days=30),
        "3m": on - timedelta(days=91),
        **{f"{n}y": years_before(on, n) for n in (1, 3, 5)},
    }
    end = next(((d, p) for d, p in reversed(rows) if d <= on), None)
    for label, target in targets.items():
        start = next(((d, p) for d, p in reversed(rows) if d <= target), None)
        valid = (
            end is not None
            and start is not None
            and start[1] > 0
            and (on - end[0]).days <= 5
            and (target - start[0]).days <= 5
        )
        out[label] = {
            "start": start[0] if valid else None,
            "end": end[0] if valid else None,
            "return_pct": float(end[1] / start[1] - 1) * 100 if valid else None,
        }
    return out


def price_momentum(rows: list, on) -> float | None:
    """Plain 12-calendar-month to 1-calendar-month price return, no volatility divisor."""
    # A calendar month, including clamping at month ends.
    from calendar import monthrange

    month = on.month - 1 or 12
    year = on.year - (on.month == 1)
    skip = on.replace(year=year, month=month, day=min(on.day, monthrange(year, month)[1]))
    target = years_before(on, 1)
    start = next(((d, p) for d, p in reversed(rows) if d <= target), None)
    end = next(((d, p) for d, p in reversed(rows) if d <= skip), None)
    if (
        not start
        or not end
        or start[1] <= 0
        or (target - start[0]).days > 5
        or (skip - end[0]).days > 5
    ):
        return None
    return float(end[1] / start[1] - 1)


def drawdown_series(dates: list, levels: list[float]) -> dict:
    """Fall below the running peak at every observation: level / max(level so far) - 1.

    The same window levels the reported maximum drawdown is computed from, so the chart
    and the number cannot disagree. Values are fractions (-0.18 = 18% below peak).
    """
    out, peak = [], None
    for value in levels:
        peak = value if peak is None else max(peak, value)
        out.append(value / peak - 1 if peak else 0.0)
    return {"dates": list(dates), "values": out}


def return_histogram(levels: list[float], bins: int = 28) -> dict | None:
    """Equal-width histogram of the window's daily returns, as fractions.

    ``edges`` has ``len(counts) + 1`` entries; the last bin is closed on the right so the
    maximum is counted. None when fewer than 20 returns exist -- a histogram of a handful
    of days describes nothing.
    """
    returns = series.daily_returns(levels)
    if len(returns) < 20:
        return None
    lo, hi = min(returns), max(returns)
    width = (hi - lo) / bins or 1.0
    counts = [0] * bins
    for r in returns:
        counts[min(bins - 1, int((r - lo) / width))] += 1
    return {"edges": [lo + i * width for i in range(bins + 1)], "counts": counts}
