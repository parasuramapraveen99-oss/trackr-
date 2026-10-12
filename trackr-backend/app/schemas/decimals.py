"""The one strict decimal boundary for PUBLIC financial request bodies.

Responses are the other half of this rule and live in ``app/database.py`` (``money``,
``rate``): a Decimal leaves as an exact string. This is ingress, and it exists because
Pydantic is lenient in exactly the direction that loses paise.

WHAT THE LENIENCY COSTS. ``quantity: Decimal`` accepts a JSON number, and by the time
Pydantic sees it the number has already been through ``json.loads`` -- it is a Python
float. ``Decimal(0.1)`` is
``0.1000000000000000055511151231257827021181583404541015625``, so a client that posted
``{"price": 0.1}`` would get that written into an APPEND-ONLY ledger, where CLAUDE.md
says nothing can ever edit or delete it. The value is wrong at rest, forever, and
nothing downstream can tell it apart from a price that was genuinely typed with 55
digits. Refusing the number is the only repair available, and it has to happen before
the request reaches a repository.

WHAT IS STILL ACCEPTED, and deliberately:

  A decimal STRING under ``schema/transaction.schema.json#/$defs/decimal`` --
  ``^-?[0-9]+(\\.[0-9]+)?$`` -- optionally padded with whitespace, which a form field
  produces and which cannot change a value.

  A ``Decimal`` OBJECT, so that internal construction keeps working. Tests, the
  corporate-action matcher and the ingestion parsers all build these DTOs in Python from
  Decimals they already hold; forcing them through ``str()`` first would be a round trip
  with no gain, and this validator is about what a REMOTE caller may send.

  ``None``, wherever the field's own declaration already permits it (``tds_amount``,
  which means "tax withheld unknown" and is not the same claim as zero).

WHAT IS NOT: a JSON number of any kind, ZERO INCLUDED -- ``0`` and ``0.0`` are refused
alongside ``0.1``, because accepting the ones that happen to be exactly representable
would make the contract depend on the value rather than the type, and a client would
discover the rule only when a real price broke. Also refused: booleans (``True`` is an
``int`` in Python and would otherwise arrive as quantity 1), arrays, objects, empty and
blank text, ``"NaN"``/``"Infinity"``, exponent notation, hexadecimal, thousands
separators, currency symbols and a leading ``+``.

NO EXISTING VALIDATOR MOVES. Each field keeps the ``ge``/``le`` bound it already had and
gains none: ``quantity`` stays ``0 <= q <= 100000000``, ``price`` stays ``>= 0``, and
``charges``/``stt``/``dividend_amount``/``cost_transfer`` stay unbounded because they
always were. The two exported grammars mirror that -- ``NON_NEGATIVE`` simply omits the
sign for the fields whose contract already refused one, so the PUBLISHED schema says what
the runtime enforces instead of advertising a number it will reject.

SCOPE IS THE HTTP SURFACE ONLY. This is not imported by ``app/engine/`` or
``app/ingest/``: a parser reads a broker's file, not JSON, and its Decimals are internal
by the time the engine sees them. Putting a string requirement on the engine's own models
would be enforcing a transport rule on code that has no transport.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from pydantic import BeforeValidator, WithJsonSchema

#: ``schema/transaction.schema.json#/$defs/decimal``, verbatim. Kept as one compiled
#: pattern rather than a ``Field(pattern=...)`` per field so the grammar has a single
#: definition and cannot drift between the twelve fields that use it.
DECIMAL_PATTERN = r"^-?[0-9]+(\.[0-9]+)?$"

#: The same grammar with the sign removed, for fields whose existing contract is already
#: ``ge=0``. A pattern can express "not negative" where JSON Schema's ``minimum`` cannot
#: -- ``minimum`` applies to numbers, and these are strings.
NON_NEGATIVE_DECIMAL_PATTERN = r"^[0-9]+(\.[0-9]+)?$"

_DECIMAL_TEXT = re.compile(DECIMAL_PATTERN)

_WHY = (
    'must be an exact decimal string such as "100.25" -- a JSON number is an IEEE '
    "double and loses paise (see app/schemas/decimals.py)"
)

_DOC = (
    "An exact decimal carried as a string. A JSON number is REFUSED, zero included: "
    "once parsed it is an IEEE double, and this ledger is append-only."
)


def require_decimal_text(value: Any) -> Any:
    """Accept decimal text or an internal Decimal; refuse everything else.

    Returns the value for Pydantic's own Decimal validation to finish, so the field's
    ``ge``/``le`` constraints and any model validator still run exactly as before -- this
    narrows what may arrive, it does not replace a single existing rule.
    """
    if value is None:
        # Nullability is the FIELD's decision, declared in its own annotation. A field
        # that is not `| None` will reject this two lines later, in core validation.
        return None

    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("must be a finite decimal, not NaN or an infinity")
        return value

    if isinstance(value, bool) or not isinstance(value, str):
        # bool first: it is an int subclass, so `True` would pass an int check and land
        # in the ledger as the quantity 1.
        raise ValueError(_WHY)

    text = value.strip()
    if not _DECIMAL_TEXT.fullmatch(text):
        # Catches "", "  ", "1e5", "0x10", "1,000", "Rs 100", "+1", ".5", "1.", "NaN"
        # and "Infinity" in one place. Decimal() would happily parse four of those.
        raise ValueError(_WHY)

    try:
        parsed = Decimal(text)
    except InvalidOperation:  # pragma: no cover -- unreachable given the pattern above
        raise ValueError(_WHY) from None

    if not parsed.is_finite():  # pragma: no cover -- the pattern cannot produce one
        raise ValueError("must be a finite decimal, not NaN or an infinity")
    return parsed


def _published(pattern: str, *, nullable: bool) -> WithJsonSchema:
    """What the GENERATED OpenAPI says about the field.

    Needed because ``BeforeValidator`` changes behaviour without changing the declared
    schema: FastAPI would otherwise publish ``anyOf: [number, string]`` for a field the
    server refuses numbers on. A contract that advertises an accepted type and then
    rejects it is worse than either choice on its own, so the published schema is pinned
    to the string grammar alone -- and never widened back to ``string | number``.
    """
    return WithJsonSchema(
        {
            "type": ["string", "null"] if nullable else "string",
            "pattern": pattern,
            "description": _DOC,
        }
    )


#: A required financial decimal on a public request body, sign allowed.
DecimalText = Annotated[
    Decimal, BeforeValidator(require_decimal_text), _published(DECIMAL_PATTERN, nullable=False)
]

#: The same for a field whose existing contract is ``ge=0``.
NonNegativeDecimalText = Annotated[
    Decimal,
    BeforeValidator(require_decimal_text),
    _published(NON_NEGATIVE_DECIMAL_PATTERN, nullable=False),
]

#: Signed, where the existing contract already allows null and null means UNKNOWN.
OptionalDecimalText = Annotated[
    Decimal | None,
    BeforeValidator(require_decimal_text),
    _published(DECIMAL_PATTERN, nullable=True),
]
