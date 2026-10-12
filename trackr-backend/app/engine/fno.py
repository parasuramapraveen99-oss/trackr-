"""F&O positions and P&L -- docs/ENGINE_SPEC.md section 15.

Pure, Decimal only, no framework imports. Inputs are ``fno_trade`` and ``fno_price``
rows as ``app.engine.fno_types`` objects; the engine does not know the broker or file.
Nothing here feeds equity XIRR, TWRR, allocation, contribution or snapshots (rule 10).

One traversal: trades in ``(trade_date, seq)`` order are FIFO-matched per
``(account, contract)``; what is still open at the end is then either derived-settled at
expiry (index contracts), left unresolved (stock contracts past expiry, or a missing
expiry-date price), or marked at the latest price (unexpired).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal

from app.engine.errors import FnoMissingPriceError, FnoQuantityError
from app.engine.fno_types import (
    ZERO,
    FnoCloseKind,
    FnoContract,
    FnoExcluded,
    FnoExpiredUnresolved,
    FnoLot,
    FnoMatch,
    FnoOpenPosition,
    FnoOptionType,
    FnoPositionSide,
    FnoPrice,
    FnoResult,
    FnoSide,
    FnoTotals,
    FnoTrade,
    FnoTradeKind,
    FnoUnresolvedReason,
)
from app.engine.xirr import financial_year_start


@dataclass
class _OpenLot:
    """A lot still open during replay. Mutable, unlike the FnoLot that is emitted."""

    open_trade_id: str
    open_date: date
    side: FnoSide
    quantity: Decimal
    price: Decimal
    # The unmatched share of the opening trade's charges, held as an amount rather than
    # a per-unit figure: see _share.
    charges: Decimal
    # The unmatched share of the opening trade's STT (a subset of ``charges``), same rule.
    stt: Decimal = ZERO

    def frozen(self) -> FnoLot:
        return FnoLot(self.open_trade_id, self.open_date, self.quantity, self.price, self.charges)


#: NUMERIC(20,6): the precision charges and STT are apportioned at (rule 11).
CHARGE_QUANTUM = Decimal("0.000001")


def _share(charges: Decimal, quantity: Decimal, taken: Decimal) -> Decimal:
    """Rules 4 and 11: ``charges x taken / quantity`` rounded half-even to 6 decimal
    places; when ``taken`` is all that is left, exactly the remainder. Because every share
    is a 6-dp amount the remainder is exact too, so the shares plus whatever stays on the
    lot always sum to the original charges -- every rupee assigned exactly once."""
    if taken == quantity:
        return charges
    return (charges * taken / quantity).quantize(CHARGE_QUANTUM, rounding=ROUND_HALF_EVEN)


def financial_year(day: date) -> str:
    """1 April-31 March label of ``day``, e.g. 2025-03-31 -> ``"2024-25"`` (rule 9).
    Same convention as ``app.engine.dividends``."""
    year = financial_year_start(day).year
    return f"{year}-{(year + 1) % 100:02d}"


def _position_side(open_side: FnoSide) -> FnoPositionSide:
    return FnoPositionSide.LONG if open_side is FnoSide.BUY else FnoPositionSide.SHORT


def _pnl(lot: _OpenLot, close_price: Decimal, quantity: Decimal) -> Decimal:
    """Rule 3: long ``(close - open) x qty``; short ``(open - close) x qty``. Also the
    unrealised formula of rule 8 with the mark as ``close_price``."""
    if lot.side is FnoSide.BUY:
        return (close_price - lot.price) * quantity
    return (lot.price - close_price) * quantity


def _match(
    lot: _OpenLot,
    contract: FnoContract,
    account_id: str,
    quantity: Decimal,
    close_price: Decimal,
    close_date: date,
    close_kind: FnoCloseKind,
    close_trade_id: str | None,
    charges: Decimal,
    receipt: str | None = None,
    stt: Decimal = ZERO,
) -> FnoMatch:
    return FnoMatch(
        account_id=account_id,
        contract_id=contract.id,
        underlying=contract.underlying,
        side=_position_side(lot.side),
        open_trade_id=lot.open_trade_id,
        open_date=lot.open_date,
        close_trade_id=close_trade_id,
        close_date=close_date,
        close_kind=close_kind,
        quantity=quantity,
        open_price=lot.price,
        close_price=close_price,
        pnl=_pnl(lot, close_price, quantity),
        # Rule 4: both legs' shares for the matched quantity, never in the price.
        charges=charges,
        financial_year=financial_year(close_date),
        settlement_receipt=receipt,
        stt=stt,
    )


def _apply(
    trade: FnoTrade,
    contract: FnoContract,
    queue: deque[_OpenLot],
    matches: list[FnoMatch],
) -> FnoExcluded | None:
    """Rule 2: an opposite-side trade closes from the front, splitting the oldest lot;
    any excess (or a same-side trade) pushes a new lot. Rule 6: an EXCHANGE_SETTLEMENT
    line is matched exactly like a TRADE, except that (rule 14) its excess never opens a
    lot: it is returned as an ``excluded`` entry instead."""
    remaining = trade.quantity
    remaining_charges = trade.charges
    remaining_stt = trade.stt
    while remaining > ZERO and queue and queue[0].side is not trade.side:
        lot = queue[0]
        taken = min(lot.quantity, remaining)
        open_share = _share(lot.charges, lot.quantity, taken)
        close_share = _share(remaining_charges, remaining, taken)
        open_stt = _share(lot.stt, lot.quantity, taken)
        close_stt = _share(remaining_stt, remaining, taken)
        matches.append(
            _match(
                lot,
                contract,
                trade.account_id,
                taken,
                trade.price,
                trade.trade_date,
                FnoCloseKind(trade.kind.value),
                trade.id,
                open_share + close_share,
                stt=open_stt + close_stt,
            )
        )
        lot.quantity -= taken
        lot.charges -= open_share
        lot.stt -= open_stt
        remaining -= taken
        remaining_charges -= close_share
        remaining_stt -= close_stt
        if lot.quantity <= ZERO:
            queue.popleft()
    if remaining > ZERO and trade.kind is FnoTradeKind.EXCHANGE_SETTLEMENT:
        return _excluded_trade(
            trade,
            contract,
            remaining,
            remaining_charges,
            FnoUnresolvedReason.SETTLEMENT_EXCEEDS_POSITION,
            f"Exchange settlement line {trade.id} exceeds the open position by {remaining}",
        )
    if remaining > ZERO:
        queue.append(
            _OpenLot(
                trade.id,
                trade.trade_date,
                trade.side,
                remaining,
                trade.price,
                remaining_charges,
                remaining_stt,
            )
        )
    return None


def _excluded_trade(
    trade: FnoTrade,
    contract: FnoContract,
    quantity: Decimal,
    charges: Decimal,
    reason: FnoUnresolvedReason,
    detail: str,
) -> FnoExcluded:
    """A trade quantity the engine refuses to turn into a position (rules 13-15)."""
    return FnoExcluded(
        trade.account_id,
        contract.id,
        contract.underlying,
        _position_side(trade.side),
        quantity,
        (FnoLot(trade.id, trade.trade_date, quantity, trade.price, charges),),
        reason,
        detail,
    )


def _expiry_settlement_price(contract: FnoContract, row: FnoPrice | None) -> Decimal | None:
    """Rule 5. Futures at ``settlement_price``; a call at ``max(0, S - K)``, a put at
    ``max(0, K - S)``, ``S = underlying_close``. None when the row or the needed column is
    absent -- a worthless option is a real ``0``, never this None."""
    if row is None:
        return None
    if contract.is_future:
        return row.settlement_price
    spot = row.underlying_close
    if spot is None or contract.strike is None:
        return None
    if contract.option_type is FnoOptionType.CE:
        return max(ZERO, spot - contract.strike)
    return max(ZERO, contract.strike - spot)


def _mark(contract: FnoContract, rows: list[FnoPrice], as_of: date) -> FnoPrice:
    """Rule 8: the latest row on or before ``as_of``. Raises ``FnoMissingPriceError``
    when there is none, or when that row lacks the column the contract is marked at."""
    eligible = [r for r in rows if r.price_date <= as_of]
    if eligible:
        row = max(eligible, key=lambda r: r.price_date)
        if (row.settlement_price if contract.is_future else row.close) is not None:
            return row
    raise FnoMissingPriceError(contract.id, as_of)


def _reversals(
    live: list[FnoTrade], by_contract: dict[str, FnoContract]
) -> tuple[set[str], list[FnoExcluded]]:
    """Rule 15: the ids voided by valid ``REVERSAL`` rows, and an ``INVALID_REVERSAL``
    entry for each reversal that voids nothing. ``live`` is already in replay order and
    truncated at ``as_of``, so the first valid reversal of a trade wins and a reversal
    dated after ``as_of`` has no effect yet."""
    by_id = {t.id: t for t in live}
    voided: set[str] = set()
    refused: list[FnoExcluded] = []
    for r in live:
        if r.kind is not FnoTradeKind.REVERSAL:
            continue
        target = by_id.get(r.reverses_id) if r.reverses_id is not None else None
        if target is None:
            why = "its target is not in the ledger"
        elif target.kind is FnoTradeKind.REVERSAL:
            why = "its target is itself a reversal"
        elif target.id in voided:
            why = "its target is already reversed"
        elif (target.account_id, target.contract_id) != (r.account_id, r.contract_id):
            why = "its target is in another account or contract"
        else:
            voided.add(target.id)
            continue
        refused.append(
            _excluded_trade(
                r,
                by_contract[r.contract_id],
                r.quantity,
                r.charges,
                FnoUnresolvedReason.INVALID_REVERSAL,
                f"Reversal {r.id} of {r.reverses_id} voids nothing: {why}",
            )
        )
    return voided, refused


def compute_fno(
    trades: list[FnoTrade],
    prices: list[FnoPrice],
    contracts: list[FnoContract],
    as_of: date,
) -> FnoResult:
    """Open positions, realised matches, expired-unresolved and excluded positions as at
    ``as_of`` -- ENGINE_SPEC section 15 rules 1-14.

    Trades dated after ``as_of`` are ignored, so the same inputs replay history. A trade
    whose ``contract_id`` is not in ``contracts`` raises ``KeyError``.
    """
    by_contract = {c.id: c for c in contracts}
    rows_by_contract: dict[str, list[FnoPrice]] = {}
    for row in prices:
        rows_by_contract.setdefault(row.contract_id, []).append(row)

    in_order = sorted(trades, key=lambda t: (t.trade_date, t.seq))
    for trade in in_order:
        if trade.quantity <= ZERO:
            raise FnoQuantityError(trade.id, trade.quantity)
    live = [t for t in in_order if t.trade_date <= as_of]
    voided, excluded = _reversals(live, by_contract)

    queues: dict[tuple[str, str], deque[_OpenLot]] = {}
    matches: list[FnoMatch] = []
    for trade in live:
        if trade.kind is FnoTradeKind.REVERSAL or trade.id in voided:
            continue
        contract = by_contract[trade.contract_id]
        if trade.trade_date > contract.expiry:
            # Rule 13: impossible data (a mis-resolved contract or date). Never matched,
            # never derived-settled at an expiry that precedes it.
            excluded.append(
                _excluded_trade(
                    trade,
                    contract,
                    trade.quantity,
                    trade.charges,
                    FnoUnresolvedReason.TRADE_AFTER_EXPIRY,
                    f"Trade {trade.id} dated {trade.trade_date} is after its contract's "
                    f"expiry {contract.expiry}",
                )
            )
            continue
        key = (trade.account_id, trade.contract_id)
        refused = _apply(trade, contract, queues.setdefault(key, deque()), matches)
        if refused is not None:
            excluded.append(refused)

    open_positions: list[FnoOpenPosition] = []
    expired_unresolved: list[FnoExpiredUnresolved] = []

    for (account_id, contract_id), queue in sorted(queues.items()):
        if not queue:
            continue
        contract = by_contract[contract_id]
        rows = rows_by_contract.get(contract_id, [])
        side = _position_side(queue[0].side)
        quantity = sum((lot.quantity for lot in queue), ZERO)
        lots = tuple(lot.frozen() for lot in queue)

        if as_of > contract.expiry:
            if not contract.is_index:
                # Rule 7: physically settled; no derived leg and no invented equity row.
                expired_unresolved.append(
                    FnoExpiredUnresolved(
                        account_id,
                        contract_id,
                        contract.underlying,
                        side,
                        quantity,
                        lots,
                        FnoUnresolvedReason.PHYSICAL_SETTLEMENT_UNCONFIRMED,
                    )
                )
                continue
            row = next((r for r in rows if r.price_date == contract.expiry), None)
            settle = _expiry_settlement_price(contract, row)
            if settle is None:
                expired_unresolved.append(
                    FnoExpiredUnresolved(
                        account_id,
                        contract_id,
                        contract.underlying,
                        side,
                        quantity,
                        lots,
                        FnoUnresolvedReason.SETTLEMENT_PRICE_MISSING,
                    )
                )
                continue
            # Rule 6: only what is still open after any EXCHANGE_SETTLEMENT line.
            for lot in queue:
                matches.append(
                    _match(
                        lot,
                        contract,
                        account_id,
                        lot.quantity,
                        settle,
                        contract.expiry,
                        FnoCloseKind.EXPIRY_SETTLE,
                        None,
                        lot.charges,
                        row.receipt,
                        lot.stt,
                    )
                )
            continue

        try:
            mark_row = _mark(contract, rows, as_of)
        except FnoMissingPriceError as exc:
            excluded.append(
                FnoExcluded(
                    account_id,
                    contract_id,
                    contract.underlying,
                    side,
                    quantity,
                    lots,
                    FnoUnresolvedReason.MISSING_PRICE,
                    str(exc),
                )
            )
            continue
        mark = mark_row.settlement_price if contract.is_future else mark_row.close
        open_positions.append(
            FnoOpenPosition(
                account_id=account_id,
                contract_id=contract_id,
                underlying=contract.underlying,
                side=side,
                quantity=quantity,
                lots=lots,
                mark=mark,
                mark_date=mark_row.price_date,
                mark_receipt=mark_row.receipt,
                unrealised=sum((_pnl(lot, mark, lot.quantity) for lot in queue), ZERO),
            )
        )

    # Stable on close_date: derived expiry legs take their place among same-day closes
    # after the trades processed that day.
    matches.sort(key=lambda m: m.close_date)
    return FnoResult(
        tuple(open_positions), tuple(matches), tuple(expired_unresolved), tuple(excluded)
    )


def _totals(matches: list[FnoMatch] | tuple[FnoMatch, ...], key) -> dict[str, FnoTotals]:
    out: dict[str, FnoTotals] = {}
    for m in matches:
        k = key(m)
        prev = out.get(k, FnoTotals(ZERO, ZERO, 0))
        out[k] = FnoTotals(prev.pnl + m.pnl, prev.charges + m.charges, prev.matches + 1)
    return dict(sorted(out.items()))


def realised_by_fy(matches: list[FnoMatch] | tuple[FnoMatch, ...]) -> dict[str, FnoTotals]:
    """Rule 9: sums of matches per FY of the closing date."""
    return _totals(matches, lambda m: m.financial_year)


def realised_by_underlying(
    matches: list[FnoMatch] | tuple[FnoMatch, ...],
) -> dict[str, FnoTotals]:
    """Rule 9: sums of matches per underlying."""
    return _totals(matches, lambda m: m.underlying)
