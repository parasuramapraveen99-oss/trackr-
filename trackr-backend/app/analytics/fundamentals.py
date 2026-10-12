"""Portfolio-level fundamental aggregates and the four Valuation Map lenses.

The aggregation rule that matters, stated once: a portfolio P/E is the weighted HARMONIC
mean of holding P/Es -- equivalently, weight the EARNINGS YIELDS arithmetically and invert.
An arithmetic mean of P/Es is dominated by whichever holding is most expensive, so one
70x position drags a cheap book's headline into "expensive" on arithmetic alone.

Loss-makers have a negative or undefined P/E and cannot enter a harmonic mean at all. They
are EXCLUDED and the excluded weight is DISCLOSED, rather than quietly rated N/A inside the
average where nobody sees them leave.
"""

from __future__ import annotations

from dataclasses import dataclass

#: X -> Y pairs per lens, with the direction that counts as better. The direction badge is
#: not decoration: it removes the "is high good here?" tax on every ratio on the page.
LENSES: dict[str, dict] = {
    "valuation": {
        "label": "Valuation",
        "caption": "How cheap or expensive each holding is against the market",
        "x": ("pe", "P/E", True),
        "y": ("pb", "P/B", True),
        "statuses": ("Attractive", "Fair", "Expensive"),
    },
    "growth": {
        "label": "Growth",
        "caption": "How fast each holding is growing year on year",
        "x": ("profit_growth_1y", "Profit 1Y", False),
        "y": ("sales_growth_1y", "Sales 1Y", False),
        "statuses": ("Strong", "Avg", "Weak"),
    },
    "profitability": {
        "label": "Profitability",
        "caption": "How efficiently each business turns capital into profit",
        "x": ("roe", "ROE", False),
        "y": ("net_margin", "Net margin", False),
        "statuses": ("Efficient", "Stable", "Poor"),
    },
    "quality": {
        "label": "Quality",
        "caption": "Financial strength and balance-sheet health",
        "x": ("debt_to_equity", "Debt / equity", True),
        "y": ("interest_cover", "Interest cover", False),
        "statuses": ("Solid", "Avg", "Risky"),
    },
}


@dataclass(frozen=True, slots=True)
class Aggregate:
    value: float | None
    #: Share of portfolio weight left OUT of this aggregate, and why it is shown: an
    #: excluded weight nobody mentions is the difference between "cheap book" and "cheap
    #: book, ignoring the third of it that loses money".
    excluded_weight_pct: float


def harmonic_mean(pairs: list[tuple[float, float | None]]) -> Aggregate:
    """Weighted harmonic mean over (weight, ratio). Non-positive ratios are excluded.

    ``pairs`` carries EVERY holding, including those with a null or negative ratio, so the
    excluded weight can be computed here rather than trusted from the caller.
    """
    total_weight = sum(w for w, _ in pairs)
    usable = [(w, v) for w, v in pairs if v is not None and v > 0]
    used_weight = sum(w for w, _ in usable)
    if not usable or used_weight <= 0 or total_weight <= 0:
        return Aggregate(None, 100.0 if total_weight > 0 else 0.0)
    denominator = sum(w / v for w, v in usable)
    return Aggregate(
        value=round(used_weight / denominator, 4) if denominator else None,
        excluded_weight_pct=round(100.0 * (total_weight - used_weight) / total_weight, 1),
    )


def weighted_mean(pairs: list[tuple[float, float | None]]) -> Aggregate:
    """Weighted arithmetic mean -- correct for yields, which add, unlike ratios."""
    total_weight = sum(w for w, _ in pairs)
    usable = [(w, v) for w, v in pairs if v is not None]
    used_weight = sum(w for w, _ in usable)
    if not usable or used_weight <= 0 or total_weight <= 0:
        return Aggregate(None, 100.0 if total_weight > 0 else 0.0)
    return Aggregate(
        value=round(sum(w * v for w, v in usable) / used_weight, 4),
        excluded_weight_pct=round(100.0 * (total_weight - used_weight) / total_weight, 1),
    )


def lens_status(
    holding: dict,
    lens_key: str,
    averages: dict[str, float | None],
) -> int | None:
    """0 = best band, 1 = middle, 2 = worst. ``None`` when neither metric is known.

    Scored against the PORTFOLIO's own averages rather than an absolute threshold, so every
    row reads as a comparison. An absolute "P/E under 15 is cheap" would be a house view
    on valuation, which is exactly the recommendation line this module does not cross.
    """
    lens = LENSES[lens_key]
    hits = 0
    counted = 0
    for axis in ("x", "y"):
        field, _, lower_is_better = lens[axis]
        value = holding.get(field)
        average = averages.get(field)
        if value is None or average is None:
            continue
        counted += 1
        better = value < average if lower_is_better else value > average
        hits += 1 if better else 0
    if counted == 0:
        return None
    ratio = hits / counted
    return 0 if ratio >= 0.75 else (1 if ratio >= 0.35 else 2)


def lens_averages(holdings: list[dict], lens_key: str) -> dict[str, float | None]:
    """Plain arithmetic mean per axis across holdings that have the figure.

    Unweighted on purpose: this is the reference line a single ROW is judged against, so
    it should not move because the reader's biggest position happens to be an outlier.
    """
    lens = LENSES[lens_key]
    out: dict[str, float | None] = {}
    for axis in ("x", "y"):
        field = lens[axis][0]
        values = [h[field] for h in holdings if h.get(field) is not None]
        out[field] = round(sum(values) / len(values), 4) if values else None
    return out
