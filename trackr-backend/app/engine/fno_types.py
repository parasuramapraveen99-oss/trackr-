"""Object shapes for the F&O engine -- docs/ENGINE_SPEC.md section 15.

Kept apart from ``engine/types.py`` on purpose: F&O is a separate ledger
(``docs/ARCHITECTURE_INVARIANTS.md`` rule 21(b)) with its own key, a contract identity
rather than an ISIN, so nothing here may be mistaken for an equity ``Transaction``.

``FnoTrade`` is the stable boundary: the parsers (FNO_PLAN Piece 1) emit it and
``app.engine.fno.compute_fno`` consumes it. Its field names are a contract; rename one
and both sides break.

Money and quantity are ``Decimal``, always. Identifiers are ``str`` (a database uuid is
passed as its string form). Nothing here imports a framework or a layer above the engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from app.engine.types import ZERO


class FnoInstrumentType(StrEnum):
    """The bhavcopy ``FinInstrmTp``. Index contracts are cash-settled, stock contracts
    physically settled -- the distinction rules 5 and 7 of section 15 turn on."""

    IDF = "IDF"  # index future
    IDO = "IDO"  # index option
    STF = "STF"  # stock future
    STO = "STO"  # stock option


INDEX_TYPES = frozenset({FnoInstrumentType.IDF, FnoInstrumentType.IDO})
FUTURE_TYPES = frozenset({FnoInstrumentType.IDF, FnoInstrumentType.STF})


class FnoOptionType(StrEnum):
    CE = "CE"
    PE = "PE"


class FnoSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class FnoTradeKind(StrEnum):
    """``TRADE`` is an order fill. ``EXCHANGE_SETTLEMENT`` is a contract note's expiry
    line (order no ``111111``, time ``00:00:00``) -- a closing trade like any other
    (section 15 rule 6), never an extra one."""

    TRADE = "TRADE"
    EXCHANGE_SETTLEMENT = "EXCHANGE_SETTLEMENT"
    # A correction (rule 15, ARCHITECTURE_INVARIANTS 21(b)): voids the trade named by
    # ``reverses_id``. Never matched, never a close.
    REVERSAL = "REVERSAL"


class FnoCloseKind(StrEnum):
    """What closed a match. ``EXPIRY_SETTLE`` is the engine-derived leg of rule 5: it has
    no ``fno_trade`` row and carries the price receipt instead."""

    TRADE = "TRADE"
    EXCHANGE_SETTLEMENT = "EXCHANGE_SETTLEMENT"
    EXPIRY_SETTLE = "EXPIRY_SETTLE"


class FnoPositionSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class FnoUnresolvedReason(StrEnum):
    """Why a position is not in ``open_positions``.

    ``PHYSICAL_SETTLEMENT_UNCONFIRMED`` -- a stock contract past expiry (rule 7).
    ``SETTLEMENT_PRICE_MISSING`` -- an index contract past expiry with no usable
    expiry-date ``fno_price`` row (rule 5).
    ``MISSING_PRICE`` -- an unexpired position with no price on or before ``as_of``
    (rule 8); it goes to ``excluded``, never valued at 0.
    ``TRADE_AFTER_EXPIRY`` -- a trade dated after its contract's expiry (rule 13); never
    matched, never derived-settled; in ``excluded``.
    ``SETTLEMENT_EXCEEDS_POSITION`` -- the part of an ``EXCHANGE_SETTLEMENT`` line larger
    than the open opposite quantity (rule 14); it never opens a position; in ``excluded``.
    ``INVALID_REVERSAL`` -- a ``REVERSAL`` whose target is missing, already reversed,
    itself a reversal, or in another account/contract (rule 15); voids nothing.
    """

    PHYSICAL_SETTLEMENT_UNCONFIRMED = "PHYSICAL_SETTLEMENT_UNCONFIRMED"
    SETTLEMENT_PRICE_MISSING = "SETTLEMENT_PRICE_MISSING"
    MISSING_PRICE = "MISSING_PRICE"
    TRADE_AFTER_EXPIRY = "TRADE_AFTER_EXPIRY"
    SETTLEMENT_EXCEEDS_POSITION = "SETTLEMENT_EXCEEDS_POSITION"
    INVALID_REVERSAL = "INVALID_REVERSAL"


@dataclass(frozen=True, slots=True)
class FnoContract:
    """One ``fno_contract`` row. ``strike`` and ``option_type`` are None for futures.

    ``lot_size`` is informational here: trade quantities are already in units (rule 1).
    None when the contract is known only from the legacy NSE archive, which carries no
    lot size (migration 063); never inferred.
    """

    id: str
    exchange: str
    instrument_type: FnoInstrumentType
    underlying: str
    expiry: date
    strike: Decimal | None
    option_type: FnoOptionType | None
    lot_size: int | None

    @property
    def is_index(self) -> bool:
        return self.instrument_type in INDEX_TYPES

    @property
    def is_future(self) -> bool:
        return self.instrument_type in FUTURE_TYPES


@dataclass(frozen=True, slots=True)
class FnoTrade:
    """One ``fno_trade`` row. APPEND-ONLY, like ``transaction``.

    ``quantity`` is in units (lots x lot size) and always positive; direction comes from
    ``side``. ``price`` is per unit. ``charges`` is this trade's share of the note's F&O
    charges; ``stt`` is the subset of ``charges`` that is STT.

    ``seq`` is the created order (rule 2 orders by ``(trade_date, created order)``). Two
    trades with equal ``(trade_date, seq)`` keep their input order.

    A correction is a ``kind = REVERSAL`` row whose ``reverses_id`` names the trade it
    voids (rule 15); the original row is never edited.
    """

    id: str
    account_id: str
    contract_id: str
    trade_date: date
    side: FnoSide
    quantity: Decimal
    price: Decimal
    charges: Decimal = ZERO
    stt: Decimal = ZERO
    kind: FnoTradeKind = FnoTradeKind.TRADE
    seq: int = 0
    # Set on a ``REVERSAL`` row: the id of the trade it voids (rule 15).
    reverses_id: str | None = None


@dataclass(frozen=True, slots=True)
class FnoPrice:
    """One ``fno_price`` row. Any price column may be None: absent is not zero."""

    contract_id: str
    price_date: date
    close: Decimal | None
    settlement_price: Decimal | None
    underlying_close: Decimal | None
    receipt: str | None = None


@dataclass(frozen=True, slots=True)
class FnoLot:
    """An open FIFO lot. ``charges`` is the still-unmatched share of the opening trade's
    charges (that trade's charges / its quantity x ``quantity``)."""

    open_trade_id: str
    open_date: date
    quantity: Decimal
    price: Decimal
    charges: Decimal


@dataclass(frozen=True, slots=True)
class FnoMatch:
    """One realised match (rule 3): an opening lot, or part of one, against a close.

    ``pnl`` is price P&L only; ``charges`` are both legs' per-unit shares x ``quantity``
    and are never netted into ``pnl`` (rule 4). ``stt`` is the subset of ``charges`` that
    is STT, apportioned the same way, so the F&O tax report (``app.engine.fno_tax``) can
    show it separately. ``close_trade_id`` is None, and ``settlement_receipt`` set, only
    for an ``EXPIRY_SETTLE`` leg.
    """

    account_id: str
    contract_id: str
    underlying: str
    side: FnoPositionSide
    open_trade_id: str
    open_date: date
    close_trade_id: str | None
    close_date: date
    close_kind: FnoCloseKind
    quantity: Decimal
    open_price: Decimal
    close_price: Decimal
    pnl: Decimal
    charges: Decimal
    financial_year: str
    settlement_receipt: str | None = None
    stt: Decimal = ZERO


@dataclass(frozen=True, slots=True)
class FnoOpenPosition:
    """An open, unexpired position marked at the latest price on or before ``as_of``
    (rule 8). All lots of one position share one side."""

    account_id: str
    contract_id: str
    underlying: str
    side: FnoPositionSide
    quantity: Decimal
    lots: tuple[FnoLot, ...]
    mark: Decimal
    mark_date: date
    mark_receipt: str | None
    unrealised: Decimal


@dataclass(frozen=True, slots=True)
class FnoExpiredUnresolved:
    """A position past expiry the engine will not close (rules 5 and 7). Excluded from
    realised P&L and named, never assumed settled."""

    account_id: str
    contract_id: str
    underlying: str
    side: FnoPositionSide
    quantity: Decimal
    lots: tuple[FnoLot, ...]
    reason: FnoUnresolvedReason


@dataclass(frozen=True, slots=True)
class FnoExcluded:
    """Named, never valued at 0, never in realised P&L. ``reason`` says which case:

    ``MISSING_PRICE`` -- an unexpired position with no usable mark (rule 8);
    ``TRADE_AFTER_EXPIRY`` -- a trade dated after its contract's expiry (rule 13);
    ``SETTLEMENT_EXCEEDS_POSITION`` -- the part of an ``EXCHANGE_SETTLEMENT`` line larger
    than the open opposite quantity (rule 14).
    """

    account_id: str
    contract_id: str
    underlying: str
    side: FnoPositionSide
    quantity: Decimal
    lots: tuple[FnoLot, ...]
    reason: FnoUnresolvedReason
    detail: str


@dataclass(frozen=True, slots=True)
class FnoResult:
    open_positions: tuple[FnoOpenPosition, ...]
    matches: tuple[FnoMatch, ...]
    expired_unresolved: tuple[FnoExpiredUnresolved, ...]
    excluded: tuple[FnoExcluded, ...]


@dataclass(frozen=True, slots=True)
class FnoTotals:
    """A sum of matches (rule 9)."""

    pnl: Decimal
    charges: Decimal
    matches: int
