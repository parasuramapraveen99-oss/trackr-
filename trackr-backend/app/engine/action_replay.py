"""Recalculate identified derived entitlements after late imports, without editing trades.

NEVER RAISES (C4, OD-5). A cancelled action, or one whose ex-date is after ``as_of``,
emits nothing. An applied row whose action lost its evidence -- gone from the table,
or no longer passing ``screen()`` -- is KEPT, marked in memory
with ``raw_payload["evidence_withdrawn"] = reason_code``. The strict replays then raise
``CorporateActionEvidenceError`` naming that ISIN, and ``compute_holdings_partitioned``
lists it in ``skipped``, so one withdrawn action never blanks a whole portfolio and its
quantity is never silently dropped or guessed.
"""

from dataclasses import replace
from datetime import date

from app.engine.corporate_actions import (
    FRACTION_SETTLED_IN_CASH_KEY,
    CorporateAction,
    free_share_effect,
    screen,
    whole_shares,
)
from app.engine.errors import CorporateActionEvidenceError
from app.engine.holdings import quantity_held_before
from app.engine.lots import ALLOTMENT_DATE_KEY, EVIDENCE_WITHDRAWN_KEY, RECORD_DATE_KEY
from app.engine.types import ZERO, Source, Transaction, TxnType

__all__ = ["CorporateActionEvidenceError", "replay_derived_actions"]

#: The anchor row names an action that is no longer in the reference table.
ACTION_MISSING = "ACTION_MISSING"
#: Retired 2026-10-08 (owner decision: whole shares credited, fraction settled in cash --
#: see ``whole_shares``). Kept so stored reason codes and the API enum still parse.
FRACTIONAL_ENTITLEMENT = "FRACTIONAL_ENTITLEMENT"


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def replay_derived_actions(
    transactions: list[Transaction], actions: list[CorporateAction], as_of: date | None = None
) -> list[Transaction]:
    def action_id(t: Transaction) -> str | None:
        if t.source is not Source.CORP_ACTION:
            return None
        return (t.raw_payload or {}).get("auto_corporate_action_id")

    tagged = [t for t in transactions if action_id(t)]
    if not tagged:
        return transactions
    safe, failures = screen(actions)
    by_id = {a.id: a for a in safe}
    all_by_id = {a.id: a for a in actions}
    # The first guard each unsafe action tripped is its reason code (UNVERIFIED, ...).
    failure_code: dict[str | None, str] = {}
    for f in failures:
        failure_code.setdefault(f.action.id, f.code)
    rebuilt = [t for t in transactions if not action_id(t)]

    def withdrawn(original: Transaction, reason_code: str) -> Transaction:
        return replace(
            original,
            raw_payload={**original.raw_payload, EVIDENCE_WITHDRAWN_KEY: reason_code},
        )

    def effective(t: Transaction) -> date:
        recorded = all_by_id.get(action_id(t))
        return recorded.ex_date if recorded is not None else t.txn_date

    for original in sorted(tagged, key=effective):
        recorded = all_by_id.get(action_id(original))
        if recorded is None:
            rebuilt.append(withdrawn(original, ACTION_MISSING))
            continue
        if recorded.ex_date > (as_of or date.today()) or recorded.cancelled:
            continue  # not yet effective, or an explicit, versioned cancellation
        action = by_id.get(recorded.id)
        if action is None:
            rebuilt.append(withdrawn(original, failure_code[recorded.id]))
            continue
        account_rows = [t for t in rebuilt if t.account_id == original.account_id]
        held = quantity_held_before(account_rows, action.isin, action.ex_date)
        if held <= ZERO:
            continue
        isin, kind, entitled = free_share_effect(action, held)
        quantity, fraction = whole_shares(entitled)
        if quantity <= ZERO:
            continue
        rebuilt.append(
            replace(
                original,
                isin=isin,
                txn_date=action.ex_date,
                txn_type=TxnType(kind),
                quantity=quantity,
                raw_payload={
                    **original.raw_payload,
                    ALLOTMENT_DATE_KEY: _iso(action.allotment_date),
                    RECORD_DATE_KEY: _iso(action.record_date),
                    **(
                        {FRACTION_SETTLED_IN_CASH_KEY: format(fraction.normalize(), "f")}
                        if fraction
                        else {}
                    ),
                },
            )
        )
    return rebuilt
