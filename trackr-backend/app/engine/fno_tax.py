"""Equity F&O as business income, per FY -- docs/TAX_ENGINE.md "Equity F&O (business
income)", docs/FNO_PLAN.md Piece 4.

**DRAFT. NOT CA-CONFIRMED.** The TAX_ENGINE section this implements is itself a draft
awaiting the owner's CA, and the research-formula delegation does not cover tax. Every
output object therefore carries ``unconfirmed_by_ca = True``, and nothing here may inform
a real filing until the CA signs off. Trackr+ reports figures for the CA; it does not file.

Pure, Decimal only, no framework imports. Input is realised matches from
``app.engine.fno.compute_fno`` (ENGINE_SPEC section 15); each match belongs to the FY of
its closing date (rule 9) and is one "transaction" for turnover (OD-F3). Per FY:

- realised P&L = sum of match ``pnl`` (item 2);
- turnover = sum of ``|pnl|`` per match, option premium NOT added (item 3, ICAI Guidance
  Note on s.44AB, 2022 revision);
- expenses = sum of match ``charges``, STT included as deductible and shown separately as
  ``stt`` so the CA can disallow it (item 4);
- net business income = realised P&L - expenses; unrealised is excluded (item 5);
- losses (item 6): a negative net is a non-speculative business loss carried forward 8
  assessment years, i.e. it may be set off in the 8 FYs after the FY it arose in, and only
  against later F&O net business income here (never another head, never other business
  income, which this module does not see). Oldest loss is set off first. What is left
  after its last year expires at the start of the next FY and is shown as expired;
- s.44AB (item 7): turnover above 1 crore, above 10 crore, and net business income below
  6% of turnover are FLAGS, never determinations. Whether cash receipts/payments are
  within 5%, and whether income exceeds the exemption limit, cannot be known here and
  are reported as ``UNKNOWN``. So is whether the loss-year return was filed by the due
  date (s.80, the carry-forward condition): surfaced as unknown, not modelled.

Grouping follows ITR-3 by name only: Schedule BP (turnover, realised, expenses, net),
Schedule BFLA (set-off of brought-forward loss), Schedule CFL (losses carried forward by
year of origin). No form line numbers are claimed. All figures are unrounded ``Decimal``;
rounding to the rupee is a presentation concern.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.engine.fno_types import FnoMatch
from app.engine.types import ZERO

#: s.44AB(a) turnover threshold (Rs 1 crore) and the raised threshold that applies only
#: when cash receipts and payments are each within 5% (Rs 10 crore). "Above" is strict.
TURNOVER_1_CRORE = Decimal("10000000")
TURNOVER_10_CRORE = Decimal("100000000")
#: Net profit below this share of turnover raises the s.44AB 6% flag. "Below" is strict.
PROFIT_RATIO_FLAG = Decimal("0.06")
#: A non-speculative business loss may be set off in this many FYs after its own.
LOSS_CARRY_FORWARD_YEARS = 8

#: A condition this module cannot evaluate, stated as such rather than decided.
UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class LossAmount:
    """A loss amount keyed by the FY it arose in. ``last_set_off_fy`` is the last FY it
    may be set off in (origin + 8)."""

    origin_fy: str
    amount: Decimal
    last_set_off_fy: str


@dataclass(frozen=True, slots=True)
class Section44ABFlags:
    """Flags for the CA, never a determination that an audit is or is not required.
    UNCONFIRMED BY CA."""

    turnover_above_1_crore: bool
    turnover_above_10_crore: bool
    net_profit_below_6_percent: bool
    cash_within_5_percent: str = UNKNOWN
    income_above_exemption_limit: str = UNKNOWN
    # s.80: a loss is carried forward only if the return for the loss year was filed by
    # the due date. Not knowable here; surfaced as unknown, not modelled.
    return_filed_by_due_date: str = UNKNOWN
    unconfirmed_by_ca: bool = True


@dataclass(frozen=True, slots=True)
class FnoTaxYear:
    """One FY of F&O business income, ITR-3-shaped. UNCONFIRMED BY CA.

    ``income_after_set_off`` is ``net_business_income`` less ``set_off``; in a loss year
    there is no set-off and it equals the (negative) net. ``carried_forward`` is the
    balance at the end of the FY, including this year's loss.
    """

    financial_year: str
    matches: int
    # Schedule BP
    turnover: Decimal
    realised_pnl: Decimal
    expenses: Decimal
    stt: Decimal
    net_business_income: Decimal
    # Schedule BFLA
    set_off: tuple[LossAmount, ...]
    income_after_set_off: Decimal
    # Schedule CFL
    expired: tuple[LossAmount, ...]
    carried_forward: tuple[LossAmount, ...]
    flags: Section44ABFlags
    unconfirmed_by_ca: bool = True


@dataclass(frozen=True, slots=True)
class FnoTaxReport:
    """Contiguous FYs from the first match's FY to the last (or ``through_fy``).
    UNCONFIRMED BY CA; Trackr+ does not file."""

    years: tuple[FnoTaxYear, ...]
    unconfirmed_by_ca: bool = True


def _fy_start(label: str) -> int:
    """``"2024-25"`` -> 2024."""
    return int(label[:4])


def _fy_label(start: int) -> str:
    return f"{start}-{(start + 1) % 100:02d}"


def _flags(turnover: Decimal, net: Decimal) -> Section44ABFlags:
    return Section44ABFlags(
        turnover_above_1_crore=turnover > TURNOVER_1_CRORE,
        turnover_above_10_crore=turnover > TURNOVER_10_CRORE,
        net_profit_below_6_percent=net < turnover * PROFIT_RATIO_FLAG,
    )


def fno_tax_report(
    matches: list[FnoMatch] | tuple[FnoMatch, ...],
    through_fy: str | None = None,
) -> FnoTaxReport:
    """Per-FY F&O business income from realised matches. DRAFT, UNCONFIRMED BY CA.

    Only losses arising in ``matches`` are tracked: a loss from before the first match's
    FY is not known here. ``through_fy`` extends the report past the last match so a
    loss's expiry is visible without later trades. No matches and no ``through_fy`` gives
    an empty report.
    """
    by_fy: dict[str, list[FnoMatch]] = {}
    for m in matches:
        by_fy.setdefault(m.financial_year, []).append(m)
    if not by_fy:
        return FnoTaxReport(())
    first = min(_fy_start(fy) for fy in by_fy)
    last = max(_fy_start(fy) for fy in by_fy)
    if through_fy is not None:
        last = max(last, _fy_start(through_fy))

    # Open losses, oldest first: [origin start year, remaining amount].
    open_losses: list[list] = []
    years: list[FnoTaxYear] = []
    for start in range(first, last + 1):
        fy = _fy_label(start)
        rows = by_fy.get(fy, [])
        realised = sum((m.pnl for m in rows), ZERO)
        turnover = sum((abs(m.pnl) for m in rows), ZERO)
        expenses = sum((m.charges for m in rows), ZERO)
        stt = sum((m.stt for m in rows), ZERO)
        net = realised - expenses

        expired = [
            LossAmount(_fy_label(o), amt, _fy_label(o + LOSS_CARRY_FORWARD_YEARS))
            for o, amt in open_losses
            if start > o + LOSS_CARRY_FORWARD_YEARS
        ]
        open_losses = [x for x in open_losses if start <= x[0] + LOSS_CARRY_FORWARD_YEARS]

        set_off: list[LossAmount] = []
        income = net
        for loss in open_losses:
            if income <= ZERO:
                break
            used = min(loss[1], income)
            loss[1] -= used
            income -= used
            set_off.append(
                LossAmount(_fy_label(loss[0]), used, _fy_label(loss[0] + LOSS_CARRY_FORWARD_YEARS))
            )
        open_losses = [x for x in open_losses if x[1] > ZERO]
        if net < ZERO:
            open_losses.append([start, -net])

        years.append(
            FnoTaxYear(
                financial_year=fy,
                matches=len(rows),
                turnover=turnover,
                realised_pnl=realised,
                expenses=expenses,
                stt=stt,
                net_business_income=net,
                set_off=tuple(set_off),
                income_after_set_off=income,
                expired=tuple(expired),
                carried_forward=tuple(
                    LossAmount(_fy_label(o), amt, _fy_label(o + LOSS_CARRY_FORWARD_YEARS))
                    for o, amt in open_losses
                ),
                flags=_flags(turnover, net),
            )
        )
    return FnoTaxReport(tuple(years))
