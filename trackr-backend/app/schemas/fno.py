"""DTOs for GET /investors/{id}/fno/positions, /fno/realised, /fno/tax.

Equity F&O, a separate ledger (docs/ARCHITECTURE_INVARIANTS.md rule 21). Every financial
field is an exact decimal string. Nothing here is blended with an equity figure (OD-F8).
Every response names what it leaves out (rule 16): positions with no usable price
(``excluded``) and positions past expiry the engine will not close
(``expired_unresolved``).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field


class FnoContractOut(BaseModel):
    """The contract's identity (rule 21(a)). ``strike``/``option_type`` are None for a
    future; ``lot_size`` None when only the legacy NSE archive has listed it."""

    contract_id: str
    exchange: str
    instrument_type: str
    underlying: str
    expiry: date
    strike: str | None
    option_type: str | None
    lot_size: int | None


class FnoLotOut(BaseModel):
    open_trade_id: str
    open_date: date
    quantity: str
    price: str
    charges: str


class FnoPositionOut(FnoContractOut):
    """One open, unexpired, marked position: ``schema/fno_position.schema.json`` plus the
    contract identity. ``mark_receipt`` is the bhavcopy provenance of the mark."""

    account_id: str
    side: str
    quantity: str
    lots: list[FnoLotOut]
    mark: str
    mark_date: date
    mark_receipt: dict[str, Any] | None
    unrealised: str


class FnoUnresolvedOut(FnoContractOut):
    """A position the engine leaves out of a total, named. ``reason`` is
    ``MISSING_PRICE`` (unexpired, no mark: never valued at 0),
    ``SETTLEMENT_PRICE_MISSING`` or ``PHYSICAL_SETTLEMENT_UNCONFIRMED`` (past expiry and
    not closed: excluded from realised P&L), or a trade-level exclusion
    (``TRADE_AFTER_EXPIRY``, ``SETTLEMENT_EXCEEDS_POSITION``, ``INVALID_REVERSAL``)."""

    account_id: str
    side: str
    quantity: str
    lots: list[FnoLotOut]
    reason: str
    detail: str | None = None


class FnoPositionsResponse(BaseModel):
    """``unrealised_total`` sums ``positions`` only; ``excluded`` are not in it."""

    as_of: date
    has_trades: bool
    positions: list[FnoPositionOut]
    unrealised_total: str
    excluded: list[FnoUnresolvedOut]
    expired_unresolved: list[FnoUnresolvedOut]


class FnoMatchOut(BaseModel):
    account_id: str
    contract_id: str
    underlying: str
    side: str
    open_trade_id: str
    open_date: date
    close_trade_id: str | None
    close_date: date
    close_kind: str
    quantity: str
    open_price: str
    close_price: str
    pnl: str
    charges: str
    stt: str
    financial_year: str
    settlement_receipt: dict[str, Any] | None
    #: True only for an engine-derived EXPIRY_SETTLE leg (no contract-note expiry line
    #: seen); ``note`` then says so in plain language. EXCHANGE_SETTLEMENT is not
    #: provisional.
    provisional: bool
    note: str | None


class FnoTotalsOut(BaseModel):
    key: str
    pnl: str
    charges: str
    matches: int
    #: Whether this total contains provisional EXPIRY_SETTLE legs, and how many.
    includes_provisional: bool
    provisional_matches: int


class FnoRealisedResponse(BaseModel):
    """Realised matches (ENGINE_SPEC section 15 rule 3), filtered to ``fy`` when given.
    ``pnl`` is price P&L; ``charges`` are never netted into it (rule 4)."""

    as_of: date
    fy: str | None
    matches: list[FnoMatchOut]
    by_fy: list[FnoTotalsOut]
    by_underlying: list[FnoTotalsOut]
    expired_unresolved: list[FnoUnresolvedOut]


class FnoLossOut(BaseModel):
    origin_fy: str
    amount: str
    last_set_off_fy: str


class FnoFlagsOut(BaseModel):
    """s.44AB flags, never determinations. ``unknown`` where Trackr+ cannot know."""

    turnover_above_1_crore: bool
    turnover_above_10_crore: bool
    net_profit_below_6_percent: bool
    cash_within_5_percent: str
    income_above_exemption_limit: str
    return_filed_by_due_date: str
    unconfirmed_by_ca: bool


class FnoTaxYearOut(BaseModel):
    financial_year: str
    matches: int
    includes_provisional: bool
    provisional_matches: int
    turnover: str
    realised_pnl: str
    expenses: str
    stt: str
    net_business_income: str
    set_off: list[FnoLossOut]
    income_after_set_off: str
    expired: list[FnoLossOut]
    carried_forward: list[FnoLossOut]
    flags: FnoFlagsOut
    unconfirmed_by_ca: bool


class FnoTaxResponse(BaseModel):
    """DRAFT, UNCONFIRMED BY CA (docs/TAX_ENGINE.md, Equity F&O). Figures for the user's
    CA; Trackr+ does not file. Positions in ``expired_unresolved`` are not in any year."""

    as_of: date
    fy: str | None
    unconfirmed_by_ca: bool
    years: list[FnoTaxYearOut]
    expired_unresolved: list[FnoUnresolvedOut]


class FnoTradeOut(BaseModel):
    """One ``fno_trade`` row with its contract identity. ``reversed`` is true when a
    REVERSAL row names it; a REVERSAL row carries ``reverses_id``."""

    id: str
    account_id: str
    contract_id: str
    trade_date: date
    side: str
    quantity: str
    price: str
    charges: str
    stt: str
    kind: str
    reverses_id: str | None
    source: str
    seq: int
    exchange: str | None = None
    instrument_type: str | None = None
    underlying: str | None = None
    expiry: date | None = None
    strike: str | None = None
    option_type: str | None = None
    reversed: bool


class FnoReverseIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
