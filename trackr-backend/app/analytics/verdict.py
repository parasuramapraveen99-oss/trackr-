"""The portfolio health grade and the insight chips.

THE HIGHEST-RISK COMPONENT IN THE MODULE, and it is built last for that reason. A single
letter grade is the most legible thing on the page and will be the most screenshotted,
which makes an undocumented or unstable one a liability rather than a feature.

Three properties are therefore non-negotiable and are enforced here, not by convention:

  1. PUBLISHED RUBRIC. Component weights and band cut-offs are data, returned to the UI and
     rendered on the tooltip -- never buried in code nobody reads.
  2. FULL DECOMPOSITION. Every grade can be taken apart into each component's contribution
     in points. A score you cannot take apart is a horoscope.
  3. NO VERDICT LANGUAGE. The band and its drivers, never "POOR HEALTH" -- that is a
     judgement on the person, not a description of a portfolio.

Insight chips follow the same discipline: each carries the value that fired it, the
threshold it crossed, and a rule_version, so a string on screen can always be traced to the
rule that produced it.

THIS MODULE NO LONGER WRITES PROSE, and that is a fourth property rather than a tidy-up.
Every ``Component`` and ``Insight`` used to carry a hand-written ``detail`` sentence with
numbers formatted into it. Those sentences were the only place several figures existed --
a component's coverage percentage, the single-name concentration threshold that did not
fire -- so a consumer that wanted the number had to parse English back out of a string
this module had just finished formatting, which is property 2 failing quietly. Prose is
now generated from ``evidence`` by ``app/narrative/``, and a number that is worth saying
in a sentence must first exist as a field here.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Bumped whenever a threshold below changes, so a screenshot stays explicable.
RULE_VERSION = "2026.10.09"

#: Component -> weight. Published, and summing to 1.0 is asserted by the tests.
HEALTH_WEIGHTS = {
    "factor_composite": 0.40,
    "concentration": 0.20,
    "balance_sheet": 0.20,
    "benchmark_relative": 0.20,
}

#: Lower bound per grade, descending. A grade change should additionally have to HOLD
#: across a boundary for three sessions before the letter moves -- that stability rule
#: belongs to the caller, which is the only layer that can see yesterday's score.
GRADE_BANDS = (("A", 80), ("B", 65), ("C", 50), ("D", 35), ("E", 0))

#: The effective-holdings count that scores full marks on concentration.
CONCENTRATION_TARGET = 20.0


@dataclass(frozen=True, slots=True)
class Component:
    key: str
    label: str
    weight: float
    score: float | None
    points: float | None
    #: THE NUMBERS THIS SCORE WAS COMPUTED FROM, not a sentence about them.
    #:
    #: This field used to be ``detail: str`` -- a hand-written f-string like "Effective
    #: holdings 4.2 of a 20-name target (1/HHI)." That prose is now generated from these
    #: values by ``app/narrative/`` instead, and the reason it had to move is property 2
    #: of this module's contract rather than a presentation preference: a sentence is not
    #: decomposable. "Covers 74% of the book" cannot be compared, ranked or thresholded by
    #: anything downstream, so every consumer that wanted the 74 had to parse English back
    #: out of a string we had just finished formatting.
    #:
    #: Keys are stable and are addressed directly as fact ids
    #: (``health.component.concentration.evidence.effective_holdings``), so renaming one
    #: breaks a fact id and is a versioned change, not a tidy-up.
    evidence: dict[str, float | None]


@dataclass(frozen=True, slots=True)
class Health:
    score: int | None
    grade: str | None
    components: list[Component]
    drivers: list[str]
    bands: tuple = GRADE_BANDS
    rule_version: str = RULE_VERSION


@dataclass(frozen=True, slots=True)
class Insight:
    key: str
    title: str
    severity: str
    #: Every number the rule looked at, keyed by name. Same move, same reason, as
    #: ``Component.evidence`` above: ``value`` and ``threshold`` are what FIRED the chip,
    #: but the prose used to carry a second set of numbers -- each name's individual
    #: weight, the single-name threshold that did NOT fire -- that existed nowhere else.
    evidence: dict[str, float | None]
    #: The securities this chip is ABOUT, in the order the rule ranked them. Separate from
    #: ``evidence`` because these are labels, not measurements: a renderer needs to link
    #: them to their holdings, while a verifier must leave them alone rather than try to
    #: compare them.
    subjects: list[str]
    value: float | None = None
    threshold: float | None = None
    rule_version: str = RULE_VERSION


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def balance_sheet_score(debt_to_equity: float | None, interest_cover: float | None) -> float | None:
    """Published linear thresholds rather than an opaque percentile.

    D/E 0.25x -> 100 falling to 1.5x -> 0; interest cover 2 -> 0 rising to 10 -> 100. These
    are conventional and legible, which matters more here than precision: a reader can
    check them, and a percentile against a universe they cannot see is unfalsifiable.

    D/E is a RATIO (0.33 = 0.33x). The same cut-offs were written 25 -> 150 while the feed's
    percent form was stored unconverted; migration 070 moved storage to a ratio and these
    thresholds moved with it, so every score is unchanged.
    """
    parts = []
    if debt_to_equity is not None:
        parts.append(clamp(100.0 - (debt_to_equity - 0.25) * (100.0 / 1.25)))
    if interest_cover is not None:
        parts.append(clamp((interest_cover - 2.0) * (100.0 / 8.0)))
    return sum(parts) / len(parts) if parts else None


def compute_health(
    factor_scores: list[float | None],
    effective_n: float | None,
    balance_sheet: float | None,
    balance_sheet_coverage: float,
    relative_1y_pp: float | None,
    benchmark_coverage_pct: float | None = None,
) -> Health:
    """Weighted composite of four published components, with the arithmetic shown."""
    usable_factors = [f for f in factor_scores if f is not None]
    factor_composite = sum(usable_factors) / 4 if len(usable_factors) == 4 else None

    concentration = (
        clamp(100.0 * effective_n / CONCENTRATION_TARGET) if effective_n is not None else None
    )
    relative = clamp(50.0 + relative_1y_pp * 2.0) if relative_1y_pp is not None else None

    # A None in ``evidence`` means UNKNOWN and must render as "no reading", never as zero
    # -- the same rule the fundamentals columns follow. Missing components contribute
    # no invented points and withhold the overall score and grade.
    specs: list[tuple[str, str, float | None, dict[str, float | None]]] = [
        (
            "factor_composite",
            "Factor composite",
            factor_composite,
            {"factors_scored": float(len(usable_factors)), "factors_offered": 4.0},
        ),
        (
            "concentration",
            "Concentration",
            concentration,
            {"effective_holdings": effective_n, "target": CONCENTRATION_TARGET},
        ),
        (
            "balance_sheet",
            "Balance sheet",
            balance_sheet,
            {"coverage_pct": balance_sheet_coverage},
        ),
        (
            "benchmark_relative",
            "Benchmark-relative 1Y",
            relative,
            {"relative_1y_pp": relative_1y_pp, "coverage_pct": benchmark_coverage_pct},
        ),
    ]

    components: list[Component] = []
    total = 0.0
    for key, label, score, evidence in specs:
        weight = HEALTH_WEIGHTS[key]
        points = round(weight * score, 1) if score is not None else None
        if score is not None:
            total += weight * score
        components.append(Component(key, label, weight, score, points, evidence))

    complete = all(c.score is not None for c in components)
    rounded = int(round(total)) if complete else None
    grade = next(g for g, floor in GRADE_BANDS if total >= floor) if complete else None
    weakest = sorted((c for c in components if c.score is not None), key=lambda c: c.score)
    return Health(
        score=rounded,
        grade=grade,
        components=components,
        drivers=[c.label.lower() for c in weakest[:2]],
    )


def build_insights(
    top_weights: list[tuple[str, float]],
    gain_share_top2: float | None,
    relative_1y_pp: float | None,
    beta: float | None,
    quality_leaders: list[str],
    top_sector: tuple[str, float] | None,
) -> list[Insight]:
    """Rule table. Each chip carries the numbers that fired it AND the thresholds it crossed.

    A chip whose threshold is invisible is an assertion, not an insight -- the reader
    cannot tell whether 27% is alarming or ordinary without knowing what the rule was.
    Thresholds therefore travel with the chip, in ``evidence``, including the ones that did
    NOT fire: "15% for any single name" is load-bearing context for a chip that fired on
    the combined figure instead, and it used to be legible only because a human had
    written it into the sentence.
    """
    out: list[Insight] = []

    if len(top_weights) >= 2:
        first, second = top_weights[0], top_weights[1]
        combined = first[1] + second[1]
        if first[1] > 15.0 or combined > 30.0:
            out.append(
                Insight(
                    key="concentration",
                    title="Concentrated at the top",
                    severity="threat",
                    value=round(combined, 1),
                    threshold=30.0,
                    evidence={
                        "top1_pct": round(first[1], 1),
                        "top2_pct": round(second[1], 1),
                        "combined_pct": round(combined, 1),
                        "single_name_threshold": 15.0,
                        "top_two_threshold": 30.0,
                    },
                    subjects=[first[0], second[0]],
                )
            )

    if gain_share_top2 is not None and gain_share_top2 > 50.0:
        out.append(
            Insight(
                key="gain_reliance",
                title="Gains rest on two names",
                severity="watch",
                value=round(gain_share_top2, 1),
                threshold=50.0,
                evidence={"gain_share_top2_pct": round(gain_share_top2, 1)},
                subjects=[],
            )
        )

    if relative_1y_pp is not None and abs(relative_1y_pp) > 5.0:
        ahead = relative_1y_pp > 0
        out.append(
            Insight(
                key="benchmark_relative",
                title="Ahead of the index" if ahead else "Behind the index",
                severity="strength" if ahead else "threat",
                value=round(relative_1y_pp, 1),
                threshold=5.0,
                evidence={
                    "relative_1y_pp": round(relative_1y_pp, 1),
                    "gap_pp": round(abs(relative_1y_pp), 1),
                },
                subjects=[],
            )
        )

    if beta is not None:
        if beta > 1.2:
            out.append(
                Insight(
                    key="beta_high",
                    title="High market sensitivity",
                    severity="threat",
                    value=round(beta, 2),
                    threshold=1.2,
                    evidence={"beta": round(beta, 2)},
                    subjects=[],
                )
            )
        elif beta >= 0.8:
            out.append(
                Insight(
                    key="beta_market",
                    title="Moves with the market",
                    severity="observation",
                    value=round(beta, 2),
                    threshold=1.2,
                    evidence={
                        "beta": round(beta, 2),
                        "band_low": 0.8,
                        "band_high": 1.2,
                    },
                    subjects=[],
                )
            )

    if len(quality_leaders) >= 3:
        out.append(
            Insight(
                key="quality_leaders",
                title="Quality leaders",
                severity="strength",
                value=float(len(quality_leaders)),
                threshold=3.0,
                evidence={
                    "count": float(len(quality_leaders)),
                    "roe_floor_pct": 30.0,
                },
                subjects=list(quality_leaders),
            )
        )

    if top_sector and top_sector[1] > 25.0:
        out.append(
            Insight(
                key="sector_tilt",
                title="Sector tilt",
                severity="watch",
                value=round(top_sector[1], 1),
                threshold=25.0,
                evidence={"sector_pct": round(top_sector[1], 1)},
                subjects=[top_sector[0]],
            )
        )

    return out
