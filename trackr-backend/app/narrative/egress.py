"""The only thing in this codebase that builds a payload for the analysis model.

AN INCLUDE-LIST, NEVER A FLATTEN-MINUS-DENY. Every field that leaves is one the registry
declared a unit for, reached through a fact the selector allowed. The alternative --
serialise the analytics payload and strip what should not go -- fails the day a new
field is added, because the new field is included by default and nobody has to decide
anything. Here a new field is excluded by default and someone has to name it.

This is NOT ``app/ingest/redaction.py``, and the difference is worth stating because the
resemblance is misleading. That module reads an open-world document where, as its own
docstring argues, a deny-list cannot work because names have no shape. This one
serialises a structure we built ourselves, so the control is type closure, not pattern
matching. Its post-hoc assertion is kept in the same spirit though: a separate pass, so
that a reordered pipeline fails instead of transmitting.

WHAT CROSSES. Per the decision recorded in the plan for this work, the full pack goes --
security names, ISINs, quantities and rupee amounts included. That is a change of kind
from every other outbound model call in this codebase: ``app/ai/llm.py`` sends redacted
document text, and the removed TypeSafe caller sent a public NSE circular. This one sends
an investor's own positions. It is therefore gated on ``insight_generation_enabled``,
which is a permission and not a capability -- see ``app/config.py`` on why that is a
separate setting from the key, and ``docs/COMPLIANCE.md`` for the processor gate it
depends on.

MONEY STAYS A STRING all the way out. A JSON number is an IEEE double by the time
anything parses it, which is the same argument ``app/database.py::money`` makes for the
browser and it does not stop being true because the reader is a model.
"""

from __future__ import annotations

from typing import Any

from app.narrative.factpack import FactPack
from app.narrative.registry import MONEY, NUMERIC_UNITS

#: Serialised per fact. Deliberately short: every extra key is tokens on every call, and
#: a field the model cannot act on is a field that only invites it to quote something.
#: `digits` is included because the model is told to describe magnitudes it can see
#: rendered, not to invent a precision.
_FACT_KEYS = ("unit", "digits", "value")


class EgressViolation(Exception):
    """A fact reached the serialiser that the registry does not describe.

    FAIL CLOSED AND SEND NOTHING. Partial transmission is the worst outcome: the call
    succeeds, the model answers, and nobody learns that a field went out unlabelled.
    """


def to_wire(pack: FactPack, *, surface: str) -> dict[str, Any]:
    """One selected pack -> the JSON body the model is given.

    ``pack`` must already be narrowed by ``select.select``; this function does not
    filter, it only serialises what it is handed. Keeping those separate is what makes
    "which facts may this surface cite" reviewable in one place.
    """
    facts: dict[str, Any] = {}
    for fact_id, fact in sorted(pack.facts.items()):
        if fact.unit not in NUMERIC_UNITS and fact.unit not in ("money", "enum", "label"):
            raise EgressViolation(
                f"{fact_id!r} has unit {fact.unit!r}, which egress does not know how to "
                "serialise; nothing was sent"
            )
        if fact.unit == MONEY and fact.value is not None and not isinstance(fact.value, str):
            raise EgressViolation(
                f"{fact_id!r} is money but is a {type(fact.value).__name__}; money "
                "crosses as a decimal string or not at all"
            )
        entry = {key: getattr(fact, key) for key in _FACT_KEYS}
        if fact.threshold is not None:
            entry["threshold"] = fact.threshold
        if fact.coverage_pct is not None:
            entry["coverage_pct"] = fact.coverage_pct
        facts[fact_id] = entry

    body = {
        "surface": surface,
        "as_of": pack.as_of,
        "benchmark": pack.benchmark,
        "facts": facts,
        # Sets are sent so the model can NAME one in a RANK_EXTREME claim. It is never
        # sent the ordering: working out which member is weakest is the verifier's job,
        # and a model that had been told the answer could not be checked against it.
        "sets": {
            set_id: {"members": list(factset.members), "metric": factset.metric}
            for set_id, factset in sorted(pack.sets.items())
        },
    }
    _assert_serialisable(body)
    return body


def _assert_serialisable(body: dict[str, Any]) -> None:
    """The separate second pass. See the module docstring on why it is not folded in."""
    import json

    try:
        json.dumps(body, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise EgressViolation(
            f"the egress body is not JSON-serialisable ({exc}); nothing was sent"
        ) from None
