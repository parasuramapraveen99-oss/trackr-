"""Schedule 112A generation -- docs/TAX_ENGINE.md, docs/FEATURE_BACKLOG.md Phase 3.

Government-form-shaped output, not internal engine logic: the column layout below is
CBDT's, not this codebase's invention, so it is quoted verbatim from primary sources
retrieved 2026-08-20 rather than reconstructed from memory (CLAUDE.md's "ask, do not
guess" rule applied "with extra force," per the session that requested this module):

- Column-by-column layout (col 1a, 2-14): CBDT's official CSV-upload instructions,
  "Instructions for filling Schedule 112A/115AD(1)(b)(iii)(P)"
  (static.incometax.gov.in/iec/foservices/assets/itr-shared/documents/
  112A_115AD_CSV_Instructions.pdf), authored 25 May 2026 -- current for AY 2026-27.
- Cross-confirmed by CBDT's "e-Filing ITR 2 Validation Rules for AY 2026-27 V1.0"
  (incometax.gov.in), whose row-84-through-90 arithmetic checks (col 6 = col4*col5,
  col 7 = higher(col8, col9), col 9 = lower(col11, col6), col 11 = col4*col10,
  col 13 = col7+col12, col 14 = col6-col13) match the CSV instructions exactly.
- The Rs 1,25,000 annual exemption (``SECTION_112A_EXEMPTION`` below): confirmed
  current for FY 2025-26 / AY 2026-27 by multiple independent secondary sources
  (tax2win, bajajfinserv, casahuja.com's AY-2026-27-specific walkthrough) as the
  Finance (No. 2) Act 2024 figure, raised from Rs 1,00,000 and unchanged since. No
  official CBDT page stating the number in plain text was reachable at time of
  writing (incometaxindia.gov.in returned 403 to automated fetches) -- this is
  therefore one notch weaker sourcing than the field layout above and should be
  re-confirmed as part of Session 9's CA sign-off rather than trusted blindly for a
  real filing.

As of the same CBDT instructions, a share/unit ACQUIRED on or before 31 Jan 2018 must
be reported scrip-wise (one row per ISIN/sale, "BE" in column 1a) because the
grandfathering computation genuinely needs per-security FMV; a share/unit acquired
after that date is reported as a SINGLE CONSOLIDATED row across the whole return
("AE" in column 1a, ISIN literally "INNOTREQUIRD", name literally "CONSOLIDATED",
quantity/sale-price/FMV columns left blank) because no per-security detail is needed
once grandfathering cannot apply. ``build_schedule_112a`` below reproduces both shapes.

Consumes Sessions 2-6's output rather than computing gains itself: it calls straight
into ``app.engine.grandfathering.grandfathered_cost_of_acquisition`` for column 7/9
(never re-deriving that formula here), and expects the caller to have already
filtered to LTCG-classified realised lots (``app.engine.tax_classification`` /
``Realised.term == Term.LONG``) and to supply ``fmv_31jan2018`` values sourced the
way Session 3 already established.

Two things this module deliberately does NOT do, both out of the stated scope of the
task that created it:
- It does not compute tax payable (the 12.5% Section 112A rate, surcharge, cess) --
  only Schedule 112A's own rows plus the exemption arithmetic that feeds the next
  schedule (Schedule CG / Schedule SI) upstream of tax computation.
- Column 12 ("Expenditure wholly and exclusively in connection with transfer") is
  always reported as 0: ``app.engine.realised.tax_proceeds`` is already net of
  sale-side charges OTHER than STT (see ``lots.py``'s ``proceeds_per_unit`` and
  ``realised.py``'s ``tax_proceeds`` -- STT is added back per Income Tax Act Section
  48, which disallows it specifically, but brokerage/GST/stamp stay netted out), so
  there is no separate expenditure figure left to report without double-counting.
  The resulting column-14 gain is still numerically correct either way (net proceeds
  minus cost equals gross proceeds minus cost minus expenditure); only the column-6
  vs column-12 split of an already-known total differs from a filer who has the raw
  charges figure to hand. Worth revisiting if this ever needs to match a broker
  contract note's own gross/net breakdown exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from app.engine.grandfathering import (
    GRANDFATHERING_CUTOFF_DATE,
    grandfathered_cost_of_acquisition,
)
from app.engine.lots import DEFAULT_LONG_TERM_DAYS
from app.engine.realised import tax_cost, tax_proceeds
from app.engine.tax_classification import classification_sensitive
from app.engine.types import ZERO, Lot, Realised, Term

#: The Section 112A annual exemption: LTCG on listed equity / equity-oriented funds /
#: business trust units up to this amount in a financial year is not taxed, only the
#: excess is. Raised from Rs 1,00,000 to Rs 1,25,000 by Finance (No. 2) Act 2024,
#: effective FY 2024-25 (AY 2025-26) onward. See the module docstring for sourcing and
#: its caveat -- this number has changed before (introduced by Finance Act 2018,
#: raised in 2024) and will change again, so it is named here rather than a bare
#: literal, exactly as CLAUDE.md requires for CII/threshold values, so a future
#: session can find and question it.
SECTION_112A_EXEMPTION = Decimal("125000")

#: A placeholder ISIN/name pair CBDT's own CSV instructions specify for the single
#: consolidated "AE" row -- not a real security, so callers must not look these up
#: against a security master.
CONSOLIDATED_AE_ISIN = "INNOTREQUIRD"
CONSOLIDATED_AE_NAME = "CONSOLIDATED"


@dataclass(frozen=True, slots=True)
class Schedule112ARow:
    """One row of Schedule 112A. Field names describe the data; the comment on each
    names the CBDT column number so the mapping back to the government form is
    never ambiguous.
    """

    acquired_on_or_before_cutoff: bool  # col 1a: True -> 'BE', False -> 'AE'
    isin: str  # col 2
    scrip_name: str  # col 3
    quantity: Decimal | None  # col 4 -- None for the consolidated 'AE' row
    sale_price_per_unit: Decimal | None  # col 5 -- None for the consolidated 'AE' row
    full_value_of_consideration: Decimal  # col 6
    cost_of_acquisition_without_indexation: Decimal  # col 7
    cost_of_acquisition: Decimal  # col 8
    grandfathering_floor: Decimal | None  # col 9 -- None when no FMV was sourced
    fmv_per_unit_31jan2018: Decimal | None  # col 10
    total_fmv_31jan2018: Decimal | None  # col 11
    expenditure_on_transfer: Decimal  # col 12 -- always 0, see module docstring
    total_deductions: Decimal  # col 13
    balance: Decimal  # col 14 -- this row's reportable LTCG
    # Not CBDT columns -- the OD-1 provisional-bonus-date flags (ENGINE_SPEC section 3).
    # A 'BE' row carries its lot's basis; the consolidated 'AE' row carries None.
    acquisition_date_basis: str | None = None
    # True when the row's classification (LTCG, or BE vs AE) could change if a
    # provisional bonus acquisition date moved forward -- any member, for the 'AE' row.
    classification_sensitive: bool = False


@dataclass(frozen=True, slots=True)
class Schedule112AReport:
    """The whole schedule: every row, plus the exemption arithmetic that feeds
    Schedule CG. ``total_ltcg`` is the sum callers should reconcile against
    Schedule CG's B3a ("LTCG u/s 112A" = total of column 14 across all rows).
    """

    rows: list[Schedule112ARow]
    total_ltcg: Decimal
    exemption_applied: Decimal
    taxable_ltcg: Decimal


def build_schedule_112a(
    ltcg_realised: list[Realised],
    fmv_31jan2018: dict[str, Decimal],
    scrip_names: dict[str, str],
    *,
    sensitivity_days: int,
    long_term_days: int = DEFAULT_LONG_TERM_DAYS,
) -> Schedule112AReport:
    """Build the Schedule 112A row structure and apply the annual exemption.

    ``ltcg_realised`` must already be filtered to LONG-term lots (Session 2's
    ``classify_term``) -- a SHORT-term entry raises rather than being silently
    skipped, since that would hide a caller bug that understates reported LTCG.

    ``fmv_31jan2018`` is ISIN -> fair market value per unit as on 31 Jan 2018
    (Session 1/3's reference data), nullable per ISIN exactly as
    ``grandfathered_cost_of_acquisition`` already treats it -- most securities never
    need one. ``scrip_names`` is ISIN -> display name, required only for lots
    acquired on or before the grandfathering cutoff (every other lot is reported
    under the CBDT-specified consolidated placeholder, never a real name lookup).

    A lot's acquisition date is recovered from ``Realised.sell_date -
    Realised.holding_days`` (the same identity ``compute_lots``'s ``_consume``
    used to set ``holding_days`` in the first place) rather than re-fetched from
    ``compute_lots`` separately, since both values are already carried on
    ``Realised``.

    ``sensitivity_days`` (required, from the caller's config) feeds
    ``classification_sensitive`` for each row; see that function.
    """

    def sensitive(r: Realised) -> bool:
        return classification_sensitive(
            r, sensitivity_days=sensitivity_days, long_term_days=long_term_days
        )

    rows: list[Schedule112ARow] = []
    consolidated_ae: list[Realised] = []

    for realised in ltcg_realised:
        if realised.term is not Term.LONG:
            raise ValueError(
                f"{realised.isin}: Schedule 112A only accepts LTCG-classified lots, "
                f"got term={realised.term}"
            )
        if realised.isin is None or realised.sell_date is None or realised.holding_days is None:
            raise ValueError(
                "Realised lot is missing isin/sell_date/holding_days -- cannot build "
                "a Schedule 112A row from it"
            )

        open_date = realised.sell_date - timedelta(days=realised.holding_days)
        acquired_on_or_before_cutoff = open_date < GRANDFATHERING_CUTOFF_DATE

        if not acquired_on_or_before_cutoff:
            consolidated_ae.append(realised)
            continue

        if realised.isin not in scrip_names:
            raise ValueError(
                f"No scrip name supplied for {realised.isin} -- required for a 'BE' "
                "Schedule 112A row (acquired on or before 31 Jan 2018)"
            )

        cost_per_unit_actual = tax_cost(realised) / realised.quantity
        sale_price_per_unit = tax_proceeds(realised) / realised.quantity
        fmv_per_unit = fmv_31jan2018.get(realised.isin)

        lot_stub = Lot(
            isin=realised.isin,
            open_date=open_date,
            quantity_open=realised.quantity,
            cost_per_unit=cost_per_unit_actual,
        )
        grandfathered_per_unit = grandfathered_cost_of_acquisition(
            lot_stub, fmv_per_unit, sale_price_per_unit
        )

        full_value_of_consideration = tax_proceeds(realised)
        cost_of_acquisition = tax_cost(realised)
        cost_of_acquisition_without_indexation = grandfathered_per_unit * realised.quantity
        total_fmv = fmv_per_unit * realised.quantity if fmv_per_unit is not None else None
        grandfathering_floor = (
            min(total_fmv, full_value_of_consideration) if total_fmv is not None else None
        )
        total_deductions = cost_of_acquisition_without_indexation
        balance = full_value_of_consideration - total_deductions

        rows.append(
            Schedule112ARow(
                acquired_on_or_before_cutoff=True,
                isin=realised.isin,
                scrip_name=scrip_names[realised.isin],
                quantity=realised.quantity,
                sale_price_per_unit=sale_price_per_unit,
                full_value_of_consideration=full_value_of_consideration,
                cost_of_acquisition_without_indexation=cost_of_acquisition_without_indexation,
                cost_of_acquisition=cost_of_acquisition,
                grandfathering_floor=grandfathering_floor,
                fmv_per_unit_31jan2018=fmv_per_unit,
                total_fmv_31jan2018=total_fmv,
                expenditure_on_transfer=ZERO,
                total_deductions=total_deductions,
                balance=balance,
                acquisition_date_basis=realised.acquisition_date_basis,
                classification_sensitive=sensitive(realised),
            )
        )

    if consolidated_ae:
        full_value_of_consideration = sum((tax_proceeds(r) for r in consolidated_ae), ZERO)
        cost_of_acquisition = sum((tax_cost(r) for r in consolidated_ae), ZERO)
        total_deductions = cost_of_acquisition
        balance = full_value_of_consideration - total_deductions

        rows.append(
            Schedule112ARow(
                acquired_on_or_before_cutoff=False,
                isin=CONSOLIDATED_AE_ISIN,
                scrip_name=CONSOLIDATED_AE_NAME,
                quantity=None,
                sale_price_per_unit=None,
                full_value_of_consideration=full_value_of_consideration,
                cost_of_acquisition_without_indexation=cost_of_acquisition,
                cost_of_acquisition=cost_of_acquisition,
                grandfathering_floor=None,
                fmv_per_unit_31jan2018=None,
                total_fmv_31jan2018=None,
                expenditure_on_transfer=ZERO,
                total_deductions=total_deductions,
                balance=balance,
                classification_sensitive=any(sensitive(r) for r in consolidated_ae),
            )
        )

    total_ltcg = sum((row.balance for row in rows), ZERO)
    exemption_applied = min(total_ltcg, SECTION_112A_EXEMPTION) if total_ltcg > ZERO else ZERO
    taxable_ltcg = max(ZERO, total_ltcg - SECTION_112A_EXEMPTION)

    return Schedule112AReport(
        rows=rows,
        total_ltcg=total_ltcg,
        exemption_applied=exemption_applied,
        taxable_ltcg=taxable_ltcg,
    )


__all__ = [
    "CONSOLIDATED_AE_ISIN",
    "CONSOLIDATED_AE_NAME",
    "SECTION_112A_EXEMPTION",
    "Schedule112ARow",
    "Schedule112AReport",
    "build_schedule_112a",
]
