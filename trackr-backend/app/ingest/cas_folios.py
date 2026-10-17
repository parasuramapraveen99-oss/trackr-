"""CDSL direct-MF balances, retained as dated evidence rather than transactions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from app.isin import has_valid_check_digit

_NUMBER = r"[\d,]+\.\d+"
_SCHEME = re.compile(
    r"Scheme Name\s*:\s*(?P<name>(?:(?!Scheme Name\s*:).)*?)"
    r"\s+Scheme Code\s*:\s*\S+\s+Folio No\s*:\s*(?P<folio>\S+)"
    r"(?:(?!Scheme Name\s*:).)*?ISIN\s*:\s*(?P<isin>INF[A-Z0-9]{8}\d)",
    re.S,
)
_ROW = re.compile(
    rf"(?P<isin>INF[A-Z0-9]{{8}}\d)\s+(?P<identity>[A-Z0-9/-]+)\s+"
    rf"(?P<quantity>{_NUMBER})\s+(?P<nav>{_NUMBER})\s+"
    rf"(?P<investment>{_NUMBER})\s+(?P<value>{_NUMBER})(?:\s|$)"
)
_SUMMARY = re.compile(rf"^Mutual Fund Folios\s+\d+\s+Folios\s+\d+\s+(?P<value>{_NUMBER})\s*$", re.M)
_TOTAL = re.compile(rf"Grand Total\s+(?P<investment>{_NUMBER})\s+(?P<value>{_NUMBER})")


@dataclass(frozen=True, slots=True)
class DeclaredFundFolio:
    isin: str
    amc_name: str
    scheme_name: str
    folio_number: str
    quantity: Decimal
    nav: Decimal
    market_value: Decimal
    snap_date: date


class FolioParseError(ValueError):
    """A present MF section could not be read completely and reconciled."""


def _decimal(raw: str) -> Decimal:
    return Decimal(raw.replace(",", ""))


def parse_folios(text: str, statement_date: date, demat_end: int) -> list[DeclaredFundFolio]:
    """Read reviewed monthly/annual rows, using their printed identities and totals.

    Annual rows wrap the folio identifier and print an ARN in its place on the numeric
    line. They can be attributed only when the account-details section names exactly
    one folio for that ISIN. Ambiguous annual rows are refused, never assigned by order.
    """
    metadata = {}
    matches = list(_SCHEME.finditer(text))
    if len(matches) != len(re.findall(r"Folio No\s*:", text)):
        raise FolioParseError("The CAS contains unreadable mutual-fund folio details.")
    for match in matches:
        amcs = list(re.finditer(r"AMC Name\s*:\s*([^\n]+)", text[: match.start()]))
        if not amcs:
            raise FolioParseError("A mutual-fund folio has no readable fund-house identity.")
        key = (match["isin"], match["folio"])
        value = (" ".join(amcs[-1][1].split()), " ".join(match["name"].split()))
        if key in metadata and metadata[key] != value:
            raise FolioParseError("A mutual-fund folio has conflicting scheme details.")
        metadata[key] = value
    if not metadata:
        if re.search(r"Folio No\s*:", text) or any(
            _decimal(s["value"]) > 0 for s in _SUMMARY.finditer(text)
        ):
            raise FolioParseError(
                "The CAS lists mutual-fund folios whose scheme details are unreadable."
            )
        return []

    section = text[demat_end:]
    rows = list(_ROW.finditer(section))
    if not rows:
        raise FolioParseError(
            "The CAS lists mutual-fund folios but their balance table is unreadable."
        )
    # Dates on the fund-table page must agree with the independently validated CAS date.
    header = section[: rows[0].start()]
    header = header[header.rfind("\nPage ") :] if "\nPage " in header else header
    # Some trilingual headings interleave letters into the date's numeric glyphs.
    # Keep the glyphs and separators in order; never supply missing digits.
    date_tokens = [
        d
        for line in header.splitlines()
        for d in re.findall(r"\d{2}-\d{2}-\d{4}", re.sub(r"[^0-9-]", "", line))
    ]
    try:
        dates = {datetime.strptime(d, "%d-%m-%Y").date() for d in date_tokens}
    except ValueError as exc:
        raise FolioParseError("The mutual-fund balance date is invalid.") from exc
    if dates != {statement_date}:
        raise FolioParseError(
            "The mutual-fund balance date is missing or disagrees with the CAS date."
        )
    annual = "ARN Code" in header
    result = []
    seen = set()
    investment_total = Decimal(0)
    for row in rows:
        isin, identity = row["isin"], row["identity"]
        key = (isin, identity)
        if (
            key not in metadata
            and annual
            and (identity == "DIRECT" or re.fullmatch(r"ARN-?\d+", identity))
        ):
            candidates = [k for k in metadata if k[0] == isin]
            if len(candidates) != 1:
                raise FolioParseError(
                    "An annual fund row cannot be attributed to exactly one folio."
                )
            key = candidates[0]
        if key not in metadata or key in seen or not has_valid_check_digit(isin):
            raise FolioParseError(
                "A fund balance has a missing, repeated or invalid folio/ISIN identity."
            )
        seen.add(key)
        qty, nav, value = (_decimal(row[k]) for k in ("quantity", "nav", "value"))
        if (
            any(v != v.quantize(Decimal("0.00000001")) or v >= Decimal("1e20") for v in (qty, nav))
            or value != value.quantize(Decimal("0.000001"))
            or value >= Decimal("1e14")
        ):
            raise FolioParseError("A fund balance exceeds the supported numeric precision.")
        if qty < 0 or nav <= 0 or value < 0 or abs(qty * nav - value) > Decimal("0.01"):
            raise FolioParseError("A mutual-fund balance does not reproduce its units × NAV value.")
        amc, name = metadata[key]
        result.append(DeclaredFundFolio(isin, amc, name, key[1], qty, nav, value, statement_date))
        investment_total += _decimal(row["investment"])
    total = sum((r.market_value for r in result), Decimal(0))
    totals = list(_TOTAL.finditer(section))
    summaries = list(_SUMMARY.finditer(text))
    if (
        seen != set(metadata)
        or len(totals) != 1
        or not summaries
        or _decimal(totals[0]["value"]) != total
        or _decimal(totals[0]["investment"]) != investment_total
        or any(_decimal(s["value"]) != total for s in summaries)
    ):
        raise FolioParseError(
            "Mutual-fund rows do not reproduce all folios and the CAS's fund totals."
        )
    return result
