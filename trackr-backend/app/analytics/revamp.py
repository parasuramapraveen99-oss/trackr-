"""Figures the revamped Analytics page shows that the advisor payload does not carry.

Specification: ``analytics-revamp-review/analytics-revamp-with-comments.html`` (the review
page and its 20 comments) and docs/ANALYTICS_BASIC_NARRATIVE.md. Each function here is
one element of that page, named after it.

PURE. The router reads the database and passes plain data in; nothing here imports a
framework or a repository. Money is Decimal from input to output and leaves as a string;
statistics (beta, correlation, drawdown) are floats, as everywhere in ``app.analytics``.

NOTHING IS DEFAULTED. A figure that cannot be measured carries a reason code instead of a
value, and a holding left out of a figure is named. Rounding happens only in the browser;
every value here is unrounded (review comment 11: "use unrounded inputs").
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from statistics import median
from typing import Any

REVAMP_VERSION = "analytics-revamp-2026.10.3"

ZERO = Decimal(0)
HUNDRED = Decimal(100)

#: Concentration impact (comment 11): the two selectors and their defaults.
IMPACT_TOP_N = (3, 5)
IMPACT_FALLS = (Decimal("0.10"), Decimal("0.20"), Decimal("0.30"))
#: Index shock (review §6): Nifty 50 moves, each holding scaled by its measured beta.
SHOCKS = (Decimal("-0.10"), Decimal("-0.20"), Decimal("-0.30"))
#: Minimum paired daily returns before a beta or a correlation is reported. Below this a
#: single month dominates the estimate; the figure is withheld and the count shown.
MIN_PAIRED_RETURNS = 120
#: Trailing window for beta and correlation: a year of sessions (comment 16: past year).
WINDOW_SESSIONS = 252
#: Correlation matrix size (comment 15: "12 largest holdings").
MATRIX_SIZE = 12
#: Liquidity (comment 12): exit at 10% of the 20-session median traded value.
PARTICIPATION = Decimal("0.10")
LIQUIDITY_SESSIONS = 20
SLOW_DAYS = Decimal(2)
STUCK_DAYS = Decimal(5)
#: Sector bucketing (review §7): "Others" holds only sectors under this share, named.
OTHERS_BELOW_PCT = Decimal(3)
#: Valuation map weight chip turns red at this single-holding weight (review §8).
WEIGHT_CHIP_RED_PCT = 20.0


def _d(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


def _f(value: Any) -> float | None:
    d = _d(value)
    return None if d is None else float(d)


# --- header ---------------------------------------------------------------------------------


def header(
    payload: dict[str, Any], bench_day: dict[str, Any] | None, snapshot_check: dict[str, Any] | None
) -> dict[str, Any]:
    """The five KPI cards and the two facts the greeting line is built from.

    ``Overall gain`` = unrealised + realised (FIFO) + dividends received (recorded ledger
    rows), all engine-owned figures from ``advisor.sections.overview.upstream_kpis``. It is
    withheld when any input is missing rather than summed over what is there.
    """
    kpi = payload.get("kpi") or {}
    upstream = (
        ((((payload.get("advisor") or {}).get("sections") or {}).get("overview") or {})
         .get("upstream_kpis") or {}).get("metrics") or {}
    )
    unrealised = _d(kpi.get("unrealised"))
    realised = _d((upstream.get("realised_pnl") or {}).get("value"))
    dividends = _d((upstream.get("dividends_received") or {}).get("value"))
    missing = [
        name
        for name, v in (
            ("unrealised", unrealised),
            ("realised", realised),
            ("dividends", dividends),
        )
        if v is None
    ]
    overall = None if missing else unrealised + realised + dividends
    worth = _d(kpi.get("market_value"))
    day = _d(kpi.get("day_change"))
    previous = worth - day if worth is not None and day is not None else None
    xirr = ((payload.get("performance") or {}).get("windows") or {}).get("all") or {}
    return {
        "invested": kpi.get("invested"),
        "worth": kpi.get("market_value"),
        "unrealised": kpi.get("unrealised"),
        "unrealised_pct": kpi.get("unrealised_pct"),
        "realised": None if realised is None else str(realised),
        "dividends_received": None if dividends is None else str(dividends),
        "overall_gain": None if overall is None else str(overall),
        "overall_gain_missing": missing,
        "xirr_all": xirr.get("xirr"),
        "xirr_all_start": str(xirr["start"]) if xirr.get("start") else None,
        # Share of today's value the XIRR's cash flows cover. With holdings unpriced the
        # XIRR can rest on sold positions alone, so the headline must say how much it covers.
        "xirr_all_coverage_pct": xirr.get("coverage_pct"),
        "day_change": kpi.get("day_change"),
        # Percent of the PREVIOUS session's value (review §3), stated as the base.
        "day_change_pct": float(day / previous * HUNDRED) if previous and day is not None else None,
        "previous_value": None if previous is None else str(previous),
        "bench_day": bench_day,
        "day_check": snapshot_check,
        "health": {
            "score": (payload.get("health") or {}).get("score"),
            "grade": (payload.get("health") or {}).get("grade"),
            "bounds": (payload.get("health") or {}).get("score_bounds"),
        },
    }


def bench_day_change(levels: list[tuple[date, Decimal]], on: date) -> dict[str, Any]:
    """The index's own move on the last session on or before ``on``, from stored levels."""
    rows = [(d, v) for d, v in levels if d <= on]
    if len(rows) < 2:
        return {"value_pct": None, "reason": "INDEX_LEVELS_MISSING"}
    (d0, v0), (d1, v1) = rows[-2], rows[-1]
    return {
        "value_pct": float((v1 / v0 - 1) * HUNDRED),
        "date": d1.isoformat(),
        "previous_date": d0.isoformat(),
    }


