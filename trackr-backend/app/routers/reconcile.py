"""POST /investors/{id}/reconcile -- ENGINE_SPEC section 7, the corporate-action
detector. docs/FULL_VISION_BACKEND_ROADMAP.md section A2: "build it in week one, not as
a tool in month six."
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.auth.dependencies import require_owned_investor
from app.database import get_conn, money, rate
from app.db.repository import load_transactions
from app.engine.reconcile import reconcile
from app.engine.types import Discrepancy
from app.schemas.reconcile import DiscrepancyOut, ReconcileRequest

router = APIRouter(prefix="/investors", tags=["ops"])


def _discrepancy_row(d: Discrepancy) -> dict[str, Any]:
    """One Discrepancy as JSON, per schema/discrepancy.schema.json."""
    return {
        "isin": d.isin,
        "computed_qty": money(d.computed_qty),
        "declared_qty": money(d.declared_qty),
        "difference": money(d.difference),
        "ratio": rate(d.ratio),
        "likely_cause": d.likely_cause,
    }


@router.post(
    "/{investor_id}/reconcile",
    response_model=list[DiscrepancyOut],
    dependencies=[Depends(require_owned_investor)],
)
def reconcile_holdings(
    investor_id: str,
    body: ReconcileRequest,
    connection: Any = Depends(get_conn),
) -> list[dict[str, Any]]:
    """Compare computed holdings against CAS- or broker-declared quantities.

    All cause-heuristic logic lives in ``app.engine.reconcile.reconcile`` -- this handler
    only loads the ledger, translates the wire shape, and formats the result. A clean
    reconciliation is an empty list, never a list of zero-difference rows -- the engine
    already guarantees that; nothing here overrides it.
    """
    transactions = load_transactions(connection, investor_id=investor_id, date_to=body.as_of)
    declared = {h.isin: h.quantity for h in body.holdings}
    discrepancies = reconcile(transactions, declared, as_of=body.as_of)
    return [_discrepancy_row(d) for d in discrepancies]
