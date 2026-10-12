"""Proving, or refusing, what the model said.

The model transcribes. This module decides. Nothing here calls a network, constructs a
prompt, or asks anything of anyone -- it takes the JSON the model produced plus the exact
text that was sent, and returns rows that survived or rejections that say why.

Why confidence is computed here and not asked for
-------------------------------------------------
The obvious design -- ask the model how sure it is, or read token log-probabilities -- is
measurably poor. On DocILE, a 55-field document-extraction benchmark, log-probabilities
score 0.705 ROC AUC, a model's own stated confidence 0.692, and self-consistency across
five samples 0.744. The reason is the failure mode we care most about: **a model
confidently transcribing noise produces high log-probabilities for a wrong answer.**

Broker documents give us something better than any of those, because they are internally
redundant. A trade line states quantity and price; charges are itemised and totalled. So
correctness here is partly *checkable* rather than merely *estimable*, and confidence is
the answer to "how much of this did we manage to prove", never "how sure does it seem".

Two independent traps, and a wrong value must defeat both:

- **Grounding (G)** -- every trade cites its source line, and that line must occur in the
  text we sent; then the ISIN, quantity and price must each occur *within that line*.
  A hallucinated trade cannot cite a line from a document it never saw. Line-scoping is
  what makes this stronger than a whole-document substring search: on a dense note the
  same ``0.05`` appears a dozen times, and "it is in there somewhere" proves nothing.
- **Arithmetic (A)** -- a transcription slip that survives grounding still has to survive
  the sums.

**Grounding catches fabrication; only arithmetic catches omission.** An invented trade
cannot cite a real line, so G1 stops it dead. A trade the model silently *dropped* leaves
every remaining row grounding perfectly -- there is no wrong citation to find. The only
evidence that the set is complete is a stated total that reconciles, and that is exactly
why its presence is what separates CONFIDENCE_DERIVED from CONFIDENCE_UNCERTAIN below.
A document that states no total yields rows that are transcribed, not proved.

Failure scope, and one deliberate asymmetry
-------------------------------------------
``base.py`` is explicit that one bad row must not cost the others: "one malformed line in
a 115-row export must not cost the other 114." That holds for type and value failures.

**Grounding is the exception: one ungrounded row rejects the whole document.**
Hallucination is not independent across rows. A model that fabricated one row on this
layout has probably fabricated others in ways the type checks happened not to catch, so
rejecting only the row we detected would keep the ones we did not. The document is still
stored, and a human still has the PDF -- that is what staging is for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from app.ingest.base import CONFIDENCE_DERIVED, CONFIDENCE_UNCERTAIN, Rejection, StagedRow
from app.ingest.normalize import (
    NotADateError,
    NotANumberError,
    normalize_date,
    normalize_money,
)
from app.ingest.zerodha_contract_note import ISIN_PATTERN, SIDES, apportion_charges

#: Above this relative gap between the trades' turnover and any stated note total, the
#: document is refused rather than flagged. A gap this size is not rounding -- it is a
#: misread column, a moved decimal point, or quantity and price swapped.
TURNOVER_TOLERANCE = Decimal("0.05")

#: A DOCUMENT TOTAL IS NOT A CHARGE, and a model will happily report one as the other.
#:
#: Found on a real note: gpt-oss-20b returned "Pay in/Pay out obligation (Rs) (29738.40)"
#: as the first entry in ``charges``. It is a perfectly reasonable reading of a line that
#: sits in the charges block -- and summing it produced 29772.40 of charges on a 29738.40
#: trade, an 850x overstatement that would have gone straight onto the cost basis.
#:
#: Grounding does not catch this: 29738.40 really is printed on the document. Only knowing
#: what the line MEANS catches it, which is why this is a label rule.
OBLIGATION_LABEL = re.compile(
    r"pay\s*[-/]?\s*in|pay\s*[-/]?\s*out|obligation|net\s+(?:amount|total|payable|receivable)"
    r"|grand\s+total|^total\b",
    re.I,
)

#: The backstop for the same failure, independent of vocabulary.
#:
#: Indian equity charges are a fraction of a percent of turnover -- STT is 0.1%, brokerage
#: is 0.03% or Rs20 capped, plus GST, SEBI fees and stamp duty. Anything approaching this
#: bound is not a charge schedule; it is a total that was misread as one. Set loose enough
#: that a tiny trade with a flat Rs20 brokerage still passes.
MAX_CHARGE_RATIO = Decimal("0.10")

#: Whitespace runs collapse before any grounding comparison: pdfplumber's column spacing
#: is not stable enough to compare literally, and a model re-emitting a line will not
#: reproduce it space for space.
_WHITESPACE = re.compile(r"\s+")


def _flat(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip().casefold()


@dataclass(frozen=True, slots=True)
class VerifiedExtraction:
    """What survived, what did not, and how much could be proved."""

    rows: list[StagedRow] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _reject_document(reason: str) -> VerifiedExtraction:
    """A document-level refusal: no rows, one rejection at row 0.

    Same shape ``zerodha_contract_note.parse_text`` uses for "not a contract note" and
    "no trade date on the note", so a reviewer reads one consistent thing regardless of
    which parser refused.
    """
    return VerifiedExtraction(rejections=[Rejection(0, reason)])


def verify(
    payload: Any,
    sent_text: str,
    account_id: str,
    today: date | None = None,
) -> VerifiedExtraction:
    """The model's JSON + the text it was given -> staged rows, or reasons why not.

    ``sent_text`` is the grounding oracle and must be exactly what was transmitted --
    ``RedactedDocument.text``, not the original document. Comparing against the original
    would "prove" a citation the model could not have seen, which is the one thing this
    function exists to make impossible.

    ``today`` is injected so the future-date bound is pinned by a test rather than by when
    the suite happens to run.
    """
    # --- S: the envelope. A malformed envelope is not a malformed row. ---------------
    if not isinstance(payload, dict):
        return _reject_document("the model did not return a JSON object")
    if payload.get("document_type") == "other":
        return _reject_document("the model did not recognise this as a contract note")
    if payload.get("document_type") != "contract_note":
        return _reject_document(
            f"unexpected document_type {payload.get('document_type')!r}"
        )

    trades = payload.get("trades")
    charges = payload.get("charges", [])
    if not isinstance(trades, list) or not isinstance(charges, list):
        return _reject_document("the model's trades/charges were not lists")
    if not trades:
        return _reject_document("the model found no trades in this document")

    try:
        txn_date = normalize_date(payload.get("trade_date"), today=today)
    except NotADateError as exc:
        return _reject_document(f"no usable trade date on the document: {exc}")

    haystack = _flat(sent_text)

    # --- T + G: per row. -------------------------------------------------------------
    candidates: list[dict[str, Any]] = []
    rejections: list[Rejection] = []
    seen_trade_numbers: set[str] = set()

    for index, trade in enumerate(trades, start=1):
        try:
            candidate = _verify_trade(trade, index, txn_date, haystack, seen_trade_numbers)
        except _Ungrounded as exc:
            # THE POISON PILL. See the module docstring: hallucination is not independent
            # across rows, so a citation we could not find rejects everything.
            return _reject_document(
                f"row {index} could not be verified against the document ({exc}); "
                "no rows from this document were staged"
            )
        except _RowRejected as exc:
            rejections.append(Rejection(index, str(exc), raw={"line": _line_of(trade)}))
            continue
        candidates.append(candidate)

    if not candidates:
        return VerifiedExtraction(
            rejections=rejections or [Rejection(0, "no rows survived verification")]
        )

    # --- A: arithmetic. --------------------------------------------------------------
    notes: list[str] = []
    total_charges, total_stt, charge_notes = _verify_charges(charges, haystack)
    notes.extend(charge_notes)

    turnover = sum((c["quantity"] * c["price"] for c in candidates), Decimal("0"))

    # The vocabulary-independent half of the OBLIGATION_LABEL defence. A label this rule
    # does not recognise -- a broker wording nobody has seen, another language -- still
    # cannot inflate the cost basis, because the arithmetic is impossible.
    if turnover > 0 and total_charges > turnover * MAX_CHARGE_RATIO:
        return _reject_document(
            f"charges of {total_charges} against turnover of {turnover} exceed "
            f"{MAX_CHARGE_RATIO:%} -- that is a document total misread as a charge, not a "
            "charge schedule"
        )

    verified_turnover = _check_turnover(payload, turnover)
    if verified_turnover is not None and not verified_turnover:
        return _reject_document(
            "the trades do not reconcile with the total stated on the document -- a gap "
            f"larger than {TURNOVER_TOLERANCE:%} is a misread column, not rounding"
        )

    # --- confidence: how much did we prove, never how sure it seems. ------------------
    # DERIVED requires evidence of COMPLETENESS, not merely of each row's identity.
    # ``verified_turnover`` is True only when the document stated a total AND the trades
    # reconcile to it; None means there was nothing to check against, and a trade the
    # model dropped would leave no trace at all. Grounding cannot see an omission.
    fully_reconciled = verified_turnover is True
    confidence = CONFIDENCE_DERIVED if fully_reconciled else CONFIDENCE_UNCERTAIN
    if not fully_reconciled:
        notes.append(
            "every row was verified line by line against the document, but the document "
            "stated no total to reconcile against -- so a trade the model omitted would "
            "not be detectable. These rows are transcribed, not proved, and need a human "
            "before promotion"
        )

    rows = apportion_charges(
        candidates,
        total_charges,
        total_stt,
        account_id,
        payload.get("contract_number"),
        confidence=confidence,
    )

    # A1: apportionment closure, asserted rather than assumed. pro_rata_share's
    # last-row-absorbs-the-remainder already guarantees this; a violation would mean the
    # charge total silently changed on the way into the rows.
    apportioned = sum((r.transaction.charges for r in rows), Decimal("0"))
    if apportioned != total_charges:
        return _reject_document(
            f"apportioned charges {apportioned} do not sum to the note total "
            f"{total_charges}"
        )

    notes.insert(
        0,
        f"transcribed by a language model and verified against the document: "
        f"{len(rows)} row(s) grounded, {len(rejections)} rejected",
    )
    return VerifiedExtraction(rows=rows, rejections=rejections, notes=notes)


class _RowRejected(Exception):
    """This row is unusable; the others may still be fine."""


class _Ungrounded(Exception):
    """This row's citation could not be found. Poisons the document."""