def day_change_check(
    day_change: Decimal | None,
    snap_now: Decimal | None,
    snap_prev: Decimal | None,
    flows_today: Decimal | None,
) -> dict[str, Any]:
    """Cross-check the day change against the snapshot difference (review §3).

    snapshot difference - net cash put in today = the market move. Only checked on a day
    with no trades: a trade's price is not the previous close, so the two legitimately
    differ by the trade's intraday gain and no tolerance makes that a check.
    """
    if day_change is None or snap_now is None or snap_prev is None:
        return {"status": "not_checked", "reason": "SNAPSHOT_MISSING"}
    if flows_today is None:
        return {"status": "not_checked", "reason": "TRADES_ON_THIS_DATE"}
    expected = snap_now - snap_prev - flows_today
    gap = abs(expected - day_change)
    return {
        "status": "matches" if gap <= Decimal(1) else "mismatch",
        "snapshot_move": str(expected),
        "gap": str(gap),
    }


# --- risk -------------------------------------------------------------------------------------


def interval_returns(series: list[tuple[date, Decimal]], sessions: list[date]) -> dict[date, float]:
    """Session-to-session returns keyed by the interval's END session.

    An interval counts only when the security has a close on BOTH adjacent sessions of the
    exchange calendar. A gap is skipped, never bridged into a multi-day "daily" return.
    """
    closes = dict(series)
    out: dict[date, float] = {}
    for a, b in zip(sessions, sessions[1:], strict=False):
        pa, pb = closes.get(a), closes.get(b)
        if pa and pb and pa > 0:
            out[b] = float(pb / pa - 1)
    return out


def _pearson(x: list[float], y: list[float]) -> float | None:
    n = len(x)
    if n < 2:
        return None
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx == 0 or syy == 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True)) / (sxx * syy) ** 0.5


def _beta(x: list[float], m: list[float]) -> float | None:
    n = len(x)
    if n < 2:
        return None
    mx, mm = sum(x) / n, sum(m) / n
    var = sum((b - mm) ** 2 for b in m)
    if var == 0:
        return None
    return sum((a - mx) * (b - mm) for a, b in zip(x, m, strict=True)) / var


