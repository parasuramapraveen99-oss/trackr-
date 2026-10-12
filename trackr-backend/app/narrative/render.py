"""A verified claim -> an ordered token list. Never a finished string.

TWO REASONS THE SERVER DOES NOT FORMAT, and both are load-bearing.

First, the formatters are TypeScript. ``trackr-frontend/app/analytics/format.ts`` owns
Indian lakh grouping and the FRACTION-versus-ALREADY-A-PERCENTAGE distinction whose own
docstring says "guessing is how a screen ends up showing 3000% volatility". Rendering
here would be a second implementation of exactly the thing this codebase is most careful
about, and the two would drift the way the effective-holdings formulas nearly did.

Second, privacy mode. Every rupee figure on the analytics page is wrapped in ``<Figure>``
from ``lib/privacy-context``. A finished server-rendered sentence cannot be masked, so a
generated paragraph would leak the numbers the mask exists to hide.

A token list solves both: the client renders each ``fact`` token through the formatter
its unit names, and wraps the money ones in ``<Figure>``. It also gives a third property
for free -- the client physically cannot render a number the server did not send, so the
"no invented numbers" guarantee survives the trip through the browser.
"""

from __future__ import annotations

from typing import Any

from app.narrative.cards import card
from app.narrative.claims import Claim
from app.narrative.factpack import FactPack
from app.narrative.lexicon import RELATION_WORDS, phrase


class RenderError(Exception):
    """A verified claim that still cannot be rendered. Should be unreachable -- verify()
    resolves every id first -- so it means the two have drifted apart."""


def render(claim: Claim, pack: FactPack) -> list[dict[str, Any]]:
    """One claim -> tokens. Assumes ``claim`` came back from ``verify``."""
    tokens: list[dict[str, Any]] = []
    for segment in claim.segments:
        if segment.t == "text":
            tokens.append({"t": "text", "v": segment.v})
        elif segment.t == "lex":
            # Rendered as text: the phrase is a literal, and the client has no reason to
            # know it came from a lexicon rather than from prose.
            tokens.append({"t": "text", "v": phrase(segment.id or "")})
        elif segment.t == "rel":
            tokens.append({"t": "text", "v": _relation_word(claim)})
        elif segment.t == "fact":
            fact = pack.get(segment.id or "")
            if fact is None:
                raise RenderError(f"verified claim points at a missing fact: {segment.id!r}")
            tokens.append(
                {
                    "t": "fact",
                    "id": fact.id,
                    "value": fact.value,
                    "unit": fact.unit,
                    "digits": fact.digits,
                }
            )
        else:
            raise RenderError(f"unknown segment kind survived verification: {segment.t!r}")
    return tokens


def _relation_word(claim: Claim) -> str:
    """The directional word, from the EVALUATOR's outcome rather than the model's choice.

    This is the mechanism that makes "a negative described as 'more'" impossible instead
    of merely checked for -- the same defect ``panels.tsx`` guards by hand today.
    """
    key = (claim.relation, claim.outcome or "true")
    word = RELATION_WORDS.get(key)
    if word is None:
        raise RenderError(
            f"no directional word for {key}; add it to lexicon.RELATION_WORDS rather "
            "than letting the model supply one"
        )
    return word


def render_all(claims: list[Claim], pack: FactPack) -> list[dict[str, Any]]:
    """Verified claims -> the block the API returns.

    Each statement carries the fact ids behind it so the "how this was made" disclosure
    is built from the claim's own declared evidence rather than reconstructed.
    """
    out: list[dict[str, Any]] = []
    for claim in claims:
        statement: dict[str, Any] = {
            "relation": claim.relation,
            "tokens": render(claim, pack),
            "cites": list(claim.cites),
        }
        if claim.card:
            title, body = card(claim.card)
            statement["card"] = {"id": claim.card, "title": title, "body": body}
        out.append(statement)
    return out