def _line_of(trade: Any) -> str:
    return str(trade.get("line", ""))[:200] if isinstance(trade, dict) else ""


def _require_str(trade: dict[str, Any], key: str) -> str:
    """T1: values arrive as strings or not at all.

    A JSON number is a **prompt-contract violation**, and rejecting it is what makes drift
    visible. ``app.ai.llm`` already parses floats as Decimal so no precision was lost --
    but silently accepting the wrong type means nobody ever learns the model stopped
    following instructions, and the next drift may not be so harmless.
    """
    value = trade.get(key)
    if value is None:
        raise _RowRejected(f"{key} is missing")
    if not isinstance(value, str):
        raise _RowRejected(
            f"{key} came back as {type(value).__name__}, not the string that was asked "
            "for -- the model stopped following the schema"
        )
    return value


def _verify_trade(
    trade: Any,
    index: int,
    txn_date: date,
    haystack: str,
    seen_trade_numbers: set[str],
) -> dict[str, Any]:
    if not isinstance(trade, dict):
        raise _RowRejected("not an object")

    # G1: the citation. Checked first -- everything after it is only meaningful if this
    # row corresponds to something actually printed.
    line = trade.get("line")
    if not isinstance(line, str) or not line.strip():
        raise _Ungrounded("no source line cited")
    flat_line = _flat(line)
    if flat_line not in haystack:
        raise _Ungrounded("the cited line does not appear in the document")

    isin = _require_str(trade, "isin").strip().upper()
    quantity_raw = _require_str(trade, "quantity")
    price_raw = _require_str(trade, "price")
    side = _require_str(trade, "side").strip().upper()

    # G2: the values must be in THAT line, not merely somewhere in the document. This is
    # what catches transcription drift -- a model reading "4,000" as "400" is proposing a
    # number that is plausible, present elsewhere, and not on this row.
    for label, raw in (("isin", isin), ("quantity", quantity_raw), ("price", price_raw)):
        if _flat(raw) not in flat_line:
            raise _RowRejected(
                f"{label} {raw!r} does not appear on the line it was read from"
            )

    # T3: reuses the pattern that already excludes INZ000031633, the broker's SEBI
    # registration, which a naive IN[A-Z0-9]{10} reads as a security.
    if not ISIN_PATTERN.match(isin):
        raise _RowRejected(f"{isin!r} is not an equity or fund ISIN")
    if side not in SIDES:
        raise _RowRejected(f"side {side!r} is neither B nor S")

    try:
        quantity = normalize_money(quantity_raw)
        price = normalize_money(price_raw)
    except NotANumberError as exc:
        raise _RowRejected(str(exc)) from exc

    if quantity <= 0:
        raise _RowRejected(f"quantity {quantity} is not positive")
    if price < 0:
        raise _RowRejected(f"price {price} is negative")

    trade_number = trade.get("trade_number")
    if isinstance(trade_number, str) and trade_number.strip():
        # G3: a repeated trade number is the same trade counted twice.
        if trade_number in seen_trade_numbers:
            raise _RowRejected(f"trade number {trade_number!r} appears more than once")
        seen_trade_numbers.add(trade_number)

    return {
        "row_number": index,
        "isin": isin,
        "symbol": trade.get("symbol") or "",
        "txn_type": SIDES[side],
        "quantity": quantity,
        "price": price,
        "txn_date": txn_date,
        "trade_id": trade_number,
        "order_id": trade.get("order_number"),
        "exchange": trade.get("exchange") or "",
        # Field-level provenance, carried into import_row.payload["raw"] by
        # apportion_charges. import_row.payload is already a free-form JSON column, so
        # this costs no migration and lets a staged row answer "where did this number
        # come from, and what proved it?" long after the fact.
        "evidence": {
            "extracted_by": "llm",
            "line": line,
            "quotes": {"isin": isin, "quantity": quantity_raw, "price": price_raw},
            "verified": ["G1", "G2", "T1", "T2", "T3", "T4", "T5"],
        },
    }