def holding_betas(
    returns: dict[str, dict[date, float]], bench: dict[date, float]
) -> dict[str, dict[str, Any]]:
    """Per-holding beta vs the benchmark TRI over matched daily returns (review §6)."""
    out = {}
    for isin, r in returns.items():
        days = sorted(set(r) & set(bench))
        if len(days) < MIN_PAIRED_RETURNS:
            out[isin] = {
                "beta": None,
                "observations": len(days),
                "reason": "TOO_FEW_PAIRED_RETURNS",
            }
            continue
        out[isin] = {
            "beta": _beta([r[d] for d in days], [bench[d] for d in days]),
            "observations": len(days),
            "start": days[0].isoformat(),
            "end": days[-1].isoformat(),
        }
    return out


def correlation_matrix(
    returns: dict[str, dict[date, float]], weights: dict[str, float], symbols: dict[str, str]
) -> dict[str, Any]:
    """Pearson correlation of matched daily returns for the largest holdings (comments 15/16).

    Each cell carries its paired-observation count and period; a pair with fewer than
    MIN_PAIRED_RETURNS shared returns is N/A with that reason, never 0. The summary is the
    value-weighted average over defined pairs (w_i * w_j, renormalised), with the share of
    portfolio value the matrix covers.
    """
    chosen = sorted((i for i in weights if i in returns), key=lambda i: -weights[i])[:MATRIX_SIZE]
    cells: list[list[dict[str, Any] | None]] = []
    num = den = 0.0
    for a in chosen:
        row = []
        for b in chosen:
            if a == b:
                row.append(None)
                continue
            days = sorted(set(returns[a]) & set(returns[b]))
            if len(days) < MIN_PAIRED_RETURNS:
                row.append(
                    {"value": None, "observations": len(days), "reason": "TOO_FEW_PAIRED_RETURNS"}
                )
                continue
            value = _pearson([returns[a][d] for d in days], [returns[b][d] for d in days])
            row.append(
                {
                    "value": value,
                    "observations": len(days),
                    "start": days[0].isoformat(),
                    "end": days[-1].isoformat(),
                }
            )
            if value is not None and chosen.index(a) < chosen.index(b):
                w = weights[a] * weights[b]
                num += w * value
                den += w
        cells.append(row)
    total = sum(weights.values())
    return {
        "isins": chosen,
        "symbols": [symbols.get(i, i) for i in chosen],
        "cells": cells,
        "weighted_average": num / den if den else None,
        "covered_weight_pct": sum(weights[i] for i in chosen) / total * 100 if total else 0.0,
        "holdings_total": len(weights),
        "method": "pearson_matched_daily_returns_1y",
        "min_paired_returns": MIN_PAIRED_RETURNS,
    }


def risk_shares(returns: dict[str, dict[date, float]], weights: dict[str, float]) -> dict[str, Any]:
    """Each holding's share of portfolio volatility (Euler): w_i (S w)_i / (w' S w).

    S is the sample covariance of matched daily returns over the trailing year, pairwise
    (each pair over the sessions both traded). Holdings with fewer than MIN_PAIRED_RETURNS
    returns are excluded and named, weights renormalise over the rest, and the covered
    share of value is stated. A non-positive portfolio variance (possible with pairwise
    estimates) withholds the result rather than printing negative shares.
    """
    usable = [i for i in weights if len(returns.get(i) or {}) >= MIN_PAIRED_RETURNS]
    excluded = [i for i in weights if i not in usable]
    wsum = sum(weights[i] for i in usable)
    if len(usable) < 2 or wsum <= 0:
        return {"shares": {}, "excluded": excluded, "reason": "TOO_FEW_HOLDINGS_WITH_HISTORY"}
    w = {i: weights[i] / wsum for i in usable}

    def cov(a: str, b: str) -> float:
        days = sorted(set(returns[a]) & set(returns[b]))
        n = len(days)
        if n < 2:
            return 0.0
        xa = [returns[a][d] for d in days]
        xb = [returns[b][d] for d in days]
        ma, mb = sum(xa) / n, sum(xb) / n
        return sum((p - ma) * (q - mb) for p, q in zip(xa, xb, strict=True)) / (n - 1)

    sigma = {a: {b: cov(a, b) for b in usable} for a in usable}
    marginal = {a: sum(sigma[a][b] * w[b] for b in usable) for a in usable}
    variance = sum(w[a] * marginal[a] for a in usable)
    if variance <= 0:
        return {"shares": {}, "excluded": excluded, "reason": "NON_POSITIVE_VARIANCE"}
    total = sum(weights.values())
    return {
        "shares": {a: w[a] * marginal[a] / variance * 100 for a in usable},
        "excluded": excluded,
        "covered_weight_pct": wsum / total * 100 if total else 0.0,
        "annual_volatility": (variance * 252) ** 0.5,
        "method": "euler_pairwise_sample_covariance_1y",
    }


