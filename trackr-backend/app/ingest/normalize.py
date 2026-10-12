"""One canonical form for every number a document states.

A broker document prints money in whatever way its template happens to. The LLM is told
to copy what is printed **digit for digit** -- brackets, commas and all -- because the one
thing a language model must never be trusted with is arithmetic. That makes this module
the place where "what was printed" becomes "what it means", deterministically, in Python.

``zerodha_contract_note._decimal`` is ``Decimal(raw.replace(",", "").strip())``. That is
right for its own use, where ``CHARGE_PATTERNS`` has already captured the digits out of
``(7.00)`` and handed over a bare number. It is **not** enough here: a transcribed value
arrives with its notation intact, and ``Decimal("(7.00)")`` raises.

Accepted, and what each means:

===========================  ==============  ====================================
printed                      value           note
===========================  ==============  ====================================
``1,000.00``                 ``1000.00``     Western grouping
``1,00,000.00``              ``100000.00``   Indian lakh grouping -- both work, the
                                             separators are simply removed
``(1,000.00)``               ``-1000.00``    accounting negative. THE ONE
                                             ``_decimal`` cannot do
``-1,000.00``                ``-1000.00``
``Rs. 1,000.00`` / ``₹ 1,000``  ``1000``     currency marker stripped
``1 000.00``                 ``1000.00``     thin/space grouping
===========================  ==============  ====================================

Refused, deliberately:

- ``1.000,00`` -- European notation. An Indian broker does not print this, so seeing it
  means something upstream is wrong, and guessing which of ``1.0`` or ``1000.0`` was meant
  is exactly the silent corruption this whole path exists to prevent.
- ``1.5e3`` -- exponent notation. Same reasoning.
- anything else -- refused rather than coerced.

**Never a float, at any point.** ``Decimal`` is constructed from the cleaned string
directly. A version of this that went via ``float()`` to "handle the formatting" would
lose paise before the value was ever checked, and CLAUDE.md rule 1 exists because that
failure takes days to find.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

#: Currency markers a note may prefix an amount with.
_CURRENCY = re.compile(r"^(?:rs\.?|inr|₹)\s*", re.I)

#: Grouping separators, including the non-breaking and thin spaces a PDF export leaves.
_GROUPING = str.maketrans({",": "", " ": "", " ": "", " ": ""})

#: European decimal notation: a dot used for grouping and a comma for the decimal.
#: Refused rather than guessed at -- see the module docstring.
_EUROPEAN = re.compile(r"^-?\d{1,3}(?:\.\d{3})+,\d+$")

#: What a bare, cleaned number may look like once every marker is gone.
_NUMERIC = re.compile(r"^-?\d+(?:\.\d+)?$")

#: Date formats seen across the real notes and the XLSX exports. Ordered most-specific
#: first; ``%d/%m/%Y`` before ``%m/%d/%Y`` is not a preference but a correctness choice --
#: Indian documents are day-first, and the ambiguous ones would otherwise silently become
#: a different date for the first twelve days of every month.
_DATE_FORMATS = ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d-%b-%Y", "%d %b %Y")

#: A trade cannot predate dematerialisation, and cannot be in the future.
EARLIEST_TRADE = date(1996, 1, 1)


class NotANumberError(ValueError):
    """The printed value is not a number this module will commit to reading.

    A ValueError rather than a silent ``None``: the caller stages a row or rejects it, and
    "I could not read this" must be impossible to mistake for "this was zero".
    """


class NotADateError(ValueError):
    """The printed value is not a date in any format these documents use."""


def normalize_money(raw: str) -> Decimal:
    """A printed amount -> ``Decimal``. See the module docstring for the accepted set.

    Raises:
        NotANumberError: the value is empty, European-notation, exponent-notation, or
            otherwise not something to guess at.
    """
    if raw is None:
        raise NotANumberError("no value")
    text = str(raw).strip()
    if not text:
        raise NotANumberError("empty value")

    text = _CURRENCY.sub("", text).strip()

    # Accounting negative: brackets wrap the whole amount. Checked before grouping is
    # stripped so a stray ")" inside a number cannot be mistaken for one.
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()
        text = _CURRENCY.sub("", text).strip()

    if _EUROPEAN.match(text):
        raise NotANumberError(
            f"{raw!r} uses European decimal notation; refusing to guess between "
            "1.0 and 1000.0"
        )

    cleaned = text.translate(_GROUPING)
    if not _NUMERIC.match(cleaned):
        raise NotANumberError(f"{raw!r} is not a plain decimal number")

    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:  # pragma: no cover - _NUMERIC already guarantees this
        raise NotANumberError(f"{raw!r} is not a decimal") from exc

    if negative and value != 0:
        value = -value
    return value


def normalize_date(raw: str, today: date | None = None) -> date:
    """A printed date -> ``date``, bounded to a range a trade can actually fall in.

    ``today`` is injected rather than read here so a test pins the upper bound instead of
    depending on when it runs.

    Raises:
        NotADateError: unparseable in every known format, or outside
            ``EARLIEST_TRADE..today``.
    """
    if not raw or not str(raw).strip():
        raise NotADateError("no date")
    text = str(raw).strip()

    parsed: date | None = None
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt).date()
            break
        except ValueError:
            continue
    if parsed is None:
        raise NotADateError(f"{raw!r} is not a date in any format these documents use")

    ceiling = today or date.today()
    if parsed < EARLIEST_TRADE:
        raise NotADateError(f"{parsed} predates dematerialisation ({EARLIEST_TRADE})")
    if parsed > ceiling:
        raise NotADateError(f"{parsed} is in the future")
    return parsed
