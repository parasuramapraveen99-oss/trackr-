"""Owned, read-only materialisation of the advisor's complete analytics contract.

This adapter reads existing sources. It never fetches, stages, promotes, migrates,
repairs, or writes the ledger. Financial accounting remains in app.engine.
"""

from __future__ import annotations

import hashlib
import json
import logging
from bisect import bisect_right
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any
from zoneinfo import ZoneInfo

from app import securities
from app.analytics import advisor, integration, milestone
from app.analytics._vendor.advisor import cross_section, stats
from app.analytics._vendor.advisor.core import Metric
from app.config import get_settings
from app.db import health_profiles, identity_review
from app.db.fundamentals import latest as latest_fundamentals
from app.db.index_levels import list_index_levels
from app.db.index_valuation import latest as latest_index_valuation
from app.db.prices import adjusted_price_series, latest_price_dates, latest_prices, price_series
from app.db.repository import load_transactions
from app.db.security_alias import verified_map
from app.db.snapshots import list_snapshots_range
from app.engine.holdings import MAX_PRICE_STALENESS_DAYS, compute_holdings_partitioned
from app.engine.snapshot import compute_snapshot
from app.engine.twrr import _period_flow
from app.engine.types import Snapshot, Transaction
from app.prices.risk_free import RATE_INDEX
from app.routers.analytics_performance import build_performance

logger = logging.getLogger(__name__)
ZERO = Decimal(0)
IST = ZoneInfo("Asia/Kolkata")
ENGINE_VERSION = "trackr-ledger-v1"


def snapshot_intervals(
    snapshots: list[Snapshot], transactions: list[Transaction], sessions: list[date]
) -> list[dict]:
    """Daily subperiods under the SAME convention as engine.compute_twrr.

    An interval needs adjacent observed market sessions, two snapshots with identical
    exclusions, and a positive opening value. It never bridges a missing snapshot or
    adds projected dividends. _period_flow is the engine's accounting authority.
    """
    by = {s.as_of: s for s in snapshots}
    out = []
    with localcontext() as ctx:
        ctx.prec = 34
        for start, end in zip(sorted(set(sessions)), sorted(set(sessions))[1:], strict=False):
            a, b = by.get(start), by.get(end)
            if not a or not b or a.market_value <= ZERO or a.excluded_isins != b.excluded_isins:
                continue
            value = (
                b.market_value
                - _period_flow([t for t in transactions if t.isin not in a.excluded_isins], a, b)
            ) / a.market_value - 1
            if value < -1:
                continue
            excluded = list(a.excluded_isins)
            reasons = ["OBSERVED_EXCHANGE_CALENDAR"] + (
                ["UPSTREAM_EXCLUDED_HOLDINGS"] if excluded else []
            )
            out.append(
                dict(
                    start=start.isoformat(),
                    end=end.isoformat(),
                    **{"return": str(value)},
                    basis="upstream_cashflow_adjusted_total_return",
                    scope="listed_equities",
                    quality="provisional",
                    reasons=reasons,
                    source_refs=["ledger", "snapshots", "calendar"],
                    excluded_isins=excluded,
                )
            )
    return out


def company_snapshots(connection, transactions, existing, start, on):
    """Revalue company-only slices using the existing snapshot engine, in memory.

    Existing cache rows can mix stocks and ETFs. No mixed cache is passed off as
    company-only performance. Raw prices retain their historical share basis; verified
    aliases coalesce identity without applying a statistical adjustment to a valuation.
    Missing prices and unreconciled segments are named and excluded, never zero-filled.
    """
    isins = {t.isin for t in transactions}
    aliases = verified_map(connection, on)
    expanded = sorted(isins | {old for old, new in aliases.items() if new in isins})
    raw = price_series(connection, expanded, start - timedelta(days=MAX_PRICE_STALENESS_DAYS), on)
    values, ambiguous = {}, set()
    for source, points in raw.items():
        canonical = aliases.get(source, source)
        if canonical not in isins:
            continue
        target = values.setdefault(canonical, {})
        for day, value in points:
            if day in target and target[day] != value:
                ambiguous.add((canonical, day))
            target[day] = value
    by_isin = {
        i: sorted((d, v) for d, v in points.items() if (i, d) not in ambiguous)
        for i, points in values.items()
    }
    date_index = {i: [d for d, _ in points] for i, points in by_isin.items()}
    snapshots = []
    for old in existing:
        day = old.as_of
        held, skipped = compute_holdings_partitioned(transactions, day)
        prices = {}
        for holding in held:
            points = by_isin.get(holding.isin, [])
            index = bisect_right(date_index.get(holding.isin, []), day) - 1
            if (
                index >= 0
                and points[index][0] >= day - timedelta(days=MAX_PRICE_STALENESS_DAYS)
                and points[index][1] > 0
            ):
                prices[holding.isin] = points[index][1]
        excluded = {s.isin for s in skipped} | {
            h.isin for h in held if h.quantity > 0 and h.isin not in prices
        }
        kept = [t for t in transactions if t.isin not in excluded]
        calculated = compute_snapshot(kept, prices, day)
        snapshots.append(replace(calculated, excluded_isins=tuple(sorted(excluded))))
    return snapshots


