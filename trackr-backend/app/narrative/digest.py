"""Quantising a fact pack, and hashing it, so identical-looking days give identical prose.

THE CONTRACT: **if two fact packs would render identically, the prose about them must be
identical.** Without it the page paraphrases itself. Prices move every session, so a hash
over full-precision values changes daily even when every figure on screen is unchanged to
the decimal a reader can see -- and each change buys a differently-worded version of a
sentence that was already right. A reader who notices the wording shift and cannot find
any number that moved learns, correctly, not to trust the text.

So every numeric fact is rounded to the precision it will actually be RENDERED at, taken
from the registry rather than guessed, and the hash is over that.

WHAT THIS DOES NOT DO, and why. An earlier design hashed the BAND rather than the value
for any figure only ever compared against a threshold -- grade letter instead of score,
correlation band instead of 0.612 -- which collapses far more days onto one key. It is
unsafe here and the reason is specific: RANK_EXTREME and RANK_AT depend on the ORDER of a
set, and two components can swap places without either crossing a band. The cached claim
"concentration is the lowest" would then survive a day on which it stopped being true.
Fewer cache misses is not worth a false comparison, so values are quantised, not banded.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any

from app.narrative.cards import CARDS
from app.narrative.factpack import FactPack
from app.narrative.lexicon import LEXICON, RELATION_WORDS
from app.narrative.registry import FRACTION, MONEY, NUMERIC_UNITS


def quantise(pack: FactPack) -> dict[str, Any]:
    """fact_id -> the value at render precision. The thing that gets hashed and stored."""
    out: dict[str, Any] = {}
    for fact_id, fact in sorted(pack.facts.items()):
        if fact.value is None:
            out[fact_id] = None
        elif fact.unit == MONEY:
            out[fact_id] = _quantise_money(fact.value, fact.digits)
        elif fact.unit in NUMERIC_UNITS:
            out[fact_id] = _quantise_number(fact.value, fact.unit, fact.digits)
        else:
            out[fact_id] = fact.value
    return out


def _quantise_money(value: Any, digits: int) -> str | None:
    """Decimal all the way. ``round()`` on a float here would reintroduce exactly the
    binary error the string representation exists to avoid."""
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    return str(amount.quantize(Decimal(1).scaleb(-digits)))


def _quantise_number(value: Any, unit: str, digits: int) -> float:
    """``digits`` is decimals of the RENDERED figure, and a fraction is rendered as a
    percentage -- ``pct()`` multiplies by a hundred first. So a volatility shown to two
    decimals as a percentage is significant to four as a fraction, and rounding it to two
    would collapse moves the reader can see."""
    places = digits + 2 if unit == FRACTION else digits
    return round(float(value), places)


def factpack_sha256(quantised: dict[str, Any]) -> str:
    """Stable digest of the quantised pack.

    ``sort_keys`` and a fixed separator make this independent of dict ordering, which is
    not a Python guarantee across processes for anything that round-trips through JSON.
    """
    encoded = json.dumps(quantised, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def definition_pack_sha() -> str:
    """Digest of everything server-owned that the prose is assembled FROM.

    Cards, phrases and directional words are all rendered verbatim into what a reader
    sees. Edit one and the cached output is no longer what this code would produce, with
    nothing in the facts to signal it -- which is precisely the drift a cache key exists
    to catch.
    """
    encoded = json.dumps(
        {
            "cards": {key: list(value) for key, value in sorted(CARDS.items())},
            "phrases": dict(sorted(LEXICON.items())),
            "words": {f"{a}|{b}": word for (a, b), word in sorted(RELATION_WORDS.items())},
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def changed_facts(
    current: dict[str, Any], previous: dict[str, Any]
) -> dict[str, tuple[Any, Any]]:
    """fact_id -> (previous, current), for facts whose RENDERED value moved.

    This is what makes "identify what is changing" a checkable claim rather than an
    impression. Both sides are quantised, so a difference here is a difference a reader
    could have seen; a fact absent from either side is not reported as a change, because
    appearing and moving are different events and conflating them would invent one.
    """
    return {
        fact_id: (previous[fact_id], current[fact_id])
        for fact_id in sorted(set(current) & set(previous))
        if current[fact_id] != previous[fact_id]
    }
