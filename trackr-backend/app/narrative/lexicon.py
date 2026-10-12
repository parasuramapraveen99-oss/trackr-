"""Digit-bearing names the prose is allowed to use, and the directional words it is not
allowed to choose.

WHY A LEXICON EXISTS AT ALL. The verifier refuses any digit in model-written text, which
is what makes "the model never emits a number" checkable rather than aspirational. But
several things on this page are NAMED with digits and are not quantities: NIFTY 500 is an
index, 12-1 momentum is a construction, 1-day 95% VaR is a measure. Banning them outright
would make the page unwritable; allowing "digits are fine inside a name" would hand back
the whole hole, because a model can call anything a name.

So the digit-bearing phrases are enumerated here and selected by id. The model picks
``LEX.nifty500``; it cannot author one. A fabricated quantity therefore has no route to
the screen -- there is no pattern that admits it, only a list that does not contain it.

DIRECTIONAL WORDS ARE SERVER-OWNED FOR THE SAME REASON. ``panels.tsx`` already guards the
defect: "Comparative wording switches on the sign -- a negative described as 'more' is
the defect this avoids." Verifying that after the fact means parsing English. Owning the
word means the disagreement cannot be expressed.
"""

from __future__ import annotations

#: Phrase id -> the literal text rendered. Every entry contains a digit on purpose; a
#: digit-free phrase does not need to be here, because plain text may already say it.
LEXICON: dict[str, str] = {
    "LEX.nifty500": "NIFTY 500",
    "LEX.nifty50": "Nifty 50",
    "LEX.momentum_12_1": "12-1 momentum",
    "LEX.var_1d_95": "1-day 95% VaR",
    "LEX.var_1d_99": "1-day 99% VaR",
    "LEX.cvar_95": "95% CVaR",
    "LEX.dma_50": "50-day moving average",
    "LEX.tbill_91d": "91-day T-bill",
    "LEX.hhi": "1/HHI",
    "LEX.amfi_bands": "the AMFI bands -- top 100 large, 101-250 mid, 251+ small",
    "LEX.sebi_oct_2017": "the SEBI October 2017 circular",
    "LEX.ltcg_365": "the 365-day long-term boundary",
    "LEX.section_112a": "Section 112A",
    "LEX.fy_2020_21": "FY 2020-21",
    "LEX.coverage_floor": "the 95% coverage floor",
}

#: (relation, outcome) -> the word. The outcome vocabulary is closed per relation and is
#: produced by the evaluator, never by the model.
RELATION_WORDS: dict[tuple[str, str], str] = {
    ("GREATER_THAN", "true"): "above",
    ("LESS_THAN", "true"): "below",
    ("EXCEEDS_THRESHOLD", "true"): "past",
    ("BELOW_THRESHOLD", "true"): "short of",
    ("RANK_EXTREME", "min"): "the lowest",
    ("RANK_EXTREME", "max"): "the highest",
    ("SIGN", "positive"): "a gain",
    ("SIGN", "negative"): "a loss",
    ("SIGN", "flat"): "unchanged",
    ("SHARE_OF", "true"): "of",
    ("WITHIN_BAND", "true"): "in",
    ("ENUM_IS", "true"): "is",
    ("COVERAGE_BELOW", "true"): "covers less than",
    ("ABSENT", "true"): "has no reading",
    ("DELTA_SINCE", "up"): "has risen",
    ("DELTA_SINCE", "down"): "has fallen",
    ("DELTA_SINCE", "unchanged"): "is unchanged",
}


class UnknownPhrase(Exception):
    """A lexicon id that does not exist. Fail closed -- see the module docstring."""


def phrase(lex_id: str) -> str:
    try:
        return LEXICON[lex_id]
    except KeyError:
        raise UnknownPhrase(
            f"{lex_id!r} is not in the phrase lexicon. A digit-bearing phrase must be "
            "added here by a human; it can never be authored by the model."
        ) from None
