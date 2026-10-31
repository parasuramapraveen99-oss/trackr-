"""Mutual fund checks: plain rules over what the investor holds, each stating its own limit.

These are Trackr+ rules, versioned as ``RULE_VERSION`` -- not regulation and not a verdict
on the investor. Every check reports the actual figure beside its limit, and a check whose
inputs are missing says NOT_ASSESSABLE rather than passing (mutual-funds
Analytics/KALPI_RESEARCH.md, "No-data must be NOT_ASSESSABLE, not PASS"). There is no
score: the checks are listed, never added up into a grade (SPEC.md, MVP boundary).

Checks that need data Trackr+ does not hold -- holdings overlap, category peers, expense
ratios, exit loads -- are not here at all rather than shown as passing.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

RULE_VERSION = "trackr-mf-checks.1"

#: One fund house holding more than this share of fund value is flagged.
AMC_LIMIT = Decimal("0.50")
#: One scheme holding more than this share of fund value is flagged.
SCHEME_LIMIT = Decimal("0.50")
#: More schemes than this is flagged as hard to follow.
MAX_SCHEMES = 12
#: This many schemes or more in one SEBI category is flagged.
SAME_CATEGORY = 3
#: A run of at least this many consecutive monthly purchases is read as a SIP.
SIP_RUN = 3
#: A SIP whose last instalment is older than this is reported as possibly paused.
SIP_GAP_DAYS = 45

Status = Literal["pass", "warn", "info", "not_assessable"]


@dataclass(frozen=True, slots=True)
class FundPosition:
    isin: str
    name: str
    amc: str | None
    category: str | None
    plan: str | None
    #: Today's value at NAV; None when the fund could not be valued.
    value: Decimal | None


@dataclass(frozen=True, slots=True)
class LockedLot:
    isin: str
    name: str
    units: Decimal
    value: Decimal | None
    unlock_date: date


@dataclass(frozen=True, slots=True)
class Check:
    key: str
    title: str
    status: Status
    summary: str
    #: The limit the check applies, in words, so the card can show it beside the figure.
    rule: str
    items: list[str] = field(default_factory=list)


def plan_kind(plan: str | None) -> Literal["Direct", "Regular", "Not stated"]:
    text = (plan or "").upper()
    if "DIRECT" in text:
        return "Direct"
    if "REGULAR" in text:
        return "Regular"
    return "Not stated"


def _pct(share: Decimal) -> str:
    return f"{(share * 100).quantize(Decimal('0.1'))}%"


def _money(value: Decimal) -> str:
    """Whole rupees in Indian grouping: 2,66,317."""
    whole = int(value.quantize(Decimal("1")))
    digits = str(abs(whole))
    head, tail = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        head, groups = head[:-2], [head[-2:], *groups]
    text = ",".join([g for g in [head, *groups] if g] + [tail])
    return f"{'-' if whole < 0 else ''}₹{text}"


def _shares(positions: list[FundPosition], key) -> list[tuple[str, Decimal]]:
    valued = [p for p in positions if p.value is not None]
    total = sum((p.value for p in valued), Decimal(0))
    groups: dict[str, Decimal] = defaultdict(Decimal)
    for p in valued:
        groups[key(p)] += p.value
    if total <= 0:
        return []
    return sorted(((k, v / total) for k, v in groups.items()), key=lambda kv: -kv[1])


def _concentration(positions, key, title, check_key, limit, noun) -> Check:
    rule = f"Flags one {noun} above {_pct(limit)} of fund value"
    shares = _shares(positions, key)
    if not shares:
        return Check(check_key, title, "not_assessable", "No fund could be valued", rule)
    name, top = shares[0]
    over = [f"{n}: {_pct(s)}" for n, s in shares if s > limit]
    if over:
        return Check(
            check_key, title, "warn", f"{name} is {_pct(top)} of your fund value", rule, over
        )
    return Check(check_key, title, "pass", f"Largest is {name} at {_pct(top)}", rule)


def _scheme_count(positions: list[FundPosition]) -> Check:
    rule = f"Flags more than {MAX_SCHEMES} schemes"
    n = len(positions)
    if n > MAX_SCHEMES:
        return Check(
            "scheme_count",
            "Number of funds",
            "warn",
            f"{n} schemes are harder to follow and often repeat each other",
            rule,
        )
    return Check(
        "scheme_count", "Number of funds", "pass", f"{n} scheme{'s' if n != 1 else ''}", rule
    )


def _regular_plans(positions: list[FundPosition]) -> Check:
    rule = "Flags any money in Regular plans, which pay distributor commission"
    shares = dict(_shares(positions, lambda p: plan_kind(p.plan)))
    if not shares:
        return Check(
            "regular_plans", "Regular plans", "not_assessable", "No fund could be valued", rule
        )
    regular = shares.get("Regular", Decimal(0))
    unknown = shares.get("Not stated", Decimal(0))
    names = [p.name for p in positions if plan_kind(p.plan) == "Regular"]
    if regular > 0:
        return Check(
            "regular_plans",
            "Regular plans",
            "warn",
            f"{_pct(regular)} is in Regular plans",
            rule,
            names,
        )
    if unknown >= Decimal("0.5"):
        return Check(
            "regular_plans",
            "Regular plans",
            "not_assessable",
            f"The plan is not stated for {_pct(unknown)} of fund value",
            rule,
        )
    return Check(
        "regular_plans", "Regular plans", "pass", "All valued money is in Direct plans", rule
    )


def _same_category(positions: list[FundPosition]) -> Check:
    rule = f"Flags {SAME_CATEGORY} or more schemes in one SEBI category"
    known = [p for p in positions if p.category]
    if not known:
        return Check(
            "same_category",
            "Same-category funds",
            "not_assessable",
            "No scheme category is known",
            rule,
        )
    by_category: dict[str, list[str]] = defaultdict(list)
    for p in known:
        by_category[p.category].append(p.name)
    crowded = [
        f"{c}: {len(n)} schemes" for c, n in sorted(by_category.items()) if len(n) >= SAME_CATEGORY
    ]
    if crowded:
        return Check(
            "same_category",
            "Same-category funds",
            "warn",
            f"{len(crowded)} categor{'ies have' if len(crowded) > 1 else 'y has'} "
            f"{SAME_CATEGORY}+ schemes",
            rule,
            crowded,
        )
    return Check(
        "same_category",
        "Same-category funds",
        "pass",
        f"No category has {SAME_CATEGORY} or more schemes",
        rule,
    )


def _unvalued(positions: list[FundPosition], unvalued: dict[str, str]) -> Check:
    rule = "Flags a fund held but not valued, so totals leave it out"
    if unvalued:
        return Check(
            "unvalued",
            "Funds not valued",
            "warn",
            f"{len(unvalued)} fund{'s are' if len(unvalued) > 1 else ' is'} left out of the totals",
            rule,
            [f"{name}: {why}" for name, why in sorted(unvalued.items())],
        )
    return Check("unvalued", "Funds not valued", "pass", "Every fund held has a current NAV", rule)


def _elss(locked: list[LockedLot], as_of: date) -> Check:
    rule = "ELSS units are locked for three years from each purchase"
    if not locked:
        return Check("elss_lock", "ELSS lock-in", "pass", "No ELSS units are locked", rule)
    value = sum((lot.value for lot in locked if lot.value is not None), Decimal(0))
    nearest = min(locked, key=lambda lot: lot.unlock_date)
    return Check(
        "elss_lock",
        "ELSS lock-in",
        "info",
        f"{_money(value)} in {len(locked)} lot{'s' if len(locked) > 1 else ''} is locked; "
        f"the next unlocks on {nearest.unlock_date.strftime('%d %b %Y')}",
        rule,
        _locked_by_fund(locked),
    )


def _locked_by_fund(locked: list[LockedLot]) -> list[str]:
    """One line per fund -- a monthly SIP leaves one lot a month, too many to list."""
    by_fund: dict[str, list[LockedLot]] = defaultdict(list)
    for lot in locked:
        by_fund[lot.name].append(lot)
    lines = []
    for name, lots in sorted(by_fund.items()):
        lots.sort(key=lambda lot: lot.unlock_date)
        value = sum((lot.value for lot in lots if lot.value is not None), Decimal(0))
        first, last = lots[0].unlock_date, lots[-1].unlock_date
        when = (
            f"unlocks {first.strftime('%d %b %Y')}"
            if len(lots) == 1
            else f"unlocks from {first.strftime('%d %b %Y')} to {last.strftime('%d %b %Y')}"
        )
        lines.append(
            f"{name}: {_money(value)} in {len(lots)} lot{'s' if len(lots) > 1 else ''}, {when}"
        )
    return lines


def _sips(positions: list[FundPosition], purchases: dict[str, list[date]], as_of: date) -> Check:
    rule = (
        f"Reads {SIP_RUN}+ purchases in consecutive months as a SIP, and flags one whose "
        f"last instalment is more than {SIP_GAP_DAYS} days old"
    )
    names = {p.isin: p.name for p in positions}
    held = [i for i in names if purchases.get(i)]
    if not held:
        return Check(
            "sips", "SIPs", "not_assessable", "No purchase history for the funds held", rule
        )
    active, paused = [], []
    for isin in held:
        dates = sorted(set(purchases[isin]))
        months = sorted({d.year * 12 + d.month for d in dates})
        run = 1
        for a, b in zip(reversed(months[:-1]), reversed(months[1:]), strict=False):
            if b - a != 1:
                break
            run += 1
        if run < SIP_RUN:
            continue
        last = dates[-1]
        if (as_of - last).days > SIP_GAP_DAYS:
            paused.append(f"{names[isin]}: last instalment {last.strftime('%d %b %Y')}")
        else:
            active.append(names[isin])
    if paused:
        return Check(
            "sips",
            "SIPs",
            "warn",
            f"{len(paused)} SIP{'s look' if len(paused) > 1 else ' looks'} paused",
            rule,
            paused,
        )
    if active:
        return Check(
            "sips",
            "SIPs",
            "pass",
            f"{len(active)} SIP{'s' if len(active) > 1 else ''} running",
            rule,
        )
    return Check("sips", "SIPs", "info", "No monthly purchase pattern found", rule)


def run_checks(
    positions: list[FundPosition],
    purchases: dict[str, list[date]],
    locked: list[LockedLot],
    unvalued: dict[str, str],
    as_of: date,
) -> list[Check]:
    """Every check over the funds held on ``as_of``.

    ``purchases`` is each fund's purchase dates, excluding dividend reinvestments (a
    monthly-IDCW fund's reinvestments are not a SIP). ``unvalued`` maps a held fund's name
    to the reason it has no value.
    """
    return [
        _concentration(
            positions,
            lambda p: p.amc or "Fund house not stated",
            "Fund house concentration",
            "amc_concentration",
            AMC_LIMIT,
            "fund house",
        ),
        _concentration(
            positions,
            lambda p: p.name,
            "Single-fund concentration",
            "scheme_concentration",
            SCHEME_LIMIT,
            "fund",
        ),
        _scheme_count(positions),
        _regular_plans(positions),
        _same_category(positions),
        _unvalued(positions, unvalued),
        _sips(positions, purchases, as_of),
        _elss(locked, as_of),
    ]
