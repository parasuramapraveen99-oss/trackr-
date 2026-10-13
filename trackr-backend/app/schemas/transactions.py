"""DTOs for GET /investors/{id}/transactions, /lots, /realised, /dividends, and
POST /investors/{id}/transactions."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.engine.types import Source, TxnType
from app.schemas.decimals import DecimalText, NonNegativeDecimalText, OptionalDecimalText

#: docs/DATA_MODEL.md's txn_type table: the only three types whose documented quantity
#: effect is 0 -- DIVIDEND ("qty = 0, price = 0, amount in dividend_amount") and the
#: DEMERGER_COST_OUT/IN pair ("a row whose quantity and price are both zero", the amount
#: living in cost_transfer instead). Every other type has a real + or - quantity effect
#: and must carry a genuine, positive quantity.
_ZERO_QUANTITY_TYPES = frozenset(
    {TxnType.DIVIDEND, TxnType.DEMERGER_COST_OUT, TxnType.DEMERGER_COST_IN}
)

#: Comfortably above any real single manual entry (the golden portfolio's largest line
#: is 2,250 shares) and far below the range where the UI's numeric columns start
#: overlapping -- a guard against a fat-fingered extra digit, not a real trading limit.
_MAX_SANE_QUANTITY = Decimal("100000000")


class TransactionIn(BaseModel):
    """One row of the POST /investors/{id}/transactions request body.

    Mirrors the writable fields of ``schema/transaction.schema.json``. ``txn_type`` and
    ``source`` reuse the engine's own enums rather than a second hand-typed list, so this
    can never drift from ``docs/DATA_MODEL.md`` -- an unlisted value is a validation
    error (422), never silently accepted. ``id``, ``dedupe_key`` and ``created_at`` are
    deliberately absent: the first and third are server-assigned on insert, and the key
    is always derived by ``app.engine.dedupe.make_dedupe_key`` rather than accepted from
    a caller who has no way to compute it correctly.

    ``account_id`` is required here even though the shared schema leaves it optional --
    that optionality is for pure-engine fixtures; ``transaction.account_id`` is
    ``NOT NULL`` in the database, and failing that at the request boundary with a 422
    beats a raw integrity-constraint error surfacing as a 500.
    """

    account_id: str
    isin: str = Field(pattern=r"^IN[A-Z0-9]{10}$")
    txn_date: date
    txn_type: TxnType
    # Every money/quantity field below takes exact decimal TEXT: a JSON number is
    # refused at the boundary rather than silently widened into a float. See
    # app/schemas/decimals.py -- this ledger is append-only, so a value that arrives
    # wrong stays wrong. `NonNegative...` marks the two fields that already carried a
    # `ge=0` bound; no field gains or loses one here.
    quantity: NonNegativeDecimalText = Field(ge=0, le=_MAX_SANE_QUANTITY)
    price: NonNegativeDecimalText = Field(ge=0)
    charges: DecimalText = Decimal("0")
    stt: DecimalText = Decimal("0")
    dividend_amount: DecimalText = Decimal("0")
    # NET of tax withheld. None means UNKNOWN, not zero -- see migration 030. A manual
    # dividend entry supplies this only when a document actually states it. The default
    # and an explicit `null` are both still accepted; only a NUMBER is not.
    tds_amount: OptionalDecimalText = None
    cost_transfer: DecimalText = Decimal("0")
    source: Source | None = None
    source_ref: str | None = None
    raw_payload: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _quantity_and_price_match_txn_type(self) -> TransactionIn:
        """The two bugs a fresh field test actually found: a BUY with quantity 0 and a
        negative price both landed silently in the append-only ledger, where nothing can
        ever edit or delete them. ``ge=0`` on the fields above only keeps DIVIDEND and the
        DEMERGER_COST pair's legitimate zero rows working; it does not stop a real trade
        from being zero. This enforces the other half of docs/DATA_MODEL.md's txn_type
        table: those three types carry no quantity or price at all, and every other type
        must carry a genuine, positive one."""
        if self.txn_type in _ZERO_QUANTITY_TYPES:
            if self.quantity != 0:
                raise ValueError(
                    f"{self.txn_type} rows carry no quantity -- see docs/DATA_MODEL.md's "
                    "txn_type table"
                )
            if self.price != 0:
                raise ValueError(
                    f"{self.txn_type} rows carry no price -- the amount belongs in "
                    "dividend_amount or cost_transfer instead"
                )
        elif self.quantity <= 0:
            raise ValueError(f"{self.txn_type} requires a positive quantity")
        return self


class ImportSummaryOut(BaseModel):
    """Mirrors ``engine.types.ImportSummary`` -- the response shape for
    POST /investors/{id}/transactions per contracts/openapi.yaml."""

    inserted: int
    skipped_duplicates: int
    rejected: list[dict[str, Any]]


class TransactionOut(BaseModel):
    id: str | None
    account_id: str | None
    isin: str
    txn_date: date
    txn_type: str
    quantity: str
    price: str
    charges: str
    stt: str
    dividend_amount: str
    # NET received. `tds_amount` is null when unknown -- which is NOT a claim that no tax
    # was withheld. Gross is the sum of the two; it is derived, so it is not sent.
    tds_amount: str | None = None
    cost_transfer: str
    source: str | None
    source_ref: str | None
    created_at: datetime | None = None


class LotOut(BaseModel):
    isin: str
    open_date: date
    quantity_open: str
    cost_per_unit: str
    acquisition_date_basis: str | None = None


class RealisedOut(BaseModel):
    isin: str | None
    sell_date: date | None
    quantity: str
    proceeds: str
    cost: str
    gain: str
    term: str
    holding_days: int | None
    acquisition_date_basis: str | None = None
    classification_sensitive: bool = False


class DividendYearOut(BaseModel):
    fy: str
    amount: str


class DividendSummaryOut(BaseModel):
    isin: str
    total: str
    by_year: list[DividendYearOut]


class ExpectedDividendOut(BaseModel):
    """A declared dividend sized to this investor -- a PROJECTION, never income.

    ``gross_expected`` is gross because nothing has been withheld from money that has not
    moved. There is deliberately no ``net`` and no ``tds`` field: both would imply a
    payment happened.
    """

    isin: str
    ex_date: date
    kind: str
    amount_per_share: str
    eligible_quantity: str
    gross_expected: str
    record_date: date | None = None
    payment_date: date | None = None
    event_id: str | None = None


class UnprojectableDividendOut(BaseModel):
    """A declared dividend that could not be sized, and why -- shown rather than dropped,
    the same contract the holdings exclusions surface already has."""

    isin: str
    ex_date: date
    code: str
    detail: str


class ExpectedDividendsOut(BaseModel):
    """The projection envelope.

    ``total_gross_expected`` is a subtotal of PROJECTIONS ONLY and must never be added to
    the received figure from ``/dividends``: one is money that arrived, the other is money
    a company said it would pay. Kept in separate envelopes so no caller can sum them by
    accident.
    """

    expected: list[ExpectedDividendOut]
    unprojectable: list[UnprojectableDividendOut]
    total_gross_expected: str
    #: Fund-unit ISINs (INF...) left out of dividend tracking -- named, not hidden.
    excluded_fund_units: list[str] = []


class RefreshDividendFeedOut(BaseModel):
    """Result of one automated dividend-notice feed run -- see
    ``app.db.dividend_events.refresh_dividend_feed``. Keys beyond ``fetched`` and
    ``error`` are dynamic outcome counts (``inserted``, ``corroborated``, ``classified``,
    ``conflict``, ``ambiguous``, ``unchanged``, ``failed``) -- normalised here to fixed,
    always-present fields so the response shape does not depend on which outcomes a
    particular run happened to produce."""

    fetched: int = 0
    inserted: int = 0
    corroborated: int = 0
    classified: int = 0
    conflict: int = 0
    ambiguous: int = 0
    unchanged: int = 0
    failed: int = 0
    error: str | None = None


class DiscoverDividendsYfinanceOut(BaseModel):
    """Result of one yfinance dividend-history discovery run -- see
    ``app.db.dividend_events.discover_dividends_from_yfinance``. Occasional, not daily:
    a ticker's whole dividend history comes back in one call."""

    inserted: int = 0
    corroborated: int = 0
    classified: int = 0
    conflict: int = 0
    ambiguous: int = 0
    unchanged: int = 0
    failed: int = 0


class ReconcileDividendsOut(BaseModel):
    """Result of one second-pass run -- see ``app.db.dividend_reconcile``. ``pending`` is
    the leftover DRAFT events a holder was eligible for; the rest count what happened to
    the ones processed this run (bounded, so a backlog drains across days)."""

    pending: int = 0
    verified: int = 0
    disputed: int = 0
    no_nse_row: int = 0
    ambiguous: int = 0
    no_bhavcopy: int = 0
    identity_unproven: int = 0
    nse_sourced: int = 0
    failed: int = 0
    #: Automatic amount disputes re-checked against later bonuses/splits, and how many of
    #: them that verified (Yahoo's scaled figure restated -- app.db.dividend_events).
    disputes_rechecked: int = 0
    disputes_restated: int = 0
    #: NSE notices verified from NSE's own bhavcopy, and why the rest were not.
    nse_self_checked: int = 0
    self_check_identity_unproven: int = 0
    self_check_implausible: int = 0
    self_check_ambiguous: int = 0
    self_check_already_verified: int = 0
    #: Stored NSE notices from the month sweep landed as dividends, and refused ones.
    stored_notices_landed: int = 0
    stored_notices_unparseable: int = 0
