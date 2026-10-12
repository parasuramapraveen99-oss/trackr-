"""The approved live research registries (BE-Q03 decision of 29 September 2026). Pure data.

The owner delegated BE-Q03 to the implementer on 2026-09-29 ("u can invent financial logic").
Every entry below is therefore IMPLEMENTER-DEFINED UNDER OWNER DELEGATION -- the ``approval``
authority says exactly that, and nothing here is attributed to the owner's own authorship.
docs/RESEARCH_CALCULATIONS.md is the human-readable form of this module; change them together.

Three registries live here, all versioned:

* ``LIVE_METRIC_CATALOGUE`` -- which filed series a person may record as a fact (metric, basis,
  unit, cadence, statement). Line items follow Ind AS Schedule III (Division II) captions.
* ``registry_body()`` -- the calculator definitions, built into
  ``research_metrics.LIVE_FORMULAS``. Each formula exists once per reporting basis
  (``consolidated-1`` / ``standalone-1``; ``...-quarter-1`` for the quarterly P&L-only
  ratios) because the engine never mixes bases.
* ``india_lodr_calendar`` -- fiscal periods and "which period must be out by now" for Indian
  listed issuers, from SEBI LODR Regulation 33(3).

SIGN CONVENTIONS every fact must follow (the formulas depend on them):

* Income and expense lines are recorded as the filed POSITIVE amount (an expense of 120 is
  ``120``). ``CHANGES_IN_INVENTORIES`` is recorded SIGNED exactly as filed (an inventory build
  is filed negative and stays negative) -- it is added, as Schedule III adds it.
* Profit lines are signed (a loss is negative).
* Balance-sheet lines are the closing balance at ``period_end`` of the fiscal period they are
  recorded against, positive.
* ``NET_CASH_FROM_OPERATING_ACTIVITIES`` is signed as filed. ``PAYMENTS_FOR_PROPERTY_PLANT_
  AND_EQUIPMENT`` is the positive MAGNITUDE of the outflow.

"Average" always means (closing balance of the current period + closing balance of the prior
adjacent period) / 2. Day counts use a fixed 365 on the YEAR cadence -- a leap year is not
special-cased, and that is part of the definition.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.engine.research_rules import (
    CadenceCalendar,
    MetricDefinition,
    MetricRegistry,
    ReportingCalendar,
    Window,
)

DECISION_DATE = "2026-09-29"
APPROVAL = {
    "authority": "IMPLEMENTER_UNDER_OWNER_DELEGATION",
    "decided_on": DECISION_DATE,
    "reference": "BACKEND_OPEN_QUESTIONS.md#be-q03 (2026-09-29); docs/RESEARCH_CALCULATIONS.md",
}
BASES = ("CONSOLIDATED", "STANDALONE")

# =========================================================================================
# metric catalogue
# =========================================================================================

PL, BS, CF, SH, NOTES = "PROFIT_AND_LOSS", "BALANCE_SHEET", "CASH_FLOW", "SHAREHOLDING", "NOTES"
_YQ = ("YEAR", "QUARTER")
_Y = ("YEAR",)

#: (metric, statement, cadences). Unit INR, NUMERIC, both bases.
_INR_SERIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("REVENUE_FROM_OPERATIONS", PL, _YQ),
    ("OTHER_INCOME", PL, _YQ),
    ("COST_OF_MATERIALS_CONSUMED", PL, _YQ),
    ("PURCHASES_OF_STOCK_IN_TRADE", PL, _YQ),
    ("CHANGES_IN_INVENTORIES", PL, _YQ),
    ("EMPLOYEE_BENEFITS_EXPENSE", PL, _YQ),
    ("FINANCE_COSTS", PL, _YQ),
    ("DEPRECIATION_AND_AMORTISATION_EXPENSE", PL, _YQ),
    ("OTHER_EXPENSES", PL, _YQ),
    ("PROFIT_BEFORE_EXCEPTIONAL_ITEMS_AND_TAX", PL, _YQ),
    ("PROFIT_BEFORE_TAX", PL, _YQ),
    ("TAX_EXPENSE", PL, _YQ),
    ("PROFIT_FROM_CONTINUING_OPERATIONS", PL, _YQ),
    ("PROFIT_FOR_THE_PERIOD", PL, _YQ),
    ("PROFIT_ATTRIBUTABLE_TO_OWNERS", PL, _YQ),
    ("TOTAL_ASSETS", BS, _Y),
    ("TOTAL_EQUITY", BS, _Y),
    ("EQUITY_ATTRIBUTABLE_TO_OWNERS", BS, _Y),
    ("TOTAL_CURRENT_ASSETS", BS, _Y),
    ("TOTAL_CURRENT_LIABILITIES", BS, _Y),
    ("TRADE_RECEIVABLES", BS, _Y),
    ("INVENTORIES", BS, _Y),
    ("PROPERTY_PLANT_AND_EQUIPMENT", BS, _Y),
    ("NON_CURRENT_INVESTMENTS", BS, _Y),
    ("NON_CURRENT_BORROWINGS", BS, _Y),
    ("CURRENT_BORROWINGS", BS, _Y),
    ("TRADE_PAYABLES", BS, _Y),
    ("CASH_AND_CASH_EQUIVALENTS", BS, _Y),
    ("NET_CASH_FROM_OPERATING_ACTIVITIES", CF, _Y),
    ("PAYMENTS_FOR_PROPERTY_PLANT_AND_EQUIPMENT", CF, _Y),
)

_OTHER_SERIES: tuple[MetricDefinition, ...] = (
    # Shareholding pattern (LODR Reg 31): issuer-level, basis-free -> recorded STANDALONE.
    MetricDefinition("PROMOTER_HOLDING", "STANDALONE", "PERCENT", "NUMERIC", ("QUARTER",), SH),
    MetricDefinition("PROMOTER_SHARES_PLEDGED", "STANDALONE", "PERCENT", "NUMERIC",
                     ("QUARTER",), SH),
    # Auditor's report: TRUE when the opinion is qualified, adverse or a disclaimer.
    *(MetricDefinition("AUDIT_OPINION_MODIFIED", b, "BOOLEAN", "BOOLEAN", _Y, NOTES)
      for b in BASES),
)

CATALOGUE_VERSION = "be-q03-catalogue-1"

LIVE_METRIC_CATALOGUE = MetricRegistry(
    version=CATALOGUE_VERSION,
    metrics=tuple(MetricDefinition(m, b, "INR", "NUMERIC", cad, st)
                  for m, st, cad in _INR_SERIES for b in BASES) + _OTHER_SERIES,
)

# =========================================================================================
# formula registry
# =========================================================================================

REGISTRY_VERSION = "be-q03-formulas-1"
AMOUNT_CONTRACT = "CONDITIONAL_COMPANY_AMOUNT_V1"
SERIES_CONTRACT = "CONDITIONAL_COMPANY_SERIES_V1"
OUTPUT_CONTRACTS = (AMOUNT_CONTRACT, SERIES_CONTRACT)

RATIO_Q = {"quantum": "0.000001", "rounding": "ROUND_HALF_EVEN"}
AMOUNT_Q = {"quantum": "0.01", "rounding": "ROUND_HALF_EVEN"}
DAYS_Q = {"quantum": "0.01", "rounding": "ROUND_HALF_EVEN"}


def i(name: str) -> list[Any]:
    return ["input", name]


def a(name: str) -> list[Any]:
    return ["assumption", name]


def c(value: str) -> list[Any]:
    return ["const", value]


def add(x: list[Any], y: list[Any], *rest: list[Any]) -> list[Any]:
    node = ["add", x, y]
    for r in rest:
        node = ["add", node, r]
    return node


def sub(x: list[Any], y: list[Any]) -> list[Any]:
    return ["sub", x, y]


def mul(x: list[Any], y: list[Any]) -> list[Any]:
    return ["mul", x, y]


def div(x: list[Any], y: list[Any]) -> list[Any]:
    return ["div", x, y]


def avg(cur: str, prior: str) -> list[Any]:
    return div(add(i(cur), i(prior)), c("2"))


#: Operand name -> metric. ``_p`` suffix = PRIOR adjacent period, otherwise CURRENT.
_OPERANDS = {
    "rev": "REVENUE_FROM_OPERATIONS", "oi": "OTHER_INCOME",
    "cmc": "COST_OF_MATERIALS_CONSUMED", "pst": "PURCHASES_OF_STOCK_IN_TRADE",
    "cii": "CHANGES_IN_INVENTORIES", "emp": "EMPLOYEE_BENEFITS_EXPENSE",
    "fin": "FINANCE_COSTS", "da": "DEPRECIATION_AND_AMORTISATION_EXPENSE",
    "othx": "OTHER_EXPENSES", "pbeit": "PROFIT_BEFORE_EXCEPTIONAL_ITEMS_AND_TAX",
    "pat": "PROFIT_FOR_THE_PERIOD", "pco": "PROFIT_FROM_CONTINUING_OPERATIONS",
    "pato": "PROFIT_ATTRIBUTABLE_TO_OWNERS",
    "ta": "TOTAL_ASSETS", "te": "TOTAL_EQUITY", "eqo": "EQUITY_ATTRIBUTABLE_TO_OWNERS",
    "ca": "TOTAL_CURRENT_ASSETS", "cl": "TOTAL_CURRENT_LIABILITIES",
    "rec": "TRADE_RECEIVABLES", "inv": "INVENTORIES", "ppe": "PROPERTY_PLANT_AND_EQUIPMENT",
    "sec": "NON_CURRENT_INVESTMENTS", "ncb": "NON_CURRENT_BORROWINGS",
    "cb": "CURRENT_BORROWINGS", "tp": "TRADE_PAYABLES", "cash": "CASH_AND_CASH_EQUIVALENTS",
    "cfo": "NET_CASH_FROM_OPERATING_ACTIVITIES",
    "capex": "PAYMENTS_FOR_PROPERTY_PLANT_AND_EQUIPMENT",
}


def _collect(node: Any, out: list[str]) -> None:
    if isinstance(node, list):
        if node and node[0] == "input" and node[1] not in out:
            out.append(node[1])
        for n in node[1:]:
            _collect(n, out)


def _formula(fid: str, title: str, expr: list[Any], unit: str, precision: dict[str, str],
             basis: str, cadence: str = "YEAR", version_tag: str = "") -> dict[str, Any]:
    names: list[str] = []
    _collect(expr, names)
    inputs = []
    for n in names:
        role = "PRIOR" if n.endswith("_p") else "CURRENT"
        metric = _OPERANDS[n.removesuffix("_p")]
        inputs.append({"name": n, "metric": metric, "basis": basis, "unit": "INR",
                       "cadence": cadence, "period_role": role})
    return {"formula_id": fid, "version": f"{basis.lower()}{version_tag}-1", "kind": "METRIC",
            "title": title, "expression": expr, "inputs": inputs, "output_unit": unit,
            "precision": precision, "approval": APPROVAL}


def _cogs(suffix: str = "") -> list[Any]:
    """Schedule III cost of goods sold: materials consumed + purchases of stock-in-trade +
    changes in inventories (signed as filed)."""
    return add(i("cmc" + suffix), i("pst" + suffix), i("cii" + suffix))


def _sga(suffix: str = "") -> list[Any]:
    """Selling, general and administrative proxy under Schedule III: employee benefits expense
    + other expenses. Indian P&Ls do not file an SG&A caption."""
    return add(i("emp" + suffix), i("othx" + suffix))


def _beneish() -> list[Any]:
    """Beneish (1999) eight-variable M-score, published coefficients, no threshold."""
    one = c("1")
    dsri = div(div(i("rec"), i("rev")), div(i("rec_p"), i("rev_p")))
    gm_c = div(sub(i("rev"), _cogs()), i("rev"))
    gm_p = div(sub(i("rev_p"), _cogs("_p")), i("rev_p"))
    gmi = div(gm_p, gm_c)
    aqi = div(sub(one, div(add(i("ca"), i("ppe"), i("sec")), i("ta"))),
              sub(one, div(add(i("ca_p"), i("ppe_p"), i("sec_p")), i("ta_p"))))
    sgi = div(i("rev"), i("rev_p"))
    depi = div(div(i("da_p"), add(i("da_p"), i("ppe_p"))),
               div(i("da"), add(i("da"), i("ppe"))))
    sgai = div(div(_sga(), i("rev")), div(_sga("_p"), i("rev_p")))
    lvgi = div(div(add(i("cl"), i("ncb")), i("ta")),
               div(add(i("cl_p"), i("ncb_p")), i("ta_p")))
    tata = div(sub(i("pco"), i("cfo")), i("ta"))
    terms = [
        mul(c("0.920"), dsri), mul(c("0.528"), gmi), mul(c("0.404"), aqi),
        mul(c("0.892"), sgi), mul(c("0.115"), depi), mul(c("4.679"), tata),
    ]
    positive = add(c("-4.84"), *terms)
    return sub(sub(positive, mul(c("0.172"), sgai)), mul(c("0.327"), lvgi))


def _year_formulas(basis: str) -> list[dict[str, Any]]:
    owners_profit = "pato" if basis == "CONSOLIDATED" else "pat"
    owners_eq = ("eqo", "eqo_p") if basis == "CONSOLIDATED" else ("te", "te_p")
    ebit = add(i("pbeit"), i("fin"))
    cap_emp = add(i("te"), i("ncb"), i("cb"))
    cap_emp_p = add(i("te_p"), i("ncb_p"), i("cb_p"))
    days = c("365")
    inv_days = mul(div(avg("inv", "inv_p"), _cogs()), days)
    rec_days = mul(div(avg("rec", "rec_p"), i("rev")), days)
    pay_days = mul(div(avg("tp", "tp_p"), _cogs()), days)
    f = _formula
    return [
        f("REVENUE_GROWTH_YOY", "Revenue from operations, change on prior year",
          div(sub(i("rev"), i("rev_p")), i("rev_p")), "RATIO", RATIO_Q, basis),
        f("PROFIT_GROWTH_YOY", "Profit for the period, change on prior year (sign-sensitive)",
          div(sub(i("pat"), i("pat_p")), i("pat_p")), "RATIO", RATIO_Q, basis),
        f("RETURN_ON_EQUITY", "Owners' profit / average owners' equity",
          div(i(owners_profit), avg(*owners_eq)), "RATIO", RATIO_Q, basis),
        f("RETURN_ON_CAPITAL_EMPLOYED",
          "EBIT (PBEIT + finance costs) / average (equity + borrowings)",
          div(ebit, div(add(cap_emp, cap_emp_p), c("2"))), "RATIO", RATIO_Q, basis),
        f("RETURN_ON_ASSETS", "Profit for the period / average total assets",
          div(i("pat"), avg("ta", "ta_p")), "RATIO", RATIO_Q, basis),
        f("ASSET_TURNOVER", "Revenue from operations / average total assets",
          div(i("rev"), avg("ta", "ta_p")), "RATIO", RATIO_Q, basis),
        f("DEBT_TO_EQUITY", "(Non-current + current borrowings) / total equity, closing",
          div(add(i("ncb"), i("cb")), i("te")), "RATIO", RATIO_Q, basis),
        f("NET_DEBT", "Borrowings - cash and cash equivalents, closing",
          sub(add(i("ncb"), i("cb")), i("cash")), "INR", AMOUNT_Q, basis),
        f("CURRENT_RATIO", "Current assets / current liabilities, closing",
          div(i("ca"), i("cl")), "RATIO", RATIO_Q, basis),
        f("RECEIVABLE_DAYS", "Average trade receivables / revenue x 365",
          rec_days, "DAYS", DAYS_Q, basis),
        f("INVENTORY_DAYS", "Average inventories / cost of goods sold x 365",
          inv_days, "DAYS", DAYS_Q, basis),
        f("PAYABLE_DAYS", "Average trade payables / cost of goods sold x 365",
          pay_days, "DAYS", DAYS_Q, basis),
        f("CASH_CONVERSION_CYCLE", "Inventory days + receivable days - payable days",
          sub(add(inv_days, rec_days), pay_days), "DAYS", DAYS_Q, basis),
        f("CASH_FLOW_QUALITY", "Net cash from operating activities / profit for the period",
          div(i("cfo"), i("pat")), "RATIO", RATIO_Q, basis),
        f("FREE_CASH_FLOW", "Net cash from operating activities - payments for PP&E",
          sub(i("cfo"), i("capex")), "INR", AMOUNT_Q, basis),
        f("SLOAN_ACCRUAL_RATIO", "(Profit for the period - operating cash flow) / avg assets",
          div(sub(i("pat"), i("cfo")), avg("ta", "ta_p")), "RATIO", RATIO_Q, basis),
        f("RECEIVABLE_GROWTH_OVER_REVENUE_GROWTH",
          "Trade receivables growth minus revenue growth, year on year",
          sub(div(sub(i("rec"), i("rec_p")), i("rec_p")),
              div(sub(i("rev"), i("rev_p")), i("rev_p"))), "RATIO", RATIO_Q, basis),
        f("BENEISH_M_SCORE", "Beneish (1999) eight-variable M-score", _beneish(),
          "SCORE", RATIO_Q, basis),
    ]


def _pl_formulas(basis: str, cadence: str, tag: str) -> list[dict[str, Any]]:
    ebitda = sub(add(i("pbeit"), i("fin"), i("da")), i("oi"))
    f = _formula
    return [
        f("GROSS_MARGIN", "(Revenue - cost of goods sold) / revenue",
          div(sub(i("rev"), _cogs()), i("rev")), "RATIO", RATIO_Q, basis, cadence, tag),
        f("EBITDA", "PBEIT + finance costs + D&A - other income",
          ebitda, "INR", AMOUNT_Q, basis, cadence, tag),
        f("EBITDA_MARGIN", "EBITDA / revenue from operations",
          div(ebitda, i("rev")), "RATIO", RATIO_Q, basis, cadence, tag),
        f("EBIT_MARGIN", "(PBEIT + finance costs - other income) / revenue",
          div(sub(add(i("pbeit"), i("fin")), i("oi")), i("rev")), "RATIO", RATIO_Q,
          basis, cadence, tag),
        f("NET_PROFIT_MARGIN", "Profit for the period / revenue from operations",
          div(i("pat"), i("rev")), "RATIO", RATIO_Q, basis, cadence, tag),
        f("INTEREST_COVERAGE", "(PBEIT + finance costs) / finance costs",
          div(add(i("pbeit"), i("fin")), i("fin")), "RATIO", RATIO_Q, basis, cadence, tag),
    ]


def _scenarios(basis: str) -> list[dict[str, Any]]:
    rev = {"name": "rev", "metric": "REVENUE_FROM_OPERATIONS", "basis": basis, "unit": "INR",
           "cadence": "YEAR", "period_role": "CURRENT"}
    growth = {"name": "growth", "unit": "RATIO", "minimum": "-1", "maximum": "10",
              "description": "Your assumed revenue change for the next year, as a ratio "
                             "(0.12 = 12%)."}
    growth2 = {**growth, "name": "growth_2",
               "description": "Your assumed revenue change for the year after, as a ratio."}
    margin = {"name": "net_margin", "unit": "RATIO", "minimum": "-10", "maximum": "1",
              "description": "Your assumed net profit margin for that year, as a ratio."}
    common = {"kind": "SCENARIO", "precision": AMOUNT_Q, "approval": APPROVAL,
              "output_unit": "INR"}
    v = f"{basis.lower()}-1"
    return [
        {**common, "formula_id": "SCENARIO_NEXT_YEAR_REVENUE", "version": v,
         "title": "Revenue next year if revenue changes by your assumed growth",
         "expression": mul(i("rev"), add(c("1"), a("growth"))), "inputs": [rev],
         "assumptions": [growth], "output_kind": "COMPANY_LEVEL_AMOUNT",
         "output_contract": AMOUNT_CONTRACT},
        {**common, "formula_id": "SCENARIO_NEXT_YEAR_PROFIT", "version": v,
         "title": "Profit next year at your assumed growth and net margin",
         "expression": mul(mul(i("rev"), add(c("1"), a("growth"))), a("net_margin")),
         "inputs": [rev], "assumptions": [growth, margin],
         "output_kind": "COMPANY_LEVEL_AMOUNT", "output_contract": AMOUNT_CONTRACT},
        {**common, "formula_id": "SCENARIO_TWO_YEAR_REVENUE", "version": v,
         "title": "Revenue two years out at your two assumed growth rates",
         "expression": mul(mul(i("rev"), add(c("1"), a("growth"))),
                           add(c("1"), a("growth_2"))),
         "inputs": [rev], "assumptions": [growth, growth2],
         "output_kind": "CONDITIONAL_SERIES_VALUE", "output_contract": SERIES_CONTRACT},
    ]


def registry_body() -> dict[str, Any]:
    formulas: list[dict[str, Any]] = []
    for basis in BASES:
        formulas += _year_formulas(basis)
        formulas += _pl_formulas(basis, "YEAR", "")
        formulas += _pl_formulas(basis, "QUARTER", "-quarter")
        formulas += _scenarios(basis)
    return {"version": REGISTRY_VERSION, "output_contracts": list(OUTPUT_CONTRACTS),
            "formulas": formulas}


#: ``app.engine.research_metrics.LIVE_FORMULAS`` is built from ``registry_body()``. This module
#: stays data-only (no import of research_metrics) so the two never import each other.

# =========================================================================================
# reporting calendar -- SEBI LODR Regulation 33(3)
# =========================================================================================

CALENDAR_VERSION = "in-lodr-33-1"
#: Earliest fiscal year generated. Older periods are simply not in the calendar.
FIRST_FISCAL_YEAR_START = date(2000, 4, 1)
#: Reg 33(3)(a): quarterly results within 45 days of quarter end (other than the last
#: quarter); Reg 33(3)(d): annual audited results within 60 days of the financial year end.
QUARTER_DEADLINE_DAYS = 45
ANNUAL_DEADLINE_DAYS = 60


def _fy_quarters(start: date) -> list[Window]:
    y = start.year
    bounds = [(date(y, 4, 1), date(y, 6, 30)), (date(y, 7, 1), date(y, 9, 30)),
              (date(y, 10, 1), date(y, 12, 31)), (date(y + 1, 1, 1), date(y + 1, 3, 31))]
    return [Window(s, e) for s, e in bounds]


def india_lodr_calendar(isins: tuple[str, ...], as_of: date) -> ReportingCalendar:
    """April-March fiscal years (Companies Act 2013 s.2(41)) for Indian issuers (ISIN prefix
    ``INE``). A period is REQUIRED once its LODR deadline has fully passed: ``as_of`` is
    strictly after ``period_end + deadline``. Anything else -- a foreign issuer, a mixed
    family -- gets no calendar (UNKNOWN), never a guessed one. A company with a tribunal-
    approved non-March year is not modelled; that is a stated limit of version 1."""
    if not isins or not all(x.startswith("INE") for x in isins):
        return ReportingCalendar(version=CALENDAR_VERSION, cadences={})
    years: list[Window] = []
    quarters: list[Window] = []
    start = FIRST_FISCAL_YEAR_START
    while start <= as_of:
        years.append(Window(start, date(start.year + 1, 3, 31)))
        quarters.extend(_fy_quarters(start))
        start = date(start.year + 1, 4, 1)

    def due(w: Window, days: int) -> bool:
        return as_of > w.end + timedelta(days=days)

    req_year = next((w for w in reversed(years) if due(w, ANNUAL_DEADLINE_DAYS)), None)
    req_q = next((w for w in reversed(quarters)
                  if due(w, ANNUAL_DEADLINE_DAYS if w.end.month == 3
                         else QUARTER_DEADLINE_DAYS)), None)
    return ReportingCalendar(version=CALENDAR_VERSION, cadences={
        "YEAR": CadenceCalendar(periods=tuple(years), required_latest=req_year),
        "QUARTER": CadenceCalendar(periods=tuple(quarters), required_latest=req_q),
    })
