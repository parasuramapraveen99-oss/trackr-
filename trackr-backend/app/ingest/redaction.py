"""What may leave this process, and what may never.

``app/ingest/llm_extract.py`` sends document text to OpenRouter. A broker contract note carries
the client's name, address, PAN, UCC, phone, email and demat number alongside the trades,
and none of that is needed to read a trade table. This module is the only thing standing
between the two facts.

Three passes, in order, and the order is the design:

1. **allow-list** -- keep only lines that look like a trade row or a charge line. An
   unrecognised line is not sent.
2. **deny-list** -- mask anything PII-shaped among the survivors, because pdfplumber wraps
   columns and a header fragment can ride along on a trade line.
3. **fail-closed assertion** -- if anything PII-shaped is still there, raise and send
   NOTHING. A redaction bug must cost an import, never a disclosure.

Allow-list first, and it is not close
-------------------------------------
A deny-list is a closed-world assumption about open-world input. A PAN, a UCC, an email,
a phone number and a GSTIN all have shapes you can write down. **A name and an address do
not**: ``P. Muthukumar, 14/3 Ganapathy Nagar, Velachery, Chennai 600042`` has no
distinguishing form. So a deny-list alone is guaranteed to leak precisely the field that
is hardest to match and most identifying, and to leak it silently, forever.

The allow-list inverts the failure mode. Its cost is recall -- a trade row in a layout
nobody anticipated gets dropped -- and that failure is **visible**: zero rows extracted,
and a human opens the PDF. The deny-list failure is a correct-looking import and an
irreversible disclosure. Between a visible miss and an invisible leak, take the miss.

The allow-list is deliberately STRUCTURAL, not layout-specific ("this line carries an
ISIN-shaped token"), where ``zerodha_contract_note.TRADE_ROW`` demands nine fields in a
fixed order. That is the point: this has to keep working on the layouts TRADE_ROW cannot
read, which is the entire reason the LLM path exists.

Two inversions that look like bugs and are not
----------------------------------------------
**1. This uses the BARE PAN sweep, where identity verification uses the labelled one.**
``zerodha_contract_note.CLIENT_PAN`` anchors on the label deliberately, because a bare
``[A-Z]{5}\\d{4}[A-Z]`` also matches the PAN embedded in Zerodha's own GSTIN -- and
verifying a document against the *broker's* identity would pass for every investor alike
and verify nothing. Here the cost function is **exactly inverted**: masking the broker's
PAN by accident costs nothing at all, and missing the client's costs everything. So this
module sweeps bare, and that disagreement with the other module is correct.

**2. This does not use ``crypto.redact_pan``.** ``ABCDE1234F -> ABXXXXXX4F`` keeps four
characters on purpose, so two log lines can be correlated as the same PAN. That is a
feature in a log and a bug in a payload leaving the process: four characters of a
ten-character fixed-format value, plus document context, is a meaningful narrowing.
Outbound gets a type token -- ``[PAN]`` -- which carries no bits at all. The model does
not need any of it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: The allow-list anchor. INE = equity, INF = fund -- the same narrowing
#: ``zerodha_contract_note.ISIN_PATTERN`` makes, and for the same reason: a broad
#: ``IN[A-Z0-9]{10}`` also matches INZ000031633, the broker's SEBI registration, which
#: appears in the header of every note and would keep the whole header block.
ISIN_TOKEN = re.compile(r"\bIN[EF][A-Z0-9]{9}\b")

#: Charge lines carry no personal data and are the other half of what the model reads.
#: Matched by vocabulary rather than by Zerodha's exact labels, so this survives the
#: wording differences between brokers -- which is the point of the LLM path.
CHARGE_WORDS = re.compile(
    r"\b(brokerage|securities\s+transaction|STT|GST|IGST|CGST|SGST|SEBI|stamp\s+duty|"
    r"turnover|exchange\s+transaction|clearing|transaction\s+charge|taxable\s+value|"
    r"pay\s*in|pay\s*out|net\s+(?:amount|total)|total)\b",
    re.I,
)

#: Document facts the model needs to date and identify the note. A date and a reference
#: number are not personal data.
DOC_FACTS = re.compile(
    r"\b(trade\s*date|contract\s*note\s*(?:no|number)|settlement\s*(?:no|number|date))\b",
    re.I,
)

#: --- the deny-list. Each is a shape a person can be identified by. -------------------
#: Bare, not label-anchored -- see the module docstring's first inversion.
BARE_PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
#: GSTIN embeds a PAN; masked first so the PAN sweep never has to see it.
GSTIN = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z]\d[A-Z]\d\b")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
#: Indian mobile, with or without country code. Bounded by non-digits so it cannot eat a
#: quantity or an order number.
PHONE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)")
#: NSDL demat IDs are IN + 14 digits -- a distinctive prefix, safe to sweep bare.
#:
#: CDSL ids are 16 BARE DIGITS, and that is label-anchored instead, because a bare
#: sixteen-digit sweep is not safe here: a Zerodha ORDER NUMBER is also sixteen digits on
#: real notes, and masking it destroys ``source_ref`` -- the field
#: ``precedence.trade_identity`` uses to recognise that a contract note and a tradebook
#: describe the same trade. Over-masking there would silently double-count trades in a
#: ledger, which is worse than the risk it removes: an unlabelled demat id can only leak
#: from a line the allow-list already kept, and demat ids live in the identity block,
#: which the allow-list drops whole.
DEMAT = re.compile(r"(?<!\w)IN\d{14}(?!\d)")
DEMAT_LABELLED = re.compile(
    r"\b((?:demat|dp\s*id|bo\s*id|client\s*id)\s*(?:no\.?)?\s*[:\-]?\s*)(\d{8,16})\b",
    re.I,
)
#: UCC as printed with its label. Bare it is indistinguishable from a ticker, so this one
#: stays label-anchored -- masking every short alphanumeric token would eat the symbols.
UCC_LABELLED = re.compile(r"\b(UCC(?:\s*of\s*Client)?\s*[:\-]?\s*)([A-Z0-9]{3,12})\b", re.I)

#: Applied in this order. GSTIN before PAN so the broker's registration is consumed whole
#: rather than leaving a masked fragment; DEMAT before PHONE so a 16-digit id is not first
#: chewed into a 10-digit phone match.
_DENY: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("email", EMAIL, "[EMAIL]"),
    ("gstin", GSTIN, "[GSTIN]"),
    ("demat", DEMAT, "[DEMAT]"),
    ("pan", BARE_PAN, "[PAN]"),
    ("phone", PHONE, "[PHONE]"),
)

#: Below this, nothing is sent anywhere. THE EGRESS GATE -- see ``redact_for_llm``.
MIN_TRADE_LINES = 1


class RedactionError(RuntimeError):
    """PII survived redaction, so NOTHING is sent. The fail-closed path.

    Never caught inside this package. A caller that swallowed it would turn the one
    guarantee this module makes into a suggestion.
    """


class NothingToSendError(RuntimeError):
    """The document has no trade-shaped content, so there is nothing to extract and no
    reason to transmit it.

    THE EGRESS GATE, and the reason an unrecognised document is safe to route here.
    ``file_validation``'s ``unknown`` is a catch-all: a Groww contract note lands in it,
    and so would a will, an insurance policy, or a medical report someone attached by
    mistake. A broker document has ISINs on it. Those others do not, so they are refused
    before a single byte leaves the process -- not because we recognised them, but because
    they never produced anything a trade parser wants.
    """


@dataclass(frozen=True, slots=True)
class RedactedDocument:
    """The only thing that may be transmitted, plus counts describing what was removed."""

    text: str
    kept_lines: int
    dropped_lines: int
    isin_lines: int
    #: pattern name -> how many were masked. COUNTS ONLY, never the values -- this ends up
    #: in an import note, and audit.py's rule ("never a PAN, a demat number or ciphertext")
    #: is the house standard for anything a person might later read.
    masked: dict[str, int] = field(default_factory=dict)


def select_trade_lines(text: str, context: int = 1) -> tuple[list[str], int]:
    """The allow-list. Returns ``(kept_lines, isin_line_count)``.

    Keeps a line when it carries an ISIN, states a charge, or states a document fact --
    plus ``context`` lines either side of an ISIN line, because pdfplumber splits a wrapped
    table row across two physical lines and the quantity can land on the second.

    Everything else is dropped, the client identity block included. There is no branch
    here that keeps a line because it looked harmless; a line is kept because it looked
    *useful*.
    """
    lines = text.splitlines()
    isin_rows = {i for i, line in enumerate(lines) if ISIN_TOKEN.search(line)}

    keep: set[int] = set()
    for i in isin_rows:
        keep.update(range(max(0, i - context), min(len(lines), i + context + 1)))
    for i, line in enumerate(lines):
        if CHARGE_WORDS.search(line) or DOC_FACTS.search(line):
            keep.add(i)

    return [lines[i] for i in sorted(keep) if lines[i].strip()], len(isin_rows)


def mask_pii(line: str) -> tuple[str, dict[str, int]]:
    """The deny-list, over what the allow-list kept. Returns the masked line and counts.

    Type tokens rather than partial masks -- ``[PAN]`` not ``ABXXXXXX4F`` -- see the
    module docstring's second inversion.
    """
    counts: dict[str, int] = {}
    masked = UCC_LABELLED.sub(lambda m: f"{m.group(1)}[UCC]", line)
    if masked != line:
        counts["ucc"] = len(UCC_LABELLED.findall(line))
    labelled_demat = len(DEMAT_LABELLED.findall(masked))
    if labelled_demat:
        counts["demat"] = counts.get("demat", 0) + labelled_demat
        masked = DEMAT_LABELLED.sub(lambda m: f"{m.group(1)}[DEMAT]", masked)
    for name, pattern, token in _DENY:
        found = len(pattern.findall(masked))
        if found:
            counts[name] = counts.get(name, 0) + found
            masked = pattern.sub(token, masked)
    return masked, counts


def assert_no_pii(text: str) -> None:
    """Fail closed. Raises ``RedactionError`` naming the pattern, never the value.

    The last line of defence, and deliberately a separate pass rather than trust in
    ``mask_pii`` having been called: a future edit that reorders the pipeline, or adds a
    path that skips masking, fails here instead of transmitting.
    """
    for name, pattern, _token in _DENY:
        if pattern.search(text):
            raise RedactionError(
                f"redaction left something {name}-shaped in the payload; nothing was sent"
            )
    # UCC is checked by label rather than shape, so it needs the value side inspected
    # separately: a masked line reads "UCC: [UCC]", which must no longer match.
    for match in UCC_LABELLED.finditer(text):
        if match.group(2) != "[UCC]".strip("[]"):
            raise RedactionError("redaction left a client code in the payload; nothing was sent")
    if DEMAT_LABELLED.search(text):
        raise RedactionError(
            "redaction left a labelled demat/client id in the payload; nothing was sent"
        )


def redact_for_llm(text: str, context: int = 1) -> RedactedDocument:
    """allow-list -> deny-list -> assert. The only supported way to prepare text for
    transmission.

    Raises:
        NothingToSendError: fewer than ``MIN_TRADE_LINES`` lines carried an ISIN, so this
            is not a document with a trade table in it. Nothing is transmitted.
        RedactionError: something PII-shaped survived. Nothing is transmitted.
    """
    kept, isin_lines = select_trade_lines(text, context=context)
    if isin_lines < MIN_TRADE_LINES:
        raise NothingToSendError(
            "no ISIN-bearing lines found; this is not a document with a trade table"
        )

    masked_lines: list[str] = []
    totals: dict[str, int] = {}
    for line in kept:
        masked, counts = mask_pii(line)
        masked_lines.append(masked)
        for name, n in counts.items():
            totals[name] = totals.get(name, 0) + n

    payload = "\n".join(masked_lines)
    assert_no_pii(payload)

    return RedactedDocument(
        text=payload,
        kept_lines=len(masked_lines),
        dropped_lines=len(text.splitlines()) - len(masked_lines),
        isin_lines=isin_lines,
        masked=totals,
    )
