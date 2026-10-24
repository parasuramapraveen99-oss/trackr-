"""DTOs for POST /investors/{id}/reconcile -- ENGINE_SPEC section 7,
schema/discrepancy.schema.json."""

from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.engine.types import TxnType
from app.schemas.decimals import NonNegativeDecimalText


class DeclaredHoldingIn(BaseModel):
    """One CAS- or broker-declared quantity, per contracts/openapi.yaml's
    ``/investors/{id}/reconcile`` request body. ``quantity`` is never negative -- a
    depository or broker statement does not declare a short position."""

    isin: str = Field(pattern=r"^IN[A-Z0-9]{10}$")
    #: Exact decimal text, not a JSON number -- app/schemas/decimals.py. A declared
    #: quantity is the thing the ledger is being CHECKED against, so a float here would
    #: manufacture the very drift the reconciliation is meant to detect.
    quantity: NonNegativeDecimalText = Field(ge=0)


class ReconcileRequest(BaseModel):
    """The full request body. ``source`` is accepted per the contract but not yet
    consumed anywhere -- ``app.engine.reconcile.reconcile`` has no parameter for it, and
    CLAUDE.md forbids inventing financial logic that ENGINE_SPEC does not state."""

    account_id: UUID | None = None
    as_of: date | None = None
    source: str | None = None
    holdings: list[DeclaredHoldingIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def reject_duplicates(self):
        if len({h.isin for h in self.holdings}) != len(self.holdings):
            raise ValueError("Duplicate ISINs are ambiguous")
        return self


class DiscrepancyOut(BaseModel):
    """Mirrors schema/discrepancy.schema.json field for field. ``computed_qty``,
    ``declared_qty``, ``difference`` AND ``ratio`` all cross as strings, per that schema's
    ``decimal`` ``$ref`` -- same convention as every other decimal field in this API.

    ``ratio`` was a JSON number until BE 02. It is declared/computed quantity, so a
    two-for-one bonus reads exactly ``"2"`` and a one-third rights entitlement reads
    ``"0.333333..."`` with every digit the engine solved, rather than a double's nearest
    approximation to it. The likely-cause hint downstream compares it to exact ratios.
    """

    isin: str
    computed_qty: str | None
    declared_qty: str | None
    difference: str | None
    ratio: str | None = None
    likely_cause: str | None = None

    status: str = "MISMATCH"
    error: str | None = None
    error_date: date | None = None
    deficit: str | None = None
    reason_code: str | None = None


# Saved runs deliberately require scope and provenance; the legacy endpoint remains
# available for old clients, but refuses ambiguous multi-account requests.


class PendingSettlementIn(BaseModel):
    transaction_id: UUID
    settlement_date: date
    evidence_ref: str = Field(min_length=1, max_length=1000)


class HistoryEvidenceIn(BaseModel):
    model_config = {"validate_default": True, "extra": "forbid"}
    isin: str = Field(pattern=r"^IN[A-Z0-9]{10}$")
    txn_date: date
    txn_type: TxnType
    quantity: NonNegativeDecimalText
    price: NonNegativeDecimalText
    charges: NonNegativeDecimalText
    stt: NonNegativeDecimalText
    dividend_amount: NonNegativeDecimalText = "0"
    tds_amount: NonNegativeDecimalText | None = None
    cost_transfer: NonNegativeDecimalText = "0"


class ReconciliationRunIn(BaseModel):
    model_config = {"extra": "forbid"}
    account_id: UUID
    as_of: date
    source: str = Field(min_length=1, max_length=1000)
    mode: Literal["HOLDINGS", "HISTORY"] = "HOLDINGS"
    complete_statement: bool = False
    use_snapshot: bool = False
    statement_id: UUID | None = None
    holdings: list[DeclaredHoldingIn] = Field(default_factory=list, max_length=10000)
    basis: Literal["TRADE_DATE", "SETTLED"] = "TRADE_DATE"
    pending_settlements: list[PendingSettlementIn] = Field(default_factory=list, max_length=1000)
    date_from: date | None = None
    transaction_types: list[TxnType] = Field(default_factory=list)
    evidence: list[HistoryEvidenceIn] = Field(default_factory=list, max_length=20000)

    @model_validator(mode="after")
    def validate_scope(self):
        if not self.source.strip():
            raise ValueError("Source reference must not be blank")
        if len({h.isin for h in self.holdings}) != len(self.holdings):
            raise ValueError("Duplicate ISINs: consolidate quantities for this account first")
        if self.statement_id is not None and not self.use_snapshot:
            raise ValueError("Statement ID requires imported statement mode")
        if self.use_snapshot and self.holdings:
            raise ValueError("Choose a saved snapshot or entered holdings, not both")
        if len({p.transaction_id for p in self.pending_settlements}) != len(
            self.pending_settlements
        ):
            raise ValueError("Duplicate pending transaction")
        if any(
            p.settlement_date <= self.as_of or not p.evidence_ref.strip()
            for p in self.pending_settlements
        ):
            raise ValueError("Pending settlements require a future settlement date and evidence")
        if self.pending_settlements and self.basis != "SETTLED":
            raise ValueError("Pending settlements require a settled-balance statement")
        if self.mode == "HISTORY":
            if self.date_from is None or self.date_from > self.as_of:
                raise ValueError("History requires a valid inclusive date range")
            if not self.transaction_types:
                raise ValueError("Declare which transaction types the evidence covers")
            if self.use_snapshot or self.holdings or self.pending_settlements:
                raise ValueError("Holdings inputs cannot be mixed with history evidence")
            if any(
                not self.date_from <= t.txn_date <= self.as_of
                or t.txn_type not in self.transaction_types
                for t in self.evidence
            ):
                raise ValueError("Evidence falls outside the declared dates or transaction types")
        elif self.evidence or self.date_from or self.transaction_types:
            raise ValueError("Transaction evidence requires HISTORY mode")
        return self
