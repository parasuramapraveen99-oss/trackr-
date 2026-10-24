"""Owned, read-only materialisation of the advisor's complete analytics contract.

This adapter reads existing sources. It never fetches, stages, promotes, migrates,
repairs, or writes the ledger. Financial accounting remains in app.engine.
"""

from __future__ import annotations

import hashlib
import json
import logging
from bisect import bisect_right
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any
from zoneinfo import ZoneInfo

from app import securities
from app.analytics import advisor, integration, milestone
from app.analytics._vendor.advisor import cross_section, stats
from app.analytics._vendor.advisor.core import Metric
from app.analytics._vendor.advisor.health import RULE_VERSION as HEALTH_RULE_VERSION
from app.config import get_settings
from app.db import company_inputs, health_profiles, identity_review
from app.db.fundamentals import latest as latest_fundamentals
from app.db.index_levels import list_index_levels
from app.db.index_valuation import latest as latest_index_valuation
from app.db.prices import adjusted_price_series, latest_price_dates, latest_prices, price_series
from app.db.repository import load_transactions
from app.db.security_alias import verified_map
from app.db.snapshots import list_snapshots_range
from app.engine.dividends import compute_dividends
from app.engine.holdings import MAX_PRICE_STALENESS_DAYS, compute_holdings_partitioned
from app.engine.realised import compute_realised
from app.engine.returns import compute_net_invested
from app.engine.snapshot import prepare_snapshot, value_prepared_snapshot
from app.engine.twrr import _period_flow
from app.engine.types import Snapshot, Transaction
from app.engine.xirr import years_before
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
    event_days = sorted({t.txn_date for t in transactions})
    generation = -1
    prepared = None
    snapshots = []
    for old in existing:
        day = old.as_of
        current_generation = bisect_right(event_days, day)
        # Between ledger events only the prices change. Reuse the engine's prepared
        # replay in this request; never persist a second holdings ledger.
        if prepared is None or generation != current_generation:
            prepared = prepare_snapshot(transactions, day)
            generation = current_generation
        held = prepared.holdings
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
        excluded = set(prepared.excluded_isins) | {
            h.isin for h in held if h.quantity > 0 and h.isin not in prices
        }
        snapshots.append(value_prepared_snapshot(prepared, prices, day, excluded))
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
    session_set = set(sessions)
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
                levels=[
                    dict(date=d.isoformat(), value=str(v)) for d, v in points if d in session_set
                ],
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
    # Company fundamentals for the health rubric: exchange filings first, vendor statements
    # only where no filing exists (app/db/company_inputs.py).
    company_isins = sorted(allowed | set(refs["members"]))
    data["fundamentals"], filed_profiles, _ = company_inputs.build(
        connection,
        company_isins,
        on,
        cutoff,
        latest_prices(connection, company_isins, on),
        {i: securities.describe(meta, i).get("sector") for i in allowed},
    )
    data["sources"].append(
        _source(
            "company_financials",
            "NSE integrated-filing XBRL (year-end results); Yahoo Finance statements where "
            "a security has no NSE filing",
            on,
            now,
        )
    )
    # Exchange-sourced rubric profiles. Current classification only; never backdated, and
    # never asserted from an observation made after the knowledge cutoff. The SEBI filing
    # format (bank / NBFC / other) is used where the BSE header is absent or unsupported.
    profiles = health_profiles.read(connection, sorted(allowed))
    profile_status = {}
    if on == now.astimezone(IST).date():
        data["health_profiles"] = []
        for isin in sorted(allowed):
            profile = profiles.get(isin)
            filed = filed_profiles.get(isin)
            if filed and (
                profile is None
                or profile.retrieved_at > cutoff
                or profile.profile == "unsupported_financial"
            ):
                profile_status[isin] = filed
                data["health_profiles"].append(
                    dict(
                        isin=isin,
                        profile=filed,
                        as_of=on.isoformat(),
                        source_refs=["company_financials"],
                        quality="provisional",
                        reasons=["SEBI_FILING_FORMAT_PROFILE"],
                    )
                )
            elif profile is None:
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
    # Engine-owned ledger figures, passed through unchanged: realised = FIFO gains
    # (ENGINE_SPEC section 3), dividends = recorded DIVIDEND rows (REALISED state, section
    # 4), net contributions = compute_net_invested (section 5). Unreconciled securities are
    # left out and named; nothing here re-derives accounting.
    unreconciled = {s.isin for s in skipped}
    reconciled = [t for t in equity_transactions if t.isin not in unreconciled]
    if reconciled:
        kpi_period = dict(start=min(t.txn_date for t in reconciled).isoformat(), end=on.isoformat())
        for name, value, basis in (
            (
                "realised_pnl",
                sum((g.gain for g in compute_realised(reconciled, on)), ZERO),
                "engine_fifo_realised_gain",
            ),
            (
                "net_contributions",
                compute_net_invested(reconciled, on),
                "engine_net_invested_buys_less_sells",
            ),
            (
                "dividends_received",
                sum((d.total for d in compute_dividends(reconciled)), ZERO),
                "engine_recorded_dividend_ledger_rows",
            ),
        ):
            data["upstream_kpis"].append(
                dict(
                    name=name,
                    value=str(value),
                    unit="INR",
                    basis=basis,
                    period=kpi_period,
                    status="provisional" if unreconciled else "available",
                    reasons=["UNRECONCILED_SEGMENTS_EXCLUDED"] if unreconciled else [],
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
    connection: Any, investor_id: str, on: date, benchmark: str, advised_only: bool, universe: str,
    *, detail: str = "full",
) -> dict:
    """Serve a stored response only on an exact input fingerprint match; else compute.

    The fingerprint is read BEFORE computing (see app/db/analytics_cache.py), so a write
    racing the computation can only cause a later recompute, never a stale hit. A failed
    cache write is logged and the freshly computed, correct response is still returned.
    """
    from time import perf_counter

    from app.analytics.transport import compact_report, expand_report
    from app.db import analytics_cache
    from app.schemas.analytics import AnalyticsResponse

    key = analytics_cache.CacheKey(investor_id, on, advised_only, benchmark, universe)
    version = analytics_cache.data_version(connection, investor_id)
    hit = (analytics_cache.get(connection, key, version, compact=True) if detail == "screen"
           else analytics_cache.get(connection, key, version))
    if hit is not None and _derived_current(hit):
        return hit
    if hit is not None:
        hit = expand_report(hit)
        # Same inputs, but cached before (or under an older) revamp/Basic rule set. The
        # rest of the payload is still exact; only the derived blocks are recomputed.
        hit["revamp"] = revamp_block(connection, investor_id, on, advised_only, hit)
        hit["basic"] = basic_block(connection, investor_id, on, advised_only, hit)
        _put(connection, key, version, hit, 0, investor_id, on)
        return compact_report(hit) if detail == "screen" else hit
    started = perf_counter()
    report = build_analytics(connection, investor_id, on, benchmark, advised_only, universe)
    payload = AnalyticsResponse.model_validate(report).model_dump(
        mode="json", exclude={"interval_sets"}
    )
    # Evaluated on the exact wire payload, so a Basic finding can only cite a figure the
    # page itself shows (docs/ANALYTICS_BASIC_NARRATIVE.md).
    payload["revamp"] = revamp_block(connection, investor_id, on, advised_only, payload)
    payload["basic"] = basic_block(connection, investor_id, on, advised_only, payload)
    elapsed = round((perf_counter() - started) * 1000)
    _put(connection, key, version, payload, elapsed, investor_id, on)
    return compact_report(payload) if detail == "screen" else payload


def basic_slots_version() -> str:
    from app.analytics.basic_slots import RULE_VERSION

    return RULE_VERSION


def _derived_current(payload: dict) -> bool:
    from app.analytics.revamp import REVAMP_VERSION

    return (payload.get("basic") or {}).get("rule_version") == basic_slots_version() and (
        payload.get("revamp") or {}
    ).get("version") == REVAMP_VERSION


def revamp_block(
    connection: Any, investor_id: str, on: date, advised_only: bool, payload: dict
) -> dict:
    """Inputs the revamped page needs beyond the payload, read once, computed in
    app.analytics.revamp (pure). See that module for each figure's rule."""
    from app.analytics import revamp
    from app.analytics.basic_slots import UNCLASSIFIED_LABELS, screen_for
    from app.db.fundamentals import latest as fundamentals_at
    from app.db.traded_value import recent_sessions
    from app.db.universe import current_universe_isins
    from app.prices.market_cap_sources import AMFI_MARKET_CAP_URL

    benchmark = payload.get("benchmark") or "NIFTY50_TRI"
    positions = payload.get("positions") or []
    isins = [p["isin"] for p in positions]
    total = Decimal(str((payload.get("kpi") or {}).get("market_value")
                        or (payload.get("coverage") or {}).get("known_value_subtotal") or "0"))
    weights = {p["isin"]: p.get("weight") or 0.0 for p in positions}
    symbols = {p["isin"]: p.get("symbol") or p["isin"] for p in positions}

    # Exchange calendar = the benchmark's own sessions; last WINDOW_SESSIONS intervals.
    levels = list_index_levels(connection, benchmark, on - timedelta(days=420), on)
    sessions = [d for d, _ in levels][-(revamp.WINDOW_SESSIONS + 1):]
    start = sessions[0] if sessions else on
    history = adjusted_price_series(connection, isins, start, on) if isins else {}
    returns = {i: revamp.interval_returns(history.get(i) or [], sessions) for i in isins}
    bench_returns = revamp.interval_returns(levels, sessions)
    betas = revamp.holding_betas(returns, bench_returns)

    risk = payload.get("risk") or {}
    one_year = risk.get("1y") or {}
    worst = next(
        (
            {"window": key, "max_drawdown": risk[key].get("max_drawdown"),
             **(risk[key].get("drawdown_dates") or {}), "period": risk[key].get("period")}
            for key in ("5y", "3y", "1y")
            if (risk.get(key) or {}).get("max_drawdown") is not None
        ),
        {"window": None, "max_drawdown": None, "reason": "NO_RISK_WINDOW"},
    )
    bench_window = [(d, float(v)) for d, v in levels if d in set(sessions)]

    # Point-in-time: a holding is screened as a lender only from an accepted profile.
    statuses = (payload.get("health") or {}).get("profile_status") or {}
    stored = health_profiles.read(connection, sorted(statuses))
    screens = {i: screen_for(st, stored[i].industry_group if i in stored else None)
               for i, st in statuses.items()}

    reported = {p["isin"]: p for p in
                (((payload.get("advisor") or {}).get("reported_inputs") or {}).get("positions")
                 or [])}
    fund_positions = [{**p, **{k: v for k, v in reported.get(p["isin"], {}).items()
                               if k in ("pe", "pb", "roe", "net_margin", "debt_to_equity",
                                        "interest_cover", "dividend_yield", "sales_growth_1y",
                                        "profit_growth_1y", "fundamentals_as_of")}}
                      for p in positions]
    universe = fundamentals_at(connection, current_universe_isins(connection), on)
    medians = revamp.market_medians([
        {k: (float(getattr(f, k)) if getattr(f, k, None) is not None else None)
         for k in ("pe", "pb", "roe", "net_margin", "debt_to_equity", "interest_cover",
                   "dividend_yield", "sales_growth_1y", "profit_growth_1y")}
        for f in universe.values()
    ])

    first_prices = {}
    if isins:
        for isin, first in connection.execute(
            "SELECT isin, min(price_date) FROM price WHERE isin = ANY(%s) GROUP BY isin",
            (isins,),
        ).fetchall():
            first_prices[isin] = first
    targets = {"1w": on - timedelta(days=7), "1m": on - timedelta(days=30),
               "3m": on - timedelta(days=91),
               **{f"{n}y": years_before(on, n) for n in (1, 3, 5)}}
    return_reasons = {
        p["isin"]: revamp.return_reasons(p.get("return_windows") or {},
                                         first_prices.get(p["isin"]), targets)
        for p in positions
    }

    snaps = list_snapshots_range(connection, investor_id, advised_only, on - timedelta(days=10), on)
    now = next((s for s in snaps if s.as_of == on), None)
    prev = next((s for s in reversed(snaps) if s.as_of < on), None)
    trades_today = sum(
        t.txn_date == on
        for t in load_transactions(connection, investor_id, advised_only=advised_only, date_to=on)
    )
    day = (payload.get("kpi") or {}).get("day_change")
    check = revamp.day_change_check(
        Decimal(str(day)) if day is not None else None,
        now.market_value if now else None, prev.market_value if prev else None,
        Decimal(0) if trades_today == 0 else None,
    )
    amfi_period = AMFI_MARKET_CAP_URL.rsplit("AverageMarketCapitalization", 1)[-1].split(".")[0]

    return {
        "version": revamp.REVAMP_VERSION,
        "header": revamp.header(payload, revamp.bench_day_change(levels, on), check),
        "risk": {
            "betas": betas,
            "risk_shares": revamp.risk_shares(returns, weights),
            "portfolio_beta": one_year.get("beta"),
            "benchmark_drawdown": revamp.drawdown(bench_window),
            "window_sessions": len(sessions) - 1,
            "concentration_impact": revamp.concentration_impact(positions, total),
            "index_shock": revamp.index_shock(positions, betas, one_year.get("beta"), total, worst),
            "liquidity": revamp.liquidity(positions, recent_sessions(connection, isins, on), total),
        },
        "diversification": {
            "correlation": revamp.correlation_matrix(returns, weights, symbols),
            "sector": revamp.sector_mix((payload.get("allocation") or {}).get("sector") or [],
                                        UNCLASSIFIED_LABELS),
            "amfi_list_period": amfi_period,
            "return_reasons": return_reasons,
        },
        "fundamentals": {
            "dataset": "vendor_reported_provisional",
            "as_of": sorted({p.get("fundamentals_as_of") for p in fund_positions
                             if p.get("fundamentals_as_of")}),
            "cards": revamp.fundamentals_cards(fund_positions, payload.get("fundamentals") or {}),
            "map": revamp.valuation_map(fund_positions, medians, screens),
            "positions": [{k: p.get(k) for k in ("isin", "symbol", "weight", "sector", "pe", "pb",
                                                 "roe", "net_margin", "debt_to_equity",
                                                 "interest_cover", "dividend_yield",
                                                 "sales_growth_1y", "profit_growth_1y")}
                          for p in fund_positions],
        },
        "coverage": revamp.coverage_footer(payload),
    }


def _put(connection: Any, key: Any, version: Any, payload: dict, elapsed: int,
         investor_id: str, on: date) -> None:
    """Store a response. A failed write is logged; the correct response is still served."""
    from app.db import analytics_cache

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


def basic_block(
    connection: Any, investor_id: str, on: date, advised_only: bool, payload: dict
) -> dict:
    """The Basic-view findings for this payload: app.analytics.basic_slots.

    Two inputs the payload does not carry are read here. The 1Y per-holding contribution
    comes from the same routine as GET /contribution, so its scope and exclusions match
    that endpoint. A missing price at either end is a named error on the loss-share
    finding, never a zero. Exchange industry groups come from security_health_profile,
    and only for ISINs whose profile the advisor accepted for this date.
    """
    from app.analytics import basic_slots
    from app.engine.errors import MissingPriceError
    from app.routers.analytical import build_contribution

    try:
        rows = build_contribution(
            connection,
            investor_id,
            period_start=on - timedelta(days=365),
            as_of=on,
            advised_only=advised_only,
            cached_prices_only=True,
        )
        contrib = {
            "period_start": str(rows["period_start"]),
            "total_gain": rows["total_gain"],
            "contributions": [
                {"isin": r["isin"], "symbol": r["symbol"], "gain": r["gain"]}
                for r in rows["contributions"]
            ],
        }
    except MissingPriceError as exc:
        logger.warning(
            "basic_contribution_unavailable",
            extra={
                "investor_ref": hashlib.sha256(investor_id.encode()).hexdigest()[:16],
                "as_of": on.isoformat(),
                "isins": list(exc.isins),
            },
        )
        contrib = {"error": f"No price at the start or end of the year for: {', '.join(exc.isins)}"}
    statuses = (payload.get("health") or {}).get("profile_status") or {}
    stored = health_profiles.read(connection, sorted(statuses))
    profiles = {
        isin: (status, stored[isin].industry_group if isin in stored else None)
        for isin, status in statuses.items()
    }
    return basic_slots.evaluate(payload, contrib, profiles)


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


def _health_exclusions(context: dict) -> list[dict]:
    """Holdings the health rubric could not see, each with the reason it was left out."""
    names = {a["isin"]: a["name"] for a in context["other_assets"]}
    unpriced = [
        dict(isin=u["isin"], name=u.get("symbol") or u.get("name"), reason=u["reason"])
        for u in context["unresolved"]
    ]
    unreconciled = [
        dict(isin=s.isin, name=names.get(s.isin), reason=s.message) for s in context["skipped"]
    ]
    return unpriced + unreconciled


def _health_report(report: dict, context: dict) -> dict:
    """The report the health rubric reads: the reconciled, priced book.

    Owner amendment (8 October 2026): analytics compute on everything that reconciles
    and name what they exclude. An unpriced holding has no market value, so the full
    run has no value denominator and every component is withheld. Re-running the same
    rubric without the unpriced rows gives a denominator that is exactly the priced
    subtotal; the excluded rows are named by ``_health_exclusions``, never valued.
    """
    data = context["data"]
    if all(row["market_value"] is not None for row in data["holdings"]):
        return report
    priced = [row for row in data["holdings"] if row["market_value"] is not None]
    if not priced:
        return report
    return milestone.calculate(dict(data, holdings=priced, portfolio_value=str(context["total"])))


def _insights(
    positions: list[dict], cashflow: dict, risk: dict, sectors: list[dict], fundamentals: list
) -> list:
    """The published insight rules (app.analytics.verdict) over the priced book.

    A window feeds a rule only when it is complete: the 1Y relative return needs 100%
    coverage, and beta needs an available 1Y risk window.
    """
    from app.analytics import verdict

    ranked = sorted(positions, key=lambda p: -p["weight"])
    gains = sorted(
        (Decimal(p["unrealised"]) for p in positions if Decimal(p["unrealised"]) > 0),
        reverse=True,
    )
    gain_share = float(sum(gains[:2]) / sum(gains) * 100) if len(gains) >= 2 else None
    one_year = (cashflow or {}).get("windows", {}).get("1y") or {}
    relative = (
        float(one_year["relative_pp"])
        if one_year.get("relative_pp") is not None and one_year.get("coverage_pct") == 100
        else None
    )
    risk_1y = risk.get("1y") or {}
    beta = risk_1y.get("beta") if risk_1y.get("status") != "unavailable" else None
    top_sector = max(sectors, key=lambda s: s["pct"]) if sectors else None
    # ROE from the same filings the health rubric reads (company_inputs), else the position's.
    roe = {f["isin"]: float(f["value"]) for f in fundamentals if f["field"] == "roe"}
    return [
        asdict(i)
        for i in verdict.build_insights(
            top_weights=[(p["symbol"] or p["isin"], p["weight"]) for p in ranked],
            gain_share_top2=gain_share,
            relative_1y_pp=relative,
            beta=beta,
            quality_leaders=[
                p["symbol"] or p["isin"]
                for p in ranked
                if (roe.get(p["isin"], p.get("roe")) or 0) > 0.30
            ],
            top_sector=(top_sector["label"], top_sector["pct"]) if top_sector else None,
        )
    ]


def _health_projection(report: dict, context: dict, enabled: bool) -> dict:
    """The health rubric onto HealthOut, from ``_health_report``. Withheld components
    stay withheld.

    When holdings are excluded the score covers the reconciled, priced book only: it is
    marked provisional, carries EXCLUDES_UNRECONCILED_SEGMENTS, and lists the exclusions.
    """
    excluded = _health_exclusions(context)
    base = dict(
        rule_version=HEALTH_RULE_VERSION,
        profile_status=context["profile_status"],
        bands=[],
        drivers=[],
        components=[],
        excluded=excluded,
    )
    if not enabled:
        return dict(
            base, score=None, grade=None, status="unavailable", reasons=["FEATURE_DISABLED"]
        )
    health = report["sections"]["overview"]["health"]
    status = health["score"]["status"]
    reasons = list(health["score"]["reasons"])
    if excluded:
        reasons.append("EXCLUDES_UNRECONCILED_SEGMENTS")
        if status == "available":
            status = "provisional"
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
        status=status,
        reasons=reasons,
        score_exact=health["score"]["value"],
        score_bounds=None
        if health["score"]["value"] is not None
        else {"lower": _float(bounds["lower"]), "upper": _float(bounds["upper"])},
        data_status=health["data_status"],
        limitations=health["limitations"],
        evidence=_health_evidence(report["sections"]["overview"]),
    )


