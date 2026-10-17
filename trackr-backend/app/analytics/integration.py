"""Read-only adapter for the pinned advisor cross-sectional kernels.

Monetary inputs come from the engine, without float conversion. Ratio inputs retain
the current analytics float boundary. This adapter never derives transactions,
acquisition history, investor returns or a suitability verdict.
"""

from datetime import date
from decimal import Decimal

from app.analytics._vendor.advisor import cross_section
from app.analytics._vendor.advisor.core import encode

METHOD = "advisor_cross_section_v1"
COMPANY_FIELDS = (
    "pe",
    "pb",
    "dividend_yield",
    "roe",
    "net_margin",
    "debt_to_equity",
    "interest_cover",
    "profit_growth_1y",
    "sales_growth_1y",
)


def is_company(row: dict) -> bool:
    """Fund wrappers never inherit company ratios, even when classified as EQ."""
    return not row["isin"].startswith("INF") and row.get("asset_class", "EQ") == "EQ"


def is_financial(row: dict) -> bool:
    return (row.get("sector") or "").casefold() == "financial services"


def materialize(positions: list[dict], excluded: list[dict], on: date) -> dict:
    """Immutable-by-copy input snapshot; no mutation of route or engine objects.

    Market-value coverage is unknown if ANY held/unreconciled position is excluded.
    Known-subtotal multiples remain useful, but cannot carry full-book weight coverage.
    """
    rows = []
    for position in positions:
        row = {
            "isin": position["isin"],
            "invested": position.get("invested"),
            "market_value": position.get("market_value"),
        }
        for field in COMPANY_FIELDS:
            value = position.get(field) if is_company(position) else None
            if field in ("debt_to_equity", "interest_cover") and is_financial(position):
                value = None
            row[field] = None if value is None else str(value)
        rows.append(row)
    rows.extend({"isin": p["isin"], "market_value": None} for p in excluded)
    ov = cross_section.overview(rows)
    metrics = {
        key: ov[key]
        for key in (
            "hhi",
            "effective_holdings",
            "count_coverage",
            "value_coverage",
            "known_value_subtotal",
        )
    }
    complete = ov["value_coverage"].status == "available"
    for field in ("pe", "pb", "dividend_yield"):
        pairs = [
            (Decimal(r["market_value"]), cross_section.maybe(r[field]))
            for r in rows
            if r.get("market_value") is not None
        ]
        metric = cross_section.aggregate(pairs, harmonic=field in ("pe", "pb"))
        metric.coverage["excluded_isins"] = [
            r["isin"]
            for r in rows
            if r.get("market_value") is None
            or r.get(field) is None
            or (field in ("pe", "pb") and Decimal(r[field]) <= 0)
        ]
        if not complete:
            metric.coverage["weight_fraction"] = None
            metric.caveat("UNKNOWN_VALUE_COVERAGE")
        metrics[field] = metric
    lenses = cross_section.lenses(rows)
    for lens in lenses.values():
        for position in positions:
            if not is_company(position):
                lens["excluded"][position["isin"]] = "NOT_COMPANY_EQUITY"
    for position in positions:
        if is_company(position) and is_financial(position):
            lenses["quality"]["excluded"][position["isin"]] = "FINANCIAL_SECTOR_LEVERAGE_BASIS"
    for metric in metrics.values():
        metric.period = {"as_of": on.isoformat()}
        metric.source_refs = ["quantity_and_cost", "prices"]
        if metric.method in ("positive_weighted_harmonic_v1", "weighted_arithmetic_v1"):
            metric.source_refs.append("fundamentals")
    return {
        "method_version": METHOD,
        "source_version": "0.2.0",
        "as_of": on.isoformat(),
        "metrics": encode(metrics),
        "lenses": encode(lenses),
    }


def lens_projection(candidate: dict, positions: list[dict]) -> dict:
    """Project approved advisor geometry onto the existing six-view board DTO."""
    out = {}
    for key, lens in candidate["lenses"].items():
        statuses = {p["isin"]: p["relative_status"] for p in lens["points"]}
        labels = ("both_favourable", "mixed_or_equal", "both_unfavourable")
        out[key] = {
            "label": key.title(),
            "x_field": lens["x_field"],
            "y_field": lens["y_field"],
            "x_average": _number(lens["x_average"]),
            "y_average": _number(lens["y_average"]),
            "reference_basis": lens["method"],
            "position_statuses": {p["isin"]: statuses.get(p["isin"]) for p in positions},
            "status_counts": {
                label: sum(s == label for s in statuses.values()) for label in labels
            },
            "status_weight_pct": {
                label: sum(p["weight"] for p in positions if statuses.get(p["isin"]) == label)
                for label in labels
            },
            "unrated_weight_pct": sum(p["weight"] for p in positions if p["isin"] not in statuses),
            "plotted": len(lens["points"]),
            "plottable_pct": sum(p["weight"] for p in positions if p["isin"] in statuses),
            "excluded": lens["excluded"],
            "method_version": lens["method"],
        }
    return out


def aggregate_projection(metric: dict, subset_coverage: float) -> dict:
    return {
        "value": _number(metric["value"]),
        "excluded_weight_pct": round(max(0.0, min(100.0, subset_coverage)), 1),
        "method_version": metric["method"],
        "status": metric["status"],
        "reasons": metric["reasons"],
        "excluded_isins": metric["coverage"]["excluded_isins"],
    }


def _number(value: str | None) -> float | None:
    return float(value) if value is not None else None
