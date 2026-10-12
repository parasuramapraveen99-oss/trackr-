"""DTOs for POST /investors/{id}/reconcile -- ENGINE_SPEC section 7,
schema/discrepancy.schema.json."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

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

    as_of: date | None = None
    source: str | None = None
    holdings: list[DeclaredHoldingIn] = Field(default_factory=list)


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
    computed_qty: str
    declared_qty: str
    difference: str
    ratio: str | None = None
    likely_cause: str | None = None
