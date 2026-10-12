"""The closed vocabulary of things a claim may assert, and the code that re-decides each.

THE MODEL DOES NOT WRITE A COMPARISON; IT DECLARES ONE. "Concentration is the weakest
component" arrives as ``RANK_EXTREME(SET.health_components_by_score, min, concentration)``
and this module recomputes the argmin. Nothing parses English, which matters because the
alternative -- read the sentence, work out what was compared, check it -- is unbounded
and fails silently on the wording nobody anticipated.

That turns the dangerous failure into an impossible one. A model CAN pick the wrong
relation or the wrong member; both are caught here. What it cannot do is produce a
sentence whose claim was never evaluated, because the sentence is a rendering of the
claim rather than its source.

WHAT IS DELIBERATELY ABSENT, and each absence is a control rather than an omission:

  - NO TEMPORAL RELATION UNTIL THERE IS A STORED YESTERDAY. ``portfolio_snapshot``
    holds totals only -- not a component score, not a factor tilt, not a weight -- which
    is why ``narrative_factpack`` exists. ``DELTA_SINCE`` evaluates against that stored
    pack and against nothing else: with no history the evaluator refuses, so on a book's
    first day change language is impossible rather than merely wrong. Both sides are
    quantised, so a reported change is one the reader could have seen.
  - NO ADVICE VERB. There is no BUY, SELL, AVOID, PREFER or SHOULD. A recommendation is
    not rejected by a word filter here; it is unexpressible, which is how
    docs/COMPLIANCE.md's "no auto-generated buy/sell recommendations" is met.
  - NO CAUSAL RELATION. "Concentration is why you are behind the index" is not checkable
    from a fact pack at a point in time, and a plausible-sounding cause is the most
    persuasive wrong thing this system could say.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from app.narrative.factpack import FactPack
from app.narrative.registry import MONEY

#: Below this absolute value a signed figure is reported as flat rather than as a
#: direction. Without a dead zone a -0.0001pp gap is described as "behind the index",
#: which is true and useless -- the same reasoning as composition.TREND_DEAD_ZONE_PCT.
FLAT_EPSILON = 1e-9

#: The coverage floor. A cited fact below this needs its claim to say so (V6).
COVERAGE_FLOOR = 95.0

#: band id -> ordered (label, lower bound) pairs, descending. Published, like
#: verdict.GRADE_BANDS, so a reader can check which band a figure fell in.
BANDS: dict[str, tuple[tuple[str, float], ...]] = {
    "BAND.grade": (("A", 80.0), ("B", 65.0), ("C", 50.0), ("D", 35.0), ("E", 0.0)),
    #: The correlation reading already shown on the Diversification panel.
    "BAND.correlation": (
        ("close to one position", 0.60),
        ("partly independent", 0.15),
        ("genuinely independent", -1.01),
    ),
    #: The 65/40 split the factor meters already colour on.
    "BAND.factor": (("a clear tilt", 65.0), ("no strong view", 40.0), ("a tilt away", 0.0)),
}


@dataclass(frozen=True, slots=True)
class Segment:
    """One piece of a rendered sentence.

    ``text`` is the only kind the model authors, and it is the only kind the digit and
    quantity-word rules police. ``fact``, ``lex`` and ``rel`` are all resolved here.
    """

    t: str
    #: Only for t == "text".
    v: str | None = None
    #: Only for t in {"fact", "lex"}; for "rel" this is filled by the renderer.
    id: str | None = None


@dataclass(frozen=True, slots=True)
class Claim:
    relation: str
    args: dict[str, Any]
    segments: tuple[Segment, ...]
    #: Every fact this claim rests on. Declared, not inferred, so the disclosure that
    #: accompanies a generated block is the model's own account of its evidence.
    cites: tuple[str, ...] = ()
    card: str | None = None
    #: Set by the evaluator, never by the model: which branch of the relation held.
    #: The renderer turns it into a directional word via RELATION_WORDS.
    outcome: str | None = None


@dataclass(frozen=True, slots=True)
class Evaluation:
    holds: bool
    outcome: str | None = None
    reason: str = ""
    #: Facts the evaluator actually read. Used by V5 to check the claim declared them.
    touched: tuple[str, ...] = field(default_factory=tuple)


def _fail(reason: str, touched: tuple[str, ...] = ()) -> Evaluation:
    return Evaluation(False, None, reason, touched)


def _numeric(pack: FactPack, fact_id: str) -> tuple[float | None, str]:
    """A comparable float, or a reason it is not one.

    MONEY IS REFUSED HERE. It is a decimal string and ``float()`` on it is exactly the
    conversion CLAUDE.md rule 1 exists to prevent. A relation that genuinely needs to
    compare rupees must go through ``_money`` below and stay in Decimal.
    """
    fact = pack.get(fact_id)
    if fact is None:
        return None, f"no such fact: {fact_id}"
    if fact.unit == MONEY:
        return None, f"{fact_id} is money and is not compared as a float"
    if not fact.comparable:
        return None, f"{fact_id} is not an orderable quantity"
    if fact.value is None:
        return None, f"{fact_id} has no reading"
    return float(fact.value), ""


def _money(pack: FactPack, fact_id: str) -> tuple[Decimal | None, str]:
    fact = pack.get(fact_id)
    if fact is None:
        return None, f"no such fact: {fact_id}"
    if fact.unit != MONEY or fact.value is None:
        return None, f"{fact_id} is not a money reading"
    try:
        return Decimal(str(fact.value)), ""
    except InvalidOperation:
        return None, f"{fact_id} is not a decimal: {fact.value!r}"


def _same_unit(pack: FactPack, a: str, b: str) -> str:
    """Two figures may only be compared when they measure the same kind of thing.

    "Your Sharpe is above your beta" is grammatical and arithmetically evaluable and
    means nothing. Unit equality is what makes that refusable.
    """
    fa, fb = pack.get(a), pack.get(b)
    if fa is None or fb is None:
        return "one side of the comparison is not in this pack"
    if fa.unit != fb.unit:
        return f"cannot compare {fa.unit} ({a}) with {fb.unit} ({b})"
    return ""


# --- the evaluators -------------------------------------------------------------------


def _compare(pack: FactPack, args: dict, greater: bool) -> Evaluation:
    subject, obj = args.get("subject"), args.get("object")
    if not subject or not obj:
        return _fail("GREATER_THAN/LESS_THAN need `subject` and `object`")
    touched = (subject, obj)
    if (clash := _same_unit(pack, subject, obj)):
        return _fail(clash, touched)
    left, why = _numeric(pack, subject)
    if left is None:
        return _fail(why, touched)
    right, why = _numeric(pack, obj)
    if right is None:
        return _fail(why, touched)
    holds = left > right if greater else left < right
    return Evaluation(
        holds, "true" if holds else None,
        "" if holds else f"{subject} ({left}) is not {'above' if greater else 'below'} "
                         f"{obj} ({right})",
        touched,
    )


def _threshold(pack: FactPack, args: dict, above: bool) -> Evaluation:
    """Against the fact's OWN published threshold, never one the model supplied.

    A model-supplied threshold is a model-supplied opinion about what counts as a lot.
    Every threshold in this system is published by a rule -- verdict's insight table,
    the grade bands -- and rides on the fact.
    """
    fact_id = args.get("fact")
    if not fact_id:
        return _fail("EXCEEDS_THRESHOLD/BELOW_THRESHOLD need `fact`")
    fact = pack.get(fact_id)
    if fact is None:
        return _fail(f"no such fact: {fact_id}", (fact_id,))
    if fact.threshold is None:
        return _fail(f"{fact_id} carries no published threshold", (fact_id,))
    value, why = _numeric(pack, fact_id)
    if value is None:
        return _fail(why, (fact_id,))
    holds = value > fact.threshold if above else value < fact.threshold
    return Evaluation(
        holds, "true" if holds else None,
        "" if holds else f"{fact_id} ({value}) is not "
                         f"{'above' if above else 'below'} {fact.threshold}",
        (fact_id,),
    )


def _rank_extreme(pack: FactPack, args: dict) -> Evaluation:
    """Recompute the argmin/argmax. This is what replaces the word "weakest"."""
    set_id, extreme, member = args.get("set"), args.get("extreme"), args.get("member")
    if not set_id or extreme not in ("min", "max") or not member:
        return _fail("RANK_EXTREME needs `set`, `extreme` in {min,max}, and `member`")
    factset = pack.sets.get(set_id)
    if factset is None:
        return _fail(f"no such set in this pack: {set_id}")
    if member not in factset.members:
        return _fail(f"{member!r} is not a member of {set_id}")
    touched = factset.fact_ids()
    readings: list[tuple[str, float]] = []
    for candidate in factset.members:
        value, why = _numeric(pack, factset.metric.format(candidate))
        if value is None:
            # A set with an unreadable member cannot be ranked. Ranking the rest would
            # be a true statement about a set the reader cannot see.
            return _fail(f"{set_id} cannot be ranked: {why}", touched)
        readings.append((candidate, value))
    picker = min if extreme == "min" else max
    winner = picker(readings, key=lambda pair: pair[1])[0]
    holds = winner == member
    return Evaluation(
        holds, extreme if holds else None,
        "" if holds else f"{extreme} of {set_id} is {winner!r}, not {member!r}",
        touched,
    )


def _rank_at(pack: FactPack, args: dict) -> Evaluation:
    set_id, member, position = args.get("set"), args.get("member"), args.get("position")
    if not set_id or not member or not isinstance(position, int):
        return _fail("RANK_AT needs `set`, `member` and an integer `position` (1-based)")
    factset = pack.sets.get(set_id)
    if factset is None:
        return _fail(f"no such set in this pack: {set_id}")
    if member not in factset.members:
        return _fail(f"{member!r} is not a member of {set_id}")
    touched = factset.fact_ids()
    readings = []
    for candidate in factset.members:
        value, why = _numeric(pack, factset.metric.format(candidate))
        if value is None:
            return _fail(f"{set_id} cannot be ranked: {why}", touched)
        readings.append((candidate, value))
    # Descending: position 1 is the largest, which is how every ranked list on the page
    # already reads.
    ordered = [name for name, _ in sorted(readings, key=lambda pair: -pair[1])]
    if not 1 <= position <= len(ordered):
        return _fail(f"position {position} is outside {set_id}", touched)
    holds = ordered[position - 1] == member
    return Evaluation(
        holds, "true" if holds else None,
        "" if holds else f"position {position} of {set_id} is {ordered[position - 1]!r}",
        touched,
    )


def _share_of(pack: FactPack, args: dict) -> Evaluation:
    """Recompute part/whole and check it against the figure the claim cites.

    NAMESPACE RULE, and it is invariant 18 rather than a type check: a part and a whole
    must come from the same namespace. Dividing an EXPECTED dividend by a REALISED total
    is how a projection gets summed into a realised figure, which is the one thing
    docs/ARCHITECTURE_INVARIANTS.md says must never happen.
    """
    part, whole, cited = args.get("part"), args.get("whole"), args.get("cited")
    if not part or not whole or not cited:
        return _fail("SHARE_OF needs `part`, `whole` and `cited`")
    touched = (part, whole, cited)
    if part.split(".")[0] != whole.split(".")[0]:
        return _fail(
            f"cannot take a share of {whole} from {part}: different namespaces", touched
        )
    if (clash := _same_unit(pack, part, whole)):
        return _fail(clash, touched)

    part_fact = pack.require(part) if part in pack else None
    if part_fact is not None and part_fact.unit == MONEY:
        numerator, why = _money(pack, part)
        denominator, why2 = _money(pack, whole)
        if numerator is None or denominator is None:
            return _fail(why or why2, touched)
        if denominator == 0:
            return _fail(f"{whole} is zero; there is no share of it", touched)
        computed = float(numerator / denominator * 100)
    else:
        numerator, why = _numeric(pack, part)
        if numerator is None:
            return _fail(why, touched)
        denominator, why = _numeric(pack, whole)
        if denominator is None:
            return _fail(why, touched)
        if denominator == 0:
            return _fail(f"{whole} is zero; there is no share of it", touched)
        computed = numerator / denominator * 100

    claimed, why = _numeric(pack, cited)
    if claimed is None:
        return _fail(why, touched)
    # Agreement to the precision the cited figure is rendered at -- tighter would fail on
    # rounding the reader cannot see, looser would pass on a difference they can.
    tolerance = 10 ** -pack.require(cited).digits / 2
    holds = abs(computed - claimed) <= max(tolerance, 0.05)
    return Evaluation(
        holds, "true" if holds else None,
        "" if holds else f"{part}/{whole} is {computed:.4f}%, but {cited} says {claimed}",
        touched,
    )


def _within_band(pack: FactPack, args: dict) -> Evaluation:
    fact_id, band_id = args.get("fact"), args.get("band")
    if not fact_id or not band_id:
        return _fail("WITHIN_BAND needs `fact` and `band`")
    bands = BANDS.get(band_id)
    if bands is None:
        return _fail(f"no such band table: {band_id}")
    value, why = _numeric(pack, fact_id)
    if value is None:
        return _fail(why, (fact_id,))
    label = next((name for name, floor in bands if value >= floor), None)
    if label is None:
        return _fail(f"{fact_id} ({value}) falls outside {band_id}", (fact_id,))
    expected = args.get("label")
    if expected is not None and expected != label:
        return _fail(
            f"{fact_id} ({value}) is in {label!r}, not {expected!r}", (fact_id,)
        )
    return Evaluation(True, "true", "", (fact_id,))


def _sign(pack: FactPack, args: dict) -> Evaluation:
    """Sign, with an explicit dead zone, and money handled as Decimal."""
    fact_id, sign = args.get("fact"), args.get("sign")
    if not fact_id or sign not in ("positive", "negative", "flat"):
        return _fail("SIGN needs `fact` and `sign` in {positive,negative,flat}")
    fact = pack.get(fact_id)
    if fact is None:
        return _fail(f"no such fact: {fact_id}", (fact_id,))
    if fact.unit == MONEY:
        amount, why = _money(pack, fact_id)
        if amount is None:
            return _fail(why, (fact_id,))
        actual = "flat" if amount == 0 else ("positive" if amount > 0 else "negative")
    else:
        value, why = _numeric(pack, fact_id)
        if value is None:
            return _fail(why, (fact_id,))
        actual = (
            "flat" if abs(value) <= FLAT_EPSILON
            else ("positive" if value > 0 else "negative")
        )
    holds = actual == sign
    return Evaluation(
        holds, actual if holds else None,
        "" if holds else f"{fact_id} is {actual}, not {sign}", (fact_id,),
    )


def _enum_is(pack: FactPack, args: dict) -> Evaluation:
    fact_id, value = args.get("fact"), args.get("value")
    if not fact_id or value is None:
        return _fail("ENUM_IS needs `fact` and `value`")
    fact = pack.get(fact_id)
    if fact is None:
        return _fail(f"no such fact: {fact_id}", (fact_id,))
    holds = fact.value == value
    return Evaluation(
        holds, "true" if holds else None,
        "" if holds else f"{fact_id} is {fact.value!r}, not {value!r}", (fact_id,),
    )


def _coverage_below(pack: FactPack, args: dict) -> Evaluation:
    fact_id = args.get("fact")
    if not fact_id:
        return _fail("COVERAGE_BELOW needs `fact`")
    value, why = _numeric(pack, fact_id)
    if value is None:
        return _fail(why, (fact_id,))
    holds = value < COVERAGE_FLOOR
    return Evaluation(
        holds, "true" if holds else None,
        "" if holds else f"{fact_id} is {value}, which is not below {COVERAGE_FLOOR}",
        (fact_id,),
    )


def _absent(pack: FactPack, args: dict) -> Evaluation:
    """How "there is no reading for this" gets said.

    Needed because the honest answer is often that a window does not exist yet, and
    without a relation for it the model's only way to mention a gap is to describe it in
    free text -- unchecked.
    """
    fact_id = args.get("fact")
    if not fact_id:
        return _fail("ABSENT needs `fact`")
    fact = pack.get(fact_id)
    if fact is None:
        return _fail(f"no such fact: {fact_id}", (fact_id,))
    holds = fact.value is None
    return Evaluation(
        holds, "true" if holds else None,
        "" if holds else f"{fact_id} has a reading ({fact.value!r})", (fact_id,),
    )


def _delta_since(pack: FactPack, args: dict) -> Evaluation:
    """Compare a figure against the most recent stored pack. Never against "yesterday".

    There is no row for a weekend or a holiday, so the comparison is against whatever
    the previous stored date turns out to be, and the claim must render
    ``delta.since_date`` to say which. A change measured from an unnamed day is an
    unfalsifiable one.

    MONEY IS COMPARED AS DECIMAL, like everywhere else. Both sides are already quantised
    strings, so this is an exact comparison of what was rendered -- not a tolerance.
    """
    fact_id, direction = args.get("fact"), args.get("direction")
    if not fact_id or direction not in ("up", "down", "unchanged"):
        return _fail("DELTA_SINCE needs `fact` and `direction` in {up,down,unchanged}")
    if not pack.previous or pack.previous_as_of is None:
        return _fail(
            "no earlier fact pack is stored, so no change can be stated", (fact_id,)
        )
    fact = pack.get(fact_id)
    if fact is None:
        return _fail(f"no such fact: {fact_id}", (fact_id,))
    if fact_id not in pack.previous:
        # Appearing and moving are different events. Reporting the first as the second
        # would invent a change out of a data gap that closed.
        return _fail(f"{fact_id} has no earlier reading to compare against", (fact_id,))

    before = pack.previous[fact_id]
    if before is None or fact.value is None:
        return _fail(f"{fact_id} has no reading on one side of the comparison", (fact_id,))

    # Quantise the current side the same way the stored side already is, so the
    # comparison is between two rendered figures rather than a rendered one and a raw one.
    from app.narrative.digest import _quantise_money, _quantise_number

    if fact.unit == MONEY:
        now_value: Any = _quantise_money(fact.value, fact.digits)
        try:
            delta = Decimal(str(now_value)) - Decimal(str(before))
        except InvalidOperation:
            return _fail(f"{fact_id} is not a decimal on both sides", (fact_id,))
        actual = "unchanged" if delta == 0 else ("up" if delta > 0 else "down")
    elif fact.unit in ("enum", "label"):
        return _fail(f"{fact_id} is not a quantity, so it cannot rise or fall", (fact_id,))
    else:
        now_value = _quantise_number(fact.value, fact.unit, fact.digits)
        delta_f = float(now_value) - float(before)
        actual = (
            "unchanged" if abs(delta_f) <= FLAT_EPSILON
            else ("up" if delta_f > 0 else "down")
        )

    holds = actual == direction
    return Evaluation(
        holds, actual if holds else None,
        "" if holds else f"{fact_id} is {actual} since {pack.previous_as_of}, not {direction}",
        (fact_id,),
    )


def _definition(pack: FactPack, args: dict) -> Evaluation:
    """A card carries no numeric claim, so there is nothing to recompute.

    It still goes through the vocabulary rather than being free text, because the card
    body is server-owned: the model chooses WHICH definition to show, never what it says.
    """
    return Evaluation(True, "true", "", ())


EVALUATORS = {
    "GREATER_THAN": lambda pack, args: _compare(pack, args, greater=True),
    "LESS_THAN": lambda pack, args: _compare(pack, args, greater=False),
    "EXCEEDS_THRESHOLD": lambda pack, args: _threshold(pack, args, above=True),
    "BELOW_THRESHOLD": lambda pack, args: _threshold(pack, args, above=False),
    "RANK_EXTREME": _rank_extreme,
    "RANK_AT": _rank_at,
    "SHARE_OF": _share_of,
    "WITHIN_BAND": _within_band,
    "SIGN": _sign,
    "ENUM_IS": _enum_is,
    "COVERAGE_BELOW": _coverage_below,
    "ABSENT": _absent,
    "DELTA_SINCE": _delta_since,
    "DEFINITION": _definition,
}

#: The whole vocabulary, as sent to the model and as checked here. One source.
RELATIONS = tuple(sorted(EVALUATORS))


def evaluate(claim: Claim, pack: FactPack) -> Evaluation:
    evaluator = EVALUATORS.get(claim.relation)
    if evaluator is None:
        return _fail(f"{claim.relation!r} is not a relation in this vocabulary")
    return evaluator(pack, claim.args or {})


def referenced_facts(claim: Claim, pack: FactPack) -> tuple[str, ...]:
    """Every fact id the claim points at, from its args and its segments alike."""
    out: list[str] = []
    for key in ("subject", "object", "fact", "part", "whole", "cited"):
        value = (claim.args or {}).get(key)
        if isinstance(value, str) and value in pack:
            out.append(value)
    out.extend(s.id for s in claim.segments if s.t == "fact" and s.id)
    return tuple(dict.fromkeys(out))
