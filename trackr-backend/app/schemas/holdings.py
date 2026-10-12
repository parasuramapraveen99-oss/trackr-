"""DTOs for GET /investors/{id}/holdings and GET /investors/{id}/summary."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class HoldingOut(BaseModel):
    isin: str
    symbol: str | None
    name: str | None
    quantity: str
    avg_cost: str
    invested: str
    last_price: str | None = None
    market_value: str | None = None
    unrealised: str | None = None
    unrealised_pct: str | None = None
    weight_pct: str | None = None
    cagr: str | None = None
    #: Move since the previous close, as a fraction. Answers "how did this do today",
    #: which is a different question from `unrealised_pct`'s "how has this done since I
    #: bought it". None when there is no earlier close to compare against.
    day_change_pct: str | None = None
    cost_estimated: bool = False
    #: True when `last_price` came from the intraday cache (app.prices.live), not
    #: bhavcopy -- only ever set for as_of == today, before tonight's bhavcopy settles.
    live: bool = False
    #: The date the price was observed (audit D2). Today's settled close and a live quote
    #: carry today's date; a carried close carries its own older date. None only when the
    #: observation date is genuinely unknown -- never filled in.
    price_date: str | None = None
    #: "settled" (bhavcopy close dated as_of), "live" (intraday quote) or "carried"
    #: (an older stored close). Same vocabulary as app.prices.current.
    price_source: str | None = None


class TotalsOut(BaseModel):
    invested: str | None
    market_value: str | None
    unrealised: str | None
    unrealised_pct: str | None = None
    cost_estimated: bool = False


class UnpricedHoldingOut(BaseModel):
    isin: str
    symbol: str | None
    name: str | None
    invested: str


class UnpricedOut(BaseModel):
    count: int
    invested: str | None
    holdings: list[UnpricedHoldingOut]


class SkippedHoldingOut(BaseModel):
    """One ISIN excluded because its own ledger segment didn't reconcile --
    ``compute_holdings_partitioned`` (app/engine/holdings.py), named individually the
    same way UnpricedHoldingOut names a missing price, so the dashboard can point at
    exactly what's wrong instead of the whole screen going blank over it."""

    isin: str
    symbol: str | None = None
    name: str | None = None
    message: str
    deficit: str | None = None
    as_of: date | None = None


class AllocationGroupOut(BaseModel):
    asset_class: str
    label: str
    market_value: str | None
    weight_pct: str | None = None
    children: list[HoldingOut]


class SectorAllocationGroupOut(BaseModel):
    sector: str | None
    label: str
    market_value: str | None
    weight_pct: str | None = None
    children: list[HoldingOut]


class MarketCapAllocationGroupOut(BaseModel):
    market_cap_bucket: str | None
    label: str
    market_value: str | None
    weight_pct: str | None = None
    children: list[HoldingOut]


class HoldingsResponse(BaseModel):
    as_of: date
    holdings: list[HoldingOut]
    totals: TotalsOut
    unpriced: UnpricedOut
    skipped: list[SkippedHoldingOut] = []
    #: ISO 8601 timestamp of the freshest intraday quote behind any `live` holding this
    #: response, or null when nothing here is live (a past `as_of`, or today but every
    #: price came from bhavcopy already).
    live_as_of: str | None = None


class SummaryResponse(BaseModel):
    as_of: date
    invested: str | None
    market_value: str | None
    unrealised: str | None
    unrealised_pct: str | None = None
    cost_estimated: bool = False
    unpriced: UnpricedOut
    skipped: list[SkippedHoldingOut] = []
    allocation: list[AllocationGroupOut]
    allocation_by_sector: list[SectorAllocationGroupOut]
    allocation_by_market_cap: list[MarketCapAllocationGroupOut]
    live_as_of: str | None = None
