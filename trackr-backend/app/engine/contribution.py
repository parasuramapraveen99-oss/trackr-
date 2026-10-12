"""Contribution analysis -- docs/ENGINE_SPEC.md section 10.

The first Intelligence-layer feature per docs/PRODUCT_VISION.md: "which holdings
contributed most?" Per-holding rupee attribution of the portfolio's gain over one
period -- section 5's TWRR flow adjustment, applied per ISIN instead of portfolio-wide,
left as a rupee amount instead of a ratio since nothing here is annualised or chained.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

from app.engine.errors import MissingPriceError
from app.engine.holdings import compute_holdings
from app.engine.ledger import ordered
from app.engine.types import (
    ACQUIRING_TYPES,
    DISPOSING_TYPES,
    ZERO,
    Contribution,
    Transaction,
    TxnType,
)
from app.engine.valuation import value_holdings


def _period_flows(period_rows: list[Transaction]) -> dict[str, Decimal]:
    """Net flow per ISIN, ENGINE_SPEC section 10's table: BUY/IPO_ALLOT/TRANSFER_IN
    positive, SELL/TRANSFER_OUT/BUYBACK negative, DIVIDEND negative.

    Uses ACQUIRING_TYPES/DISPOSING_TYPES -- the same grouping compute_holdings uses for
    the cost pool -- not section 5's PORTFOLIO_IN_TYPES/PORTFOLIO_OUT_TYPES, which omit
    transfers. A TRANSFER_IN moves market_value without being gain, so leaving it out
    here would misattribute a transfer as price appreciation.
    """
    flows: dict[str, Decimal] = {}
    for txn in period_rows:
        if txn.txn_type in ACQUIRING_TYPES:
            flows[txn.isin] = flows.get(txn.isin, ZERO) + txn.quantity * txn.price + txn.charges
        elif txn.txn_type in DISPOSING_TYPES:
            flows[txn.isin] = flows.get(txn.isin, ZERO) - (txn.quantity * txn.price - txn.charges)
        elif txn.txn_type is TxnType.DIVIDEND and txn.dividend_amount != ZERO:
            flows[txn.isin] = flows.get(txn.isin, ZERO) - txn.dividend_amount
    return flows


def compute_contribution(
    transactions: list[Transaction],
    start_prices: dict[str, Decimal],
    end_prices: dict[str, Decimal],
    start_date: date,
    as_of: date,
) -> list[Contribution]:
    """Per-ISIN rupee contribution to the portfolio's gain over ``(start_date, as_of]``.

    ``gain_i = (end_value_i - start_value_i) - net_flow_i``, ENGINE_SPEC section 10.
    Every ISIN held at ``start_date``, held at ``as_of``, or carrying a transaction dated
    in the window appears in the result -- including one fully exited mid-period, which
    is exactly the "which investments destroyed value?" answer
    docs/PRODUCT_VISION.md's intelligence layer asks for; dropping it because it is no
    longer held would hide the worst offender instead of surfacing it.

    ``pct_of_total`` (``gain / total_gain``, ``total_gain = sum of every gain here``) is
    left ``None`` only when ``total_gain`` is exactly zero. Otherwise every ISIN gets one,
    including negative and above-100% values when holdings offset -- shown as-is, never
    capped or normalised, matching section 4's "show both variants, don't flatter either"
    posture on ``xirr_all``/``xirr_holdings``.

    Raises:
        MissingPriceError: an ISIN held at ``start_date`` has no ``start_prices`` entry,
            or one held at ``as_of`` has no ``end_prices`` entry -- via ``value_holdings``
            (section 2), exactly like any other valuation. Never substituted with zero.
    """
    # value_holdings itself raises MissingPriceError with no `as_of` -- every other
    # caller only ever checks one date, so it has never needed to say which. This is the
    # one caller checking two (period_start and as_of), and a bare "no usable price for
    # X" reads as a flat contradiction of a screen that only ever asks about today (the
    # Dashboard) unless it names which of the two dates it means.
    try:
        start_holdings = {
            h.isin: h.market_value
            for h in value_holdings(compute_holdings(transactions, start_date), start_prices)
        }
    except MissingPriceError as exc:
        raise MissingPriceError(exc.isins, as_of=start_date) from exc
    try:
        end_holdings = {
            h.isin: h.market_value
            for h in value_holdings(compute_holdings(transactions, as_of), end_prices)
        }
    except MissingPriceError as exc:
        raise MissingPriceError(exc.isins, as_of=as_of) from exc

    period_rows = [t for t in ordered(transactions, as_of) if start_date < t.txn_date <= as_of]
    flows = _period_flows(period_rows)

    isins = set(start_holdings) | set(end_holdings) | {t.isin for t in period_rows}

    contributions = []
    for isin in sorted(isins):
        start_value = start_holdings.get(isin, ZERO)
        end_value = end_holdings.get(isin, ZERO)
        net_flow = flows.get(isin, ZERO)
        contributions.append(
            Contribution(
                isin=isin,
                start_value=start_value,
                end_value=end_value,
                net_flow=net_flow,
                gain=(end_value - start_value) - net_flow,
            )
        )

    total_gain = sum((c.gain for c in contributions), ZERO)
    if total_gain == ZERO:
        return contributions

    return [replace(c, pct_of_total=c.gain / total_gain) for c in contributions]


__all__ = ["compute_contribution"]