def drawdown(levels: list[tuple[date, float]]) -> dict[str, Any]:
    """Maximum fall from a running peak, with the peak, trough and recovery dates."""
    if len(levels) < 2:
        return {"max_drawdown": None, "reason": "SERIES_TOO_SHORT"}
    peak_d, peak_v = levels[0]
    worst, w_peak, w_trough = 0.0, None, None
    for d, v in levels:
        if v > peak_v:
            peak_d, peak_v = d, v
        fall = v / peak_v - 1 if peak_v else 0.0
        if fall < worst:
            worst, w_peak, w_trough = fall, peak_d, d
    recovered = None
    if w_peak is not None:
        target = dict(levels)[w_peak]
        recovered = next((d for d, v in levels if d > w_trough and v >= target), None)
    return {
        "max_drawdown": worst,
        "peak": w_peak.isoformat() if w_peak else None,
        "trough": w_trough.isoformat() if w_trough else None,
        "recovered": recovered.isoformat() if recovered else None,
    }


def concentration_impact(positions: list[dict[str, Any]], total: Decimal) -> dict[str, Any]:
    """Top-N holdings x an assumed fall -> portfolio decline (comment 11).

    loss = combined current value of the selected holdings x fall; decline = loss / total
    portfolio value. Ranked by current value; fewer holdings than N uses those available
    and says so. Hypothetical: every other price is assumed unchanged.
    """
    ranked = sorted(
        (p for p in positions if _d(p.get("market_value"))), key=lambda p: -_d(p["market_value"])
    )
    out: dict[str, Any] = {"total_value": str(total), "options": {}}
    for n in IMPACT_TOP_N:
        chosen = ranked[:n]
        value = sum((_d(p["market_value"]) for p in chosen), ZERO)
        out["options"][str(n)] = {
            "requested": n,
            "count": len(chosen),
            "holdings": [
                {
                    "isin": p["isin"],
                    "symbol": p.get("symbol") or p["isin"],
                    "market_value": p["market_value"],
                    "weight_pct": float(_d(p["market_value"]) / total * HUNDRED),
                }
                for p in chosen
            ],
            "combined_value": str(value),
            "combined_weight_pct": float(value / total * HUNDRED) if total else None,
            "falls": {
                str(int(f * 100)): {
                    "loss": str(value * f),
                    "portfolio_decline_pct": float(value * f / total * HUNDRED) if total else None,
                }
                for f in IMPACT_FALLS
            },
        }
    return out


def index_shock(
    positions: list[dict[str, Any]],
    betas: dict[str, dict[str, Any]],
    portfolio_beta: float | None,
    total: Decimal,
    worst_fall: dict[str, Any],
) -> dict[str, Any]:
    """Nifty 50 down 10/20/30% x each holding's measured beta: a rough estimate (review §6).

    Holdings without a beta are listed as not estimated and excluded from the summed
    estimate, which then states how much value it covers. Alongside: the portfolio's own
    worst real fall from its recorded history.
    """
    rows = []
    covered = ZERO
    totals = {str(int(-s * 100)): ZERO for s in SHOCKS}
    for p in sorted(positions, key=lambda p: -(_d(p.get("market_value")) or ZERO)):
        mv = _d(p.get("market_value"))
        b = (betas.get(p["isin"]) or {}).get("beta")
        if mv is None:
            continue
        row = {
            "isin": p["isin"],
            "symbol": p.get("symbol") or p["isin"],
            "market_value": str(mv),
            "beta": b,
            "reason": None
            if b is not None
            else (betas.get(p["isin"]) or {}).get("reason", "NO_RETURN_HISTORY"),
        }
        if b is not None:
            covered += mv
            row["moves"] = {}
            for s in SHOCKS:
                change = mv * s * Decimal(str(b))
                row["moves"][str(int(-s * 100))] = str(change)
                totals[str(int(-s * 100))] += change
        rows.append(row)
    return {
        "rows": rows,
        "summed": {k: str(v) for k, v in totals.items()},
        "summed_pct": {k: float(v / total * HUNDRED) if total else None for k, v in totals.items()},
        "covered_weight_pct": float(covered / total * HUNDRED) if total else 0.0,
        "portfolio_beta": portfolio_beta,
        "portfolio_beta_moves_pct": {
            str(int(-s * 100)): float(s) * portfolio_beta * 100 for s in SHOCKS
        }
        if portfolio_beta is not None
        else None,
        "worst_real_fall": worst_fall,
        "basis": "estimate_beta_times_index_move",
    }


