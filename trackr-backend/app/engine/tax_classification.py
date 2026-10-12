"""Asset-class-aware SHORT/LONG classification -- docs/TAX_ENGINE.md
"Holding-period thresholds".

``engine/lots.py`` already classifies EQ's realised gains for Phase 1
(``Realised.term``), but it does so with one hardcoded default
(``DEFAULT_LONG_TERM_DAYS = 365``) applied to every ISIN -- correct today because
Phase 1 builds EQ alone and the low-level FIFO replay never learns an ISIN's
asset_class (that join is deliberately left to the web boundary, see
``app/securities.py``). It does not generalise: MF_DEBT's threshold is 1095 days,
REIT/InvIT moved from 1095 to 365 in Budget 2024, and several classes (FD, PPF_EPF,
gold, jewellery, art among them) have no capital-gains distinction at all --
``schema/asset_classes.json`` marks those ``long_term_days: null``.

``classify_term`` is the general version a Phase 3 session (grandfathering,
indexation, report generation) should call once it needs to classify a lot outside
EQ. It takes the threshold as a parameter rather than reading the file itself, for
the same reason ``engine/lots.py`` does: the engine performs no I/O. A caller reads
``schema/asset_classes.json`` once and passes the resolved value in --
``long_term_days_for`` below does that lookup against an already-parsed file, which
keeps it a pure function too.
"""

from __future__ import annotations

from datetime import timedelta

from app.engine.errors import CapitalGainsNotApplicableError
from app.engine.grandfathering import GRANDFATHERING_CUTOFF_DATE
from app.engine.lots import DEFAULT_LONG_TERM_DAYS
from app.engine.types import AcquisitionDateBasis, Realised, Term

#: Bases whose acquisition date is an evidenced LOWER bound, not the date itself.
PROVISIONAL_BASES = frozenset(
    {AcquisitionDateBasis.PROVISIONAL_RECORD_DATE, AcquisitionDateBasis.PROVISIONAL_EX_DATE}
)


def long_term_days_for(asset_class: str, asset_classes: list[dict]) -> int | None:
    """Look up one class's ``long_term_days`` in a parsed
    ``schema/asset_classes.json``'s ``asset_classes`` list.

    Takes the already-parsed list, not a path -- the engine performs no I/O; a
    caller reads the file once (test setup, or a future startup-time load) and
    passes the parsed list in. Returns ``None`` when the class has no capital-gains
    threshold, which ``classify_term`` below turns into a raised error rather than
    a guessed term.
    """
    entry = next((row for row in asset_classes if row["code"] == asset_class), None)
    if entry is None:
        raise KeyError(f"No asset class {asset_class!r} in schema/asset_classes.json")
    return entry["long_term_days"]


def classify_term(realised: Realised, asset_class: str, long_term_days: int | None) -> Term:
    """SHORT or LONG for one realised lot, given its asset class's threshold.

    Strictly-greater-than, matching ENGINE_SPEC section 3 and ``engine/lots.py``: a
    lot held for exactly ``long_term_days`` is still SHORT; ``long_term_days + 1``
    is the first LONG day.

    ``long_term_days`` must come from the caller (see ``long_term_days_for``),
    resolved from ``schema/asset_classes.json`` for ``asset_class``. Never hardcode
    a class's number here -- that is precisely the bug this function exists to
    prevent, since thresholds move with the annual budget (Budget 2024 cut
    REIT/InvIT from 1095 days to 365).

    Raises ``CapitalGainsNotApplicableError`` when ``long_term_days`` is ``None`` --
    see that error's docstring for which classes this covers today. A realised lot
    with no ``holding_days`` is a caller bug (``compute_realised`` always sets it),
    so that raises plainly too rather than being coerced into a guess.
    """
    if long_term_days is None:
        raise CapitalGainsNotApplicableError(asset_class)
    if realised.holding_days is None:
        raise ValueError(f"Realised lot for {realised.isin} has no holding_days -- cannot classify")
    return Term.LONG if realised.holding_days > long_term_days else Term.SHORT


def classification_sensitive(
    realised: Realised,
    *,
    sensitivity_days: int,
    long_term_days: int = DEFAULT_LONG_TERM_DAYS,
) -> bool:
    """True when a slice drawn from a PROVISIONAL bonus lot would change classification
    if its acquisition date moved FORWARD by up to ``sensitivity_days`` -- ENGINE_SPEC
    section 3 / docs/TAX_ENGINE.md, OD-1 (6 Oct 2026,
    IMPLEMENTER_UNDER_OWNER_DELEGATION).

    Forward only, because a provisional date (record date, else ex-date) is an evidenced
    lower bound: the real allotment can only be the same day or later, so the true
    holding period can only be shorter. Two classifications are checked:

      - STCG/LTCG: LONG today (``holding_days > long_term_days``) but not LONG once
        ``sensitivity_days`` are removed.
      - The 31 Jan 2018 grandfathering cut-off: acquired before
        ``GRANDFATHERING_CUTOFF_DATE`` today, on or after it once moved forward.

    A flag, never a changed figure: ``term`` and ``holding_days`` stay as computed.
    ``sensitivity_days`` is required and comes from the caller (config); the engine
    never reads configuration. Always False for an ALLOTMENT-dated or non-bonus slice.
    """
    if sensitivity_days < 0:
        raise ValueError(f"sensitivity_days must be >= 0, got {sensitivity_days}")
    if realised.acquisition_date_basis not in PROVISIONAL_BASES:
        return False
    if realised.holding_days is None or realised.sell_date is None:
        raise ValueError(
            f"Realised lot for {realised.isin} has no holding_days/sell_date -- "
            "cannot test classification sensitivity"
        )
    term_flips = long_term_days < realised.holding_days <= long_term_days + sensitivity_days
    acquired = realised.sell_date - timedelta(days=realised.holding_days)
    cutoff_flips = (
        acquired < GRANDFATHERING_CUTOFF_DATE
        <= acquired + timedelta(days=sensitivity_days)
    )
    return term_flips or cutoff_flips
