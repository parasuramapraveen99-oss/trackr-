"""Proving, or refusing, what the model proposed. No network, no model, no exceptions.

Same shape and same discipline as ``app/ingest/llm_verify.py``, which this deliberately
mirrors: *the model proposes; this module disposes.* That module's docstring argues that
asking a model for its own confidence is measurably worse than checking its work, and the
argument transfers -- with one advantage. A contract note can only be checked against
itself. A claim here is checked against a fact pack that was computed independently, by
the engine, from the ledger.

THE SEVEN RULES, and what each one is actually for:

  V1 RESOLUTION     every fact, lexicon phrase, card and set the claim names exists in
                    THIS pack. Catches a fabricated reference.
  V2 NO DIGITS      model-written text contains no digit at all. This is what makes
                    "the model never emits a number" a checkable property rather than an
                    intention.
  V3 NO QUANTITIES  nor a quantity spelled out in words. Without V3, V2 does not remove
                    the capability, it only changes the register -- and "roughly a fifth
                    of the book" is worse than a digit, because no rule can catch it
                    afterwards.
  V4 THE RELATION   the declared relation re-computes true over the pack. Catches the
                    subtle killer: correct numbers, false comparison.
  V5 EVIDENCE       every fact the claim leans on is declared in ``cites``. The
                    disclosure shown to the reader is then the claim's own account of
                    its evidence, not a guess assembled afterwards.
  V6 COVERAGE       a claim resting on a thinly covered figure must SHOW the coverage,
                    not merely know it.
  V7 NO ADVICE      a belt-and-braces text filter. The real control is that the
                    vocabulary in ``claims.py`` has no advice verb, so a recommendation
                    cannot be formed; this catches one smuggled into prose.

ONE REJECTED CLAIM DOES NOT POISON THE OTHERS, and that is the opposite of
``llm_verify``'s rule -- deliberately. There, one ungrounded row rejects the document,
because a model that fabricated one trade has probably fabricated others the type checks
missed, and a wrong row becomes a ledger entry. Here nothing is written anywhere: a
rejected claim is a sentence not shown. Dropping the whole block because one claim
over-reached would throw away three good sentences to punish a fourth.

WHAT SURVIVES IS NOT "TRUE". It is checked -- which is a narrower and more honest word.
A claim can pass all seven and still be a banal thing to say. Selecting what is worth
saying is the model's job, and the only part of this feature that is genuinely its job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.narrative.cards import CARDS
from app.narrative.claims import COVERAGE_FLOOR, Claim, evaluate, referenced_facts
from app.narrative.factpack import FactPack
from app.narrative.lexicon import LEXICON

#: V2. Any digit at all in model-written text. Digit-bearing NAMES are not written, they
#: are selected from the lexicon, so there is no legitimate digit left to allow.
_DIGIT = re.compile(r"\d")

#: V3. Quantities spelled out, plus the symbols a model reaches for when digits are gone.
#:
#: This is a deny-list, which ``app/ingest/redaction.py`` argues against -- but that
#: argument is that NAMES HAVE NO SHAPE, and it does not transfer. Quantity words are a
#: closed, small, enumerable class. They have exactly the shape the argument requires.
_QUANTITY_WORDS = re.compile(
    r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|dozen|"
    r"half|halves|third|thirds|quarter|quarters|fifth|fifths|"
    r"double|triple|twice|thrice|"
    r"most|majority|minority|nearly all|almost all|almost none|hardly any|"
    r"percent|percentage points|basis points|bps|crore|lakh|rupees)\b"
    r"|[%×✕]|₹|\bRs\b",
    re.IGNORECASE,
)

#: V7. Recommendation language. The vocabulary already makes advice unexpressible as a
#: CLAIM; this catches it smuggled into connective prose.
_ADVICE = re.compile(
    r"\b(?:buy|sell|should|ought|must|recommend\w*|advis\w*|suggest\w*|"
    r"trim|rebalance|divest|liquidate|overweight|underweight|"
    r"target price|take profit|book profit|cut your|add to your)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Rejection:
    """Why one claim was refused, naming the rule that refused it.

    Invariant 5 in spirit: a failed verification is a recorded exception, never a
    silently dropped sentence. A count of these is the only way anyone learns that a
    prompt change started producing claims nothing can check.
    """

    rule: str
    index: int
    reason: str


@dataclass(frozen=True, slots=True)
class VerifiedNarrative:
    claims: list[Claim] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)

    @property
    def rejected_rules(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for rejection in self.rejections:
            counts[rejection.rule] = counts.get(rejection.rule, 0) + 1
        return counts


def verify(claims: list[Claim], pack: FactPack) -> VerifiedNarrative:
    """Claims the model proposed -> the ones that survived, and why the rest did not."""
    kept: list[Claim] = []
    rejections: list[Rejection] = []

    for index, claim in enumerate(claims):
        failure = _check(claim, pack)
        if failure is None:
            evaluation = evaluate(claim, pack)
            kept.append(
                Claim(
                    relation=claim.relation,
                    args=claim.args,
                    segments=claim.segments,
                    cites=claim.cites,
                    card=claim.card,
                    outcome=evaluation.outcome,
                )
            )
            continue
        rule, reason = failure
        rejections.append(Rejection(rule, index, reason))

    return VerifiedNarrative(claims=kept, rejections=rejections)


def _check(claim: Claim, pack: FactPack) -> tuple[str, str] | None:
    """The first rule the claim fails, or None. Order matters: resolution before
    evaluation, because an unresolvable id makes every later check meaningless."""

    # --- V1: everything it names must exist, in THIS pack --------------------------
    for segment in claim.segments:
        if segment.t == "fact":
            if not segment.id or segment.id not in pack:
                return "V1", f"segment cites a fact not in this pack: {segment.id!r}"
        elif segment.t == "lex":
            if not segment.id or segment.id not in LEXICON:
                return "V1", f"unknown lexicon phrase: {segment.id!r}"
        elif segment.t == "rel":
            pass  # resolved by the renderer from the evaluator's outcome
        elif segment.t == "text":
            if segment.v is None:
                return "V1", "a text segment carries no text"
        else:
            return "V1", f"unknown segment kind: {segment.t!r}"

    if claim.card is not None and claim.card not in CARDS:
        return "V1", f"unknown definition card: {claim.card!r}"

    for cited in claim.cites:
        if cited not in pack:
            return "V1", f"cites a fact not in this pack: {cited!r}"

    # --- V2/V3/V7: what the model actually wrote ------------------------------------
    for segment in claim.segments:
        if segment.t != "text" or not segment.v:
            continue
        if _DIGIT.search(segment.v):
            return "V2", f"a digit in model-written text: {segment.v!r}"
        if (found := _QUANTITY_WORDS.search(segment.v)):
            return "V3", f"a quantity in words: {found.group(0)!r} in {segment.v!r}"
        if (found := _ADVICE.search(segment.v)):
            return "V7", f"recommendation language: {found.group(0)!r}"

    # --- V4: the relation must re-compute true --------------------------------------
    evaluation = evaluate(claim, pack)
    if not evaluation.holds:
        return "V4", evaluation.reason or f"{claim.relation} does not hold"

    # --- V5: the evidence must be declared ------------------------------------------
    undeclared = [
        fact_id
        for fact_id in set(referenced_facts(claim, pack)) | set(evaluation.touched)
        if fact_id not in claim.cites and fact_id in pack
    ]
    if undeclared:
        return "V5", f"leans on facts it does not cite: {sorted(undeclared)}"

    # --- V6: thin coverage must be shown, not merely known --------------------------
    thin = [
        fact_id
        for fact_id in claim.cites
        if (fact := pack.get(fact_id)) is not None
        and fact.coverage_pct is not None
        and fact.coverage_pct < COVERAGE_FLOOR
    ]
    if thin:
        shows_coverage = any(
            segment.t == "fact" and (segment.id or "").startswith("coverage.")
            for segment in claim.segments
        )
        if not shows_coverage:
            return (
                "V6",
                f"rests on thinly covered facts {sorted(thin)} without rendering a "
                "coverage figure the reader can see",
            )

    return None
