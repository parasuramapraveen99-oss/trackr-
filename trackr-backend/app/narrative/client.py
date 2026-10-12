"""The ONLY file in this package permitted to reach a model. Enforced by ``make lint``.

Everything else under ``app/narrative/`` is pure: it can be run, and is run, entirely
offline. Concentrating the network call in one small module is the same split
``app/ingest/llm_extract.py`` and ``app/ingest/llm_verify.py`` already use, and it is
what lets the verifier be trusted -- a module that cannot call anything cannot be
influenced by what a provider returned.

THIS MODULE BELIEVES NOTHING. It parses the envelope, coerces each statement into a
``Claim``, and hands them to ``verify``. A malformed statement is dropped here with a
recorded reason rather than raising, for the same reason ``app/ingest/base.py`` gives:
one bad row must not cost the others. The difference from the ingest path is that
nothing here reaches a ledger, so the failure budget is prose, not money.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.ai.llm import LlmClient, LlmResponseError, LlmUnavailable
from app.narrative.claims import Claim, Segment
from app.narrative.egress import to_wire
from app.narrative.factpack import FactPack
from app.narrative.prompt import SYSTEM_PROMPT, build_user_content

#: A block is two to four statements; more than this is a model ignoring the brief, and
#: parsing an unbounded list would let one response cost an unbounded amount of work.
_MAX_STATEMENTS = 8

#: Longest a single text segment may be. Connective prose between facts is a clause, not
#: a paragraph -- and a long unverifiable passage is where an unsupported assertion hides.
_MAX_SEGMENT_CHARS = 240


class NarrativeUnavailable(Exception):
    """The model could not be reached, or answered in an unusable shape.

    One exception rather than two, unlike ``LlmUnavailable``/``LlmResponseError``,
    because the caller's response is identical either way: render the page without a
    narrative. The distinction still matters for logging and is preserved in the message.
    """


@dataclass(frozen=True, slots=True)
class ParseOutcome:
    claims: list[Claim] = field(default_factory=list)
    #: Statements that never reached the verifier. Counted alongside V1-V7 rejections so
    #: "the model returned rubbish" and "the model was wrong" stay distinguishable.
    dropped: list[str] = field(default_factory=list)


def generate_claims(
    client: LlmClient, pack: FactPack, surface: str
) -> ParseOutcome:
    """One selected pack -> candidate claims. Nothing here is checked; that is `verify`."""
    body = to_wire(pack, surface=surface)
    try:
        payload = client.complete_json(SYSTEM_PROMPT, build_user_content(body))
    except LlmUnavailable as exc:
        raise NarrativeUnavailable(f"transport: {exc}") from None
    except LlmResponseError as exc:
        raise NarrativeUnavailable(f"response shape: {exc}") from None
    return parse_claims(payload)


def parse_claims(payload: Any) -> ParseOutcome:
    """The model's JSON -> Claim objects. Offline and separately testable on purpose."""
    if not isinstance(payload, dict):
        raise NarrativeUnavailable("the model did not return a JSON object")
    statements = payload.get("statements")
    if not isinstance(statements, list):
        raise NarrativeUnavailable("the model returned no 'statements' list")

    claims: list[Claim] = []
    dropped: list[str] = []

    for index, statement in enumerate(statements[:_MAX_STATEMENTS]):
        try:
            claims.append(_one(statement))
        except ValueError as exc:
            dropped.append(f"statement {index}: {exc}")

    if len(statements) > _MAX_STATEMENTS:
        dropped.append(
            f"{len(statements) - _MAX_STATEMENTS} statement(s) beyond the "
            f"{_MAX_STATEMENTS} cap were not read"
        )
    return ParseOutcome(claims=claims, dropped=dropped)


def _one(statement: Any) -> Claim:
    if not isinstance(statement, dict):
        raise ValueError("not an object")

    relation = statement.get("relation")
    if not isinstance(relation, str) or not relation:
        raise ValueError("no relation")

    raw_segments = statement.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ValueError("no segments")

    segments: list[Segment] = []
    for raw in raw_segments:
        if not isinstance(raw, dict):
            raise ValueError("a segment is not an object")
        kind = raw.get("t")
        if kind == "text":
            text = raw.get("v")
            if not isinstance(text, str):
                raise ValueError("a text segment carries no string")
            if len(text) > _MAX_SEGMENT_CHARS:
                raise ValueError(f"a text segment is longer than {_MAX_SEGMENT_CHARS} chars")
            segments.append(Segment("text", v=text))
        elif kind in ("fact", "lex"):
            ident = raw.get("id")
            if not isinstance(ident, str) or not ident:
                raise ValueError(f"a {kind} segment carries no id")
            segments.append(Segment(kind, id=ident))
        elif kind == "rel":
            segments.append(Segment("rel"))
        else:
            # Unknown kinds are refused HERE rather than left for V1, so the rejection
            # reason names the parse rather than the verification.
            raise ValueError(f"unknown segment kind {kind!r}")

    cites = statement.get("cites") or []
    if not isinstance(cites, list) or not all(isinstance(c, str) for c in cites):
        raise ValueError("cites is not a list of fact ids")

    args = statement.get("args") or {}
    if not isinstance(args, dict):
        raise ValueError("args is not an object")

    card = statement.get("card")
    if card is not None and not isinstance(card, str):
        raise ValueError("card is not an id")

    return Claim(
        relation=relation,
        args=args,
        segments=tuple(segments),
        cites=tuple(dict.fromkeys(cites)),
        card=card,
    )
