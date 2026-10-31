"""Contribution analysis -- docs/ENGINE_SPEC.md section 10.

The first Intelligence-layer feature per docs/PRODUCT_VISION.md: "which holdings
contributed most?" Per-holding rupee attribution of the portfolio's gain over one
period -- section 5's TWRR flow adjustment, applied per ISIN instead of portfolio-wide,
left as a rupee amount instead of a ratio since nothing here is annualised or chained.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import date
from decimal import Decimal

from app.engine.errors import MissingPriceError
from app.engine.holdings import compute_holdings
from app.engine.ledger import cash_paid, cash_received, ordered
from app.engine.types import (
    ACQUIRING_TYPES,
    DISPOSING_TYPES,
    ZERO,
    Contribution,
    PeriodBridge,
    Transaction,
    TxnType,
)
from app.engine.valuation import value_holdings

_TRANSFERS = frozenset({TxnType.TRANSFER_IN, TxnType.TRANSFER_OUT})


def _period_flows(
    period_rows: list[Transaction],
    transfer_prices: Mapping[tuple[str, date], Decimal] | None = None,
) -> dict[str, Decimal]:
    """Net flow per ISIN, ENGINE_SPEC section 10's table: BUY/IPO_ALLOT/TRANSFER_IN
    positive, SELL/TRANSFER_OUT/BUYBACK negative, DIVIDEND negative.

    Uses ACQUIRING_TYPES/DISPOSING_TYPES -- the same grouping compute_holdings uses for
    the cost pool -- not section 5's PORTFOLIO_IN_TYPES/PORTFOLIO_OUT_TYPES, which omit
    transfers. A TRANSFER_IN moves market_value without being gain, so leaving it out
    here would misattribute a transfer as price appreciation.
    """
    flows: dict[str, Decimal] = {}
    for txn in period_rows:
        if transfer_prices is not None and txn.txn_type in _TRANSFERS:
            # Amendment 2026-10-30 (ENGINE_SPEC s.10): a transfer moves shares at their
            # market value on its date (GIPS in-kind), never at its recorded price -- a
            # price-0 TRANSFER_OUT correction leg would otherwise book phantom gain/loss.
            price = transfer_prices.get((txn.isin, txn.txn_date))
            if price is None:
                raise MissingPriceError([txn.isin], as_of=txn.txn_date)
            sign = 1 if txn.txn_type is TxnType.TRANSFER_IN else -1
            flows[txn.isin] = flows.get(txn.isin, ZERO) + sign * txn.quantity * price
        elif txn.txn_type in ACQUIRING_TYPES:
            flows[txn.isin] = flows.get(txn.isin, ZERO) + cash_paid(txn)
        elif txn.txn_type in DISPOSING_TYPES:
            flows[txn.isin] = flows.get(txn.isin, ZERO) - cash_received(txn)
        elif txn.txn_type is TxnType.DIVIDEND and txn.dividend_amount != ZERO:
            flows[txn.isin] = flows.get(txn.isin, ZERO) - txn.dividend_amount
    return flows


def compute_contribution(
    transactions: list[Transaction],
    start_prices: dict[str, Decimal],
    end_prices: dict[str, Decimal],
    start_date: date,
    as_of: date,
    transfer_prices: Mapping[tuple[str, date], Decimal] | None = None,
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
            Also, when ``transfer_prices`` is given, an in-window TRANSFER_IN/OUT with no
            ``(isin, txn_date)`` entry, named with the transfer date.

    ``transfer_prices`` (amendment 2026-10-30): when given, transfers flow at quantity x
    the market price on their date (GIPS in-kind), so a same-date out/in correction pair
    nets to zero. Every production caller passes it. ``None`` keeps the original
    recorded-value rule for the hand-computed section 10 cases only.
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
    flows = _period_flows(period_rows, transfer_prices)

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


#: Which bridge line each ledger type feeds, ENGINE_SPEC section 16 rule 2. A type absent
#: here (BONUS, SPLIT_ADJ, DEMERGER_IN, DEMERGER_COST_*) carries no flow.
BRIDGE_FIGURE: dict[TxnType, str] = {
    TxnType.BUY: "purchases",
    TxnType.IPO_ALLOT: "purchases",
    TxnType.SELL: "proceeds",
    TxnType.BUYBACK: "proceeds",
    TxnType.DIVIDEND: "dividends",
    TxnType.TRANSFER_IN: "transfers_in",
    TxnType.TRANSFER_OUT: "transfers_out",
}

#: The flow lines of a ``PeriodBridge``, in display order.
BRIDGE_LINES = ("purchases", "proceeds", "dividends", "transfers_in", "transfers_out")


def period_line(
    txn: Transaction, transfer_prices: dict[tuple[str, date], Decimal]
) -> tuple[str, Decimal | None]:
    """The bridge line one ledger row feeds, and its amount -- the ONE definition both
    ``compute_period_bridge`` and any per-row listing use, so the rows always sum to the
    lines.

    ``("none", None)`` for a type with no flow. A transfer is valued at the market close
    on its own date, ``transfer_prices[(isin, txn_date)]`` (the GIPS in-kind rule), never
    at its recorded price; with no such price its amount is ``None``, never zero.
    """
    figure = BRIDGE_FIGURE.get(txn.txn_type)
    if figure is None:
        return "none", None
    if figure == "purchases":
        return figure, cash_paid(txn)
    if figure == "proceeds":
        return figure, cash_received(txn)
    if figure == "dividends":
        return figure, txn.dividend_amount
    price = transfer_prices.get((txn.isin, txn.txn_date))
    return figure, None if price is None else txn.quantity * price


def compute_period_bridge(
    transactions: list[Transaction],
    start_prices: dict[str, Decimal],
    end_prices: dict[str, Decimal],
    start_date: date,
    end_date: date,
    transfer_prices: dict[tuple[str, date], Decimal],
) -> PeriodBridge:
    """Opening value to closing value over ``(start_date, end_date]`` -- ENGINE_SPEC
    section 16.

    Opening and closing are section 10's per-ISIN ``start_value``/``end_value`` summed,
    from one ``compute_contribution`` call. The flow lines are the in-window rows grouped
    by ``period_line``, and
    ``gain = closing + proceeds + dividends + transfers_out - opening - purchases -
    transfers_in``. It equals the sum of section 10's gains for the same window and
    prices, transfers included: both value a transfer at the market close on its date.

    Raises:
        MissingPriceError: an ISIN held at either end has no price (via
            ``compute_contribution``), or an in-window transfer has no
            ``transfer_prices`` entry for its date. Never valued at zero or at cost.
    """
    contributions = compute_contribution(
        transactions, start_prices, end_prices, start_date, end_date, transfer_prices
    )
    opening = sum((c.start_value for c in contributions), ZERO)
    closing = sum((c.end_value for c in contributions), ZERO)

    lines = dict.fromkeys(BRIDGE_LINES, ZERO)
    for txn in ordered(transactions, end_date):
        if txn.txn_date <= start_date:
            continue
        figure, amount = period_line(txn, transfer_prices)
        if figure == "none":
            continue
        if amount is None:
            raise MissingPriceError([txn.isin], as_of=txn.txn_date)
        lines[figure] += amount

    gain = (
        closing
        + lines["proceeds"]
        + lines["dividends"]
        + lines["transfers_out"]
        - opening
        - lines["purchases"]
        - lines["transfers_in"]
    )
    return PeriodBridge(opening=opening, closing=closing, gain=gain, **lines)


__all__ = [
    "BRIDGE_FIGURE",
    "BRIDGE_LINES",
    "compute_contribution",
    "compute_period_bridge",
    "period_line",
]