def liquidity(
    positions: list[dict[str, Any]], traded: dict[str, list[tuple[date, Decimal]]], total: Decimal
) -> dict[str, Any]:
    """Days to exit each holding selling at 10% of its 20-session median traded value.

    Stocks and ETFs only (exchange-traded). A mutual fund is listed as not exchange-traded.
    A holding with fewer than 20 traded sessions in the lookback is "not measured" with the
    count, never padded with zeros.
    """
    rows = []
    slow = ZERO
    measured = ZERO
    for p in positions:
        mv = _d(p.get("market_value"))
        if mv is None:
            continue
        base = {
            "isin": p["isin"],
            "symbol": p.get("symbol") or p["isin"],
            "market_value": str(mv),
            "weight_pct": float(mv / total * HUNDRED) if total else None,
        }
        if p["isin"].startswith("INF") and not p.get("exchange_traded", False):
            rows.append({**base, "status": "not_exchange_traded"})
            continue
        sessions = traded.get(p["isin"]) or []
        if len(sessions) < LIQUIDITY_SESSIONS:
            rows.append(
                {
                    **base,
                    "status": "not_measured",
                    "sessions": len(sessions),
                    "sessions_needed": LIQUIDITY_SESSIONS,
                }
            )
            continue
        adv = Decimal(str(median([v for _, v in sessions])))
        days = mv / (PARTICIPATION * adv)
        tier = "slow" if days > STUCK_DAYS else "watch" if days > SLOW_DAYS else "easy"
        measured += mv
        if days > STUCK_DAYS:
            slow += mv
        rows.append(
            {
                **base,
                "status": "measured",
                "median_traded_value": str(adv),
                "days_to_exit": str(days),
                "tier": tier,
                "first_session": sessions[-1][0].isoformat(),
                "last_session": sessions[0][0].isoformat(),
            }
        )
    return {
        "rows": sorted(rows, key=lambda r: -float(_d(r.get("days_to_exit")) or -1)),
        "slow_weight_pct": float(slow / total * HUNDRED) if total else None,
        "measured_weight_pct": float(measured / total * HUNDRED) if total else 0.0,
        "participation": str(PARTICIPATION),
        "sessions": LIQUIDITY_SESSIONS,
        "thresholds_days": {"watch": str(SLOW_DAYS), "slow": str(STUCK_DAYS)},
    }


# --- diversification ----------------------------------------------------------------------------


def sector_mix(rows: list[dict[str, Any]], unclassified_labels: frozenset[str]) -> dict[str, Any]:
    """Allocation rows -> named sectors, "Others" (each under 3%, listed) and Unclassified."""
    named, others, unclassified = [], [], ZERO
    for r in rows:
        pct = _d(r.get("pct")) or ZERO
        if r["label"] in unclassified_labels:
            unclassified += pct
        elif pct < OTHERS_BELOW_PCT:
            others.append({"label": r["label"], "pct": float(pct)})
        else:
            named.append({"label": r["label"], "pct": float(pct), "value": r.get("value")})
    return {
        "named": sorted(named, key=lambda r: -r["pct"]),
        "others": sorted(others, key=lambda r: -r["pct"]),
        "others_pct": float(sum((_d(o["pct"]) for o in others), ZERO)),
        "unclassified_pct": float(unclassified),
        "others_rule_pct": float(OTHERS_BELOW_PCT),
    }