def _capital_projection(report: dict, context: dict, positions: list[dict], enabled: bool):
    """Bucket money in paise, weights in percent; UNKNOWN is a bucket, never dropped.

    Reads ``_health_report``: per-holding states over the reconciled, priced book, with
    the exclusions named on the health block."""
    metric = report["sections"]["overview"]["capital_state"]
    if not enabled or metric["value"] is None:
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
    # Sessions in the trailing 252-interval health window with no engine snapshot return.
    # One missing interval withholds every full-year statistic, so the page names it.
    window_sessions = data["sessions"][-253:]
    observed = {(r["start"], r["end"]) for r in data["returns"]}
    report["scope"] = {
        "asset_class": "EQ",
        "excluded": context["other_assets"],
        "actual_series_reason": None,
        "health_window_gaps": [
            {"start": a, "end": b}
            for a, b in zip(window_sessions, window_sessions[1:], strict=False)
            if (a, b) not in observed
        ],
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
    health_report = _health_report(report, context)
    if health_report is not report:
        # Keep raw Health tooltips on the same included book as the projected grade,
        # while retaining the full-book availability of all other overview figures.
        overview_health = health_report["sections"]["overview"]
        report = dict(report, sections={
            **report["sections"],
            "overview": {**report["sections"]["overview"],
                         "health": overview_health["health"],
                         "capital_state": overview_health["capital_state"]},
        })
    capital_money, capital_pct, capital_total = _capital_projection(
        health_report, context, positions, health_enabled
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
        health=_health_projection(health_report, context, health_enabled),
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
        insights=_insights(
            positions, context["cashflow"], risk, allocation("sector"), data["fundamentals"]
        ),
        unresolved=context["unresolved"],
        integration=candidate,
        advisor=report,
    )
