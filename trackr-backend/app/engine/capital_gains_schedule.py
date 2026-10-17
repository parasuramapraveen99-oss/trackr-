"""One taxpayer's capital-gains schedule for one financial year: listed shares and fund
units together -- docs/MF_ETF_PLAN.md section 5.5.

The caller has already classified every realised slice into a rate line (bucket + base
rate) with its TAX cost (STT excluded, grandfathered or indexed where the law says) and
tax consideration. This module only does the year-level arithmetic, in the statute's order:

1. NET within each line (same bucket, same base rate).
2. SET OFF losses within the head "capital gains" (1961 Act s.70; 2025 Act equivalent):
   a short-term loss against any gain, a long-term loss against long-term gains only.
   The order is the taxpayer's choice; this uses a fixed, stated order (``SET_OFF_ORDER``):
   ordinary-rate gains first (the slab rate of an investor with gains is usually at or
   above the special rates), then special-rate lines from the highest rate down, equity
   LTCG last so its exemption is not wasted. The order used is returned with the result.
3. EXEMPT one annual threshold of equity LTCG (s.112A / 2025 Act s.198): Rs 1,00,000 for
   FY 2018-19 to 2023-24, Rs 1,25,000 from FY 2024-25 -- once per taxpayer per year,
   shared by shares and equity-oriented funds, never per broker or folio. In FY 2024-25,
   where 10% and 12.5% lines coexist, it is applied to the 12.5% line first.
4. BASE TAX on special-rate lines (taxable x rate). Ordinary-rate gains are listed as
   taxable but their tax is NOT calculated: it depends on the taxpayer's other income,
   regime and slab. No surcharge, cess, rebate or basic-exemption adjustment is applied.

Unabsorbed losses are reported for carry-forward; using them in a later year needs the
return to have been filed in time, which this system cannot know.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum

from app.engine.types import ZERO


class Bucket(StrEnum):
    EQUITY_STCG = "EQUITY_STCG"  # 1961 s.111A / 2025 s.196
    EQUITY_LTCG = "EQUITY_LTCG"  # 1961 s.112A / 2025 s.198
    OTHER_LTCG = "OTHER_LTCG"  # 1961 s.112 / 2025 s.197
    ORDINARY_STCG = "ORDINARY_STCG"  # slab rates (incl. specified-fund gains)


LONG_TERM = {Bucket.EQUITY_LTCG, Bucket.OTHER_LTCG}

#: Line order for absorbing losses (see module docstring, step 2). Within a bucket,
#: higher base rates come first.
SET_OFF_ORDER = (Bucket.ORDINARY_STCG, Bucket.EQUITY_STCG, Bucket.OTHER_LTCG, Bucket.EQUITY_LTCG)

EXEMPTION_FROM_FY_2024_25 = Decimal("125000")
EXEMPTION_FY_2018_19_TO_2023_24 = Decimal("100000")


@dataclass(frozen=True)
class ScheduleSlice:
    isin: str
    instrument_kind: str
    sell_date: date
    acquired: date
    quantity: Decimal
    consideration: Decimal
    cost: Decimal
    bucket: Bucket
    #: None for ORDINARY_STCG (slab rate, not known here).
    base_rate: Decimal | None
    section: str | None = None
    note: str | None = None

    @property
    def gain(self) -> Decimal:
        return self.consideration - self.cost


@dataclass
class Line:
    bucket: Bucket
    base_rate: Decimal | None
    gains: Decimal = ZERO
    losses: Decimal = ZERO
    set_off_received: Decimal = ZERO
    exemption: Decimal = ZERO
    taxable: Decimal = ZERO
    base_tax: Decimal | None = None
    slices: int = 0

    @property
    def net(self) -> Decimal:
        return self.gains - self.losses


@dataclass
class Schedule:
    fy: str
    lines: list[Line]
    exemption_limit: Decimal
    exemption_used: Decimal
    special_rate_tax: Decimal
    ordinary_rate_taxable: Decimal
    carry_forward_short_term_loss: Decimal
    carry_forward_long_term_loss: Decimal
    set_off_order: tuple[str, ...]
    notes: list[str] = field(default_factory=list)


def exemption_limit(fy_start: date) -> Decimal:
    if fy_start >= date(2024, 4, 1):
        return EXEMPTION_FROM_FY_2024_25
    if fy_start >= date(2018, 4, 1):
        return EXEMPTION_FY_2018_19_TO_2023_24
    raise ValueError("Section 112A did not exist before FY 2018-19; not supported")


def _rank(line: Line) -> tuple[int, Decimal]:
    return SET_OFF_ORDER.index(line.bucket), -(line.base_rate or Decimal(1))


def build_schedule(fy: str, fy_start: date, slices: Iterable[ScheduleSlice]) -> Schedule:
    lines: dict[tuple[Bucket, Decimal | None], Line] = {}
    for s in slices:
        line = lines.setdefault((s.bucket, s.base_rate), Line(s.bucket, s.base_rate))
        line.slices += 1
        if s.gain >= 0:
            line.gains += s.gain
        else:
            line.losses += -s.gain
    ordered = sorted(lines.values(), key=_rank)
    for line in ordered:
        line.taxable = max(line.net, ZERO)

    short_loss = sum((-ln.net for ln in ordered if ln.net < 0 and ln.bucket not in LONG_TERM), ZERO)
    long_loss = sum((-ln.net for ln in ordered if ln.net < 0 and ln.bucket in LONG_TERM), ZERO)

    # Long-term losses: long-term lines only.
    for line in ordered:
        if line.bucket in LONG_TERM and long_loss > 0 and line.taxable > 0:
            used = min(long_loss, line.taxable)
            line.taxable -= used
            line.set_off_received += used
            long_loss -= used
    # Short-term losses: any line.
    for line in ordered:
        if short_loss > 0 and line.taxable > 0:
            used = min(short_loss, line.taxable)
            line.taxable -= used
            line.set_off_received += used
            short_loss -= used

    limit = exemption_limit(fy_start)
    remaining = limit
    for line in sorted(
        (ln for ln in ordered if ln.bucket == Bucket.EQUITY_LTCG),
        key=lambda ln: -(ln.base_rate or 0),
    ):
        used = min(remaining, line.taxable)
        line.exemption = used
        line.taxable -= used
        remaining -= used

    special = ZERO
    ordinary = ZERO
    for line in ordered:
        if line.base_rate is None:
            ordinary += line.taxable
        else:
            line.base_tax = line.taxable * line.base_rate
            special += line.base_tax

    return Schedule(
        fy=fy,
        lines=ordered,
        exemption_limit=limit,
        exemption_used=limit - remaining,
        special_rate_tax=special,
        ordinary_rate_taxable=ordinary,
        carry_forward_short_term_loss=short_loss,
        carry_forward_long_term_loss=long_loss,
        set_off_order=tuple(str(b) for b in SET_OFF_ORDER),
        notes=[
            "Base tax only: surcharge, health & education cess, rebate and any unused "
            "basic-exemption adjustment are not applied.",
            "Ordinary-rate gains are taxed at the taxpayer's slab rate; that tax is not "
            "calculated here.",
            "Losses are set off in the stated order; the taxpayer may choose another in "
            "the return.",
            "Carry-forward losses can be used later only if the return is filed on time.",
        ],
    )
