"""The Basic-view findings: which ones fire, their tone, direction and size word.

docs/ANALYTICS_BASIC_NARRATIVE.md is the specification. This module is the half of the
Basic view that decides what is TRUE; ``app/narrative/`` is the half that words it. The
LLM may choose which of these findings to say and how, but it can never change a tone, a
direction, a magnitude word or a number decided here -- the verifier checks its line
against the slot this module produced.

PURE. Reads the analytics payload (the plain dict the router returns) plus the 1Y
contribution rows, and returns a plain dict. No database, no network, no framework.
Money stays Decimal until it becomes a string at the edge; statistics are floats, as in
the rest of ``app.analytics``.

EVERY FIGURE A LINE CAN SHOW IS EMITTED AS A FACT here, under ``basic.<tab>.<slot>.*``,
so ``app/narrative/factpack.py`` remains a projection: a number in a Basic sentence is
always one this module computed and a test pinned, never one the narrative layer derived.

A FINDING THAT CANNOT BE MEASURED IS NOT DROPPED. It is returned under ``insufficient``
with a reason code and the facts that explain the gap (CLAUDE.md rule 4: absence is
never quietly turned into a number, and never into "none").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

RULE_VERSION = "basic-slots-2026.10.2"

ZERO = Decimal(0)
ONE_LAKH = Decimal(100000)

#: Tones, per review comment 4. ``neutral`` is for strategy-dependent observations.
NEEDS_ATTENTION = "needs_attention"
WATCH = "watch"
POSITIVE = "positive"
NEUTRAL = "neutral"
INSUFFICIENT = "insufficient_data"

#: Insufficient-data reason codes (docs §5.0).
TOO_FEW_OBSERVATIONS = "too_few_observations"
INPUT_NOT_INGESTED = "input_not_ingested"
NOT_CLASSIFIED = "not_classified"
HISTORY_TOO_SHORT = "history_too_short"
METRIC_UNAVAILABLE = "metric_unavailable"

#: Historical VaR needs a full year of daily returns. App policy, stated in the tooltip.
VAR_MIN_OBSERVATIONS = 252
#: Lenders: leverage is their business, so D/E and interest cover do not apply. Matched on
#: the exchange's own industry group (``security_health_profile``), never on the macro
#: sector -- "Financial Services" also holds exchanges and depositories (IEX), which are
#: fee businesses and take the ordinary screen.
LENDER_INDUSTRY_GROUPS = frozenset({"banks", "finance"})
#: Industry groups where neither screen is meaningful (insurance float, etc.).
UNSCREENED_INDUSTRY_GROUPS = frozenset({"insurance"})
#: Allocation labels that mean "no classification", across both calculation paths.
UNCLASSIFIED_LABELS = frozenset({"Unclassified", "Not classified", "Not on AMFI list", "UNKNOWN"})
#: A profile that names an exchange classification we can read the group from.
_PROFILE_OK = ("non_financial", "bank", "UNSUPPORTED_FINANCIAL:")


# --- magnitude words ------------------------------------------------------------------
#
# Fixed lexicons. The LLM is given the word; it may not choose another (verifier V9).


def ratio_gap(a: float | None, b: float | None) -> float | None:
    """Symmetric relative gap ``max/min - 1``.

    The review page used ``|1 - a/b|``, which calls 2x dearer a 100% gap but 2x cheaper
    only 50%. Two portfolios equally far from the index in opposite directions must get
    the same size word. Undefined unless both are positive (a negative P/E is not cheap,
    it is loss-making, and is excluded upstream).
    """
    if a is None or b is None or a <= 0 or b <= 0:
        return None
    return max(a, b) / min(a, b) - 1.0


def ratio_magnitude(gap: float) -> str:
    """< 5% in line · 5-15% slightly · 15-40% clearly · > 40% far."""
    if gap < 0.05:
        return "in line"
    if gap < 0.15:
        return "slightly"
    if gap <= 0.40:
        return "clearly"
    return "far"


def yield_magnitude(gap_pp: float) -> str:
    """Dividend-yield gap in percentage points: < 0.25 in line · 0.25-1 slightly ·
    1-3 clearly · > 3 far."""
    gap = abs(gap_pp)
    if gap < 0.25:
        return "in line"
    if gap < 1.0:
        return "slightly"
    if gap <= 3.0:
        return "clearly"
    return "far"


def return_magnitude(gap_pp: float) -> str:
    """Return gap in percentage points: < 2 in line (noise and costs) · 2-5 slightly ·
    5-15 clearly · > 15 far."""
    gap = abs(gap_pp)
    if gap < 2.0:
        return "in line"
    if gap < 5.0:
        return "slightly"
    if gap <= 15.0:
        return "clearly"
    return "far"


def share_word(pct: float) -> str:
    """A share of value or of a total, as the one quantity word the line may use."""
    if pct < 10:
        return "a small part"
    if pct < 33:
        return "a sizeable part"
    if pct < 50:
        return "a large part"
    if pct <= 75:
        return "most"
    return "nearly all"


# --- the computations the review audit corrected ----------------------------------------


@dataclass(frozen=True, slots=True)
class GrossShare:
    """Top-n holdings' share of the GROSS same-sign contribution.

    ``side`` is "loss" when the portfolio's total is negative (share of all losing
    holdings' losses) and "gain" otherwise. A share of the NET total is not used: when
    gainers offset losers it exceeds 100% and stops meaning anything (TEST20: KDL alone is
    78.5% of the net 1Y loss but 62.2% of gross losses).
    """

    side: str
    total: Decimal
    gross: Decimal
    top: list[tuple[str, Decimal]]
    share_pct: float


def gross_share(rows: list[tuple[str, Decimal]], top_n: int = 2) -> GrossShare | None:
    """``rows`` = (isin, rupee gain over the period). None when there is nothing to share."""
    total = sum((g for _, g in rows), ZERO)
    side = "loss" if total < ZERO else "gain"
    same = [(i, g) for i, g in rows if (g < ZERO if side == "loss" else g > ZERO)]
    gross = sum((g for _, g in same), ZERO)
    if gross == ZERO or len(same) < top_n:
        return None
    same.sort(key=lambda r: r[1] if side == "loss" else -r[1])
    top = same[:top_n]
    share = sum((g for _, g in top), ZERO) / gross * 100
    return GrossShare(side, total, gross, top, float(share))


@dataclass(frozen=True, slots=True)
class WeightedCorrelation:
    value: float
    pair_count: int
    holdings_covered: int
    covered_weight_pct: float
    start: str | None
    end: str | None


def weighted_correlation(
    pairs: list[dict[str, Any]], weights: dict[str, float]
) -> WeightedCorrelation | None:
    """Value-weighted average pairwise correlation, keyed by ISIN.

    Each pair ``(i, j)`` is weighted ``w_i * w_j`` and the weights are renormalised over the
    pairs that actually have a correlation, so an undefined pair never counts as zero.
    The review mockup took weights by list position; that is right only while the matrix
    order happens to match the holdings order, and it is the defect this replaces.
    """
    num = den = 0.0
    covered: set[str] = set()
    starts: list[str] = []
    ends: list[str] = []
    for pair in pairs:
        a, b = pair.get("isin_a"), pair.get("isin_b")
        metric = pair.get("correlation") or {}
        raw = metric.get("value") if isinstance(metric, dict) else metric
        if raw is None or a not in weights or b not in weights:
            continue
        w = weights[a] * weights[b]
        if w <= 0:
            continue
        num += w * float(Decimal(str(raw)))
        den += w
        covered.update((a, b))
        intervals = (
            (metric.get("coverage") or {}).get("intervals") if isinstance(metric, dict) else None
        )
        if intervals:
            starts.append(intervals[0][0])
            ends.append(intervals[-1][1])
    if den == 0:
        return None
    total_weight = sum(weights.values())
    return WeightedCorrelation(
        value=num / den,
        pair_count=sum(1 for _ in pairs),
        holdings_covered=len(covered),
        covered_weight_pct=(sum(weights[i] for i in covered) / total_weight * 100)
        if total_weight
        else 0.0,
        start=min(starts) if starts else None,
        end=max(ends) if ends else None,
    )


def screen_for(profile_status: str | None, industry_group: str | None) -> str | None:
    """Which quality screen applies: "operating", "lender", "none", or None (unknown).

    None means the exchange classification is not available for this date; the holding
    is then unscored for the quality core, never assumed to be either kind.
    """
    if not profile_status or not profile_status.startswith(_PROFILE_OK):
        return None
    group = (industry_group or "").strip().casefold()
    if profile_status == "bank" or group in LENDER_INDUSTRY_GROUPS:
        return "lender"
    if group in UNSCREENED_INDUSTRY_GROUPS:
        return "none"
    if profile_status.startswith("UNSUPPORTED_FINANCIAL:") and not group:
        return None
    return "operating"


def passes_quality(position: dict[str, Any], screen: str) -> bool | None:
    """True/False, or None when an input the screen needs is missing."""
    roe = position.get("roe")
    if screen == "operating":
        de, ic = position.get("debt_to_equity"), position.get("interest_cover")
        if roe is None or de is None or ic is None:
            return None
        return roe >= 0.20 and de <= 1.0 and ic >= 4.0
    if screen == "lender":
        gnpa = position.get("gross_npa_ratio")
        if roe is None or gnpa is None:
            return None
        return roe >= 0.15 and gnpa <= 0.03
    return None


# --- what each rule is, for the chip tooltip and Advanced "Why flagged" ---------------------
#
# Definitions, not prose about this portfolio: they are true of every book, so they are
# fixed text, versioned with RULE_VERSION. (label, threshold, method and period).
RULES: dict[str, dict[str, str]] = {
    "OV.CONTRIBUTION_SHARE": {
        "label": "Loss or gain concentration",
        "threshold": "Top 2 holdings ≥50% of gross same-sign 1Y P&L",
        "method": "Per-holding 1Y contribution from the ledger",
        "period": "Trailing year",
    },
    "RR.CIRCUIT": {
        "label": "Price-limit hits",
        "threshold": "≥3 price-band hits in 90 sessions",
        "method": "Closes at the day's exchange price band",
        "period": "Last 90 sessions",
    },
    "FD.TRADE_OFF": {
        "label": "Paying up for growth",
        "threshold": "P/E dearer by ≥15% and weighted profit growth ≥ the index's",
        "method": "Portfolio P/E and value-weighted profit growth vs the Nifty 50",
        "period": "Latest reported",
    },
    "OV.SINGLE_NAME": {
        "label": "Concentrated",
        "threshold": "Largest holding ≥10% watch · ≥20% needs attention",
        "method": "Current market value of the largest holding ÷ total value",
        "period": "Today",
    },
    "OV.LOSS_SHARE": {
        "label": "Loss concentration",
        "threshold": "Top 2 holdings ≥50% of gross losses",
        "method": (
            "Top 2 losing holdings' 1Y loss ÷ the losses "
            "of every holding that fell (not the net total)"
        ),
        "period": "Trailing year",
    },
    "OV.GAIN_SHARE": {
        "label": "Gain concentration",
        "threshold": "Top 2 holdings ≥50% of gross gains",
        "method": "Top 2 gaining holdings' 1Y gain ÷ the gains of every holding that rose",
        "period": "Trailing year",
    },
    "OV.VS_INDEX": {
        "label": "Versus the index",
        "threshold": "Gap ≥2 percentage points behind or ahead",
        "method": "Time-weighted return (cash flows neutralised) vs Nifty 50 TRI",
        "period": "Exactly one year",
    },
    "OV.QUALITY_CORE": {
        "label": "Quality core",
        "threshold": "≥30% of value passes the screen",
        "method": (
            "Operating companies: ROE ≥20%, debt/equity ≤1, interest "
            "cover ≥4. Lenders: ROE ≥15%, gross NPA ≤3%"
        ),
        "period": "Latest reported fundamentals",
    },
    "OV.PRICE_TREND": {
        "label": "In a downtrend",
        "threshold": "Caution share ≥50% of value",
        "method": "Caution = at a loss and price below its 50-day average",
        "period": "Today",
    },
    "OV.GRADE_DRIVERS": {
        "label": "Grade drivers",
        "threshold": "Always shown when two or more parts are scored",
        "method": "Weakest and strongest scored parts of portfolio-health-v1",
        "period": "Today",
    },
    "RR.SWINGS": {
        "label": "High swings",
        "threshold": "≥1.25× watch · ≥1.5× needs attention · ≤0.90× positive",
        "method": (
            "Annualised volatility of daily time-weighted returns "
            "(√252) ÷ Nifty 50 TRI's, same sessions"
        ),
        "period": "Trailing year",
    },
    "RR.DEEPEST_FALL": {
        "label": "Deepest fall",
        "threshold": "Watch when deeper than the Nifty 50's over the same year",
        "method": "Largest fall from a running peak of the cash-flow-adjusted value series",
        "period": "Trailing year",
    },
    "RR.BAD_DAY": {
        "label": "Bad-day loss",
        "threshold": "Shown once 252 daily returns exist (app policy)",
        "method": "Historical 1-day 95% VaR: a loss exceeded on about 1 day in 20, not a maximum",
        "period": "Trailing 252 sessions",
    },
    "RR.MARKET_SENSITIVITY": {
        "label": "Market sensitivity",
        "threshold": "Beta ≥1.1 watch · ≤0.9 noted",
        "method": "Regression slope of daily returns on Nifty 50 TRI daily returns",
        "period": "Trailing year",
    },
    "RR.RISK_CONCENTRATION": {
        "label": "Drives your risk",
        "threshold": "Risk share ≥1.5× money share and ≥15%",
        "method": "Euler share of volatility from the covariance of daily returns, today's weights",
        "period": "Trailing year",
    },
    "RR.LIQUIDITY": {
        "label": "Hard to exit",
        "threshold": "≥5% of value needs more than 5 days to sell",
        "method": "Holding value ÷ (10% × 20-session median traded value, NSE + BSE)",
        "period": "Last 20 sessions",
    },
    "DV.SECTOR": {
        "label": "Sector-heavy",
        "threshold": "Largest sector ≥20% watch · ≥30% needs attention",
        "method": "Share of value by exchange macro-sector",
        "period": "Today",
    },
    "DV.CAP_MIX": {
        "label": "Company-size mix",
        "threshold": "Largest AMFI bucket ≥60%",
        "method": "AMFI half-yearly categorisation: top 100 large, 101–250 mid, rest small",
        "period": "Latest AMFI list",
    },
    "DV.EFFECTIVE_N": {
        "label": "Uneven allocation",
        "threshold": "<8 watch · <5 needs attention",
        "method": "1 ÷ Σ(weight²): position-size concentration, not a count of independent risks",
        "period": "Today",
    },
    "DV.CO_MOVEMENT": {
        "label": "Move together",
        "threshold": ">0.40 watch · >0.50 needs attention · <0.30 positive",
        "method": (
            "Value-weighted average Pearson correlation of "
            "matched daily returns, 12 largest holdings"
        ),
        "period": "Trailing year",
    },
    "FD.PE": {
        "label": "Price to earnings",
        "threshold": "Gap ≥5% (in line below that)",
        "method": (
            "Value-weighted harmonic mean; loss-makers excluded and "
            "counted; vs Nifty 50 published P/E"
        ),
        "period": "Latest reported",
    },
    "FD.PB": {
        "label": "Price to book",
        "threshold": "Gap ≥5%",
        "method": "Value-weighted harmonic mean vs Nifty 50 published P/B",
        "period": "Latest reported",
    },
    "FD.YIELD": {
        "label": "Dividend yield",
        "threshold": "Gap ≥0.25 percentage points",
        "method": "Value-weighted yield vs Nifty 50 published yield",
        "period": "Latest reported",
    },
    "FD.LOSS_MAKERS": {
        "label": "Loss-making holdings",
        "threshold": "Any loss-making holding",
        "method": "Negative net margin (or ROE); such holdings have no P/E",
        "period": "Latest reported",
    },
    "FD.MAP_COUNT": {
        "label": "Valuation map",
        "threshold": "Always shown",
        "method": (
            "Each holding's P/E and P/B against the "
            "Nifty 500 median; both lower = attractive"
        ),
        "period": "Latest reported",
    },
    "PF.ONE_LAKH": {
        "label": "₹1 lakh a year ago",
        "threshold": "Full one-year window",
        "method": "₹1,00,000 × (1 + one-year time-weighted return), portfolio and Nifty 50 TRI",
        "period": "Exactly one year",
    },
    "PF.XIRR_VS_INDEX": {
        "label": "Your money vs the index",
        "threshold": "Gap ≥2 percentage points",
        "method": "Your XIRR vs the XIRR of the same cash, same dates, in Nifty 50 TRI",
        "period": "Since your first investment",
    },
}


# --- slot plumbing -----------------------------------------------------------------------


@dataclass
class _Out:
    facts: dict[str, Any] = field(default_factory=dict)
    tabs: dict[str, dict[str, list]] = field(default_factory=dict)

    def fact(self, fact_id: str, value: Any) -> str:
        if isinstance(value, Decimal):
            value = str(value)
        self.facts[fact_id] = value
        return fact_id

    def _tab(self, tab: str) -> dict[str, list]:
        return self.tabs.setdefault(tab, {"candidates": [], "insufficient": []})

    def fire(
        self,
        tab: str,
        slot: str,
        tone: str,
        facts: dict[str, str],
        must_cite: list[str],
        *,
        direction: str | None = None,
        magnitude: str | None = None,
        subjects: list[str] | None = None,
        implication: str = "NONE",
        allowed: list[str] | None = None,
        forbidden: list[str] | None = None,
        coverage_fact: str | None = None,
        priority: int = 50,
    ) -> None:
        self._tab(tab)["candidates"].append(
            {
                "slot": slot,
                "tone": tone,
                "direction": direction,
                "magnitude_word": magnitude,
                "facts": facts,
                "must_cite": must_cite,
                "subjects": subjects or [],
                "implication": implication,
                "allowed_words": allowed or [],
                "forbidden_words": forbidden or [],
                "coverage_fact": coverage_fact,
                "priority": priority,
                "rule": RULES.get(slot),
            }
        )

    def lack(
        self,
        tab: str,
        slot: str,
        reason: str,
        facts: dict[str, str] | None = None,
        detail: list[str] | None = None,
    ) -> None:
        self._tab(tab)["insufficient"].append(
            {
                "slot": slot,
                "rule": RULES.get(slot),
                "tone": INSUFFICIENT,
                "reason": reason,
                "facts": facts or {},
                "detail": detail or [],
            }
        )


_TONE_ORDER = {NEEDS_ATTENTION: 0, WATCH: 1, POSITIVE: 2, NEUTRAL: 3}

_MORE_VOLATILE = ["bumpier", "swings more", "bigger ups and downs", "more"]
_LESS_VOLATILE = ["calmer", "smoother", "steadier", "smaller ups and downs", "less"]
_CHEAPER = ["cheaper", "less", "lower price"]
_DEARER = ["dearer", "more", "higher price", "pricier"]
_BEHIND = ["behind", "trailed", "lagged", "less", "lower"]
_AHEAD = ["ahead", "beat", "more", "higher"]


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(Decimal(str(value)))
    except ArithmeticError:
        return None


def _weights(payload: dict[str, Any]) -> dict[str, float]:
    return {p["isin"]: p["weight"] for p in payload.get("positions") or [] if p.get("weight")}


def _symbols(payload: dict[str, Any]) -> dict[str, str]:
    return {p["isin"]: p.get("symbol") or p["isin"] for p in payload.get("positions") or []}


# --- the tabs ----------------------------------------------------------------------------


def _overview(
    o: _Out,
    payload: dict[str, Any],
    contribution: dict[str, Any] | None,
    profiles: dict[str, tuple[str | None, str | None]],
) -> None:
    positions = sorted(payload.get("positions") or [], key=lambda p: -(p.get("weight") or 0))
    if positions:
        top = positions[0]
        w = top.get("weight") or 0.0
        if w >= 10:
            base = "basic.overview.single_name"
            o.fire(
                "overview",
                "OV.SINGLE_NAME",
                NEEDS_ATTENTION if w >= 20 else WATCH,
                {
                    "weight": o.fact(f"{base}.weight", w),
                    "name": o.fact(f"{base}.subject.0.name", top.get("symbol")),
                },
                ["weight", "name"],
                subjects=[top.get("symbol")],
                implication="MOVES_WHOLE_BOOK",
                priority=10,
            )

    # Loss or gain concentration over the trailing year, on GROSS same-sign contribution.
    if contribution is None:
        o.lack("overview", "OV.CONTRIBUTION_SHARE", METRIC_UNAVAILABLE)
    elif contribution.get("error"):
        o.lack(
            "overview", "OV.CONTRIBUTION_SHARE", INPUT_NOT_INGESTED, detail=[contribution["error"]]
        )
    else:
        rows = [(r["isin"], Decimal(r["gain"])) for r in contribution.get("contributions") or []]
        share = gross_share(rows)
        if share is not None and share.share_pct >= 50:
            base = f"basic.overview.{share.side}_share"
            symbols = {
                r["isin"]: r.get("symbol") or r["isin"]
                for r in contribution.get("contributions") or []
            }
            # Unsigned rupees: "of the {gross} lost" must not read "of the −₹7,01,310
            # lost". The side ("loss"/"gain") carries the sign; the net total keeps its own.
            facts = {
                "share": o.fact(f"{base}.share_of_gross_pct", share.share_pct),
                "gross": o.fact(f"{base}.gross_value", abs(share.gross)),
                "total": o.fact(f"{base}.total_value", share.total),
                "period_start": o.fact(
                    f"{base}.period_start", str(contribution.get("period_start"))
                ),
            }
            for index, (isin, gain) in enumerate(share.top):
                facts[f"name_{index}"] = o.fact(f"{base}.subject.{index}.name", symbols[isin])
                facts[f"gain_{index}"] = o.fact(f"{base}.subject.{index}.gain_value", abs(gain))
            o.fire(
                "overview",
                "OV.LOSS_SHARE" if share.side == "loss" else "OV.GAIN_SHARE",
                NEEDS_ATTENTION,
                facts,
                ["share", "name_0", "name_1"],
                direction=share.side,
                magnitude=share_word(share.share_pct),
                subjects=[symbols[i] for i, _ in share.top],
                implication="MOVES_WHOLE_BOOK",
                priority=20,
            )

    # Versus the index over exactly one year (TWRR, cash flows neutralised).
    risk = (payload.get("risk") or {}).get("1y") or {}
    period = risk.get("period") or {}
    full_year = period.get("start") and period.get("start") == period.get("requested_start")
    mine, bench = risk.get("cum_return"), risk.get("bench_cum_return")
    if not full_year or mine is None or bench is None:
        o.lack(
            "overview", "OV.VS_INDEX", HISTORY_TOO_SHORT if not full_year else METRIC_UNAVAILABLE
        )
    else:
        gap_pp = (mine - bench) * 100
        if abs(gap_pp) >= 2.0:
            base = "basic.overview.vs_index"
            behind = gap_pp < 0
            o.fire(
                "overview",
                "OV.VS_INDEX",
                NEEDS_ATTENTION if behind else POSITIVE,
                {
                    "yours": o.fact(f"{base}.cum_return", mine),
                    "index": o.fact(f"{base}.bench_cum_return", bench),
                    "gap": o.fact(f"{base}.gap_pp", gap_pp),
                },
                ["yours", "index"],
                direction="behind" if behind else "ahead",
                magnitude=return_magnitude(gap_pp),
                allowed=_BEHIND if behind else _AHEAD,
                forbidden=_AHEAD if behind else _BEHIND,
                priority=30,
            )

    # Quality core: exchange-classified screen per holding (lender vs operating company).
    weights = _weights(payload)
    passed: list[tuple[str, float]] = []
    scored = 0.0
    for p in positions:
        screen = screen_for(*profiles.get(p["isin"], (None, None)))
        if screen is None or screen == "none":
            continue
        ok = passes_quality(p, screen)
        if ok is None:
            continue
        scored += weights.get(p["isin"], 0.0)
        if ok:
            passed.append((p.get("symbol") or p["isin"], weights.get(p["isin"], 0.0)))
    total_w = sum(weights.values())
    scored_pct = scored / total_w * 100 if total_w else 0.0
    if scored_pct < 50:
        o.lack(
            "overview",
            "OV.QUALITY_CORE",
            METRIC_UNAVAILABLE,
            {"coverage": o.fact("basic.overview.quality_core.coverage_pct", scored_pct)},
        )
    else:
        quality_w = sum(w for _, w in passed)
        if quality_w >= 30:
            base = "basic.overview.quality_core"
            facts = {
                "weight": o.fact(f"{base}.weight", quality_w),
                "coverage": o.fact(f"{base}.coverage_pct", scored_pct),
            }
            for index, (name, _) in enumerate(passed[:3]):
                facts[f"name_{index}"] = o.fact(f"{base}.subject.{index}.name", name)
            o.fire(
                "overview",
                "OV.QUALITY_CORE",
                POSITIVE,
                facts,
                ["weight", "name_0"],
                subjects=[n for n, _ in passed[:3]],
                coverage_fact=facts["coverage"] if scored_pct < 95 else None,
                priority=60,
            )

    components = [
        c
        for c in (payload.get("health") or {}).get("components") or []
        if c.get("score") is not None
    ]
    missing = [
        c for c in (payload.get("health") or {}).get("components") or [] if c.get("score") is None
    ]
    if len(components) < 2:
        o.lack(
            "overview",
            "OV.GRADE_DRIVERS",
            METRIC_UNAVAILABLE,
            {
                "measured": o.fact("basic.overview.grade_drivers.count", len(components)),
                "missing": o.fact("basic.overview.grade_drivers.missing_count", len(missing)),
            },
            detail=[c.get("label") for c in missing],
        )
    else:
        ranked = sorted(components, key=lambda c: c["score"])
        facts = {}
        for name, c in (("weakest", ranked[0]), ("second", ranked[1]), ("strongest", ranked[-1])):
            b = f"basic.overview.grade_drivers.{name}"
            facts[f"{name}_name"] = o.fact(f"{b}.label", c.get("label"))
            facts[f"{name}_score"] = o.fact(f"{b}.score", c.get("score"))
        o.fire(
            "overview",
            "OV.GRADE_DRIVERS",
            NEUTRAL,
            facts,
            ["weakest_name", "strongest_name"],
            priority=70,
        )

    caution = ((payload.get("derived") or {}).get("capital_state_pct") or {}).get("CAUTION")
    if caution is not None and caution >= 50:
        o.fire(
            "overview",
            "OV.PRICE_TREND",
            WATCH,
            {"caution": o.fact("basic.overview.price_trend.caution_pct", caution)},
            ["caution"],
            magnitude=share_word(caution),
            priority=40,
        )


def _risk(o: _Out, payload: dict[str, Any]) -> None:
    risk = (payload.get("risk") or {}).get("1y") or {}
    obs = risk.get("observations") or 0
    vol, bvol = risk.get("volatility"), risk.get("bench_volatility")
    base = "basic.risk"

    if vol is None or not bvol:
        o.lack("risk", "RR.SWINGS", METRIC_UNAVAILABLE)
    else:
        ratio = vol / bvol
        tone = (
            NEEDS_ATTENTION
            if ratio >= 1.5
            else WATCH
            if ratio >= 1.25
            else POSITIVE
            if ratio <= 0.90
            else None
        )
        if tone is not None:
            more = ratio > 1
            o.fire(
                "risk",
                "RR.SWINGS",
                tone,
                {
                    "ratio": o.fact(f"{base}.swings.vol_ratio_1y", ratio),
                    "yours": o.fact(f"{base}.swings.volatility", vol),
                    "index": o.fact(f"{base}.swings.bench_volatility", bvol),
                },
                ["ratio"],
                direction="more_volatile" if more else "less_volatile",
                magnitude=ratio_magnitude(ratio_gap(vol, bvol) or 0.0),
                allowed=_MORE_VOLATILE if more else _LESS_VOLATILE,
                forbidden=_LESS_VOLATILE if more else _MORE_VOLATILE,
                implication="SWINGS_BOTH_WAYS",
                priority=10,
            )

    mdd = risk.get("max_drawdown")
    dates = risk.get("drawdown_dates") or {}
    bench_dd = (_revamp(payload, "risk").get("benchmark_drawdown") or {}).get("max_drawdown")
    if mdd is None:
        o.lack("risk", "RR.DEEPEST_FALL", METRIC_UNAVAILABLE)
    else:
        recovered = dates.get("recovered")
        facts = {
            # Magnitude, positive: the line says "down {fall}", never "down −37.5%".
            "fall": o.fact(f"{base}.deepest_fall.max_drawdown_magnitude", -mdd),
            "peak_date": o.fact(f"{base}.deepest_fall.peak_date", dates.get("peak")),
            "recovered_date": o.fact(f"{base}.deepest_fall.recovered_date", recovered),
        }
        if bench_dd is not None:
            facts["index_fall"] = o.fact(
                f"{base}.deepest_fall.bench_max_drawdown_magnitude", -bench_dd
            )
        worse = bench_dd is not None and mdd < bench_dd
        o.fire(
            "risk",
            "RR.DEEPEST_FALL",
            # Review §6: amber when the fall is deeper than the index's over the same year.
            WATCH if worse else NEUTRAL,
            facts,
            ["fall", "peak_date"],
            direction=("worse_than_index" if worse else "not_worse_than_index")
            if bench_dd is not None
            else ("recovered" if recovered else "not_recovered"),
            allowed=["deeper", "more", "worse"] if worse else ["less", "shallower"],
            forbidden=["less", "shallower", "better"] if worse else ["deeper", "worse"],
            priority=20,
        )

    var = risk.get("var_1d_95")
    if obs < VAR_MIN_OBSERVATIONS or var is None:
        o.lack(
            "risk",
            "RR.BAD_DAY",
            TOO_FEW_OBSERVATIONS if obs < VAR_MIN_OBSERVATIONS else METRIC_UNAVAILABLE,
            {
                "have": o.fact(f"{base}.bad_day.observations", obs),
                "need": o.fact(f"{base}.bad_day.var_obs_needed", VAR_MIN_OBSERVATIONS),
                # Trading sessions still needed. The line renders this number; it never
                # converts it to calendar time ("about a week").
                "remaining": o.fact(
                    f"{base}.bad_day.var_obs_remaining", max(VAR_MIN_OBSERVATIONS - obs, 0)
                ),
            },
        )
    else:
        o.fire(
            "risk",
            "RR.BAD_DAY",
            NEUTRAL,
            {
                "pct": o.fact(f"{base}.bad_day.var_1d_95", var),
                "value": o.fact(f"{base}.bad_day.var_1d_95_value", risk.get("var_1d_95_value")),
            },
            ["value"],
            priority=40,
        )

    beta = risk.get("beta")
    if beta is None:
        o.lack("risk", "RR.MARKET_SENSITIVITY", METRIC_UNAVAILABLE)
    elif beta >= 1.1 or beta <= 0.9:
        more = beta >= 1.1
        o.fire(
            "risk",
            "RR.MARKET_SENSITIVITY",
            WATCH if more else NEUTRAL,
            {
                "beta": o.fact(f"{base}.sensitivity.beta", beta),
                "observations": o.fact(f"{base}.sensitivity.observations", obs),
            },
            ["beta"],
            direction="more_sensitive" if more else "less_sensitive",
            allowed=["more", "further", "amplified"] if more else ["less", "muted"],
            forbidden=["less", "muted"] if more else ["more", "further", "amplified"],
            priority=30,
        )

    shares = _risk_shares(payload)
    rs_cover = (_revamp(payload, "risk").get("risk_shares") or {}).get("covered_weight_pct")
    weights = _weights(payload)
    symbols = _symbols(payload)
    if shares is None:
        o.lack("risk", "RR.RISK_CONCENTRATION", METRIC_UNAVAILABLE)
    else:
        hits = sorted(
            (
                (i, s)
                for i, s in shares.items()
                if s >= 15 and weights.get(i) and s >= 1.5 * weights[i]
            ),
            key=lambda r: -r[1],
        )
        if hits:
            isin, share = hits[0]
            b = f"{base}.risk_concentration"
            o.fire(
                "risk",
                "RR.RISK_CONCENTRATION",
                NEEDS_ATTENTION,
                {
                    "risk_share": o.fact(f"{b}.risk_share_pct", share),
                    "weight": o.fact(f"{b}.weight", weights[isin]),
                    "name": o.fact(f"{b}.subject.0.name", symbols.get(isin)),
                    "coverage": o.fact(f"{b}.covered_weight_pct", rs_cover),
                },
                ["risk_share", "weight", "name"],
                subjects=[symbols.get(isin)],
                implication="MOVES_WHOLE_BOOK",
                priority=15,
            )

    liq = _revamp(payload, "risk").get("liquidity") or {}
    measured = liq.get("measured_weight_pct") or 0.0
    if measured < 50:
        rows = liq.get("rows") or []
        have = max(
            (r.get("sessions") or 0 for r in rows if r.get("status") == "not_measured"), default=0
        )
        o.lack(
            "risk",
            "RR.LIQUIDITY",
            INPUT_NOT_INGESTED if have == 0 else TOO_FEW_OBSERVATIONS,
            {
                "have": o.fact(f"{base}.liquidity.observations", have),
                "need": o.fact(f"{base}.liquidity.liquidity_sessions_needed", 20),
                "coverage": o.fact(f"{base}.liquidity.coverage_pct", measured),
            },
            detail=["20-session median traded value"],
        )
    elif (liq.get("slow_weight_pct") or 0) >= 5:
        slow = [r for r in liq["rows"] if r.get("tier") == "slow"]
        facts = {
            "weight": o.fact(f"{base}.liquidity.weight", liq["slow_weight_pct"]),
            "coverage": o.fact(f"{base}.liquidity.coverage_pct", measured),
        }
        for index, r in enumerate(slow[:3]):
            facts[f"name_{index}"] = o.fact(f"{base}.liquidity.subject.{index}.name", r["symbol"])
        o.fire(
            "risk",
            "RR.LIQUIDITY",
            WATCH,
            facts,
            ["weight"],
            subjects=[r["symbol"] for r in slow[:3]],
            implication="SLOW_TO_SELL",
            coverage_fact=facts["coverage"] if measured < 95 else None,
            priority=35,
        )
    # Price bands are not in the bhavcopy; a band-hit count needs the NSE band file.
    o.lack("risk", "RR.CIRCUIT", INPUT_NOT_INGESTED, detail=["daily price-band file"])


def _revamp(payload: dict[str, Any], section: str) -> dict[str, Any]:
    return (payload.get("revamp") or {}).get(section) or {}


def _risk_shares(payload: dict[str, Any]) -> dict[str, float] | None:
    """Holding -> share of portfolio risk, in percent: the revamp's Euler shares, else the
    legacy path or advisor evidence."""
    euler = (_revamp(payload, "risk").get("risk_shares") or {}).get("shares")
    if euler:
        return euler
    rows = (payload.get("risk_contributions") or {}).get("1y")
    if rows:
        return {r["isin"]: r["risk_share_pct"] for r in rows if r.get("risk_share_pct") is not None}
    cov = ((payload.get("health") or {}).get("evidence") or {}).get("covariance") or {}
    value = cov.get("value") or {}
    risk_shares = value.get("risk_shares")
    if not risk_shares:
        return None
    return {
        i: r["risk_share"] * 100 for i, r in risk_shares.items() if r.get("risk_share") is not None
    }


def _diversification(o: _Out, payload: dict[str, Any]) -> None:
    allocation = payload.get("allocation") or {}
    base = "basic.diversification"

    sectors = allocation.get("sector") or []
    named = [r for r in sectors if r["label"] not in UNCLASSIFIED_LABELS]
    unclassified = sum(r["pct"] for r in sectors if r not in named)
    if not named:
        o.lack(
            "diversification",
            "DV.SECTOR",
            NOT_CLASSIFIED,
            {"unclassified": o.fact(f"{base}.sector.unclassified_pct", unclassified)},
        )
    else:
        top = max(named, key=lambda r: r["pct"])
        if top["pct"] >= 20:
            o.fire(
                "diversification",
                "DV.SECTOR",
                NEEDS_ATTENTION if top["pct"] >= 30 else WATCH,
                {
                    "weight": o.fact(f"{base}.sector.pct", top["pct"]),
                    "name": o.fact(f"{base}.sector.label", top["label"]),
                    "unclassified": o.fact(f"{base}.sector.unclassified_pct", unclassified),
                },
                ["weight", "name"],
                subjects=[top["label"]],
                implication="MOVES_WHOLE_BOOK",
                priority=20,
            )

    caps = allocation.get("market_cap") or []
    if not caps:
        o.lack("diversification", "DV.CAP_MIX", METRIC_UNAVAILABLE)
    else:
        top = max(caps, key=lambda r: r["pct"])
        unclassified_cap = sum(r["pct"] for r in caps if r["label"] in UNCLASSIFIED_LABELS)
        if top["label"] in UNCLASSIFIED_LABELS:
            o.lack(
                "diversification",
                "DV.CAP_MIX",
                NOT_CLASSIFIED,
                {"unclassified": o.fact(f"{base}.cap_mix.unclassified_pct", unclassified_cap)},
            )
        elif top["pct"] >= 60:
            o.fire(
                "diversification",
                "DV.CAP_MIX",
                WATCH if top["label"] == "Small Cap" else NEUTRAL,
                {
                    "weight": o.fact(f"{base}.cap_mix.pct", top["pct"]),
                    "name": o.fact(f"{base}.cap_mix.label", top["label"]),
                    "unclassified": o.fact(f"{base}.cap_mix.unclassified_pct", unclassified_cap),
                },
                ["weight", "name"],
                magnitude=share_word(top["pct"]),
                implication="SWINGS_BOTH_WAYS" if top["label"] == "Small Cap" else "NONE",
                priority=40,
            )

    effective = (payload.get("derived") or {}).get("effective_holdings")
    count = (payload.get("derived") or {}).get("position_count")
    if effective is None:
        o.lack("diversification", "DV.EFFECTIVE_N", METRIC_UNAVAILABLE)
    elif effective < 8:
        o.fire(
            "diversification",
            "DV.EFFECTIVE_N",
            NEEDS_ATTENTION if effective < 5 else WATCH,
            {
                "effective": o.fact(f"{base}.effective_n.effective_holdings", effective),
                "count": o.fact(f"{base}.effective_n.position_count", count),
            },
            ["effective", "count"],
            # 1/HHI is position-size concentration, not a count of independent risks.
            allowed=["spread like", "equal-sized holdings"],
            forbidden=["behave like", "real bets", "independent"],
            priority=30,
        )

    matrix = _revamp(payload, "diversification").get("correlation") or {}
    if matrix.get("weighted_average") is not None:
        starts = [c["start"] for row in matrix["cells"] for c in row if c and c.get("start")]
        ends = [c["end"] for row in matrix["cells"] for c in row if c and c.get("end")]
        corr = WeightedCorrelation(
            value=matrix["weighted_average"],
            pair_count=len(matrix["isins"]) * (len(matrix["isins"]) - 1) // 2,
            holdings_covered=len(matrix["isins"]),
            covered_weight_pct=matrix["covered_weight_pct"],
            start=min(starts) if starts else None,
            end=max(ends) if ends else None,
        )
    else:
        corr = weighted_correlation(
            (payload.get("correlation") or {}).get("pairs") or [], _weights(payload)
        )
    if corr is None:
        o.lack("diversification", "DV.CO_MOVEMENT", METRIC_UNAVAILABLE)
    else:
        tone = (
            NEEDS_ATTENTION
            if corr.value > 0.50
            else WATCH
            if corr.value > 0.40
            else POSITIVE
            if corr.value < 0.30
            else None
        )
        if tone is not None:
            together = corr.value > 0.40
            b = f"{base}.co_movement"
            covered = o.fact(f"{b}.covered_weight_pct", corr.covered_weight_pct)
            o.fire(
                "diversification",
                "DV.CO_MOVEMENT",
                tone,
                {
                    "average": o.fact(f"{b}.weighted_average", corr.value),
                    "holdings": o.fact(f"{b}.holdings_covered", corr.holdings_covered),
                    "coverage": covered,
                    "start": o.fact(f"{b}.period_start", corr.start),
                    "end": o.fact(f"{b}.period_end", corr.end),
                },
                ["average"],
                direction="move_together" if together else "move_independently",
                implication="MOVES_TOGETHER" if together else "MOVES_INDEPENDENTLY",
                forbidden=["safe", "protected", "never", "prevents"],
                coverage_fact=covered if corr.covered_weight_pct < 95 else None,
                priority=50,
            )


def _fundamentals(o: _Out, payload: dict[str, Any]) -> None:
    """From the one dataset the page shows (revamp: vendor-reported, labelled provisional,
    dated). Falls back to nothing: without that block every finding is not measured."""
    rev = _revamp(payload, "fundamentals")
    cards = rev.get("cards") or {}
    bench = cards.get("benchmark") or {}
    base = "basic.fundamentals"
    for key, slot in (("pe", "FD.PE"), ("pb", "FD.PB")):
        agg = cards.get(key) or {}
        mine, idx = agg.get("value"), bench.get(key)
        if mine is None or idx is None:
            o.lack(
                "fundamentals",
                slot,
                METRIC_UNAVAILABLE,
                detail=["index ratios missing" if idx is None else "portfolio figure missing"],
            )
            continue
        gap = ratio_gap(mine, idx)
        if gap is None or gap < 0.05:
            continue
        cheaper = mine < idx
        coverage = o.fact(f"{base}.{key}.coverage_pct", agg.get("coverage_pct"))
        o.fire(
            "fundamentals",
            slot,
            NEUTRAL,
            {
                "yours": o.fact(f"{base}.{key}.{key}", mine),
                "index": o.fact(f"{base}.{key}.bench_{key}", idx),
                "index_date": o.fact(
                    f"{base}.{key}.benchmark_as_of", str(cards.get("benchmark_as_of"))
                ),
                "coverage": coverage,
            },
            ["yours", "index"],
            direction="cheaper" if cheaper else "dearer",
            magnitude=ratio_magnitude(gap),
            allowed=_CHEAPER if cheaper else _DEARER,
            forbidden=_DEARER if cheaper else _CHEAPER,
            # Comment 6: a lower ratio alone does not prove a favourable valuation.
            coverage_fact=coverage if (agg.get("coverage_pct") or 0) < 95 else None,
            priority=20 if key == "pe" else 30,
        )

    agg = cards.get("dividend_yield") or {}
    mine, idx = agg.get("value"), bench.get("dividend_yield")
    if mine is None or idx is None:
        o.lack("fundamentals", "FD.YIELD", METRIC_UNAVAILABLE)
    else:
        gap_pp = (mine - idx) * 100
        if abs(gap_pp) >= 0.25:
            higher = gap_pp > 0
            coverage = o.fact(f"{base}.yield.coverage_pct", agg.get("coverage_pct"))
            o.fire(
                "fundamentals",
                "FD.YIELD",
                NEUTRAL,
                {
                    "yours": o.fact(f"{base}.yield.dividend_yield", mine),
                    "index": o.fact(f"{base}.yield.bench_dividend_yield", idx),
                    "gap": o.fact(f"{base}.yield.yield_gap_pp", gap_pp),
                    "coverage": coverage,
                },
                ["yours", "index"],
                direction="higher" if higher else "lower",
                magnitude=yield_magnitude(gap_pp),
                allowed=["higher", "more"] if higher else ["lower", "less", "below"],
                forbidden=["lower", "less", "below", "barely"]
                if higher
                else ["higher", "more", "above"],
                coverage_fact=coverage if (agg.get("coverage_pct") or 0) < 95 else None,
                priority=40,
            )

    pe = cards.get("pe") or {}
    if not cards:
        o.lack("fundamentals", "FD.LOSS_MAKERS", METRIC_UNAVAILABLE)
    elif pe.get("loss_making"):
        makers = pe["loss_making"]
        facts = {
            "count": o.fact(f"{base}.loss_makers.count", len(makers)),
            "weight": o.fact(f"{base}.loss_makers.weight", pe.get("loss_making_weight_pct")),
        }
        for index, p in enumerate(makers[:3]):
            facts[f"name_{index}"] = o.fact(f"{base}.loss_makers.subject.{index}.name", p["symbol"])
        o.fire(
            "fundamentals",
            "FD.LOSS_MAKERS",
            WATCH,
            facts,
            ["count", "weight"],
            subjects=[p["symbol"] for p in makers[:3]],
            implication="NOT_PRICED_IN_PE",
            priority=10,
        )

    lens = ((rev.get("map") or {}).get("lenses") or {}).get("valuation")
    if not lens:
        o.lack("fundamentals", "FD.MAP_COUNT", METRIC_UNAVAILABLE)
    else:
        good, mid, bad = lens["statuses"]
        counts = lens["counts"]
        total = sum(counts.values())
        b = f"{base}.map"
        o.fire(
            "fundamentals",
            "FD.MAP_COUNT",
            NEUTRAL,
            {
                "good": o.fact(f"{b}.attractive_count", counts[good]),
                "mixed": o.fact(f"{b}.fair_count", counts[mid]),
                "bad": o.fact(f"{b}.expensive_count", counts[bad]),
                "na": o.fact(f"{b}.na_count", counts["N/A"]),
                "total": o.fact(f"{b}.position_count", total),
                "reference": o.fact(f"{b}.label", lens.get("reference_label")),
            },
            ["good", "total"],
            priority=50,
        )
    # Needs the index's weighted profit growth, which no stored source provides.
    o.lack(
        "fundamentals",
        "FD.TRADE_OFF",
        INPUT_NOT_INGESTED,
        detail=["Nifty 50 weighted profit growth"],
    )


def _performance(o: _Out, payload: dict[str, Any]) -> None:
    risk = (payload.get("risk") or {}).get("1y") or {}
    period = risk.get("period") or {}
    base = "basic.performance"
    mine, bench = risk.get("cum_return"), risk.get("bench_cum_return")
    full_year = period.get("start") and period.get("start") == period.get("requested_start")
    if not full_year or mine is None or bench is None:
        o.lack(
            "performance", "PF.ONE_LAKH", HISTORY_TOO_SHORT if not full_year else METRIC_UNAVAILABLE
        )
    else:
        yours = (ONE_LAKH * (1 + Decimal(str(mine)))).quantize(Decimal("1"))
        index = (ONE_LAKH * (1 + Decimal(str(bench)))).quantize(Decimal("1"))
        gap = yours - index
        behind = gap < 0
        o.fire(
            "performance",
            "PF.ONE_LAKH",
            NEEDS_ATTENTION if behind else POSITIVE,
            {
                "yours": o.fact(f"{base}.one_lakh.portfolio_value", yours),
                "index": o.fact(f"{base}.one_lakh.benchmark_value", index),
                # Unsigned rupees: the direction word carries the sign, so the line can
                # never read "₹-27,692 more".
                "gap": o.fact(f"{base}.one_lakh.gap_value", abs(gap)),
                "start": o.fact(f"{base}.one_lakh.period_start", period.get("start")),
            },
            ["yours", "index", "gap"],
            direction="less" if behind else "more",
            allowed=["less", "behind", "short"] if behind else ["more", "ahead"],
            forbidden=["more", "ahead"] if behind else ["less", "behind", "short"],
            priority=10,
        )

    windows = (payload.get("performance") or {}).get("windows") or {}
    window = windows.get("all") or {}
    x, bx = _f(window.get("xirr")), _f(window.get("benchmark_xirr"))
    if x is None or bx is None:
        o.lack("performance", "PF.XIRR_VS_INDEX", METRIC_UNAVAILABLE)
    else:
        gap_pp = (x - bx) * 100
        behind = gap_pp < 0
        in_line = abs(gap_pp) < 2.0
        o.fire(
            "performance",
            "PF.XIRR_VS_INDEX",
            NEUTRAL if in_line else NEEDS_ATTENTION if behind else POSITIVE,
            {
                "yours": o.fact(f"{base}.xirr.xirr", x),
                "index": o.fact(f"{base}.xirr.benchmark_xirr", bx),
                "start": o.fact(f"{base}.xirr.period_start", str(window.get("start"))),
                "annualised": o.fact(f"{base}.xirr.annualised", bool(window.get("annualised"))),
            },
            ["yours", "index", "start"],
            direction=None if in_line else "behind" if behind else "ahead",
            magnitude=return_magnitude(gap_pp),
            allowed=[] if in_line else _BEHIND if behind else _AHEAD,
            forbidden=[] if in_line else _AHEAD if behind else _BEHIND,
            priority=20,
        )


def evaluate(
    payload: dict[str, Any],
    contribution: dict[str, Any] | None,
    profiles: dict[str, tuple[str | None, str | None]] | None = None,
) -> dict[str, Any]:
    """The analytics payload -> ``{"rule_version", "facts", "tabs"}``.

    ``contribution`` is the 1Y ``/contribution`` response (or ``{"error": ...}`` when it
    could not be computed). ``profiles`` maps ISIN -> (profile_status, industry_group)
    from ``security_health_profile``.
    """
    o = _Out()
    _overview(o, payload, contribution, profiles or {})
    _risk(o, payload)
    _diversification(o, payload)
    _fundamentals(o, payload)
    _performance(o, payload)
    for tab in o.tabs.values():
        tab["candidates"].sort(key=lambda c: (_TONE_ORDER.get(c["tone"], 9), c["priority"]))
    return {"rule_version": RULE_VERSION, "facts": o.facts, "tabs": o.tabs}
