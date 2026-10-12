"""Indexation for pre-April-2023 debt-MF acquisitions -- docs/TAX_ENGINE.md
"Indexation".

    indexed_cost = cost x (CII_sale_year / CII_purchase_year)

Only reachable in this system's data today for ``MF_DEBT`` (docs/TAX_ENGINE.md also
names property and unlisted assets, but neither is a Phase 1/2 asset class -- per
CLAUDE.md's scope-discipline rule, this module builds for MF_DEBT only; property and
unlisted assets are a Phase 4 seam, not code to write ahead of data that doesn't exist).

Applies only to units ACQUIRED before 1 April 2023 -- units bought on/after that date
are taxed at slab rate with no indexation regardless of how long they are held or when
they are sold (schema/asset_classes.json's MF_DEBT note: "Post 1-Apr-2023 purchases
taxed at slab; pre-2023 indexation applies"). The gate is on the PURCHASE year only; a
pre-cutoff lot sold well after 2023 still gets indexed against whatever year it is
actually sold in.

Like ``app.engine.grandfathering.grandfathered_cost_of_acquisition``, the cutoff check
lives inside the one exported function rather than being left to the caller: any lot can
be passed through unconditionally and the right thing happens, indexed or not.
"""

from __future__ import annotations

from decimal import Decimal

from app.engine.errors import MissingCIIError

#: "Before 1 April 2023" per docs/TAX_ENGINE.md, expressed as the first FY the cutoff
#: excludes rather than as a bare date. Every Indian financial year starts 1 April
#: (app.engine.xirr.financial_year_start), so "acquired before 1 Apr 2023" is exactly
#: "purchase FY is earlier than 2023-24" -- FY 2023-24 contains no date before 1 Apr
#: 2023. Named, and re-derivable independently of GRANDFATHERING_CUTOFF_DATE
#: (app.engine.grandfathering), which is a different cutoff for a different rule, so a
#: future session can find and question either one on its own.
INDEXATION_CUTOFF_FY = "2023-24"


def indexed_cost_of_acquisition(
    cost: Decimal,
    purchase_fy: str,
    sale_fy: str,
    cii_table: dict[str, Decimal],
) -> Decimal:
    """Cost of acquisition to use for a pre-cutoff MF_DEBT lot's gain, indexed for
    inflation between its purchase and sale financial years.

    ``purchase_fy``/``sale_fy`` are Indian FY labels, e.g. ``"2022-23"`` -- the same
    ``"YYYY-YY"`` convention ``cost_inflation_index.financial_year`` is keyed by
    (migration 013) and ``app.engine.dividends``'s ``by_year`` already uses. ``
    cii_table`` is that table already read into a plain ``{financial_year: cii_value}``
    dict -- passed in as data, never queried from inside this function, per the
    engine's layer-purity rule (``app/engine/`` never imports ``app/db/``).

    Returns ``cost`` UNCHANGED -- no indexation applied -- when ``purchase_fy`` is
    ``INDEXATION_CUTOFF_FY`` or later. This is not a missing-data case; slab-rate
    taxation genuinely has no indexed cost to compute for these units, so returning the
    plain cost is correct, not a fallback.

    Raises:
        MissingCIIError: the lot IS eligible for indexation (purchase_fy is before the
            cutoff) but ``purchase_fy`` and/or ``sale_fy`` has no entry in
            ``cii_table``. Never defaults a missing year to 1.0 and never silently
            returns the unindexed cost instead -- per CLAUDE.md rule 4, a missing
            reference value raises rather than understating the eventual capital gain.
    """
    if purchase_fy >= INDEXATION_CUTOFF_FY:
        return cost

    missing = [fy for fy in (purchase_fy, sale_fy) if fy not in cii_table]
    if missing:
        raise MissingCIIError(missing)

    return cost * (cii_table[sale_fy] / cii_table[purchase_fy])


__all__ = ["INDEXATION_CUTOFF_FY", "indexed_cost_of_acquisition"]
