"""Grounding a free-text reply. WEAKER THAN THE ANALYTICS PATH, and the difference matters.

On the analytics surfaces the model cannot emit a number at all: it declares a claim, and
``render.py`` inserts the figures. Conversation cannot work that way -- an answer to an
unanticipated question has no template, and forcing one would make the assistant useless
at the only thing it is for.

So the chat falls back to the rule ``app/ingest/llm_verify.py`` calls G2: **every numeral
in the reply must appear in the facts the model was given.** That is genuinely weaker,
and the weakness is specific and worth naming rather than glossing:

  - It proves a number was PRESENT. It does not prove it was used for the right thing.
    "Your Sharpe is 1.18" passes if 1.18 is the beta.
  - It cannot check a relation, so "concentration is your strongest component" is
    invisible to it.

What it does catch is the failure that actually matters here and is by far the most
common: a figure that exists nowhere in the portfolio, stated with confidence. A model
that invents a holding value cannot have that value in the pack.

The honest consequence: this path is for understanding and orientation, and the
analytics surfaces are where a checked assertion lives. The system prompt says so to the
model, and the UI says so to the reader.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from app.narrative.factpack import FactPack
from app.narrative.registry import FRACTION, NUMERIC_UNITS

#: Any run of digits with optional grouping, decimals and a trailing percent.
_NUMERAL = re.compile(r"\d[\d,]*(?:\.\d+)?")

#: Numerals that are never portfolio figures and would only produce noise: a year, a
#: day of the month, and the small ordinals that appear in ordinary sentences ("the
#: first", "one of"). Bounded deliberately -- a whitelist by VALUE, not by context.
_UNREMARKABLE = {str(n) for n in range(0, 13)} | {str(y) for y in range(1990, 2101)}


def _normalise(token: str) -> str:
    return token.replace(",", "").rstrip(".")


def allowed_numerals(pack: FactPack) -> set[str]:
    """Every numeral the model could legitimately repeat, in the forms it might write it.

    A single value yields several strings because a model will not reproduce our
    precision: 0.184 may come back as "18.4" (as a percentage), "18", "0.18" or "0.184".
    All are the same reading, and rejecting a correct figure for being rounded
    differently would make the assistant unusable while catching nothing.
    """
    allowed: set[str] = set(_UNREMARKABLE)
    for fact in pack.facts.values():
        if fact.value is None:
            continue
        try:
            amount = Decimal(str(fact.value))
        except (InvalidOperation, ValueError):
            continue
        candidates = [amount]
        if fact.unit == FRACTION:
            # Fractions are read aloud as percentages, which is how they are rendered.
            candidates.append(amount * 100)
        elif fact.unit not in NUMERIC_UNITS:
            # Money and counts: also allow the absolute value, since a loss is commonly
            # spoken as a magnitude with the direction in words.
            candidates.append(abs(amount))
        for candidate in list(candidates):
            candidates.append(abs(candidate))
        for candidate in candidates:
            for places in range(0, 5):
                quantised = candidate.quantize(Decimal(1).scaleb(-places))
                allowed.add(_normalise(str(quantised)))
                allowed.add(_normalise(str(quantised.normalize())))
    return allowed


def ungrounded_numerals(reply: str, pack: FactPack) -> list[str]:
    """Numerals in the reply that are in none of the facts. Empty means grounded."""
    allowed = allowed_numerals(pack)
    found: list[str] = []
    for match in _NUMERAL.finditer(reply):
        token = _normalise(match.group(0))
        if token in allowed:
            continue
        # A trailing-zero difference is not a different number: "18.40" and "18.4".
        try:
            if _normalise(str(Decimal(token).normalize())) in allowed:
                continue
        except InvalidOperation:
            pass
        found.append(match.group(0))
    return found


def is_grounded(reply: str, pack: FactPack) -> bool:
    return not ungrounded_numerals(reply, pack)