def _verify_charges(
    charges: list[Any], haystack: str
) -> tuple[Decimal, Decimal, list[str]]:
    """Itemised charges -> (total, stt, notes).

    A charge that will not parse is dropped with a note rather than rejecting the
    document. Charges are note-level and additive: missing one understates cost basis,
    which is bad, while discarding every trade because a footer would not parse is worse.
    The note is what stops it being silent.
    """
    total = Decimal("0")
    stt = Decimal("0")
    notes: list[str] = []

    for entry in charges:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or "").strip()
        if OBLIGATION_LABEL.search(label):
            # A document total, not a charge -- see OBLIGATION_LABEL. Skipped silently
            # rather than noted: the model reporting it here is expected behaviour, not a
            # defect, and a note on every single document would train people to ignore
            # notes.
            continue
        amount_raw = entry.get("amount")
        if not isinstance(amount_raw, str):
            notes.append(f"charge {label!r} was not a string amount and was ignored")
            continue
        if _flat(amount_raw) not in haystack:
            notes.append(f"charge {label!r} does not appear in the document and was ignored")
            continue
        try:
            amount = normalize_money(amount_raw)
        except NotANumberError:
            notes.append(f"charge {label!r} ({amount_raw!r}) could not be read and was ignored")
            continue

        # Charges are printed as bracketed negatives; the ledger carries them positive.
        amount = abs(amount)
        total += amount
        if re.search(r"securities\s+transaction|(?<!\w)stt(?!\w)", label, re.I):
            stt += amount

    if not charges:
        notes.append(
            "NO CHARGE LINES FOUND. Rows carry charges of 0, which is absence of data "
            "rather than evidence the trades were free."
        )
    return total, stt, notes


def _check_turnover(payload: dict[str, Any], turnover: Decimal) -> bool | None:
    """A3, the weakest check here, and labelled as such.

    A note's "net amount receivable/payable" is net of charges, so it is definitionally
    NOT the sum of quantity x price, and any tolerance is therefore unprincipled. Returns
    None when the document stated nothing to compare against -- which is common, and is
    why it lowers confidence rather than failing.
    """
    stated = payload.get("stated_total")
    if not isinstance(stated, str) or not stated.strip():
        return None
    try:
        value = abs(normalize_money(stated))
    except NotANumberError:
        return None
    if value == 0 or turnover == 0:
        return None
    gap = abs(value - turnover) / max(value, turnover)
    return gap <= TURNOVER_TOLERANCE
