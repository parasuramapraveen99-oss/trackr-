"""ITR-format capital gains report -- docs/TAX_ENGINE.md, docs/FEATURE_BACKLOG.md
Phase 3 ("ITR-format capital gains report").

Broader than Schedule 112A (Session 7, LTCG only): this covers STCG too, per
docs/FEATURE_BACKLOG.md listing them as one Phase 3 row and docs/TAX_ENGINE.md's
holding-period table naming both STCG and LTCG for listed equity. Government-form-
shaped output, same "ask, do not guess" discipline as Session 7 applied "with extra
force" -- the field layout below is quoted from primary sources, not reconstructed
from memory or an old sample:

- The field layout and Sl.No. labels (A2ia/A2ibi.../A2ic/A2id/A2ie for STCG s.111A,
  B3a/B3b/B3c for LTCG s.112A) are read directly out of CBDT's own AY 2026-27 ITR-2
  offline utility (``ITR2_AY_26-27_V1.3.xlsm``, downloaded from
  incometax.gov.in/iec/foportal/sites/default/files/2026-08/ITR2_AY_26-27_V1.3.zip,
  the "CG" worksheet) -- the actual macro-enabled workbook CBDT publishes for filers
  and that third-party tax software is itself built against, current for AY 2026-27.
  Column E of that sheet gives the true Sl.No. per item once Excel's merged cells are
  resolved (``openpyxl``, not a raw XML/regex read, which is what surfaced the
  wrong answer on a first pass): row 34 (``E34=2``) is Schedule CG item **A2**,
  "From sale of equity share or unit of equity oriented Mutual Fund (MF) or unit of
  a business trust on which STT is paid (i) 111A (For others) (ii) Sec
  115AD(1)(b)(ii) ... [for FIIs]" -- sub-item (i) is the resident/111A case this
  module builds; row 249 (``E249=3``) is item **B3**, "From sale of equity share in
  a company or unit of equity oriented fund or unit of a business trust on which STT
  is paid under section 112A".
- Cross-confirmed by CBDT's "ITR 2 -- Validation Rules for AY 2026-27 Version 1.0"
  (26 May 2026, incometax.gov.in), whose arithmetic checks reference exactly these
  labels: rule "In schedule CG, Sl. No. A2 biv of STCG Total should be equal to sum
  of A2(bi+bii+biii)" and "In Schedule CG Sl.no. A2e of STCG should be equal to the
  sum of A(2c+2d)" (the doc's prose shorthand for the workbook's own A2ibiv/A2ic/
  A2id/A2ie -- sub-item (i)'s "i" dropped in the rule's plain-English phrasing, but
  unambiguous once matched against the workbook's own named Excel ranges, e.g.
  ``STCG.A2ibiv_TotalDedn``); and "In Schedule CG, Sl. No. B3a LTCG u/s 112A should
  be equal to total of Col. 14 of Schedule 112A" / "Sl.no. B3c of LTCG should be
  equal to B(3a-3b)", which match the workbook cell for B3a verbatim
  (``=ROUND(Total_Balance_112A,0)``) -- i.e. B3a is exactly
  ``app.engine.schedule_112a.Schedule112AReport.total_ltcg``, not a separately
  computed figure.
- The workbook's own B3(a) sub-fields for the "before/after 23 July 2024" LTCG-rate
  split are present as form furniture but hardcoded to literal ``0`` (not formulas)
  in the AY 2026-27 utility -- consistent with secondary reporting that this split
  was removed for the current filing year (FY 2025-26 falls entirely after that
  cutoff) -- so this module does not reproduce it.

Two fields the real form has that this module deliberately does NOT compute, exactly
as Schedule 112A's module left its own expenditure column at a fixed 0 rather than
guessing:
- **A2id**, "Loss to be disallowed under section 94(7) or 94(8)" -- the
  dividend-stripping / bonus-stripping anti-abuse disallowance. No detection logic
  for that scenario exists anywhere in this codebase (Session 5's bonus-stripping
  work makes the *gain* side of bonus stripping correctly computable via FIFO; it
  does not implement the section 94(7)/(8) loss-disallowance rule, which is a
  distinct provision). Always reported as 0 here, which is the correct value for a
  filer who triggered neither, but is a known scope gap, not a computed answer --
  raise, don't guess, if this ever needs to be real.
- **B3b**, "Deduction under sections 54F" -- capital-gains reinvestment exemption.
  Trackr Plus tracks no reinvestment/exemption claims anywhere in the ledger. Always
  0 for the same reason as A2id above.

Everything else in the real Schedule CG -- land/building, slump sale, bonds/
debentures, FII- and non-resident-specific items, DTAA relief, deemed capital gains,
buyback capital loss, and every Sl.No. beyond A2(i) and B3 -- is out of scope, per
CLAUDE.md's MVP scope table (Indian listed equities only, no NR/FII investor type
modeled) and PHASE3_PROMPTS.md's "Not included on purpose" list. ``total_capital_gains``
below is therefore a Trackr-Plus-scoped subset of the real Schedule CG item C
("A9+B12"), not the form's own total -- it equals A2ie+B3c because every other addend
of the real A9/B12 totals is structurally zero for this system's data.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.engine.lots import DEFAULT_LONG_TERM_DAYS
from app.engine.realised import tax_cost, tax_proceeds
from app.engine.schedule_112a import Schedule112AReport
from app.engine.tax_classification import classification_sensitive
from app.engine.types import ZERO, Realised, Term


@dataclass(frozen=True, slots=True)
class ITRCapitalGainsReport:
    """Schedule CG, restricted to Trackr Plus's Phase 1/3 scope (listed equity,
    STT paid). Field names carry the CBDT AY 2026-27 Sl.No. in a trailing comment so
    a filer or CA can find the matching box on the real form; see the module
    docstring for sourcing and for which fields are named-but-unimplemented zeros.
    """

    a2i_full_value_of_consideration: Decimal  # A2ia
    a2i_cost_of_acquisition: Decimal  # A2ibi
    a2i_cost_of_improvement: Decimal  # A2ibii -- always 0, not applicable to securities
    a2i_expenditure_on_transfer: Decimal  # A2ibiii -- always 0, proceeds net of non-STT charges
    a2i_total_deductions: Decimal  # A2ibiv
    a2i_balance: Decimal  # A2ic
    a2i_loss_disallowed_94_7_94_8: Decimal  # A2id -- always 0, NOT implemented, see docstring
    a2i_stcg: Decimal  # A2ie -- net STCG u/s 111A

    b3a_ltcg_112a: Decimal  # B3a -- == Schedule112AReport.total_ltcg
    b3b_deduction_54f: Decimal  # B3b -- always 0, NOT implemented, see module docstring
    b3c_ltcg: Decimal  # B3c -- net LTCG u/s 112A

    total_capital_gains: Decimal  # C, Trackr-Plus-scoped -- A2ie + B3c

    # Not a form field: True when any STCG slice or Schedule 112A row rests on a
    # provisional bonus acquisition date close enough to a boundary that its
    # classification could change (OD-1, ENGINE_SPEC section 3). A flag, never a figure.
    classification_sensitive: bool = False


def build_itr_capital_gains(
    stcg_realised: list[Realised],
    schedule_112a: Schedule112AReport,
    *,
    sensitivity_days: int,
    long_term_days: int = DEFAULT_LONG_TERM_DAYS,
) -> ITRCapitalGainsReport:
    """Build the Schedule CG line items this system can populate.

    ``stcg_realised`` must already be filtered to SHORT-term lots (Session 2's
    ``classify_term``) -- a LONG-term entry raises rather than being silently
    skipped, matching ``build_schedule_112a``'s own precedent for the opposite
    filter mistake.

    ``schedule_112a`` is Session 7's already-built report; B3a is read straight off
    ``total_ltcg`` rather than re-summing anything, since the CBDT workbook's own
    B3a formula is exactly that total (see module docstring).

    ``sensitivity_days`` (required, from the caller's config) feeds
    ``classification_sensitive`` -- see ``app.engine.tax_classification``.
    """
    for realised in stcg_realised:
        if realised.term is not Term.SHORT:
            raise ValueError(
                f"{realised.isin}: ITR capital gains STCG section only accepts "
                f"SHORT-term lots, got term={realised.term}"
            )

    full_value_of_consideration = sum((tax_proceeds(r) for r in stcg_realised), ZERO)
    cost_of_acquisition = sum((tax_cost(r) for r in stcg_realised), ZERO)
    cost_of_improvement = ZERO
    expenditure_on_transfer = ZERO
    total_deductions = cost_of_acquisition + cost_of_improvement + expenditure_on_transfer
    balance = full_value_of_consideration - total_deductions
    loss_disallowed = ZERO
    stcg = balance + loss_disallowed

    b3a = schedule_112a.total_ltcg
    b3b = ZERO
    b3c = b3a - b3b

    return ITRCapitalGainsReport(
        a2i_full_value_of_consideration=full_value_of_consideration,
        a2i_cost_of_acquisition=cost_of_acquisition,
        a2i_cost_of_improvement=cost_of_improvement,
        a2i_expenditure_on_transfer=expenditure_on_transfer,
        a2i_total_deductions=total_deductions,
        a2i_balance=balance,
        a2i_loss_disallowed_94_7_94_8=loss_disallowed,
        a2i_stcg=stcg,
        b3a_ltcg_112a=b3a,
        b3b_deduction_54f=b3b,
        b3c_ltcg=b3c,
        total_capital_gains=stcg + b3c,
        classification_sensitive=any(row.classification_sensitive for row in schedule_112a.rows)
        or any(
            classification_sensitive(
                r, sensitivity_days=sensitivity_days, long_term_days=long_term_days
            )
            for r in stcg_realised
        ),
    )


__all__ = ["ITRCapitalGainsReport", "build_itr_capital_gains"]
