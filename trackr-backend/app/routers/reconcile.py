"""POST /investors/{id}/reconcile -- ENGINE_SPEC section 7, the corporate-action
detector. docs/FULL_VISION_BACKEND_ROADMAP.md section A2: "build it in week one, not as
a tool in month six."
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from app.auth.dependencies import require_owned_investor
from app.database import get_conn, money, rate
from app.db import holdings as snapshots
from app.db import reconciliation as runs
from app.db.repository import account_belongs_to, load_transactions
from app.db.security_alias import verified_map
from app.engine.reconcile import reconcile
from app.engine.reconciliation_report import compare_history, holdings_report
from app.engine.types import Discrepancy, Transaction, TxnType
from app.schemas.reconcile import DiscrepancyOut, ReconcileRequest, ReconciliationRunIn

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
        "status": d.status,
        "error": d.error,
        "error_date": d.error_date.isoformat() if d.error_date else None,
        "deficit": money(d.deficit),
        "reason_code": d.reason_code,
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
    account_id = str(body.account_id) if body.account_id else None
    if account_id is None:
        with connection.cursor() as cur:
            cur.execute("SELECT id FROM account WHERE investor_id=%s", (investor_id,))
            accounts = cur.fetchall()
        if len(accounts) != 1:
            raise HTTPException(422, "Select one account to reconcile")
        account_id = str(accounts[0][0])
    _owned_account(connection, investor_id, account_id)
    transactions = load_transactions(
        connection, investor_id=investor_id, account_id=account_id, date_to=body.as_of
    )
    declared = {h.isin: h.quantity for h in body.holdings}
    discrepancies = reconcile(transactions, declared, as_of=body.as_of)
    return [_discrepancy_row(d) for d in discrepancies]


def _owned_account(conn, investor_id, account_id):
    if not account_belongs_to(conn, account_id, investor_id):
        raise HTTPException(404, "No such account for this investor")


@router.get("/{investor_id}/reconcile/statements", dependencies=[Depends(require_owned_investor)])
def reconciliation_statements(
    investor_id: str, account_id: UUID, connection: Any = Depends(get_conn)
):
    _owned_account(connection, investor_id, str(account_id))
    return snapshots.statement_dates(connection, investor_id, str(account_id))


@router.post("/{investor_id}/reconcile/runs", dependencies=[Depends(require_owned_investor)])
def create_reconciliation_run(
    investor_id: str, body: ReconciliationRunIn, connection: Any = Depends(get_conn)
):
    account_id = str(body.account_id)
    _owned_account(connection, investor_id, account_id)
    transactions = load_transactions(
        connection, investor_id=investor_id, account_id=account_id, date_to=body.as_of
    )
    common = {
        "account_id": account_id,
        "as_of": body.as_of.isoformat(),
        "source": body.source,
        "mode": body.mode,
        "coverage": "Agreement with supplied evidence only; not a certification of "
        "complete history, acquisition costs, capital gains or tax.",
        "cost_basis_status": "NOT_VERIFIED",
        "historical_run": "Saved at run time; rerun after changing ledger or evidence.",
    }
    aliases = verified_map(connection, body.as_of)
    used_isins = {t.isin for t in body.evidence} | {h.isin for h in body.holdings}
    if body.mode == "HISTORY":
        ledger = [
            t
            for t in transactions
            if body.date_from <= t.txn_date <= body.as_of and t.txn_type in body.transaction_types
        ]
        evidence = [
            replace(Transaction(**t.model_dump()), isin=aliases.get(t.isin, t.isin))
            for t in body.evidence
        ]
        report = {
            **common,
            **compare_history(ledger, evidence),
            "date_from": body.date_from.isoformat(),
            "transaction_types": [str(t) for t in body.transaction_types],
            "ledger_issues": [
                _discrepancy_row(d)
                for d in reconcile(transactions, {}, body.as_of)
                if d.status == "UNCOMPUTABLE"
            ],
        }
    else:
        declared = {h.isin: h.quantity for h in body.holdings}
        statement = None
        if body.use_snapshot:
            try:
                declared, statement = snapshots.selected_statement(
                    connection,
                    account_id,
                    body.as_of,
                    str(body.statement_id) if body.statement_id else None,
                )
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            if not declared:
                raise HTTPException(404, "No imported holdings observations on this exact date")
        used_isins.update(declared)
        canonical_declared = {}
        for isin, quantity in declared.items():
            canonical = aliases.get(isin, isin)
            if canonical in canonical_declared:
                raise HTTPException(
                    422,
                    "Two statement identifiers resolve to the same security; "
                    "review the original statement before combining them",
                )
            canonical_declared[canonical] = quantity
        declared = canonical_declared
        pending = []
        by_id = {str(t.id): t for t in transactions}
        for item in body.pending_settlements:
            t = by_id.get(str(item.transaction_id))
            if t is None or t.txn_type not in (TxnType.BUY, TxnType.SELL):
                raise HTTPException(
                    422,
                    "Pending trade must be a BUY or SELL in this account "
                    "on or before the statement date",
                )
            pending.append(t)
        rows = holdings_report(
            transactions, declared, body.as_of, complete=body.complete_statement, pending=pending
        )
        report = {
            **common,
            "rows": [_discrepancy_row(r) for r in rows],
            "counts": dict(Counter(r.status for r in rows)),
            "complete_statement": body.complete_statement,
            "basis": body.basis,
            "statement": statement,
            "settlement_evidence": [p.model_dump(mode="json") for p in body.pending_settlements],
            "settlement_note": "Ledger uses trade dates. Only supplied settlement "
            "evidence explains timing differences; no settlement "
            "calendar or settlement completeness is assumed.",
        }
    report["identity_mappings"] = {k: v for k, v in aliases.items() if k in used_isins}
    request = body.model_dump(mode="json")
    # Freeze the exact replay inputs too: corrections/reference revisions can change
    # later reads without changing the historical meaning of this run.
    from dataclasses import asdict
    from decimal import Decimal

    from fastapi.encoders import jsonable_encoder

    request["ledger_at_run"] = jsonable_encoder(
        [asdict(t) for t in transactions], custom_encoder={Decimal: str}
    )
    return runs.save_run(connection, investor_id, account_id, request, report)


@router.get("/{investor_id}/reconcile/runs", dependencies=[Depends(require_owned_investor)])
def reconciliation_runs(investor_id: str, connection: Any = Depends(get_conn)):
    return runs.list_runs(connection, investor_id)


@router.get(
    "/{investor_id}/reconcile/runs/{run_id}", dependencies=[Depends(require_owned_investor)]
)
def reconciliation_run(investor_id: str, run_id: UUID, connection: Any = Depends(get_conn)):
    report = runs.get_run(connection, investor_id, run_id)
    if report is None:
        raise HTTPException(404, "No such reconciliation run")
    return report