# --- fundamentals ------------------------------------------------------------------------------


def _loss_making(p: dict[str, Any]) -> bool:
    nm, roe = p.get("net_margin"), p.get("roe")
    return (nm is not None and nm < 0) or (nm is None and roe is not None and roe < 0)


def fundamentals_cards(positions: list[dict[str, Any]], index: dict[str, Any]) -> dict[str, Any]:
    """P/E, P/B (value-weighted harmonic: sum w / sum w/x) and yield (weighted arithmetic).

    Loss-makers have no P/E: they are excluded and counted with their weight, separately
    from holdings whose figure is simply missing. Weights renormalise over included
    holdings; coverage is the included share of value.
    """
    total = sum((p.get("weight") or 0) for p in positions)
    out: dict[str, Any] = {
        "benchmark": index.get("benchmark") or {},
        "benchmark_as_of": index.get("benchmark_as_of"),
        "benchmark_source": index.get("benchmark_source"),
    }
    for key, harmonic in (("pe", True), ("pb", True), ("dividend_yield", False)):
        used, loss, missing = [], [], []
        for p in positions:
            w, x = p.get("weight") or 0.0, p.get(key)
            if key == "pe" and x is None and _loss_making(p):
                loss.append(p)
            elif x is None or (harmonic and x <= 0):
                (loss if harmonic and x is not None and x <= 0 else missing).append(p)
            else:
                used.append((w, x))
        wsum = sum(w for w, _ in used)
        if not used or wsum <= 0:
            value = None
        elif harmonic:
            value = wsum / sum(w / x for w, x in used)
        else:
            value = sum(w * x for w, x in used) / wsum
        out[key] = {
            "value": value,
            "method": "weighted_harmonic_mean" if harmonic else "weighted_arithmetic_mean",
            "coverage_pct": wsum / total * 100 if total else 0.0,
            "loss_making": [{"symbol": p.get("symbol"), "weight": p.get("weight")} for p in loss],
            "loss_making_weight_pct": sum(p.get("weight") or 0 for p in loss),
            "missing": [{"symbol": p.get("symbol"), "weight": p.get("weight")} for p in missing],
        }
    return out


#: The four Valuation Map lenses (review §8): (x, y, x higher-is-better, y higher-is-better,
#: status words for both-better / mixed / both-worse).
LENSES = {
    "valuation": ("pe", "pb", False, False, ("Attractive", "Fair", "Expensive")),
    "growth": ("profit_growth_1y", "sales_growth_1y", True, True, ("Strong", "Average", "Weak")),
    "profitability": ("roe", "net_margin", True, True, ("Efficient", "Stable", "Poor")),
    "quality": ("debt_to_equity", "interest_cover", False, True, ("Solid", "Average", "Risky")),
}


def market_medians(universe: list[dict[str, Any]]) -> dict[str, Any]:
    """Nifty 500 median per metric, over members with that metric (P/E, P/B positive only)."""
    out = {}
    for key in {k for lens in LENSES.values() for k in lens[:2]} | {"dividend_yield"}:
        values = [
            r[key]
            for r in universe
            if r.get(key) is not None and (key not in ("pe", "pb") or r[key] > 0)
        ]
        out[key] = {"median": median(values) if values else None, "count": len(values)}
    out["universe_size"] = len(universe)
    return out


