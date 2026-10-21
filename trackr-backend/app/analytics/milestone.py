"""Presentation adapter for the owner's pinned Decimal analytics package.

The package owns every analytical formula. This module projects its status-bearing
results onto existing visual cards and derives chart geometry from the same returns.
It has no database, accounting, network or application-framework dependency.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, localcontext

from app.analytics._vendor.advisor import hypothetical, periods, service, stats
from app.analytics._vendor.advisor.core import Metric, encode, precision

WINDOW_KEYS = {
    "1M": "1m",
    "3M": "3m",
    "6M": "6m",
    "1Y": "1y",
    "3Y": "3y",
    "5Y": "5y",
    "All": "available",
}
ZERO = Decimal(0)
ONE = Decimal(1)


def calculate(data: dict) -> dict:
    """Run the full, unchanged calculation package, then identify the live consumer."""
    report = service.analyse(data)
    report["integration"] = {
        "backend": True,
        "consumer_contract": "trackr-advisor-v2",
        "source_version": "0.4.0",
    }
    report["reasons"] = [
        r
        for r in report.get("reasons", [])
        if r not in ("INDEPENDENT_BACKEND_NOT_INTEGRATED", "LIVE_KALPI_PARITY_UNVERIFIED")
    ]
    # The old offline compatibility refusal describes a renderer that is no longer used
    # by this versioned consumer. Unavailable metric envelopes remain unchanged.
    report["compatibility"] = {
        "compatible": bool(report.get("sections")),
        "contract": "trackr-advisor-v2",
    }
    return report


def number(metric: dict | None) -> float | None:
    value = (metric or {}).get("value")
    return float(value) if value is not None else None


def chart_geometry(records: list[dict]) -> dict:
    """Chart-only shapes use exactly the same dated returns as the reported metrics."""
    if not records:
        return {"drawdown_series": None, "return_histogram": None, "drawdown_dates": {}}
    with localcontext() as ctx:
        ctx.prec = 34
        dates = [records[0]["start"], *[r["end"] for r in records]]
        value = peak = ONE
        values = [ZERO]
        peak_index = low_peak_index = low_index = 0
        for index, record in enumerate(records, 1):
            value *= ONE + Decimal(record["return"])
            if value > peak:
                peak, peak_index = value, index
            values.append(value / peak - ONE)
            if values[-1] < values[low_index]:
                low_index, low_peak_index = index, peak_index
        recovered = (
            next((dates[i] for i in range(low_index + 1, len(values)) if values[i] == ZERO), None)
            if values[low_index] < ZERO
            else None
        )
        returns = [Decimal(r["return"]) for r in records]
        lo, hi = min(returns), max(returns)
        if lo == hi:
            padding = max(abs(lo) / Decimal(10), Decimal("0.0001"))
            lo, hi = lo - padding, hi + padding
        # Bin count is display geometry, not a financial/sample publication threshold.
        bins = min(24, max(5, int(len(returns) ** 0.5)))
        step = (hi - lo) / bins
        counts = [0] * bins
        for value in returns:
            counts[min(bins - 1, int((value - lo) / step))] += 1
        return {
            "drawdown_series": {"dates": dates, "values": [float(v) for v in values]},
            "drawdown_dates": {
                "peak": dates[low_peak_index] if values[low_index] < ZERO else None,
                "trough": dates[low_index] if values[low_index] < ZERO else None,
                "recovered": recovered,
            },
            "return_histogram": {
                "edges": [float(lo + step * i) for i in range(bins + 1)],
                "counts": counts,
            },
        }


def project_window(window: dict, data: dict) -> dict:
    payload = window.get("value")
    metrics = payload["risk"] if payload else {}
    period = window.get("period", {})
    records = [
        r
        for r in data.get("returns", [])
        if period.get("start", "9999") <= r["start"] and r["end"] <= period.get("end", "")
    ]
    result = {
        key: number(metrics.get(key))
        for key in (
            "cum_return",
            "cagr",
            "volatility",
            "sharpe",
            "sortino",
            "beta",
            "tracking_error",
            "information_ratio",
            "var_1d_95",
            "var_1d_99",
            "cvar_1d_95",
            "max_drawdown",
        )
    }
    result.update(
        observations=len(records) if payload else 0,
        risk_free_rate=None,
        var_method="historical_r7_loss_floor_v1",
        sortino_target="matched_risk_free_interval_return",
        var_1d_95_value=metrics.get("var_1d_95_value", {}).get("value"),
        status=window["status"],
        reasons=window.get("reasons", []),
        metrics=metrics,
        period=period,
        basis=payload.get("basis") if payload else None,
    )
    result.update(chart_geometry(records) if payload else chart_geometry([]))
    if payload:
        rf_map = {
            (r["start"], r["end"]): Decimal(r["return"]) for r in data.get("risk_free_returns", [])
        }
        if records and all((r["start"], r["end"]) in rf_map for r in records):
            rf_growth = ONE
            for record in records:
                rf_growth *= ONE + rf_map[(record["start"], record["end"])]
            days = (
                date.fromisoformat(records[-1]["end"]) - date.fromisoformat(records[0]["start"])
            ).days
            result["risk_free_rate"] = (
                float(rf_growth ** (Decimal(365) / days) - ONE) if days else None
            )
        pairs = {(r["start"], r["end"]): r for r in data.get("benchmark_returns", [])}
        matched = [
            pairs[(r["start"], r["end"])] for r in records if (r["start"], r["end"]) in pairs
        ]
        # A partial index does not get a full-window comparator.
        if records and len(matched) == len(records):

            def optional(name):
                by = {(r["start"], r["end"]): r["return"] for r in data.get(name, [])}
                return (
                    [by[(r["start"], r["end"])] for r in records]
                    if all((r["start"], r["end"]) in by for r in records)
                    else None
                )

            bench = encode(
                stats.risk(
                    [r["return"] for r in matched],
                    rf=optional("risk_free_returns"),
                    target=optional("target_returns"),
                    elapsed_days=None,
                    annualise_allowed=False,
                )
            )
            result.update(
                {
                    "bench_" + key: number(bench[key])
                    for key in ("cum_return", "volatility", "sharpe")
                }
            )
        total = data.get("portfolio_value")
        if total is not None and result["cvar_1d_95"] is not None:
            result["cvar_1d_95_value"] = Metric(
                Decimal(total) * Decimal(metrics["cvar_1d_95"]["value"]), unit="INR"
            ).json()["value"]
        if total is not None and result["volatility"] is not None:
            result["volatility_1d_value"] = Metric(
                Decimal(total) * Decimal(metrics["volatility"]["value"]) / Decimal(252).sqrt(),
                unit="INR",
            ).json()["value"]
    return result


def risk_projection(report: dict, data: dict) -> dict:
    windows = report.get("sections", {}).get("performance", {}).get("windows", {})
    return {WINDOW_KEYS[name]: project_window(window, data) for name, window in windows.items()}


@precision
def illustrations(data: dict) -> dict:
    """Independent illustrations for each requested period; never fill investor history.

    A window includes every positive-value constituent and every observed session. A
    missing constituent or close makes that window unavailable, without renormalisation.
    """
    sessions = data["sessions"]
    holdings = data["holdings"]
    complete = bool(holdings) and all(r.get("market_value") is not None for r in holdings)
    total = sum((Decimal(r["market_value"]) for r in holdings), ZERO) if complete else ZERO
    weights = (
        {r["isin"]: Decimal(r["market_value"]) / total for r in holdings} if total > ZERO else {}
    )
    histories = {
        h["isin"]: {
            "levels": {p["date"]: Decimal(p["value"]) for p in h["levels"]},
            "basis": h["basis"],
            "source_refs": h["source_refs"],
            "reasons": h.get("reasons", []),
        }
        for h in data.get("asset_histories", [])
    }
    active = [i for i, w in weights.items() if w > ZERO]
    starts = [
        min(histories[i]["levels"]) for i in active if i in histories and histories[i]["levels"]
    ]
    out = {}
    for name, months in periods.WINDOWS.items():
        if not sessions or not active or len(starts) != len(active):
            out[name] = Metric.unavailable(
                "MISSING_REQUIRED_ASSET_HISTORY", unit="illustration"
            ).json()
            continue
        requested = (
            periods.subtract_months(date.fromisoformat(data["as_of"]), months).isoformat()
            if months
            else max(starts)
        )
        before = [s for s in sessions if s <= requested]
        if not before or before[-1] >= sessions[-1]:
            out[name] = Metric.unavailable(
                "INSUFFICIENT_WINDOW_HISTORY",
                unit="illustration",
                period={"requested_start": requested},
            ).json()
            continue
        start, end = before[-1], sessions[-1]
        result = hypothetical.buy_and_hold(
            {"start": start, "end": end, "basis": "split_adjusted_price"},
            weights,
            histories,
            sessions,
            data["as_of"],
            sorted({ref for row in holdings for ref in row["source_refs"]}),
            ["OBSERVED_EXCHANGE_CALENDAR"],
        )
        encoded = result.json()
        if result.value:
            records = [
                {
                    **r,
                    "return": str(r["return"]),
                    "basis": "hypothetical_initial_weight_price_return",
                    "scope": "equities",
                    "quality": "provisional",
                    "reasons": result.reasons,
                    "source_refs": result.source_refs,
                }
                for r in result.value["returns"]
            ]
            # TRI returns cannot be compared to price-only hypothetical asset returns.
            request = {
                **data,
                "returns": records,
                "benchmark_returns": data.get("price_benchmark_returns", []),
                "portfolio_value": str(total),
            }
            matched = periods.windows(
                records,
                sessions,
                data["as_of"],
                calendar=data.get("calendar_coverage"),
                benchmark=request["benchmark_returns"],
                rf=data.get("risk_free_returns", []),
                target=data.get("target_returns", []),
                portfolio_value=total,
            )
            window = encode(matched[name])
            encoded["window"] = window
            encoded["risk"] = project_window(window, request)
            encoded["risk_contributions"] = risk_shares(records, weights, histories)
        out[name] = encoded
    return out


@precision
def risk_shares(records: list[dict], weights: dict, histories: dict) -> list[dict]:
    """Euler covariance shares of the EXACT drifting-weight buy-and-hold basket.

    Component daily contribution is w_i*(L_it-L_i,t-1)/L_i0 / basket_(t-1).
    Cov(component, basket return)/Var(basket return) sums to one. This is the
    existing covariance attribution expressed on the advisor's frozen-units basis.
    """
    if not records:
        return []
    returns = [Decimal(r["return"]) for r in records]
    variable = stats.variance(returns) if len(returns) >= 30 else ZERO
    start = records[0]["start"]
    components = {i: [] for i in weights}
    for r in records:
        before = sum(
            (
                w * histories[i]["levels"][r["start"]] / histories[i]["levels"][start]
                for i, w in weights.items()
            ),
            ZERO,
        )
        for i, w in weights.items():
            levels = histories[i]["levels"]
            components[i].append(
                w * (levels[r["end"]] - levels[r["start"]]) / levels[start] / before
            )
    mean = stats.mean(returns)
    out = []
    for i, values in components.items():
        average = stats.mean(values)
        covariance = (
            sum(((x - average) * (y - mean) for x, y in zip(values, returns, strict=True)), ZERO)
            / (len(returns) - 1)
            if variable
            else None
        )
        out.append(
            {
                "isin": i,
                "weight_pct": float(weights[i] * 100),
                "risk_share_pct": float(covariance / variable * 100)
                if covariance is not None
                else None,
            }
        )
    return out


@precision
def reported_factor_diagnostics(
    rows: dict, members: list[str], weights: dict, universe: str, *, complete: bool
) -> dict:
    """Owner factor kernels on explicitly UNVALIDATED cached vendor observations.

    This is separate from validated section outputs. Publication/reporting basis is
    unknown, and no timestamp or basis is fabricated to satisfy the strict package.
    Membership and 30-eligible-peer gates still apply; no holdings-only fallback.
    """
    from app.analytics._vendor.advisor.cross_section import factor, ranks

    out = {}
    for key, fields in [
        ("value", ("pe", "pb")),
        ("quality", ("roe", "net_margin", "debt_to_equity")),
    ]:
        eligible = {
            i for i in members if all(rows.get(i, {}).get(field) is not None for field in fields)
        }
        if key == "value":
            eligible = {i for i in eligible if all(Decimal(rows[i][field]) > 0 for field in fields)}
        components = []
        for field in fields:
            values = {
                i: ONE / Decimal(rows[i][field])
                if key == "value"
                else -Decimal(rows[i][field])
                if field == "debt_to_equity"
                else Decimal(rows[i][field])
                for i in eligible
            }
            components.append(ranks(values))
        scores = (
            {i: sum((r[i] for r in components), ZERO) / len(components) for i in eligible}
            if len(eligible) >= 2
            else {}
        )
        result = factor(scores, weights, members, universe, pre_scored=True)
        if not complete:
            result["portfolio"] = Metric.unavailable(
                "UNKNOWN_VALUE_DENOMINATOR", unit="score_0_100"
            )
        result["portfolio"].caveat("REPORTING_BASIS_AND_PUBLICATION_NOT_RECORDED")
        out[key] = encode(result)
    return out
