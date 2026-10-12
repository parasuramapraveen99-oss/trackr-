"""DTOs for /families and GET /investors/overview -- the adviser's clients view.

Money and rates cross as strings (app.database.money / rate), never JSON numbers.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


class FamilyIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class FamilyOut(BaseModel):
    id: str
    name: str
    created_at: datetime


class InvestorFamilyIn(BaseModel):
    """PUT /investors/{id}/family. ``family_id`` null takes the client out of its family."""

    family_id: str | None = None


class OverviewFigures(BaseModel):
    """Totals over PRICED holdings only, exactly as /summary computes them. Null when
    ``error`` is set: nothing is shown for a ledger that could not be valued."""

    invested: str | None = None
    market_value: str | None = None
    unrealised: str | None = None
    unrealised_pct: str | None = None
    #: Holdings left out of the totals because no price exists for them.
    unpriced_count: int = 0
    #: Stocks left out because their own trades do not add up (/summary's ``skipped``).
    skipped_count: int = 0
    error: str | None = None


class OverviewAccount(OverviewFigures):
    id: str
    broker: str
    label: str | None = None
    is_advised: bool


class OverviewClient(OverviewFigures):
    id: str
    code: str
    display_name: str
    family_group_id: str | None = None
    accounts: list[OverviewAccount]


class ClientsOverview(BaseModel):
    as_of: date
    families: list[FamilyOut]
    clients: list[OverviewClient]
