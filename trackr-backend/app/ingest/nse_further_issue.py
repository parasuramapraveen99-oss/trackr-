"""NSE "Listing of further issues" circulars -- the public record of a rights allotment.

A rights allotment never appears on a contract note. NSE lists the allotted shares in a
circular whose entries are fixed-format; verified live 2026-10-08 on NSE/CML/61406
(1 Apr 2024), entry 9:

    The South Indian Bank Limited Symbol SOUTHBANK Name of the Company The South Indian
    Bank Limited Series EQ ... ISIN* INE683A01023 Face Value (In Re.) 1 Paid-up Value
    (In Re.) 1 Issue Price (In Rs.) 22 Security Description Equity shares of Re. 1/- each
    allotted under Rights Issue Date of Allotment 27-Mar-2024 No. of Securities 523185254

That one entry carries every rights term rule 20d needs except the ratio (which the
corporate-actions notice states): the CURRENT ISIN, the price per share, and the
allotment date that dates the acquisition for tax. Pure parsing; fetching is
``NseCircularListSource`` (same warmed-session posture as the other NSE readers).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

#: One listed entry, from its "Symbol" to just before the next entry or the end. Fields
#: are matched by their printed labels, never by position: the "Continuation Sheet" page
#: break lands mid-entry in the real circular (between Series and ISIN for SOUTHBANK).
_ENTRY_RE = re.compile(
    r"Symbol\s+(?P<symbol>[A-Z0-9&\-]+)\s+Name of the Company\s+(?P<company>.+?)\s+Series\s+"
    r"(?P<series>[A-Z0-9]+)(?P<body>.+?)(?=\s\d+\.\s+[^.]{0,120}?\s+Symbol\s+[A-Z0-9&\-]+\s+Name"
    r" of the Company|\Z)",
    re.S,
)
_ISIN_RE = re.compile(r"ISIN\*?\s+(IN[A-Z0-9]{10})")
_PRICE_RE = re.compile(r"Issue Price \(In Rs?e?\.\)\s+([\d,]+(?:\.\d+)?)")
_DESC_RE = re.compile(r"Security Description\s+(.+?)\s+Date of Allotment", re.S)
_ALLOT_RE = re.compile(r"Date of Allotment\s+(\d{2}-[A-Za-z]{3}-\d{4})")
_QTY_RE = re.compile(r"No\. of Securities\s+([\d,]+)")


@dataclass(frozen=True)
class FurtherIssue:
    symbol: str
    company: str
    isin: str
    issue_price: Decimal | None
    description: str
    allotment_date: date
    quantity: int | None

    @property
    def is_rights(self) -> bool:
        return "rights issue" in self.description.lower()


def parse_further_issues(text: str) -> list[FurtherIssue]:
    """Every entry that states an ISIN and an allotment date. An entry missing either is
    skipped, never guessed -- those two are what make it usable evidence."""
    flat = " ".join(text.split())
    out: list[FurtherIssue] = []
    for m in _ENTRY_RE.finditer(flat):
        body = m.group("body")
        isin, allot = _ISIN_RE.search(body), _ALLOT_RE.search(body)
        if not isin or not allot:
            continue
        price_m, desc_m, qty_m = _PRICE_RE.search(body), _DESC_RE.search(body), _QTY_RE.search(body)
        try:
            price = Decimal(price_m.group(1).replace(",", "")) if price_m else None
        except InvalidOperation:
            price = None
        out.append(
            FurtherIssue(
                symbol=m.group("symbol"),
                company=m.group("company").strip(),
                isin=isin.group(1),
                issue_price=price,
                description=" ".join(desc_m.group(1).split()) if desc_m else "",
                allotment_date=datetime.strptime(allot.group(1), "%d-%b-%Y").date(),
                quantity=int(qty_m.group(1).replace(",", "")) if qty_m else None,
            )
        )
    return out


_RIGHTS_SUBJECT_RE = re.compile(
    r"Rights\s+(\d+)\s*:\s*(\d+)\s*@\s*(?:Premium\s+)?Rs\.?\s*([\d.]+)", re.IGNORECASE
)


def parse_rights_ratio(subject: str) -> tuple[int, int] | None:
    """(new, held) from an NSE notice subject such as "Rights 1:4 @ Premium Rs 21/-"."""
    m = _RIGHTS_SUBJECT_RE.search(subject)
    return (int(m.group(1)), int(m.group(2))) if m else None
