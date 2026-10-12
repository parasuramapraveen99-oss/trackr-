"""Diversification structure: concentration, correlation, allocation, capital state.

Weights are diversification's APPEARANCE. Correlation is its substance -- fifteen holdings
that all move together are one position wearing fifteen names, and the weight column
cannot show that. In a concentrated book the correlation matrix is the most load-bearing
thing in the whole module.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.analytics.series import correlation, daily_returns, mean

#: The single-name weight past which a position is flagged. Not a recommendation and not a
#: limit -- a factual marker against a conventional default, which the guardrail layer will
#: later replace with the user's OWN declared ceiling. Stating someone else's threshold as
#: if it were theirs is the line this product does not cross.
DEFAULT_CONCENTRATION_CEILING = 15.0

#: Deviation from the 50-day moving average that counts as a trend rather than noise.
#: Without a dead zone a holding flickers between buckets on every tick, and a bucket that
#: flickers teaches the reader to ignore it.
TREND_DEAD_ZONE_PCT = 2.0


@dataclass(frozen=True, slots=True)
class CorrelationMatrix:
    symbols: list[str]
    matrix: list[list[float | None]]
    average: float | None
    tightest: tuple[str, str, float] | None
    loosest: tuple[str, str, float] | None


def herfindahl(weights: list[float]) -> float | None:
    """Sum of squared weight shares. 1/HHI is the 'effective number of holdings'."""
    total = sum(weights)
    if total <= 0:
        return None
    return sum((w / total) ** 2 for w in weights)


def effective_holdings(weights: list[float]) -> float | None:
    hhi = herfindahl(weights)
    return (1.0 / hhi) if hhi else None


def correlation_matrix(
    aligned: dict[str, list[float]],
    labels: dict[str, str],
) -> CorrelationMatrix:
    """Pairwise correlation of DAILY RETURNS -- never of price levels.

    Two rising series correlate near 1.0 on levels whatever they actually do day to day;
    that number would be a description of the market's drift, not of these two holdings.
    """
    isins = sorted(aligned, key=lambda i: labels.get(i, i))
    returns = {i: daily_returns(aligned[i]) for i in isins}
    size = len(isins)
    matrix: list[list[float | None]] = [[None] * size for _ in range(size)]
    pairs: list[tuple[str, str, float]] = []

    for a in range(size):
        matrix[a][a] = 1.0
        for b in range(a + 1, size):
            value = correlation(returns[isins[a]], returns[isins[b]])
            matrix[a][b] = matrix[b][a] = value
            if value is not None:
                pairs.append(
                    (
                        labels.get(isins[a], isins[a]),
                        labels.get(isins[b], isins[b]),
                        value,
                    )
                )

    return CorrelationMatrix(
        symbols=[labels.get(i, i) for i in isins],
        matrix=matrix,
        average=round(mean([p[2] for p in pairs]), 4) if pairs else None,
        tightest=max(pairs, key=lambda p: p[2]) if pairs else None,
        loosest=min(pairs, key=lambda p: p[2]) if pairs else None,
    )


def allocation(
    rows: list[tuple[str | None, Decimal]],
    unclassified: str = "Unclassified",
) -> list[dict]:
    """Group market value by a label, descending. Money stays Decimal the whole way.

    An unlabelled security is shown as 'Unclassified', never folded into an 'Others'
    bucket with real categories -- a 33% Others slice makes a donut useless, and hiding
    missing classification inside it turns a data gap into a fake category.
    """
    totals: dict[str, Decimal] = {}
    for label, value in rows:
        key = label or unclassified
        totals[key] = totals.get(key, Decimal(0)) + value
    grand = sum(totals.values())
    out = [
        {
            "label": k,
            "value": v,
            "pct": float(v / grand * 100) if grand else 0.0,
        }
        for k, v in totals.items()
    ]
    return sorted(out, key=lambda r: -r["pct"])


def trend(last_close: float, dma50: float | None) -> str:
    if not dma50:
        return "unknown"
    deviation = (last_close / dma50 - 1.0) * 100.0
    if abs(deviation) <= TREND_DEAD_ZONE_PCT:
        return "flat"
    return "up" if deviation > 0 else "down"


def capital_state(in_profit: bool, direction: str) -> str:
    """The 2x2 of current P/L sign against recent trend.

    STRONG / WEAKENING / CAUTION / RECOVERY. The four buckets must sum to portfolio worth
    exactly -- every priced holding lands in exactly one, and the caller asserts the
    reconciliation rather than hoping for it.
    """
    if direction == "unknown":
        return "UNKNOWN"
    if in_profit:
        return "STRONG" if direction in ("up", "flat") else "WEAKENING"
    return "RECOVERY" if direction == "up" else "CAUTION"


def moving_average(values: list[float], window: int = 50) -> float | None:
    if len(values) < window:
        return None
    tail = values[-window:]
    return sum(tail) / len(tail)