def _source(key: str, dataset: str, on: date, now: datetime, **fields) -> dict:
    return dict(
        id=key,
        provider=fields.pop("provider", "Trackr cached inputs"),
        origin=fields.pop("origin", "existing Trackr reference and ledger stores"),
        url=fields.pop("url", f"urn:trackr:{key}"),
        dataset=dataset,
        observed_at=datetime.combine(on, time.min, IST).isoformat(),
        retrieved_at=now.isoformat(),
        source_version="stored-input-v1",
        licence_note="Internal computation only; no new redistribution rights",
        usage_status="authorised",
        **fields,
    )


def _intervals(
    levels: dict[date, Decimal], sessions: list[date], source: str, basis: str
) -> list[dict]:
    rows = []
    for start, end in zip(sessions, sessions[1:], strict=False):
        a, b = levels.get(start), levels.get(end)
        if a is not None and b is not None and a > 0 and b > 0:
            rows.append(
                dict(
                    start=start.isoformat(),
                    end=end.isoformat(),
                    **{"return": str(b / a - 1)},
                    basis=basis,
                    scope=source,
                    source_refs=[source],
                    quality="available",
                )
            )
    return rows


def read_reference_inputs(
    connection: Any, start: date, on: date, universe: str, benchmark: str
) -> dict:
    """Bulk reads, exact dated levels, and no carried-forward daily observations."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT price_date FROM price "
            "WHERE price_date BETWEEN %s AND %s ORDER BY price_date",
            (start, on),
        )
        sessions = {r[0] for r in cur.fetchall()}
        cur.execute(
            "SELECT isin,added_on,removed_on FROM universe_member "
            "WHERE universe=%s AND added_on<=%s "
            "AND (removed_on IS NULL OR removed_on>%s) ORDER BY isin",
            (universe, on, on),
        )
        members = cur.fetchall()
        cur.execute(
            "SELECT level_date,close FROM risk_free_level "
            "WHERE index_name=%s AND level_date BETWEEN %s AND %s ORDER BY level_date",
            (RATE_INDEX, start, on),
        )
        risk_free = dict(cur.fetchall())
    benchmark_levels = dict(list_index_levels(connection, benchmark, start, on))
    sessions.update(benchmark_levels)
    return {
        "sessions": sorted(sessions),
        "members": [r[0] for r in members],
        "member_since": max((r[1] for r in members), default=None),
        "risk_free": risk_free,
        "benchmark": benchmark_levels,
    }


def materialize(
    connection: Any, investor_id: str, on: date, benchmark: str, advised_only: bool, universe: str
) -> dict:
    now = datetime.now(UTC)
    cutoff = min(now.astimezone(IST), datetime.combine(on, time.max, IST))
    start = on - timedelta(days=366 * 6)
    transactions = load_transactions(connection, investor_id, advised_only=advised_only, date_to=on)
    holdings, skipped = compute_holdings_partitioned(transactions, on)
    holdings = [h for h in holdings if h.quantity > 0]
    meta = securities.lookup(connection, [h.isin for h in holdings])
    # The advisor model is explicitly company-equity only. Fund units retain their own
    # price/NAV routes and never inherit company ratios or enter these risk totals.
    in_scope = [
        h
        for h in holdings
        if integration.is_company({"isin": h.isin, **securities.describe(meta, h.isin)})
    ]
    other_assets = [
        dict(
            isin=h.isin,
            name=securities.display_name(meta, h.isin),
            asset_class=securities.describe(meta, h.isin)["asset_class"],
            reason="OUTSIDE_ADVISOR_COMPANY_EQUITY_SCOPE",
        )
        for h in holdings
        if h not in in_scope
    ]
    allowed = {h.isin for h in in_scope}
    # Closed positions still belong to actual historical performance.
    transaction_meta = securities.lookup(connection, sorted({t.isin for t in transactions}))
    equity_transactions = [
        t
        for t in transactions
        if integration.is_company({"isin": t.isin, **securities.describe(transaction_meta, t.isin)})
    ]
    prices = latest_prices(connection, sorted(allowed), on)
    dates = latest_price_dates(connection, sorted(allowed), on)
    refs = (
        read_reference_inputs(connection, start, on, universe, benchmark)
        if equity_transactions
        else dict(sessions=[], members=[], member_since=None, risk_free={}, benchmark={})
    )
    sessions = refs["sessions"]
    all_isins = sorted(allowed | set(refs["members"])) if allowed else []
    history = adjusted_price_series(connection, all_isins, start, on)
    reference_meta = securities.lookup(connection, refs["members"]) if allowed else {}
    fundamentals = latest_fundamentals(connection, all_isins, on)
    overrides = identity_review.confirmed_overrides(connection, investor_id, sorted(allowed))
    fundamentals.update(overrides)
    sources = [
        _source("ledger", "Owned immutable transactions replayed by the financial engine", on, now),
        _source(
            "prices",
            "Cached NSE/BSE closes; verified split/bonus/demerger and ISIN adjustments",
            max((d for rows in history.values() for d, _ in rows), default=on),
            now,
            origin="NSE/BSE exchange observations",
            provider="NSE/BSE",
        ),
        _source(
            "calendar",
            "Market sessions observed in cached exchange and index data",
            sessions[-1] if sessions else on,
            now,
        ),
        _source("snapshots", "Engine-derived portfolio snapshots", on, now),
        _source(
            "benchmark",
            f"{benchmark} exact dated total-return levels",
            max(refs["benchmark"], default=on),
            now,
            provider="NSE Indices",
        ),
        _source(
            "risk_free",
            "Nifty 1D Rate Index; exact matched interval returns",
            max(refs["risk_free"], default=on),
            now,
            provider="NSE Indices",
        ),
        _source(
            "classifications",
            "Current sourced security sector and AMFI size classifications",
            on,
            now,
        ),
        _source("universe", f"Stored {universe} membership with recorded tenure", on, now),
    ]
    rows, positions, unresolved = [], [], []
    for holding in in_scope:
        info = securities.describe(meta, holding.isin)
        price = prices.get(holding.isin)
        value = (holding.quantity * price).quantize(Decimal("0.01")) if price is not None else None
        observed = dates.get(holding.isin)
        reasons = ["PREVIOUS_SESSION_CLOSE"] if observed and observed != on else []
        rows.append(
            dict(
                isin=holding.isin,
                quantity=str(holding.quantity),
                invested=str(holding.invested),
                market_value=None if value is None else str(value),
                valuation_date=on.isoformat(),
                source_refs=["ledger", "prices"],
                quality="provisional" if reasons else "available",
                reasons=reasons,
            )
        )
        position = dict(
            isin=holding.isin,
            name=info.get("name"),
            symbol=info.get("symbol"),
            quantity=str(holding.quantity),
            invested=str(holding.invested),
            avg_cost=str(holding.avg_cost),
            last_price=None if price is None else str(price),
            market_value=None if value is None else str(value),
            unrealised=None if value is None else str(value - holding.invested),
            unrealised_pct=float((value - holding.invested) / holding.invested * 100)
            if value is not None and holding.invested
            else None,
            sector=info.get("sector"),
            market_cap_bucket=info.get("market_cap_bucket"),
            input_notes=[],
            factors={},
            returns={},
            return_windows={},
        )
        if value is None:
            unresolved.append(
                dict(
                    isin=holding.isin,
                    name=info.get("name"),
                    symbol=info.get("symbol"),
                    quantity=str(holding.quantity),
                    invested=str(holding.invested),
                    reason="MISSING_CURRENT_PRICE",
                )
            )
        else:
            positions.append(position)
    if len(sessions) >= 2:
        previous_session, closing_session = sessions[-2:]
        for row in rows:
            points = dict(history.get(row["isin"], []))
            previous, closing = points.get(previous_session), points.get(closing_session)
            if (
                previous is not None
                and closing is not None
                and previous > 0
                and prices.get(row["isin"]) == closing
            ):
                row["day_change"] = str(Decimal(row["quantity"]) * (closing - previous))
                row["day_change_basis"] = "upstream_verified_same_share_basis"
    for item in skipped:
        # An unresolved segment has no invented quantity or cost. It is named outside
        # numerical holdings, and full-book metrics are marked provisional below.
        other_assets.append(
            dict(
                isin=item.isin,
                name=securities.display_name(transaction_meta, item.isin),
                asset_class="UNRECONCILED",
                reason=item.message,
            )
        )
    total = sum((Decimal(p["market_value"]) for p in positions), ZERO)
    for position in positions:
        position["weight"] = float(Decimal(position["market_value"]) / total * 100) if total else 0
    revision = hashlib.sha256(
        json.dumps([asdict(t) for t in equity_transactions], default=str, sort_keys=True).encode()
    ).hexdigest()
    data = dict(
        schema_version="0.1",
        currency="INR",
        portfolio_scope="long_only_indian_equities",
        as_of=on.isoformat(),
        knowledge_cutoff=cutoff.isoformat(),
        generated_at=now.isoformat(),
        engine_version=ENGINE_VERSION,
        ledger_revision=revision,
        calendar_id="stored_exchange_observations_v1",
        sessions=[s.isoformat() for s in sessions],
        sources=sources,
        holdings=rows,
        fundamentals=[],
        classifications=[],
        asset_histories=[
            dict(
                isin=i,
                basis="split_adjusted_price",
                adjustment_version="verified-corporate-actions-and-aliases-v1",
                levels=[dict(date=d.isoformat(), value=str(v)) for d, v in points if d in sessions],
                source_refs=["prices"],
                quality="available",
            )
            for i, points in history.items()
        ],
        returns=[],
        benchmark_returns=_intervals(refs["benchmark"], sessions, "benchmark", "total_return"),
        risk_free_returns=_intervals(
            refs["risk_free"], sessions, "risk_free", "overnight_rate_index_return"
        ),
        upstream_kpis=[],
        portfolio_value=str(total) if not unresolved and not skipped else None,
    )
    data["target_returns"] = [
        {**r, "basis": "minimum_acceptable_return_equals_matched_overnight_index"}
        for r in data["risk_free_returns"]
    ]
    if sessions:
        data["calendar_coverage"] = dict(
            start=sessions[0].isoformat(),
            end=on.isoformat(),
            source_refs=["calendar"],
            quality="provisional",
            reasons=["OBSERVED_EXCHANGE_CALENDAR"],
        )
    if refs["members"] and refs["member_since"]:
        data["universe"] = dict(
            id=universe,
            members=refs["members"],
            effective_from=refs["member_since"].isoformat(),
            effective_to=on.isoformat(),
            known_at=datetime.combine(refs["member_since"], time.min, IST).isoformat(),
            source_refs=["universe"],
            quality="provisional",
            reasons=["MEMBERSHIP_TENURE_NOT_PUBLICATION_ARCHIVE"],
        )
    for holding in in_scope:
        info = securities.describe(meta, holding.isin)
        # Current metadata has no historical revisions; never backdate it.
        if on == now.astimezone(IST).date():
            for field, attr, scheme in [
                ("sector", "sector", "NSE_BSE_HARMONISED"),
                ("market_cap", "market_cap_bucket", "AMFI"),
            ]:
                if info.get(attr):
                    data["classifications"].append(
                        dict(
                            isin=holding.isin,
                            field=field,
                            bucket=info[attr],
                            scheme=scheme,
                            scheme_version="current-cache",
                            effective_from=on.isoformat(),
                            effective_to=on.isoformat(),
                            known_at=now.isoformat(),
                            source_refs=["classifications"],
                            quality="provisional",
                            reasons=["CURRENT_CLASSIFICATION_SNAPSHOT"],
                        )
                    )
    # Exchange-sourced rubric profiles. Current classification only; never backdated, and
    # never asserted from an observation made after the knowledge cutoff.
    profiles = health_profiles.read(connection, sorted(allowed))
    profile_status = {}
    if on == now.astimezone(IST).date():
        data["health_profiles"] = []
        for isin in sorted(allowed):
            profile = profiles.get(isin)
            if profile is None:
                profile_status[isin] = "PROFILE_NOT_YET_CLASSIFIED"
            elif profile.retrieved_at > cutoff:
                profile_status[isin] = "LOOKAHEAD_INPUT"
            elif profile.profile == "unsupported_financial":
                profile_status[isin] = "UNSUPPORTED_FINANCIAL:" + (profile.industry or "")
            else:
                profile_status[isin] = profile.profile
                data["health_profiles"].append(
                    dict(
                        isin=isin,
                        profile=profile.profile,
                        as_of=on.isoformat(),
                        source_refs=["health_profiles"],
                        quality="provisional",
                        reasons=["CURRENT_EXCHANGE_CLASSIFICATION_SNAPSHOT"],
                    )
                )
    else:
        profile_status = {i: "HISTORICAL_PROFILE_NOT_RECORDED" for i in sorted(allowed)}
    data["sources"].append(
        _source(
            "health_profiles",
            "BSE scrip header sector / industry group (bank vs non-financial rubric)",
            min(
                on,
                max(
                    (
                        p.retrieved_at.astimezone(IST).date()
                        for p in profiles.values()
                        if p.retrieved_at <= cutoff
                    ),
                    default=on,
                ),
            ),
            now,
            provider="BSE",
            origin="BSE scrip classification",
        )
    )
    snapshots = list_snapshots_range(connection, investor_id, advised_only, start, on)
    # Existing snapshots exclude mutual funds but may include ETFs. Never project a
    # mixed snapshot series onto a company-only model or silently subtract fund values.
    mixed_history = any(
        not integration.is_company(
            {"isin": t.isin, **securities.describe(transaction_meta, t.isin)}
        )
        for t in transactions
    )
    if mixed_history and snapshots:
        snapshots = company_snapshots(connection, equity_transactions, snapshots, start, on)
    data["returns"] = snapshot_intervals(snapshots, equity_transactions, sessions)
    cashflow = build_performance(
        connection,
        investor_id,
        equity_transactions,
        prices,
        on,
        benchmark,
        advised_only,
        {s.isin for s in skipped} | {p["isin"] for p in unresolved},
    )
    all_performance = cashflow.get("windows", {}).get("all")
    if all_performance and equity_transactions:
        period = dict(start=all_performance["start"].isoformat(), end=on.isoformat())
        xirr = all_performance.get("xirr")
        data["upstream_kpis"].append(
            dict(
                name="xirr",
                value=xirr,
                unit="fraction",
                basis="ledger_cash_flows_xirr",
                period=period,
                status="available" if xirr is not None else "unavailable",
                reasons=[] if xirr is not None else ["UNDEFINED_XIRR"],
                source_refs=["ledger"],
                ledger_revision=revision,
                engine_version=ENGINE_VERSION,
                currency="INR",
                as_of=on.isoformat(),
            )
        )
    return dict(
        data=data,
        positions=positions,
        unresolved=unresolved,
        other_assets=other_assets,
        fundamentals=fundamentals,
        history=history,
        cashflow=cashflow,
        meta=meta,
        total=total,
        mixed_history=mixed_history,
        universe_members=refs["members"],
        universe=universe,
        reference_meta=reference_meta,
        skipped=skipped,
        profile_status=profile_status,
    )


def _reported_inputs(context: dict, report: dict) -> None:
    """Keep cached observations inspectable without inventing missing filing metadata.

    The strict advisor metrics remain unavailable where publication/basis fields do not
    exist in the current cache. No made-up fiscal dates, TTM/consolidation or published-at
    values are supplied to the calculation library to make its validation pass.
    """
    cutoff = datetime.fromisoformat(context["data"]["knowledge_cutoff"])
    for position in context["positions"]:
        row = context["fundamentals"].get(position["isin"])
        if not row:
            continue
        if row.fetched_at and (
            row.fetched_at > cutoff
            or row.fetched_at.date() > date.fromisoformat(context["data"]["as_of"])
        ):
            position["input_notes"].append("LOOKAHEAD_INPUT")
            continue
        position["fundamentals_as_of"] = row.as_of
        position["fundamentals_source"] = row.source
        position["fiscal_period"] = row.fiscal_period
        position["input_notes"].append("REPORTING_BASIS_AND_PUBLICATION_NOT_RECORDED")
    reported_positions = []
    for position in context["positions"]:
        row = context["fundamentals"].get(position["isin"])
        current = dict(position)
        if row and row.fetched_at and row.fetched_at <= cutoff:
            for field in integration.COMPANY_FIELDS:
                value = getattr(row, field, None)
                if field in ("debt_to_equity", "interest_cover") and integration.is_financial(
                    position
                ):
                    value = None
                current[field] = float(value) if value is not None else None
        reported_positions.append(current)
    observed = integration.materialize(
        reported_positions, context["unresolved"], date.fromisoformat(context["data"]["as_of"])
    )
    reason = "REPORTING_BASIS_AND_PUBLICATION_NOT_RECORDED"
    for metric in observed["metrics"].values():
        if metric["value"] is not None:
            metric["status"] = "provisional"
            metric["reasons"] = sorted(set(metric["reasons"] + [reason]))
    observed_rows = {}
    for isin, row in context["fundamentals"].items():
        if row.fetched_at and row.fetched_at <= cutoff:
            observed_rows[isin] = {
                field: None if getattr(row, field, None) is None else str(getattr(row, field))
                for field in integration.COMPANY_FIELDS
            }
    weights = (
        {p["isin"]: Decimal(p["market_value"]) / context["total"] for p in context["positions"]}
        if context["total"] > 0
        else {}
    )
    reported_factors = milestone.reported_factor_diagnostics(
        observed_rows,
        context["universe_members"],
        weights,
        context["universe"],
        complete=not context["unresolved"] and not context["skipped"],
    )
    for position in reported_positions:
        position["factors"] = {
            key: None
            if factor.get("holdings", {}).get(position["isin"]) is None
            else float(factor["holdings"][position["isin"]])
            for key, factor in reported_factors.items()
        }
    reported_characteristics = {}
    for field in ("roe", "debt_to_equity", "interest_cover"):
        metric = cross_section.aggregate(
            [
                (
                    Decimal(p["market_value"]),
                    None if p.get(field) is None else Decimal(str(p[field])),
                )
                for p in reported_positions
            ]
        )
        fraction = metric.coverage.get("weight_fraction")
        reported_characteristics[field] = dict(
            value=None if metric.value is None else float(metric.value),
            coverage_pct=0 if fraction is None else float(fraction * 100),
            note="REPORTING_BASIS_AND_PUBLICATION_NOT_RECORDED",
        )
    factor_projection = {}
    for key, factor in reported_factors.items():
        metric = factor["portfolio"]
        fraction = metric["coverage"].get("weight_fraction")
        factor_projection[key] = dict(
            score=milestone.number(metric),
            universe=factor.get("universe") or context["universe"],
            universe_size=factor.get("total_count", 0),
            eligible_count=factor.get("eligible_count", 0),
            coverage_pct=0 if fraction is None else float(Decimal(fraction) * 100),
            status=metric["status"],
            reasons=metric["reasons"],
        )
    reported_fundamentals = {}
    for key in ("pe", "pb", "dividend_yield"):
        metric = observed["metrics"][key]
        fraction = metric["coverage"].get("weight_fraction")
        reported_fundamentals[key] = dict(
            value=milestone.number(metric),
            excluded_weight_pct=100 if fraction is None else float((1 - Decimal(fraction)) * 100),
            method_version=metric["method"],
            status=metric["status"],
            reasons=metric["reasons"],
            excluded_isins=metric["coverage"]["excluded_isins"],
        )
    report["reported_inputs"] = dict(
        status="provisional",
        reasons=[reason],
        positions=reported_positions,
        fundamentals=reported_fundamentals,
        lenses=integration.lens_projection(observed, reported_positions),
        metrics=observed["metrics"],
        factors=factor_projection,
        factor_metrics=reported_factors,
        characteristics=reported_characteristics,
    )
    # The source cache does not yet store this mandatory per-observation metadata.
    for metric in report["sections"]["fundamentals"].values():
        if isinstance(metric, dict) and metric.get("status") == "unavailable":
            metric["reasons"] = sorted(
                set(metric.get("reasons", []) + ["REPORTING_BASIS_AND_PUBLICATION_NOT_RECORDED"])
            )


def _correlations(report: dict, positions: list[dict], data: dict) -> dict:
    ids = [p["isin"] for p in positions]
    labels = {p["isin"]: p.get("symbol") or p["isin"] for p in positions}
    matrix = [[None for _ in ids] for _ in ids]
    values = []
    pairs = report["sections"]["diversification"].get("correlation_pairs", [])
    for pair in pairs:
        a, b = pair["isin_a"], pair["isin_b"]
        if a in ids and b in ids:
            value = milestone.number(pair["correlation"])
            matrix[ids.index(a)][ids.index(b)] = matrix[ids.index(b)][ids.index(a)] = value
            if value is not None:
                values.append([labels[a], labels[b], value])
    # A constant history has no artificial self-correlation of one.
    histories = {h["isin"]: h for h in data.get("asset_histories", [])}
    for index, i in enumerate(ids):
        history = histories.get(i)
        if history:
            levels = {p["date"]: p["value"] for p in history["levels"]}
            returns = [r["return"] for r in stats.interval_returns(levels, data["sessions"])]
            matrix[index][index] = milestone.number(stats.correlation(returns, returns).json())
    return dict(
        isins=ids,
        symbols=[labels[i] for i in ids],
        matrix=matrix,
        average=sum(p[2] for p in values) / len(values) if values else None,
        tightest=max(values, key=lambda p: p[2]) if values else None,
        loosest=min(values, key=lambda p: p[2]) if values else None,
        pairs=pairs,
    )


def cached_analytics(
    connection: Any, investor_id: str, on: date, benchmark: str, advised_only: bool, universe: str
) -> dict:
    """Serve a stored response only on an exact input fingerprint match; else compute.

    The fingerprint is read BEFORE computing (see app/db/analytics_cache.py), so a write
    racing the computation can only cause a later recompute, never a stale hit. A failed
    cache write is logged and the freshly computed, correct response is still returned.
    """
    from time import perf_counter

    from app.db import analytics_cache
    from app.schemas.analytics import AnalyticsResponse

    key = analytics_cache.CacheKey(investor_id, on, advised_only, benchmark, universe)
    version = analytics_cache.data_version(connection)
    hit = analytics_cache.get(connection, key, version)
    if hit is not None:
        return hit
    started = perf_counter()
    report = build_analytics(connection, investor_id, on, benchmark, advised_only, universe)
    payload = AnalyticsResponse.model_validate(report).model_dump(mode="json")
    elapsed = round((perf_counter() - started) * 1000)
    try:
        analytics_cache.put(connection, key, version, payload, elapsed)
    except Exception:
        connection.rollback()
        logger.exception(
            "analytics_cache_put_failed",
            extra={
                "investor_ref": hashlib.sha256(investor_id.encode()).hexdigest()[:16],
                "as_of": on.isoformat(),
                "compute_ms": elapsed,
            },
        )
    return payload


CAPITAL_STATES = ("STRONG", "WEAKENING", "CAUTION", "RECOVERY", "NEUTRAL", "UNKNOWN")


def _float(value: Any) -> float | None:
    """Presentation-only conversion of a Decimal string ratio/score. Never money."""
    return None if value is None else float(Decimal(str(value)))


def _paise(value: Any) -> str | None:
    return (
        None if value is None else str(Decimal(str(value)).quantize(Decimal("0.01"), ROUND_HALF_UP))
    )


def _flat_evidence(evidence: dict) -> dict[str, float | None]:
    """Scalar evidence figures for the card; nested detail stays in the advisor report."""
    out: dict[str, float | None] = {}
    for key, value in evidence.items():
        if key == "subscores" and isinstance(value, dict):
            out.update({f"{name}_score": _float(score) for name, score in value.items()})
        elif value is None or isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = None if value is None else float(value)
        elif isinstance(value, str):
            try:
                out[key] = float(Decimal(value))
            except ArithmeticError:
                continue  # a label (ISIN, basis), not a figure
    return out


def _metric_view(metric: dict, value: Any) -> dict:
    return {"status": metric["status"], "reasons": metric["reasons"], "value": value}


def _health_evidence(overview: dict) -> dict:
    cov = overview["health_covariance_evidence"]
    cov_value = None
    if cov["value"]:
        sample = cov["value"]["sample_covariance"]["value"]
        cov_value = {
            "annual_volatility": _float(sample["annual_volatility"]),
            "diversification_volatility_ratio": _float(sample["diversification_volatility_ratio"]),
            "observations": sample["observations"],
            "risk_shares": {
                isin: {
                    "risk_share": _float(row["risk_share"]),
                    "annual_volatility_contribution": _float(row["annual_volatility_contribution"]),
                }
                for isin, row in sample["risk_contributions"].items()
            },
            "shrinkage_sensitivity": {
                alpha: None
                if variant["value"] is None
                else {
                    "annual_volatility": _float(variant["value"]["annual_volatility"]),
                    "diversification_volatility_ratio": _float(
                        variant["value"]["diversification_volatility_ratio"]
                    ),
                }
                for alpha, variant in cov["value"]["identity_target_sensitivity"].items()
            },
            "period": cov["period"],
        }
    recovery = overview["health_recovery"]
    weights = overview["health_weight_sensitivity"]
    cutoffs = overview["health_grade_sensitivity"]
    return {
        "covariance": _metric_view(cov, cov_value),
        "recovery": _metric_view(
            recovery,
            None
            if recovery["value"] is None
            else {
                **recovery["value"],
                "terminal_drawdown": _float(recovery["value"]["terminal_drawdown"]),
                "period": recovery["period"],
            },
        ),
        "weight_sensitivity": _metric_view(
            weights,
            None
            if weights["value"] is None
            else {
                "minimum": _float(weights["value"]["minimum"]),
                "maximum": _float(weights["value"]["maximum"]),
                "grades": weights["value"]["grades"],
                "relative_weight_change": _float(weights["value"]["relative_weight_change"]),
            },
        ),
        "grade_cutoff_sensitivity": _metric_view(cutoffs, cutoffs["value"]),
        "validation": overview["health_validation"],
    }


def _health_projection(report: dict, context: dict, enabled: bool) -> dict:
    """portfolio-health-v1 onto HealthOut. Withheld components stay withheld."""
    base = dict(
        rule_version="portfolio-health-v1",
        profile_status=context["profile_status"],
        bands=[],
        drivers=[],
        components=[],
    )
    if not enabled:
        return dict(
            base, score=None, grade=None, status="unavailable", reasons=["FEATURE_DISABLED"]
        )
    health = report["sections"]["overview"]["health"]
    if context["skipped"]:
        # The calculation saw only reconciled holdings; any full-book grade would be false.
        return dict(
            base,
            score=None,
            grade=None,
            status="unavailable",
            reasons=["UNRECONCILED_SEGMENTS"],
            bands=health["bands"],
            limitations=health["limitations"],
        )
    components = [
        dict(
            key=c["key"],
            label=c["label"],
            weight=float(Decimal(c["weight"])),
            score=_float(c["score"]["value"]),
            points=_float(c["contribution"]),
            evidence=_flat_evidence(c["evidence"]),
            status=c["score"]["status"],
            reasons=c["score"]["reasons"],
        )
        for c in health["components"]
    ]
    bounds = health["score_bounds"]
    return dict(
        base,
        score=health["display_score"],
        grade=health["grade"],
        components=components,
        drivers=health["drivers"],
        bands=health["bands"],
        status=health["score"]["status"],
        reasons=health["score"]["reasons"],
        score_exact=health["score"]["value"],
        score_bounds=None
        if health["score"]["value"] is not None
        else {"lower": _float(bounds["lower"]), "upper": _float(bounds["upper"])},
        data_status=health["data_status"],
        limitations=health["limitations"],
        evidence=_health_evidence(report["sections"]["overview"]),
    )


def _capital_projection(report: dict, context: dict, positions: list[dict], enabled: bool):
    """Bucket money in paise, weights in percent; UNKNOWN is a bucket, never dropped."""
    metric = report["sections"]["overview"]["capital_state"]
    if not enabled or context["skipped"] or metric["value"] is None:
        return {}, {}, None
    value = metric["value"]
    for position in positions:
        state = value["states"].get(position["isin"])
        position["capital_state"] = state["state"] if state else None
        position["capital_state_reason"] = state["reason"] if state else None
    money = {k: _paise(value["buckets"][k]) for k in CAPITAL_STATES}
    pct = (
        {k: float(Decimal(value["bucket_weights"][k]) * 100) for k in CAPITAL_STATES}
        if value["bucket_weights"]
        else {}
    )
    return money, pct, _paise(value["total"])


def build_analytics(
    connection: Any, investor_id: str, on: date, benchmark: str, advised_only: bool, universe: str
) -> dict:
    context = materialize(connection, investor_id, on, benchmark, advised_only, universe)
    data = context["data"]
    report = milestone.calculate(data)
    if not report.get("sections"):
        # A contract failure is visible and cannot silently invoke legacy calculations.
        from fastapi import HTTPException

        logger.error(
            "advisor_input_unavailable",
            extra={
                "investor_ref": hashlib.sha256(investor_id.encode()).hexdigest()[:16],
                "as_of": on.isoformat(),
                "method_version": report.get("method_version"),
                "reasons": report.get("reasons", []),
            },
        )
        raise HTTPException(
            422, detail={"code": "ADVISOR_INPUT_UNAVAILABLE", "reasons": report.get("reasons", [])}
        )
    _reported_inputs(context, report)
    if context["skipped"]:
        overview = report["sections"]["overview"]
        for key in (
            "invested",
            "market_value",
            "unrealised",
            "unrealised_pct",
            "hhi",
            "effective_holdings",
            "value_coverage",
        ):
            overview[key] = Metric.unavailable(
                "UNRECONCILED_SEGMENTS", unit=overview[key]["unit"]
            ).json()
        if overview["known_value_subtotal"]["value"] is not None:
            overview["known_value_subtotal"]["status"] = "provisional"
            overview["known_value_subtotal"]["reasons"].append("UNRECONCILED_SEGMENTS")
    if context["skipped"]:
        for factor in report["sections"]["factor_score"].values():
            factor["portfolio"] = Metric.unavailable(
                "UNKNOWN_VALUE_DENOMINATOR", unit="score_0_100"
            ).json()
        report["sections"]["risk_return"]["var_1d_95_value"] = Metric.unavailable(
            "UNKNOWN_VALUE_DENOMINATOR", unit="INR"
        ).json()
        for window in report["sections"]["performance"]["windows"].values():
            if window["value"]:
                window["value"]["risk"]["var_1d_95_value"] = Metric.unavailable(
                    "UNKNOWN_VALUE_DENOMINATOR", unit="INR"
                ).json()
    sections = report["sections"]
    overview = sections["overview"]
    positions = context["positions"]
    illustrations = (
        milestone.illustrations(data)
        if not context["skipped"]
        else {
            name: Metric.unavailable("UNRECONCILED_SEGMENTS", unit="illustration").json()
            for name in milestone.WINDOW_KEYS
        }
    )
    report["illustrations"] = illustrations
    report["scope"] = {
        "asset_class": "EQ",
        "excluded": context["other_assets"],
        "actual_series_reason": None,
    }
    risk = milestone.risk_projection(report, data)
    risk_coverage = {}
    for key, row in risk.items():
        if row["period"].get("start"):
            risk_coverage[key] = dict(
                start=row["period"]["start"],
                end=row["period"]["end"],
                observations=row["observations"],
                weight_pct=100 if not context["unresolved"] and not context["skipped"] else None,
                market_value=str(context["total"]),
                included_isins=[p["isin"] for p in positions],
                excluded=[],
                basis="upstream_cashflow_adjusted_total_return",
            )
    by_isin = {p["isin"]: p for p in positions}
    for lens in sections["valuation_map"].values():
        for point in lens["points"]:
            if point["isin"] in by_isin:
                by_isin[point["isin"]][lens["x_field"]] = float(point["x"])
                by_isin[point["isin"]][lens["y_field"]] = float(point["y"])
    factors = {}
    for key, item in sections["factor_score"].items():
        metric = item["portfolio"]
        factors[key] = dict(
            score=milestone.number(metric),
            universe=item.get("universe") or universe,
            universe_size=item.get("total_count", 0),
            coverage_pct=float(Decimal(metric["coverage"].get("weight_fraction") or "0") * 100),
            status=metric["status"],
            reasons=metric["reasons"],
            eligible_count=item.get("eligible_count", 0),
        )
        for position in positions:
            value = item.get("holdings", {}).get(position["isin"])
            position["factors"][key] = float(value) if value is not None else None
    full = not context["unresolved"] and not context["skipped"]
    priced = len(positions)
    total_count = priced + len(context["unresolved"]) + len(context["skipped"])
    coverage = dict(
        priced_pct=100 if full and total_count else None,
        priced_count_pct=100 * priced / total_count if total_count else 0,
        priced_count=priced,
        total_count=total_count,
        known_value_subtotal=str(context["total"]),
        basis="complete_priced_book" if full else "priced_subset",
        **{f + "_pct": 0 for f in ("pe", "pb", "roe", "debt_to_equity", "interest_cover")},
        sector_pct=sum(p["weight"] for p in positions if p.get("sector")),
        roe_derived_weight_pct=0,
        price_history_pct=sum(p["weight"] for p in positions if context["history"].get(p["isin"])),
    )
    candidate = integration.materialize(
        positions, context["unresolved"] + [{"isin": s.isin} for s in context["skipped"]], on
    )
    candidate["active_method"] = "advisor_v2"
    candidate["method_version"] = "trackr-analytics-0.4"
    candidate["lenses"] = sections["valuation_map"]

    def allocation(field):
        metric = sections["diversification"][field]
        return [
            dict(label=k, value=v["value"], pct=float(Decimal(v["weight"]) * 100))
            for k, v in (metric.get("value") or {}).items()
        ]

    fundamentals = {}
    for field in ("pe", "pb", "dividend_yield"):
        metric = sections["fundamentals"][field]
        fundamentals[field] = dict(
            value=milestone.number(metric),
            excluded_weight_pct=100
            if metric["coverage"].get("weight_fraction") is None
            else float((1 - Decimal(metric["coverage"]["weight_fraction"])) * 100),
            method_version=metric["method"],
            status=metric["status"],
            reasons=metric["reasons"],
            excluded_isins=[p["isin"] for p in positions if p.get(field) is None],
        )
    # Raw reported index observations retain their method; no incompatible spread.
    index = latest_index_valuation(connection, benchmark, on)
    fundamentals.update(index, benchmark_comparison="reported_separately_incompatible_aggregation")
    for position in positions:
        position["return_windows"] = advisor.holding_returns(
            context["history"].get(position["isin"], []), on
        )
        position["returns"] = {k: v["return_pct"] for k, v in position["return_windows"].items()}
    kpi = {
        key: overview[key]["value"]
        for key in ("invested", "market_value", "unrealised", "day_change")
    }
    kpi.update(
        unrealised_pct=None
        if overview["unrealised_pct"]["value"] is None
        else float(Decimal(overview["unrealised_pct"]["value"]) * 100),
        day_change_pct=None
        if overview["day_change_pct"]["value"] is None
        else float(Decimal(overview["day_change_pct"]["value"]) * 100),
    )
    health_enabled = get_settings().advisor_health_enabled
    capital_money, capital_pct, capital_total = _capital_projection(
        report, context, positions, health_enabled
    )
    # Legacy KPI fields allow null in this new coordinated contract; full value must
    # never become the known subtotal merely to satisfy a required string.
    return dict(
        as_of=on,
        generated_at=datetime.now(UTC),
        benchmark=benchmark,
        coverage=coverage,
        sources={s["id"]: s["dataset"] for s in data["sources"]},
        kpi=kpi,
        health=_health_projection(report, context, health_enabled),
        factors=factors,
        risk=risk,
        risk_coverage=risk_coverage,
        risk_contributions={},
        performance=context["cashflow"],
        characteristics={},
        concentration=advisor.concentration(positions),
        price_events=[],
        allocation={"sector": allocation("sector"), "market_cap": allocation("market_cap")},
        correlation=_correlations(report, positions, data),
        fundamentals=fundamentals,
        capital_state=capital_money,
        derived=dict(
            effective_holdings=milestone.number(overview["effective_holdings"]) if full else None,
            concentration_target=None,
            position_count=len(positions),
            vol_ratio_1y=None,
            growth_delta_vs_benchmark=sections["performance"]["growth_delta"]["value"],
            capital_state_pct=capital_pct,
            capital_state_total=capital_total,
            lenses=integration.lens_projection(candidate, positions),
        ),
        growth_of_100k=None,
        positions=positions,
        insights=[],
        unresolved=context["unresolved"],
        integration=candidate,
        advisor=report,
    )