def valuation_map(
    positions: list[dict[str, Any]], medians: dict[str, Any], screens: dict[str, str | None]
) -> dict[str, Any]:
    """Per lens: each holding's two metrics against the Nifty 500 median, and a status.

    Status combines BOTH columns: both better -> first word (green), mixed -> second
    (amber), both worse -> third (red). A holding missing either metric, or one where the
    lens does not apply, is N/A with a reason, so counts always total the holdings.
    """
    lenses = {}
    for name, (x, y, x_up, y_up, words) in LENSES.items():
        rows = []
        counts = {words[0]: 0, words[1]: 0, words[2]: 0, "N/A": 0}
        for p in positions:
            screen = screens.get(p["isin"])
            reason = None
            if name in ("quality",) and screen == "lender":
                reason = "NOT_APPLICABLE_FOR_LENDERS"
            elif name in ("quality", "profitability") and screen is None and _financial(p):
                reason = "FINANCIAL_NOT_CLASSIFIED"
            elif name == "profitability" and screen == "lender":
                reason = "LENDER_NEEDS_GNPA_NOT_INGESTED"
            elif name == "growth" and _loss_making(p):
                reason = "LOSS_MAKING_GROWTH_NOT_COMPARABLE"
            elif name == "valuation" and p.get(x) is None and _loss_making(p):
                reason = "LOSS_MAKING_NO_PE"
            xv, yv = p.get(x), p.get(y)
            mx, my = (medians.get(x) or {}).get("median"), (medians.get(y) or {}).get("median")
            if reason is None and (xv is None or yv is None):
                reason = "METRIC_MISSING"
            if reason is None and (mx is None or my is None):
                reason = "MARKET_REFERENCE_MISSING"
            status = None
            if reason is None:
                # Each figure is better, worse or level with the reference. Level is
                # neither, so a holding AT the median on one figure is judged on the other.
                sides = [_side(xv, mx, x_up), _side(yv, my, y_up)]
                if "worse" not in sides and "better" in sides:
                    status = words[0]
                elif sides == ["worse", "worse"]:
                    status = words[2]
                else:
                    status = words[1]
            counts[status or "N/A"] += 1
            rows.append(
                {
                    "isin": p["isin"],
                    "symbol": p.get("symbol"),
                    "weight": p.get("weight"),
                    "weight_flag": (p.get("weight") or 0) >= WEIGHT_CHIP_RED_PCT,
                    x: xv,
                    y: yv,
                    "status": status,
                    "reason": reason,
                }
            )
        lenses[name] = {
            "x": x,
            "y": y,
            "x_higher_is_better": x_up,
            "y_higher_is_better": y_up,
            "statuses": list(words),
            "counts": counts,
            "rows": rows,
            "reference": {
                x: (medians.get(x) or {}).get("median"),
                y: (medians.get(y) or {}).get("median"),
            },
            "reference_label": "Nifty 500 median",
        }
    return {
        "lenses": lenses,
        "universe_size": medians.get("universe_size"),
        "weight_flag_pct": WEIGHT_CHIP_RED_PCT,
    }


def _side(value: float, reference: float, higher_is_better: bool) -> str:
    if value == reference:
        return "level"
    return "better" if (value > reference) == higher_is_better else "worse"


def _financial(p: dict[str, Any]) -> bool:
    return (p.get("sector") or "").casefold() == "financial services"


# --- returns ----------------------------------------------------------------------------------


def return_reasons(
    windows: dict[str, dict[str, Any]], first_price: date | None, targets: dict[str, date]
) -> dict[str, str | None]:
    """Why each unavailable holding-return cell is empty (comment 17)."""
    out = {}
    for label, cell in windows.items():
        if cell.get("return_pct") is not None:
            out[label] = None
        elif first_price is None:
            out[label] = "HISTORICAL_PRICES_UNAVAILABLE"
        elif first_price > targets[label]:
            out[label] = "INSUFFICIENT_PRICE_HISTORY"
        else:
            out[label] = "NO_CLOSE_NEAR_WINDOW_END"
    return out


def coverage_footer(payload: dict[str, Any]) -> dict[str, Any]:
    """One line per tab: "Covers 15 of 17 holdings · 96% of value · 2 not valued: X, Y"."""
    cov = payload.get("coverage") or {}
    unresolved = payload.get("unresolved") or []
    return {
        "priced_count": cov.get("priced_count"),
        "total_count": cov.get("total_count"),
        "priced_pct": cov.get("priced_pct"),
        "basis": cov.get("basis"),
        "not_valued": [u.get("symbol") or u.get("isin") for u in unresolved],
        "as_of": str(payload.get("as_of")),
    }
