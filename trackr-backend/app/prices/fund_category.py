"""AMFI category headings -> display category, instrument kind and exposure bucket.

docs/MF_ETF_PLAN.md section 3.3. An EXPLICIT table of complete headings, each one copied
from a real AMFI file (NAVAll.txt and the NAV history report, both fetched 13-Oct-2026).
Matching is on the whole heading after the tested spacing/punctuation rules in
``normalize_heading`` -- never ``"ETF" in heading``: a fund of funds that invests in ETFs
is not itself an ETF, and "Other Scheme - Other  ETFs" (two spaces, as AMFI prints it) is.

A heading not in this table is UNKNOWN to the parser: its rows are quarantined with a
reason code and the scheme stays unclassified until someone reviews the new heading and
adds it here. Guessing a kind for an unreviewed heading would put an ETF on the NAV route
or a fund on the exchange route, and both mistakes value the holding from the wrong price.

THE EXPOSURE BUCKET IS A DISPLAY CANDIDATE, NOT A TAX FACT. An "Equity Scheme" heading
says what SEBI category the AMC filed the scheme under today; whether it was an
equity-oriented fund for a particular disposal's period is a separate, evidenced question
(plan section 5.2). Nothing here may be read as a tax qualification.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class InstrumentKind(StrEnum):
    STOCK = "STOCK"
    ETF = "ETF"
    MUTUAL_FUND = "MUTUAL_FUND"
    UNRESOLVED = "UNRESOLVED"


class Exposure(StrEnum):
    DOMESTIC_EQUITY = "DOMESTIC_EQUITY"
    DEBT = "DEBT"
    COMMODITY = "COMMODITY"
    INTERNATIONAL = "INTERNATIONAL"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Category:
    #: The heading's inner text, normalised (the alias-table key).
    key: str
    #: Short label for screens, e.g. "Large Cap Fund".
    display: str
    kind: InstrumentKind
    exposure: Exposure


#: Structure prefixes AMFI puts before the parenthesised category.
_STRUCTURES = ("Open Ended Schemes", "Close Ended Schemes", "Interval Fund Schemes")
_HEADING = re.compile(
    r"^(?P<structure>Open Ended Schemes|Close Ended Schemes|Interval Fund Schemes)"
    r"\s*\((?P<inner>.*)\)\s*$"
)


def is_heading(line: str) -> bool:
    return bool(_HEADING.match(line.strip()))


def normalize_heading(line: str) -> tuple[str, str] | None:
    """``(structure, inner key)`` for a heading line, or None when it is not one.

    The rules, each covered by a test against a real heading:
    * the daily file prints ``Schemes(Equity ...)``, the history report
      ``Schemes ( Equity ... )`` -- whitespace inside the brackets is trimmed;
    * runs of whitespace collapse to one space (``Other  ETFs``);
    * the typographic apostrophe in ``Children’s Fund`` becomes ``'``.
    Nothing else is rewritten: ``Equity Scheme`` and ``Equity Schemes`` stay distinct keys,
    each listed below, because AMFI uses both.
    """
    match = _HEADING.match(line.strip())
    if match is None:
        return None
    inner = match.group("inner").replace("’", "'")
    inner = re.sub(r"\s+", " ", inner).strip()
    return match.group("structure"), inner


_E, _D, _C, _I, _M, _U = (
    Exposure.DOMESTIC_EQUITY,
    Exposure.DEBT,
    Exposure.COMMODITY,
    Exposure.INTERNATIONAL,
    Exposure.MIXED,
    Exposure.UNKNOWN,
)
_MF, _ETF = InstrumentKind.MUTUAL_FUND, InstrumentKind.ETF

#: (inner key, display, kind, exposure). Every key was observed in an AMFI file.
_TABLE: list[tuple[str, str, InstrumentKind, Exposure]] = [
    # --- equity (SEBI 2017 categories; both "Scheme" and "Schemes" spellings) ---------
    *[
        (f"Equity Scheme{s} - {name}", name, _MF, _E)
        for s in ("", "s")
        for name in (
            "Contra Fund",
            "Dividend Yield Fund",
            "Flexi Cap Fund",
            "Focused Fund",
            "Large & Mid Cap Fund",
            "Large Cap Fund",
            "Mid Cap Fund",
            "Multi Cap Fund",
            "Small Cap Fund",
            "Value Fund",
        )
    ],
    ("Equity Scheme - ELSS", "ELSS", _MF, _E),
    ("Equity Schemes - ELSS- Tax Saver Fund", "ELSS", _MF, _E),
    # Sectoral/thematic may hold international stocks under SEBI's rules: not "domestic".
    ("Equity Scheme - Sectoral/ Thematic", "Sectoral/Thematic Fund", _MF, _U),
    ("Equity Schemes - Sectoral Fund", "Sectoral Fund", _MF, _U),
    ("Equity Schemes - Thematic Fund", "Thematic Fund", _MF, _U),
    # --- debt ---------------------------------------------------------------------------
    *[
        (f"Debt Scheme - {name}", name, _MF, _D)
        for name in (
            "Banking and PSU Fund",
            "Corporate Bond Fund",
            "Credit Risk Fund",
            "Dynamic Bond",
            "Gilt Fund",
            "Liquid Fund",
            "Long Duration Fund",
            "Low Duration Fund",
            "Medium Duration Fund",
            "Medium to Long Duration Fund",
            "Money Market Fund",
            "Overnight Fund",
            "Short Duration Fund",
            "Ultra Short Duration Fund",
        )
    ],
    *[
        (f"Income/Debt Oriented Schemes - {name}", name, _MF, _D)
        for name in (
            "10-year Constant Maturity Gilt Fund",
            "Banking and PSU Debt Fund",
            "Corporate Bond Fund",
            "Credit Risk Fund",
            "Dynamic Term Fund",
            "Floating Interest Rates Fund",
            "Gilt Fund",
            "Liquid Fund",
            "Long Term Fund",
            "Medium Term Fund",
            "Medium to Long Term Fund",
            "Money Market Fund",
            "Overnight Fund",
            "Short Term Fund",
            "Ultra Short Term Fund",
            "Ultra Short to Short Term Fund",
            "Fixed Term Plan",
            "Other Debt Scheme",
        )
    ],
    # A debt-oriented heading naming a "sectoral" fund is contradictory; do not guess.
    ("Income/Debt Oriented Schemes - Sectoral Fund", "Sectoral Fund (debt-oriented)", _MF, _U),
    # Pre-2017 categories, still printed for old schemes.
    ("Income", "Income", _MF, _D),
    ("Gilt", "Gilt", _MF, _D),
    ("Money Market", "Money Market", _MF, _D),
    ("Growth", "Growth", _MF, _U),
    ("ELSS", "ELSS", _MF, _E),
    # --- hybrid --------------------------------------------------------------------------
    ("Hybrid Scheme - Aggressive Hybrid Fund", "Aggressive Hybrid Fund", _MF, _E),
    ("Hybrid Schemes - Aggressive Hybrid Fund", "Aggressive Hybrid Fund", _MF, _E),
    ("Hybrid Scheme - Arbitrage Fund", "Arbitrage Fund", _MF, _E),
    ("Hybrid Schemes - Arbitrage Fund", "Arbitrage Fund", _MF, _E),
    ("Hybrid Scheme - Balanced Hybrid Fund", "Balanced Hybrid Fund", _MF, _M),
    ("Hybrid Schemes - Balanced Hybrid Fund", "Balanced Hybrid Fund", _MF, _M),
    ("Hybrid Scheme - Conservative Hybrid Fund", "Conservative Hybrid Fund", _MF, _M),
    ("Hybrid Schemes - Conservative Hybrid Fund", "Conservative Hybrid Fund", _MF, _M),
    (
        "Hybrid Scheme - Dynamic Asset Allocation or Balanced Advantage",
        "Balanced Advantage Fund",
        _MF,
        _M,
    ),
    (
        "Hybrid Schemes - Balanced Advantage Fund/ Dynamic Asset Allocation",
        "Balanced Advantage Fund",
        _MF,
        _M,
    ),
    ("Hybrid Scheme - Equity Savings", "Equity Savings Fund", _MF, _M),
    ("Hybrid Schemes - Equity Savings Fund", "Equity Savings Fund", _MF, _M),
    ("Hybrid Scheme - Multi Asset Allocation", "Multi Asset Allocation Fund", _MF, _M),
    ("Hybrid Schemes - Multi Asset Allocation Fund", "Multi Asset Allocation Fund", _MF, _M),
    # --- exchange traded funds --------------------------------------------------------------
    ("Exchange Traded Funds (ETFs) - Equity ETF", "Equity ETF", _ETF, _E),
    ("Exchange Traded Funds (ETFs) - Debt ETF", "Debt ETF", _ETF, _D),
    ("Exchange Traded Funds (ETFs) - Gold ETF", "Gold ETF", _ETF, _C),
    ("Exchange Traded Funds (ETFs) - Silver ETF", "Silver ETF", _ETF, _C),
    ("Exchange Traded Funds (ETFs) - ETFs investing overseas", "Overseas ETF", _ETF, _I),
    ("Exchange Traded Funds (ETFs) - Hybrid ETF", "Hybrid ETF", _ETF, _M),
    ("Exchange Traded Funds (ETFs) - Other ETF", "Other ETF", _ETF, _U),
    ("Other Scheme - Gold ETF", "Gold ETF", _ETF, _C),
    ("Other Scheme - Other ETFs", "Other ETF", _ETF, _U),
    # --- index funds, fund of funds ---------------------------------------------------------
    ("Index Funds - Equity Funds", "Equity Index Fund", _MF, _E),
    ("Index Funds - Debt Funds", "Debt Index Fund", _MF, _D),
    ("Index Funds - Hybrid Fund", "Hybrid Index Fund", _MF, _M),
    ("Other Scheme - Index Funds", "Index Fund", _MF, _U),
    (
        "Fund of Funds Scheme (Domestic) - Fund of Funds Scheme (Domestic)",
        "Fund of Funds (Domestic)",
        _MF,
        _U,
    ),
    ("Other Scheme - FoF Domestic", "Fund of Funds (Domestic)", _MF, _U),
    ("Other Scheme - FoF Overseas", "Fund of Funds (Overseas)", _MF, _I),
    (
        "Overseas Fund of Funds - Fund of Funds investing overseas",
        "Fund of Funds (Overseas)",
        _MF,
        _I,
    ),
    # --- solution oriented, life cycle -----------------------------------------------------
    ("Children's Fund - Childrens' Fund", "Children's Fund", _MF, _M),
    ("Solution Oriented Scheme - Children's Fund", "Children's Fund", _MF, _M),
    ("Solution Oriented Scheme - Retirement Fund", "Retirement Fund", _MF, _M),
    ("Solution Oriented Schemes ** - Retirement Fund", "Retirement Fund", _MF, _M),
    *[
        (
            f"Life Cycle Funds - Life Cycle Fund with Maturity of {n} Years",
            f"Life Cycle Fund ({n} years)",
            _MF,
            _M,
        )
        for n in (5, 10, 15)
    ],
]

CATEGORIES: dict[str, Category] = {
    key: Category(key=key, display=display, kind=kind, exposure=exposure)
    for key, display, kind, exposure in _TABLE
}
if len(CATEGORIES) != len(_TABLE):  # pragma: no cover - a duplicate key is a table bug
    raise RuntimeError("duplicate AMFI category key in fund_category._TABLE")


def classify_heading(line: str) -> Category | None:
    """The reviewed category for a heading line, or None when the heading is unknown."""
    normalized = normalize_heading(line)
    if normalized is None:
        return None
    return CATEGORIES.get(normalized[1])
