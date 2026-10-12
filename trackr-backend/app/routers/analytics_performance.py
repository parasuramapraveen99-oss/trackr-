"""Recorded investor returns and a cash-flow-matched index, using cached inputs only."""

from datetime import date
from decimal import Decimal
from typing import Any

from app import securities
from app.db.index_levels import get_index_levels
from app.db.prices import latest_prices
from app.db.repository import load_transactions
from app.db.security_alias import verified_map
from app.engine.benchmark import compute_benchmark, compute_period_benchmark, required_index_dates
from app.engine.errors import MissingIndexLevelError
from app.engine.holdings import compute_holdings_partitioned
from app.engine.types import Transaction, TxnType
from app.engine.valuation import total_market_value
from app.engine.xirr import (
    _transaction_flows,
    compute_period_xirr,
    compute_xirr,
    compute_xirr_holdings,
    years_before,
)


def build_performance(
    connection: Any,
    investor_id: str,
    transactions: list[Transaction],
    prices: dict[str, Decimal],
    on: date,
    benchmark: str,
    advised_only: bool,
    excluded_isins: set[str],
) -> dict:
    """The same ledger and price scope as headline money, independently for each period.

    Opening holdings are replayed with the identities valid AT THE OPENING DATE. Using
    today's normalized identifiers can lose a real historical price after an ISIN change.
    Missing opening holdings are excluded from both return and index replication, named,
    and disclosed; their cash flows never remain in one side of the comparison.
    """
    kept = [t for t in transactions if t.isin not in excluded_isins]
    if not kept:
        return {"windows": {}, "xirr_holdings": None, "recorded_dividend_count": 0}
    aliases = verified_map(connection, on)

    def canonical(isin):
        return aliases.get(isin, isin)

    current_holdings, _ = compute_holdings_partitioned(kept, on)
    full_value = total_market_value(current_holdings, prices)
    earliest = min(t.txn_date for t in kept)
    out = {}
    for label, start in [("all", earliest), *[(f"{n}y", years_before(on, n)) for n in (1, 3, 5)]]:
        if start < earliest:
            continue
        local = kept
        missing = set()
        opening_value = Decimal(0)
        if label != "all":
            opening_transactions = load_transactions(
                connection,
                investor_id,
                advised_only=advised_only,
                date_to=start,
            )
            allowed = {t.isin for t in kept}
            opening_transactions = [t for t in opening_transactions if canonical(t.isin) in allowed]
            opening_holdings, skipped = compute_holdings_partitioned(opening_transactions, start)
            opening_prices = latest_prices(connection, [h.isin for h in opening_holdings], start)
            missing = {canonical(h.isin) for h in opening_holdings if h.isin not in opening_prices}
            missing.update(canonical(s.isin) for s in skipped)
            local = [t for t in kept if t.isin not in missing]
            opening_holdings = [h for h in opening_holdings if canonical(h.isin) not in missing]
            opening_value = total_market_value(opening_holdings, opening_prices)
        closing_holdings, _ = compute_holdings_partitioned(local, on)
        closing = total_market_value(closing_holdings, prices)
        actual = (
            compute_xirr(local, prices, on)
            if label == "all"
            else compute_period_xirr(local, prices, start, opening_value, on)
        )
        needed = (
            required_index_dates(local, on)
            if label == "all"
            else sorted(
                {start, on}
                | {d for d, _ in _transaction_flows([t for t in local if t.txn_date > start])}
            )
        )
        levels = get_index_levels(connection, benchmark, needed)
        comparison = None
        missing_dates = []
        try:
            comparison = (
                compute_benchmark(local, levels, on, benchmark)
                if label == "all"
                else compute_period_benchmark(local, levels, start, opening_value, on, benchmark)
            )
        except MissingIndexLevelError:
            missing_dates = [d for d in needed if d not in levels]
        relative = (
            (actual - comparison.xirr) * 100
            if (actual is not None and comparison is not None and comparison.xirr is not None)
            else None
        )
        meta = securities.lookup(connection, list(missing)) if missing else {}
        out[label] = {
            "start": start,
            "end": on,
            "xirr": str(actual) if actual is not None else None,
            "benchmark_xirr": str(comparison.xirr)
            if comparison and comparison.xirr is not None
            else None,
            "relative_pp": str(relative) if relative is not None else None,
            "market_value": str(closing),
            "benchmark_terminal_value": str(comparison.terminal_value) if comparison else None,
            "terminal_gap": str(closing - comparison.terminal_value)
            if comparison and comparison.terminal_value is not None
            else None,
            "coverage_pct": float(closing / full_value * 100) if full_value else 0.0,
            "excluded": [
                {
                    "isin": i,
                    "symbol": meta.get(i, {}).get("symbol") or i,
                    "reason": "Missing or unreconciled opening position.",
                }
                for i in sorted(missing)
            ],
            "benchmark_missing_dates": missing_dates,
            "basis": "recorded_cash_flow_xirr",
            "annualised": True,
        }
    return {
        "windows": out,
        "xirr_holdings": _rate(compute_xirr_holdings(kept, prices, on)),
        "recorded_dividend_count": sum(t.txn_type == TxnType.DIVIDEND for t in kept),
        "auto_resolved_cash_flow_count": sum(
            bool(t.source_ref and t.source_ref.startswith("AUTO_RESOLVED"))
            and t.txn_type in (TxnType.BUY, TxnType.IPO_ALLOT)
            for t in kept
        ),
        "basis": ("Recorded security cash flows including charges; "
                  "excludes idle cash and unrecorded dividends."),
    }


def _rate(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None
