"""DB-layer glue for verified corporate actions: CRUD on the ``corporate_action``
reference table, and staging matched corrections into the exact ``import_batch``/
``import_row`` pipeline every file adapter already uses.

Nothing here writes to ``transaction`` directly. Promotion is
``app.db.imports.promote_batch``, reused unchanged -- a batch staged by this module is
indistinguishable, at review/promote/discard time, from one staged by a file upload.
That is the point: the "adapter never writes to the ledger" rule
(``app.db.imports``'s own docstring) holds here with zero new enforcement code, because
this was never given a second path to the ledger in the first place.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any

from app.db import audit
from app.db.imports import ACTION_BATCH_STAGED, _txn_to_payload
from app.db.prices import latest_prices, prices_on_date
from app.db.repository import load_transactions, save_transactions
from app.engine.corporate_actions import (
    FRACTION_SETTLED_IN_CASH_KEY,
    CorpActionType,
    CorporateAction,
    Verification,
    check_price_consistency,
    check_sourced,
    free_share_effect,
    screen,
    whole_shares,
)
from app.engine.dedupe import make_dedupe_key
from app.engine.holdings import quantity_held_before
from app.engine.types import ZERO, ImportSummary, Source, Transaction, TxnType
from app.ingest.corporate_action_matcher import stage_corporate_actions as _match

logger = logging.getLogger("corporate_actions")

#: import_batch.adapter value for a batch this module staged, as opposed to a real
#: file-upload adapter's name. Carries no parser, no file, no document_id -- review and
#: promote do not need to know the difference, but a human reading import_batch does.
ADAPTER_NAME = "corporate_action_matcher"

_SELECT_COLUMNS = (
    "id, isin, action_type::text, ex_date, bonus_new, bonus_for, "
    "face_value_before, face_value_after, resulting_isin, "
    "demerger_new, demerger_for, price_cum, price_ex, "
    "source, source_url, verification::text, notes, "
    "price_waived, price_waiver_reason, allotment_date, record_date, cancelled, "
    "automatic_application"
)


def _row_to_action(row: tuple) -> CorporateAction:
    (
        id_, isin, action_type, ex_date, bonus_new, bonus_for,
        face_value_before, face_value_after, resulting_isin,
        demerger_new, demerger_for, price_cum, price_ex,
        source, source_url, verification, notes,
        price_waived, price_waiver_reason, allotment_date, record_date, cancelled,
        automatic_application,
    ) = row
    return CorporateAction(
        id=str(id_),
        isin=isin,
        action_type=CorpActionType(action_type),
        ex_date=ex_date,
        bonus_new=bonus_new,
        bonus_for=bonus_for,
        face_value_before=face_value_before,
        face_value_after=face_value_after,
        resulting_isin=resulting_isin,
        demerger_new=demerger_new,
        demerger_for=demerger_for,
        price_cum=price_cum,
        price_ex=price_ex,
        source=source,
        source_url=source_url,
        verification=Verification(verification),
        notes=notes or "",
        allotment_date=allotment_date,
        record_date=record_date,
        cancelled=cancelled,
        automatic_application=automatic_application,
        price_waived=price_waived,
        price_waiver_reason=price_waiver_reason or "",
    )


def add_corporate_action(
    conn: Any, action: CorporateAction, actor_uid: str | None = None
) -> str:
    """Insert one corporate action -- the manual/bulk entry path, same product shape as
    MProfit's Bulk Corporate Action form (date, security, action type, the primitive
    ratio facts).

    ``action`` must already construct cleanly: ``CorporateAction.__post_init__`` raises
    ``CorporateActionRatioError`` for an unusable record before this is ever called --
    an incomplete row fails at entry, not at match time. The table's own
    ``UNIQUE (isin, action_type, ex_date)`` constraint is the same DUPLICATE guard
    ``app.engine.corporate_actions.screen()`` applies to a batch already in memory,
    enforced here too since two humans can enter the same notice independently.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO corporate_action (
                isin, action_type, ex_date, bonus_new, bonus_for,
                face_value_before, face_value_after, resulting_isin,
                demerger_new, demerger_for, price_cum, price_ex,
                source, source_url, verification, notes,
                price_waived, price_waiver_reason, allotment_date, record_date, cancelled,
                automatic_application
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                action.isin, action.action_type.value, action.ex_date,
                action.bonus_new, action.bonus_for,
                action.face_value_before, action.face_value_after, action.resulting_isin,
                action.demerger_new, action.demerger_for,
                action.price_cum, action.price_ex,
                action.source, action.source_url,
                action.verification.value, action.notes or None,
                action.price_waived, action.price_waiver_reason or None,
                action.allotment_date, action.record_date, action.cancelled,
                action.automatic_application,
            ),
        )
        action_id = str(cur.fetchone()[0])

    audit.record(
        conn,
        audit.ACTION_CORPORATE_ACTION_ADDED,
        "corporate_action",
        action_id,
        actor_uid,
        {
            "isin": action.isin,
            "action_type": action.action_type.value,
            "ex_date": action.ex_date.isoformat(),
            "source": action.source,
        },
    )

    # RETROACTIVE SWEEP. A brand-new VERIFIED entry can explain a gap that has been
    # sitting in someone's ledger since before this entry existed -- exactly the
    # KAMAHOLD/GENSOL situation this feature was built for. Every account that has ever
    # transacted this ISIN gets checked immediately, not just future imports.
    # ``apply_known_actions_for_account`` is idempotent, so this is safe to call even
    # for accounts that already have the resulting transaction.
    if action.verification is Verification.VERIFIED:
        _sweep_holders(conn, action.isin, actor_uid)

    return action_id


def _sweep_holders(conn: Any, isin: str, actor_uid: str | None) -> None:
    """Apply known actions to every account that has ever transacted ``isin`` or one of
    its VERIFIED retired identifiers -- the ledger keeps the ISIN a trade was booked
    under, so a holder who bought before an ISIN change is found only through the
    alias. Each account is re-derived from its own ledger only."""
    identifiers = [isin, *_superseded_by(conn, isin)]
    with conn.cursor() as cur:
        cur.execute(
            'SELECT DISTINCT account_id FROM "transaction" WHERE isin = ANY(%s)',
            (identifiers,),
        )
        account_ids = [row[0] for row in cur.fetchall()]
    for account_id in account_ids:
        apply_known_actions_for_account(conn, str(account_id), actor_uid=actor_uid)


def list_corporate_actions(
    conn: Any, isins: list[str] | None = None, verification: Verification | None = None
) -> list[CorporateAction]:
    """Every corporate action on file, optionally restricted to ``isins`` -- matched on
    EITHER the action's own ``isin`` or its ``resulting_isin``, since a DEMERGER is
    filed under the parent security but is what explains a discrepancy on the child --
    and optionally to one ``verification`` status, e.g. every ``DRAFT`` row for the
    ``Exceptions`` surface (``list_pending_corporate_actions`` below)."""
    query = f"SELECT {_SELECT_COLUMNS} FROM corporate_action"
    clauses: list[str] = []
    params: list[Any] = []
    if isins is not None:
        clauses.append("(isin = ANY(%s) OR resulting_isin = ANY(%s))")
        params += [isins, isins]
    if verification is not None:
        clauses.append("verification = %s")
        params.append(verification.value)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY ex_date"
    with conn.cursor() as cur:
        cur.execute(query, params)
        return [_row_to_action(row) for row in cur.fetchall()]


def get_corporate_action(conn: Any, action_id: str) -> CorporateAction | None:
    """One corporate action by id, or ``None`` -- the direct lookup the ``Exceptions``
    router endpoints (verify/dispute) need after mutating a row, instead of listing
    everything and filtering client-side."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_SELECT_COLUMNS} FROM corporate_action WHERE id = %s", (action_id,))
        row = cur.fetchone()
        return _row_to_action(row) if row else None


def list_pending_corporate_actions(conn: Any) -> list[CorporateAction]:
    """Every ``DRAFT`` row -- the ``Exceptions`` surface. Deliberately named apart from
    ``list_corporate_actions(verification=DRAFT)``, which it wraps: on a healthy feed
    this should be short and rare, an exception list, not a queue someone works through
    daily -- see ``docs/ENGINE_SPEC.md``'s note on why it's named that way."""
    return list_corporate_actions(conn, verification=Verification.DRAFT)


def verify_corporate_action(conn: Any, action_id: str, actor_uid: str | None = None) -> None:
    """Promote a ``DRAFT`` row to ``VERIFIED`` -- the one legitimate ``UPDATE`` this
    module performs. Safe against CLAUDE.md's append-only rule because it targets
    ``corporate_action`` (maintained reference/master data with its own review state),
    never ``transaction`` (the append-only ledger) -- this function writes no
    transaction row itself; it only unlocks ``add_corporate_action``'s retroactive
    sweep, reused unchanged, to do that.

    Used two ways: a human confirming an ``Exceptions`` entry by hand, and the
    automated feed's own self-correcting retry (``refresh_corporate_action_feed``)
    promoting a row that failed its guards on an earlier run and now passes -- same
    function, same audit trail, the caller is what differs.

    Refuses a ``DISPUTED`` row. Dispute is terminal (docs/ENGINE_SPEC.md section 3; the
    route already answers 409 on it), and this guard is what makes that true of the
    function rather than only of the one HTTP route that happens to call it -- the same
    contract ``dispute_dividend_event`` states for ``dividend_event``. It runs before
    the revision guard because a disputed row that also has a blocked revision is
    disputed, and that is the more precise thing to say.
    """
    from app.ingest.action_source import ActionConflictError, ActionIngestionError
    state = conn.execute(
        "SELECT verification::text FROM corporate_action WHERE id = %s", (action_id,)
    ).fetchone()
    if state is not None and state[0] == Verification.DISPUTED.value:
        raise ActionConflictError("a disputed corporate action is terminal", "DISPUTED")
    if revision_blocks_verification(conn, action_id):
        raise ActionIngestionError("Latest event revision lacks confirmed accepted evidence")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE corporate_action SET verification = %s WHERE id = %s AND verification != %s",
            (Verification.VERIFIED.value, action_id, Verification.VERIFIED.value),
        )
        if cur.rowcount == 0:
            return  # already VERIFIED, or no such row -- nothing to sweep

        cur.execute(
            f"SELECT {_SELECT_COLUMNS} FROM corporate_action WHERE id = %s", (action_id,)
        )
        action = _row_to_action(cur.fetchone())

    audit.record(
        conn, audit.ACTION_CORPORATE_ACTION_VERIFIED, "corporate_action", action_id, actor_uid,
        {"isin": action.isin, "action_type": action.action_type.value},
    )

    _sweep_holders(conn, action.isin, actor_uid)


def revision_blocks_verification(conn: Any, action_id: str) -> bool:
    """True when the action's latest revision is not a confirmed, accepted notice -- an
    operator-file revision still pending, a cancellation, or an NSE notice that
    contradicted the stored terms (``_record_feed_conflict``). An action with no revision
    at all is not blocked."""
    latest = conn.execute(
        "SELECT o.result,r.terms->>'status' FROM corporate_action_revision r "
        "JOIN corporate_action_observation o ON o.id=r.observation_id "
        "WHERE r.action_id=%s ORDER BY r.recorded_at DESC,r.revision DESC LIMIT 1",
        (action_id,),
    ).fetchone()
    return bool(latest) and (latest[0] != "ACCEPTED" or latest[1] != "CONFIRMED")


class PriceEvidenceWaiverError(ValueError):
    """Raised by ``waive_price_evidence`` when a row does not qualify -- distinct from
    a plain 404/lookup miss, which the caller (a router) turns into its own HTTP status."""


def waive_price_evidence(
    conn: Any, action_id: str, reason: str, actor_uid: str | None = None
) -> None:
    """The one legitimate way to unblock a BONUS/SPLIT whose price evidence is not
    merely missing but PERMANENTLY UNOBTAINABLE -- see ``025_price_evidence_waiver.sql``.

    GATED ON VERIFIED COVERAGE, NOT ON A DATE. An earlier version refused any ex_date on
    or after ``NSE_BHAVCOPY_EARLIEST``, assuming everything inside that window was
    obtainable. That assumption was wrong and this system disproved it: GUJTHEM's October
    2023 split sat comfortably inside the window and had no evidence anywhere, because the
    security was BSE-only at the time and only NSE was being asked. A date says nothing
    about whether a particular security's price can be fetched; only having actually
    looked does.

    So a waiver now requires one of two positive findings, never an assumption:

      - the archive has ANSWERED for this security without carrying it
        (``price_absent``, via ``app.db.prices.absent_on_or_before``) -- a retired or
        never-listed identifier no future fetch will produce; or
      - the ex_date precedes ``NSE_BHAVCOPY_EARLIEST``, the floor ``backfill_days``
        clamps to, so no fetch will ever be ATTEMPTED and an absence can never be
        recorded. A structural dead end rather than an unexplored one.

    And it refuses in the two cases that used to slip through in the other direction:

      - evidence already EXISTS -- then verify it, do not waive it; and
      - nobody has looked yet -- an unfetched price is not an unobtainable one, and
        waiving it would convert a temporary gap into a permanent assertion. Run a
        backfill and let the normal self-correcting retry resolve it.

    Still never a blanket bypass: a waiver removes only the price check, never
    ``check_sourced`` or ``check_verified``, which ``screen()`` applies afterwards exactly
    as before. Requires a non-empty ``reason`` -- an audited human judgement call needs a
    stated basis, not just a click.
    """
    from app.db.prices import absent_on_or_before
    from app.prices.pipe import NSE_BHAVCOPY_EARLIEST

    if not reason.strip():
        raise PriceEvidenceWaiverError("a waiver needs a stated reason")

    action = get_corporate_action(conn, action_id)
    if action is None:
        raise PriceEvidenceWaiverError(f"no such corporate action: {action_id}")
    if action.action_type not in (CorpActionType.BONUS, CorpActionType.SPLIT):
        raise PriceEvidenceWaiverError(
            f"price evidence is not required for {action.action_type.value}; nothing to waive"
        )
    if not action.source or not action.source_url:
        raise PriceEvidenceWaiverError("cannot waive an unsourced action -- add a source first")

    cum, ex = _price_evidence(conn, action.isin, action.ex_date)
    if cum is not None and ex is not None:
        raise PriceEvidenceWaiverError(
            f"price evidence for {action.isin} on {action.ex_date} is already on file "
            f"(cum={cum}, ex={ex}) -- verify this action, do not waive it"
        )

    # The same identifiers _price_evidence itself searches, so "we looked" means the same
    # thing in both places: a retired ISIN's absence is the security's absence.
    relevant = [action.isin, *_superseded_by(conn, action.isin)]
    confirmed_absent = bool(absent_on_or_before(conn, relevant, action.ex_date))
    never_attempted = action.ex_date < NSE_BHAVCOPY_EARLIEST

    if not (confirmed_absent or never_attempted):
        raise PriceEvidenceWaiverError(
            f"no price evidence for {action.isin} on {action.ex_date}, but the archive has "
            "not been asked for those dates either -- an unfetched price is not an "
            "unobtainable one. Run a backfill for that window first; the pending-row retry "
            "resolves it automatically if the data is there"
        )

    # WHICH finding justified this, recorded structurally. The two are not equally
    # durable: a confirmed absence is permanent, while "below the floor" is true only
    # relative to a constant designed to be lowered, so that one carries the floor it
    # rested on and can be re-examined later. See migration 028 and stale_price_waivers.
    basis = "ABSENCE_CONFIRMED" if confirmed_absent else "BELOW_ARCHIVE_FLOOR"
    floor = None if confirmed_absent else NSE_BHAVCOPY_EARLIEST

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE corporate_action SET price_waived = true, price_waiver_reason = %s, "
            "price_waiver_basis = %s, price_waiver_floor = %s, verification = %s "
            "WHERE id = %s AND verification != %s",
            (
                reason,
                basis,
                floor,
                Verification.VERIFIED.value,
                action_id,
                Verification.DISPUTED.value,
            ),
        )
        if cur.rowcount == 0:
            return  # DISPUTED -- a waiver cannot resurrect a rejected row

    audit.record(
        conn, audit.ACTION_CORPORATE_ACTION_PRICE_WAIVED, "corporate_action", action_id,
        actor_uid, {"isin": action.isin, "action_type": action.action_type.value, "reason": reason},
    )

    _sweep_holders(conn, action.isin, actor_uid)


def stale_price_waivers(conn: Any) -> list[tuple[str, str, date]]:
    """Waivers whose premise has EXPIRED -- returns ``(action_id, isin, ex_date)``.

    Only ``BELOW_ARCHIVE_FLOOR`` waivers can go stale, and only in one way: the waiver
    said "no fetch will ever be attempted for this date", which was true relative to
    ``NSE_BHAVCOPY_EARLIEST`` at the time. Lower that floor past the ex_date -- an
    explicitly supported follow-up -- and the statement becomes false, while the waiver
    goes on asserting it.

    ``ABSENCE_CONFIRMED`` waivers never appear here: the archive answered and did not
    carry the security, and no later change to our own floor alters that.

    Reporting, not mutation. ``refresh_corporate_action_feed`` acts on the result, so a
    caller that only wants to know can ask without changing anything.
    """
    from app.prices.pipe import NSE_BHAVCOPY_EARLIEST

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, isin, ex_date FROM corporate_action "
            "WHERE price_waived = true AND price_waiver_basis = 'BELOW_ARCHIVE_FLOOR' "
            "AND ex_date >= %s",
            (NSE_BHAVCOPY_EARLIEST,),
        )
        return [(row[0], row[1], row[2]) for row in cur.fetchall()]


def revoke_stale_price_waivers(conn: Any, actor_uid: str | None = None) -> dict[str, Any]:
    """Re-examine every expired waiver and put each back on an honest footing.

    Two outcomes per row, and NEITHER leaves a false assertion standing:

      - the price pipe can now corroborate it -> the waiver is cleared and the real
        evidence is written, so the action stands on data instead of on an override;
      - it still cannot -> the waiver is cleared and the row returns to ``DRAFT`` in
        ``Exceptions``, where an unverifiable action belongs. It is not re-waived
        automatically: the original basis is gone, and whether a NEW basis exists is a
        question for the evidence, not for this function.

    This is what stops a lowered floor from silently validating old overrides, and it is
    the same self-correcting posture the pending-row retry already has -- run from the
    same place, for the same reason.
    """
    revoked, reverified = 0, 0
    for action_id, isin, ex_date in stale_price_waivers(conn):
        cum, ex = _price_evidence(conn, isin, ex_date)
        with conn.cursor() as cur:
            if cum is not None and ex is not None:
                cur.execute(
                    "UPDATE corporate_action SET price_waived = false, "
                    "price_waiver_basis = NULL, price_waiver_floor = NULL, "
                    "price_cum = %s, price_ex = %s WHERE id = %s",
                    (cum, ex, action_id),
                )
                reverified += 1
            else:
                cur.execute(
                    "UPDATE corporate_action SET price_waived = false, "
                    "price_waiver_basis = NULL, price_waiver_floor = NULL, "
                    "verification = %s WHERE id = %s",
                    (Verification.DRAFT.value, action_id),
                )
                revoked += 1
        audit.record(
            conn, audit.ACTION_CORPORATE_ACTION_PRICE_WAIVED, "corporate_action",
            action_id, actor_uid,
            {"isin": isin, "waiver_premise_expired": True,
             "outcome": "reverified" if cum and ex else "returned_to_exceptions"},
        )
        conn.commit()
    return {"waivers_reverified": reverified, "waivers_returned_to_exceptions": revoked}


def dispute_corporate_action(
    conn: Any, action_id: str, actor_uid: str | None = None, reason: str = ""
) -> None:
    """Mark a row ``DISPUTED`` -- permanently excludes it from ever matching
    (``screen()``'s ``check_verified`` guard) and, just as importantly, from the
    automated feed's retry sweep ever re-attempting it. The only way out of
    ``DISPUTED`` is a human entering a corrected record; this function never deletes
    the row, so a bad feed notice stays visible as a record of what was rejected and
    why, not silently gone."""
    with conn.cursor() as cur:
        note = f"\nDISPUTED: {reason}" if reason else "\nDISPUTED"
        cur.execute(
            "UPDATE corporate_action SET verification = %s, "
            "notes = COALESCE(NULLIF(notes, ''), '') || %s WHERE id = %s",
            (Verification.DISPUTED.value, note, action_id),
        )
    audit.record(
        conn, audit.ACTION_CORPORATE_ACTION_DISPUTED, "corporate_action", action_id, actor_uid,
        {"reason": reason},
    )


def stage_corporate_actions(
    conn: Any,
    investor_id: str,
    account_id: str,
    declared_holdings: dict[str, Decimal],
    actor_uid: str | None = None,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Reconcile this account against ``declared_holdings``, match against verified
    corporate actions on file, and stage the result -- the MProfit "Apply" step: never
    silent, always landing in the same review-then-confirm flow
    (``/ingest/stage/{batch_id}`` -> ``/promote``) a file upload goes through.

    Idempotent the way a file upload is: ``app.db.imports.stage_batch`` short-circuits
    on ``UNIQUE (account_id, sha256)`` so re-uploading identical bytes returns the
    existing batch rather than a duplicate. There is no file here to hash, so this
    function hashes what was actually matched instead and applies the identical rule by
    hand -- running this again with nothing changed returns the same batch, not a
    second one sitting alongside it.
    """
    transactions = load_transactions(conn, investor_id=investor_id, account_id=account_id)
    isins = sorted({t.isin for t in transactions} | set(declared_holdings))
    actions = list_corporate_actions(conn, isins)

    result = _match(transactions, declared_holdings, actions, account_id, as_of=as_of)

    digest = hashlib.sha256(
        json.dumps(
            {
                "matched": [
                    {
                        "isin": r.transaction.isin,
                        "txn_type": str(r.transaction.txn_type),
                        "quantity": str(r.transaction.quantity),
                        "source_ref": r.transaction.source_ref,
                    }
                    for r in result.rows
                ],
                "as_of": as_of.isoformat() if as_of else None,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM import_batch WHERE sha256 = %s AND account_id = %s",
            (digest, account_id),
        )
        existing = cur.fetchone()
        if existing:
            return {
                "batch_id": str(existing[0]),
                "parsed": result.parsed,
                "rejected": result.rejected,
                "notes": result.notes,
            }

        cur.execute(
            """
            INSERT INTO import_batch (investor_id, account_id, adapter, filename, sha256,
                                      row_count, rejected_count, notes)
            VALUES (%s,%s,%s,NULL,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                investor_id, account_id, ADAPTER_NAME, digest,
                result.parsed, result.rejected, json.dumps(result.notes),
            ),
        )
        batch_id = str(cur.fetchone()[0])

        for staged in result.rows:
            cur.execute(
                """
                INSERT INTO import_row (batch_id, row_number, confidence, dedupe_key, payload)
                VALUES (%s,%s,%s,%s,%s)
                """,
                (
                    batch_id, staged.row_number, staged.confidence,
                    make_dedupe_key(staged.transaction),
                    json.dumps(
                        {"transaction": _txn_to_payload(staged.transaction), "raw": staged.raw}
                    ),
                ),
            )
        for rejection in result.rejections:
            cur.execute(
                """
                INSERT INTO import_row (batch_id, row_number, status, payload, reason)
                VALUES (%s,%s,'REJECTED',%s,%s)
                """,
                (
                    batch_id, rejection.row_number,
                    json.dumps({"raw": rejection.raw}), rejection.reason,
                ),
            )

    audit.record(
        conn,
        ACTION_BATCH_STAGED,
        "import_batch",
        batch_id,
        actor_uid,
        {"adapter": ADAPTER_NAME, "parsed": result.parsed, "rejected": result.rejected},
    )

    return {
        "batch_id": batch_id,
        "parsed": result.parsed,
        "rejected": result.rejected,
        "notes": result.notes,
    }


_TXN_TYPE = {
    "BONUS": TxnType.BONUS,
    "SPLIT_ADJ": TxnType.SPLIT_ADJ,
    "DEMERGER_IN": TxnType.DEMERGER_IN,
}


def apply_known_actions_for_account(
    conn: Any, account_id: str, actor_uid: str | None = None
) -> ImportSummary:
    """Apply every VERIFIED, guard-passing corporate action on file to this account's
    ledger directly -- no declared-holdings statement, no staging, no promote click.

    THE SAFETY ARGUMENT, stated once here because it is the reason this is allowed to
    skip the review-then-promote flow every other write to the ledger goes through.
    ``screen()`` (unchanged, reused) already requires a VERIFIED, sourced action whose
    recorded ratio is corroborated by the actual cum/ex price move -- three independent
    checks, not one. Given that, applying it is not a guess: a real corporate action
    credits EVERY eligible holder of record automatically and unconditionally (that is
    what "corporate action" means -- nobody opts in), so the only remaining question is
    eligibility, which ``quantity_held_before`` answers directly from this account's own
    ledger with no external input needed. This is a stronger safety position than
    ``stage_corporate_actions``'s reconcile-against-a-declared-statement path, not a
    weaker one -- that path exists for DISCOVERING an action nothing verified explains
    yet; this one is for APPLYING one that is already fully verified.

    IDEMPOTENT on TWO independent levels, deliberately, not one. ``source_ref`` matches
    the exact string ``app.ingest.corporate_action_matcher``'s staged rows build, so the
    two paths' ``dedupe_key`` values agree and ``save_transactions``'s
    ``ON CONFLICT DO NOTHING`` absorbs a re-run. But a dedupe_key match requires an
    EXACT string, and this table's ``source``/``source_url`` text is free-form and can
    legitimately be edited or reworded later -- so this also checks the account's own
    ledger directly for an existing ``CORP_ACTION`` row on the same (isin, ex_date,
    txn_type) before ever building a candidate, which recognises "this was already
    applied" regardless of exactly what text produced it the first time. That check is
    the one this function actually depends on for correctness; the dedupe_key agreement
    is a second, redundant backstop, not the only thing standing between here and a
    silently duplicated bonus.

    Generated rows carry the source action's ID. The read boundary recalculates these
    derived entitlements after late imports without editing stored broker history.
    Older untagged adjustments still require explicit correction evidence.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM account WHERE id=%s FOR UPDATE", (account_id,))
        if cur.fetchone() is None:
            raise ValueError("Corporate action account does not exist")
    transactions = load_transactions(conn, account_id=account_id)
    if not transactions:
        return ImportSummary()

    isins = sorted({t.isin for t in transactions})
    from app.db.security_alias import verified_map

    aliases = verified_map(conn)
    actions = [replace(a, isin=aliases.get(a.isin, a.isin))
               for a in list_corporate_actions(conn)
               if aliases.get(a.isin, a.isin) in isins]
    safe_actions, _failures = screen(actions)

    # The primary idempotency check -- content-based, not string-based. A CORP_ACTION
    # row already sitting at this exact (isin, ex_date, txn_type) means some path
    # (this one, a prior run, or a human's manual/staged entry) already applied this
    # action here, whatever source_ref text it happened to carry.
    already_applied = {
        (t.isin, t.txn_date, t.txn_type)
        for t in transactions
        if t.source is Source.CORP_ACTION
    }

    to_insert: list[Transaction] = []
    for action in safe_actions:
        if (action.cancelled or not action.automatic_application or action.ex_date > date.today()
                or action.action_type not in (CorpActionType.BONUS, CorpActionType.SPLIT)):
            continue
        if sum(a.isin == action.isin and a.ex_date == action.ex_date
               for a in safe_actions) > 1:
            conn.execute(
                "INSERT INTO reference_account_issue(account_id,action_id,isin,reason) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (account_id, action.id, action.isin,
                 "Combined actions require ordered terms and price basis evidence"),
            )
            continue
        with conn.cursor() as cur:
            cur.execute("DELETE FROM reference_account_issue WHERE account_id=%s AND action_id=%s",
                        (account_id, action.id))
        held_qty = quantity_held_before(transactions, action.isin, action.ex_date)
        if held_qty <= ZERO:
            continue  # not a holder of record -- this action does not apply here
        isin, txn_type, entitled = free_share_effect(action, held_qty)
        # Whole shares credited, the fraction settled in cash and named on the row
        # (owner decision 2026-10-08; ``whole_shares``).
        quantity, fraction = whole_shares(entitled)
        if quantity <= ZERO:
            continue
        if (isin, action.ex_date, _TXN_TYPE[txn_type]) in already_applied:
            continue
        to_insert.append(
            Transaction(
                isin=isin,
                txn_date=action.ex_date,
                txn_type=_TXN_TYPE[txn_type],
                quantity=quantity,
                price=ZERO,
                account_id=account_id,
                source=Source.CORP_ACTION,
                # SAME format app.ingest.corporate_action_matcher's staged rows use --
                # deliberately, not incidentally: dedupe_key is derived from this
                # string, so the auto-apply sweep and the manual stage/review/promote
                # path must produce an IDENTICAL source_ref for the same event, or the
                # two paths fail to recognise each other's work and double-credit it.
                source_ref=f"{action.source} {action.source_url}",
                raw_payload={
                    "auto_corporate_action_id": action.id,
                    "corporate_action_allotment_date": (
                        action.allotment_date.isoformat() if action.allotment_date else None
                    ),
                    **(
                        {FRACTION_SETTLED_IN_CASH_KEY: format(fraction.normalize(), "f")}
                        if fraction
                        else {}
                    ),
                },
            )
        )

        # A later event in this sweep must see earlier entitlements too.
        transactions.append(to_insert[-1])

    summary = save_transactions(conn, to_insert, actor_uid=actor_uid)
    if summary.inserted:
        audit.record(
            conn,
            audit.ACTION_CORPORATE_ACTION_AUTO_APPLIED,
            "account",
            account_id,
            actor_uid,
            {
                "inserted": summary.inserted,
                "isins": sorted({t.isin for t in to_insert}),
            },
        )
    return summary


def apply_known_actions_for_investor(
    conn: Any, investor_id: str, actor_uid: str | None = None
) -> ImportSummary:
    """``apply_known_actions_for_account`` across every account this investor has --
    holdings are computed per account (advised vs self-directed, one broker vs
    another), so eligibility has to be checked per account too, not on the investor's
    combined ledger."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM account WHERE investor_id = %s", (investor_id,))
        account_ids = [row[0] for row in cur.fetchall()]

    total = ImportSummary()
    for account_id in account_ids:
        result = apply_known_actions_for_account(conn, str(account_id), actor_uid)
        total = ImportSummary(
            inserted=total.inserted + result.inserted,
            skipped_duplicates=total.skipped_duplicates + result.skipped_duplicates,
            rejected=total.rejected + result.rejected,
        )
    return total


#: NSE's corporate-filings API is INTERIM -- see app.ingest.corporate_action_feed's
#: module docstring. Recorded here as the ``source`` every auto-fetched row carries, so
#: a human reading the reference table can see where a given entry actually came from.
_FEED_SOURCE_NAME = "NSE corporate-filings API"
_FEED_SOURCE_URL = "https://www.nseindia.com/companies-listing/corporate-filings-actions"


def _price_evidence(conn: Any, isin: str, ex_date: date) -> tuple[Decimal | None, Decimal | None]:
    """(price_cum, price_ex) for a BONUS/SPLIT candidate, from bhavcopy already stored
    by this system's own daily price pipe -- never fetched fresh here, never guessed.

    cum = the most recent close ON OR BEFORE the day before ex_date (``latest_prices``'s
    walk-back handles a preceding weekend/holiday); ex = the EXACT ex_date close, no
    carry-forward (``prices_on_date`` -- the same distinction
    ``app.routers.holdings._portfolio`` relies on for "has today's bhavcopy settled
    yet?"). Either can legitimately be ``None`` if bhavcopy hasn't caught up yet -- not
    an error, just not yet resolvable; the caller lands the candidate in ``Exceptions``
    and the next feed run retries it.
    """
    cum_day = date.fromordinal(ex_date.toordinal() - 1)
    cum = latest_prices(conn, [isin], as_of=cum_day).get(isin)
    ex = prices_on_date(conn, [isin], ex_date).get(isin)

    # A split that CHANGES THE ISIN puts cum and ex on different identifiers: the old one
    # trades up to the change and the new one from it, so a single-ISIN lookup finds an ex
    # price and no cum price and the guard fails for a reason that has nothing to do with
    # the ratio. A security's price history spans its identifiers exactly as its
    # transactions do (app.db.repository.load_transactions), so follow the same VERIFIED
    # aliases backwards for the cum side only -- the asymmetry is the point, since after
    # the ex-date it is the new identifier that trades.
    if cum is None:
        for predecessor in _superseded_by(conn, isin):
            cum = latest_prices(conn, [predecessor], as_of=cum_day).get(predecessor)
            if cum is not None:
                break
    return cum, ex


def _superseded_by(conn: Any, isin: str) -> list[str]:
    """ISINs that a VERIFIED alias points AT ``isin`` -- its retired identifiers.

    A database that has not run migration 027 raises ``UndefinedTable`` here. That is a
    deployment fault to surface, never "no aliases": treating it as empty silently split
    one renamed holding into two.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT old_isin FROM security_alias WHERE current_isin = %s "
            "AND verification = 'VERIFIED'",
            (isin,),
        )
        return [row[0] for row in cur.fetchall()]


def _evaluate_bonus_split(
    conn: Any,
    isin: str,
    action_type: CorpActionType,
    ex_date: date,
    bonus_new: int | None,
    bonus_for: int | None,
    face_value_before: Decimal | None,
    face_value_after: Decimal | None,
    notes: str,
    source: str = _FEED_SOURCE_NAME,
    source_url: str = _FEED_SOURCE_URL,
) -> CorporateAction:
    """Build a candidate BONUS/SPLIT ``CorporateAction`` with real price evidence
    attached, tentatively marked VERIFIED so ``check_sourced``/``check_price_consistency``
    can be asked "if this were verified, would it pass?" -- the caller
    (``refresh_corporate_action_feed``, ``app.db.action_ingestion``) is what actually
    decides the stored verification, based on the answer.

    ``source``/``source_url`` default to the NSE-notices feed's own identity; pass
    explicit values for a different candidate source (the operator file import does)
    so the reference table always records which path actually found it.
    """
    price_cum, price_ex = _price_evidence(conn, isin, ex_date)
    return CorporateAction(
        isin=isin, action_type=action_type, ex_date=ex_date,
        source=source, source_url=source_url,
        verification=Verification.VERIFIED,
        bonus_new=bonus_new, bonus_for=bonus_for,
        face_value_before=face_value_before, face_value_after=face_value_after,
        price_cum=price_cum, price_ex=price_ex, notes=notes,
    )


#: ``corporate_action_observation.provider`` for NSE notice rows. Must equal the name
#: ``app.reference_worker.RecordingActionSource`` records each fetched row under, so the
#: writer below updates that same observation rather than adding a second one.
_NSE_OBSERVATION_PROVIDER = "NSE"

#: Outcomes of one NSE bonus/split row, counted by ``refresh_corporate_action_feed``.
_VERIFIED, _EXCEPTION, _CONFLICT, _ON_FILE, _UNREGISTERED = (
    "verified", "exception", "conflict", "on_file", "unregistered",
)


def _observe_feed_row(
    conn: Any,
    row: Any,
    rights_reference: str | None,
    *,
    result: str,
    reason_code: str,
    action_id: str | None = None,
    reason: str | None = None,
) -> str:
    """Record (or update) the observation for one parsed NSE row; return its ID.

    Keyed on the exact source row (``FeedRow.raw``), so a row the worker already recorded
    as PENDING gets its outcome and ``action_id`` here rather than a duplicate."""
    from app.db.action_ingestion import record_observation
    from app.ingest.corporate_action_feed import NSE_PARSER_VERSION

    if row.raw is None:
        raise ValueError("FeedRow carries no source row; only parse_corporate_action_feed rows")
    return record_observation(
        conn,
        provider=_NSE_OBSERVATION_PROVIDER,
        raw=row.raw,
        parser_version=NSE_PARSER_VERSION,
        classification=row.action_type.value,
        result=result,
        reason_code=reason_code,
        isin=row.isin,
        ex_date=row.ex_date,
        action_id=action_id,
        reason=reason,
        rights_reference=rights_reference,
    )


def _term_differences(stored: CorporateAction, row: Any) -> list[str]:
    """The ratio terms (and a record date both sides state) on which a notice disagrees
    with the stored action for the same (isin, action_type, ex_date)."""
    differences = [
        name
        for name in ("bonus_new", "bonus_for", "face_value_before", "face_value_after")
        if getattr(stored, name) != getattr(row, name)
    ]
    if stored.record_date and row.record_date and stored.record_date != row.record_date:
        differences.append("record_date")
    return differences


def _record_feed_conflict(
    conn: Any, row: Any, stored: CorporateAction, observation_id: str, differences: list[str]
) -> None:
    """Append a ``corporate_action_revision`` for a contradicting NSE notice.

    NSE issues no event ID, so the revision is keyed on the notice's own natural key
    ``isin:action_type:ex_date`` -- the same key ``corporate_action`` is unique on. The
    stored row is never modified. Because the revision's status is CONFLICT,
    ``revision_blocks_verification`` keeps a DRAFT row from being auto-verified until a
    human resolves it; a VERIFIED row keeps its verification. Idempotent per
    observation: re-reading the same contradicting notice adds nothing.
    """
    event_id = f"{row.isin}:{row.action_type.value}:{row.ex_date.isoformat()}"
    already = conn.execute(
        "SELECT 1 FROM corporate_action_revision WHERE observation_id=%s", (observation_id,)
    ).fetchone()
    if already:
        return
    terms = {
        "status": "CONFLICT",
        "differs": differences,
        "bonus_new": row.bonus_new,
        "bonus_for": row.bonus_for,
        "face_value_before": str(row.face_value_before) if row.face_value_before else None,
        "face_value_after": str(row.face_value_after) if row.face_value_after else None,
        "record_date": row.record_date.isoformat() if row.record_date else None,
        "subject": row.raw_subject,
    }
    conn.execute(
        """INSERT INTO corporate_action_revision
               (provider,event_id,component_id,revision,observation_id,terms,action_id)
           SELECT %s,%s,%s,COALESCE(max(revision),0)+1,%s,%s::jsonb,%s
             FROM corporate_action_revision
            WHERE provider=%s AND event_id=%s AND component_id=%s""",
        (
            _NSE_OBSERVATION_PROVIDER, event_id, row.action_type.value, observation_id,
            json.dumps(terms, sort_keys=True), stored.id,
            _NSE_OBSERVATION_PROVIDER, event_id, row.action_type.value,
        ),
    )


def _land_feed_row(
    conn: Any, row: Any, rights_reference: str | None, actor_uid: str | None
) -> str:
    """Write one parsed NSE bonus/split row; return its outcome. Does not commit.

    A transaction-scoped advisory lock on the notice's natural key serialises two feed
    runs reading the same notice, so the lookup-then-insert below cannot race itself.
    """
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
        (f"corporate_action:{row.isin}:{row.action_type.value}:{row.ex_date.isoformat()}",),
    )
    found = conn.execute(
        f"SELECT {_SELECT_COLUMNS} FROM corporate_action "
        "WHERE isin=%s AND action_type=%s AND ex_date=%s",
        (row.isin, row.action_type.value, row.ex_date),
    ).fetchone()
    if found is not None:
        stored = _row_to_action(found)
        differences = _term_differences(stored, row)
        if differences:
            # Never merged by guesswork and never overwritten: the stored row stands,
            # the contradiction is recorded and blocks automatic verification.
            observation_id = _observe_feed_row(
                conn, row, rights_reference, result="CONFLICT", reason_code="TERMS_CONFLICT",
                action_id=stored.id,
                reason=f"Notice differs from the stored action on {', '.join(differences)}",
            )
            _record_feed_conflict(conn, row, stored, observation_id, differences)
            logger.warning(
                "corporate_action_feed_conflict",
                extra={"provider": _NSE_OBSERVATION_PROVIDER, "isin": row.isin,
                       "observation_id": observation_id, "reason_code": "TERMS_CONFLICT",
                       "action_id": stored.id, "differs": differences},
            )
            return _CONFLICT
        result, reason_code = {
            Verification.VERIFIED: ("ACCEPTED", "ALREADY_PROCESSED"),
            Verification.DRAFT: ("REVIEW", "AWAITING_VERIFICATION"),
            Verification.DISPUTED: ("REJECTED", "ALREADY_PROCESSED"),
        }[stored.verification]
        _observe_feed_row(
            conn, row, rights_reference, result=result, reason_code=reason_code,
            action_id=stored.id,
        )
        return _ON_FILE

    if conn.execute("SELECT 1 FROM security WHERE isin=%s", (row.isin,)).fetchone() is None:
        # The feed is all-market: a notice for a security nobody here holds is expected.
        # corporate_action.isin is a foreign key to security, so it cannot be stored yet.
        _observe_feed_row(
            conn, row, rights_reference, result="REVIEW", reason_code="SECURITY_NOT_REGISTERED"
        )
        return _UNREGISTERED

    candidate = replace(
        _evaluate_bonus_split(
            conn, row.isin, row.action_type, row.ex_date,
            row.bonus_new, row.bonus_for, row.face_value_before, row.face_value_after,
            notes=row.raw_subject,
        ),
        record_date=row.record_date,
    )
    failure = check_sourced(candidate) or check_price_consistency(candidate)
    if failure is not None:
        candidate = replace(
            candidate, verification=Verification.DRAFT,
            notes=f"{candidate.notes}\n{failure.code}: {failure.detail}",
        )
    action_id = add_corporate_action(conn, candidate, actor_uid=actor_uid)
    if failure is not None:
        _observe_feed_row(
            conn, row, rights_reference, result="REVIEW", reason_code="AWAITING_VERIFICATION",
            action_id=action_id,
        )
        return _EXCEPTION
    _observe_feed_row(
        conn, row, rights_reference, result="ACCEPTED", reason_code="PRICE_CORROBORATED",
        action_id=action_id,
    )
    return _VERIFIED


def refresh_corporate_action_feed(
    conn: Any,
    start: date,
    end: date,
    source: Any | None = None,
    actor_uid: str | None = None,
    rights_reference: str | None = None,
) -> dict[str, Any]:
    """Fetch NSE's corporate-actions notices for ``[start, end]``, auto-verify every
    BONUS/SPLIT that passes ``check_sourced``/``check_price_consistency``, and land
    everything else in ``Exceptions`` -- see ``docs/ENGINE_SPEC.md`` §7 "Automated
    ingestion" for the full trust-model writeup this implements.

    Every parsed row updates its ``corporate_action_observation`` with its outcome and
    the ``action_id`` it produced or matched. A notice whose terms contradict the stored
    action for the same (isin, action_type, ex_date) is recorded CONFLICT with a
    revision; the stored row is never overwritten.

    SELF-CORRECTING RETRY, not just fresh notices: every existing ``DRAFT`` row is
    re-evaluated on every run too (fresh price evidence looked up again), so a
    candidate that failed only for want of a not-yet-published bhavcopy close
    self-promotes to VERIFIED the moment that price lands, with no human step and no
    need for a brand-new notice to re-trigger it. A DRAFT whose latest revision is a
    conflict is skipped until a human resolves it.

    A fetch or format failure raises. One bad row never discards the others: each row
    commits on its own, a failed row is recorded REJECTED with its exception type, and
    the run raises ``ActionIngestionError`` at the end if any row failed.

    ``rights_reference`` is provenance for an injected ``source``. When ``source`` is
    omitted the real NSE feed is built here and its reference comes from
    ``NSE_AUTOMATED_ACCESS_RIGHTS_REFERENCE``. ``record_observation`` never clears a
    reference already stored (the worker's ``RecordingActionSource`` records it first).
    """
    from app.config import get_settings
    from app.ingest.corporate_action_feed import (
        CorporateActionFeedFetchFailed,
        CorporateActionFeedFormatError,
        nse_corporate_action_feed,
        parse_corporate_action_feed,
    )

    feed = source
    if feed is None:
        feed = nse_corporate_action_feed()
        rights_reference = get_settings().nse_automated_access_rights_reference
    try:
        payload = feed.fetch(start, end)
    except CorporateActionFeedFetchFailed as exc:
        logger.warning("corporate_action_feed_fetch_failed",
                       extra={"provider": _NSE_OBSERVATION_PROVIDER, "start": str(start),
                              "end": str(end), "error_type": type(exc).__name__})
        raise

    try:
        feed_rows = parse_corporate_action_feed(payload)
    except CorporateActionFeedFormatError as exc:
        logger.warning("corporate_action_feed_format_changed",
                       extra={"provider": _NSE_OBSERVATION_PROVIDER, "start": str(start),
                              "end": str(end), "error_type": type(exc).__name__})
        raise

    counts = {_VERIFIED: 0, _EXCEPTION: 0, _CONFLICT: 0, _ON_FILE: 0, _UNREGISTERED: 0}
    errors = 0
    for row in feed_rows:
        try:
            counts[_land_feed_row(conn, row, rights_reference, actor_uid)] += 1
            # Commit PER ROW: a later row's failure and rollback must not discard this
            # row's already-successful work.
            conn.commit()
        except Exception as exc:
            conn.rollback()
            errors += 1
            observation_id = _observe_feed_row(
                conn, row, rights_reference, result="REJECTED", reason_code="UNEXPECTED_ERROR",
                reason=type(exc).__name__,
            )
            conn.commit()
            logger.warning(
                "corporate_action_feed_row_failed",
                extra={"provider": _NSE_OBSERVATION_PROVIDER, "isin": row.isin,
                       "observation_id": observation_id, "reason_code": "UNEXPECTED_ERROR",
                       "error_type": type(exc).__name__},
                exc_info=True,
            )

    # Before retrying pending rows, re-examine any WAIVER whose premise has expired --
    # a lowered archive floor makes "no fetch will ever be attempted" false, and a
    # waiver must not go on asserting it. See migration 028.
    revoke_stale_price_waivers(conn, actor_uid=actor_uid)

    verified = counts[_VERIFIED]
    reevaluable = (CorpActionType.BONUS, CorpActionType.SPLIT)
    for pending in list_pending_corporate_actions(conn):
        if pending.action_type not in reevaluable:
            continue  # not a type this feed re-evaluates
        if revision_blocks_verification(conn, pending.id):
            logger.info(
                "corporate_action_retry_blocked",
                extra={"isin": pending.isin, "action_id": pending.id,
                       "reason_code": "TERMS_CONFLICT"},
            )
            continue  # a contradicting notice is on file; a human resolves it
        try:
            candidate = _evaluate_bonus_split(
                conn, pending.isin, pending.action_type, pending.ex_date,
                pending.bonus_new, pending.bonus_for,
                pending.face_value_before, pending.face_value_after,
                notes=pending.notes,
            )
            if check_sourced(candidate) or check_price_consistency(candidate):
                continue  # still not resolvable -- retried again next run
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE corporate_action SET price_cum = %s, price_ex = %s WHERE id = %s",
                    (candidate.price_cum, candidate.price_ex, pending.id),
                )
            verify_corporate_action(conn, pending.id, actor_uid=actor_uid)
            conn.commit()  # per-row, same reasoning as the fetch loop above
            verified += 1
        except Exception as exc:
            conn.rollback()
            errors += 1
            logger.warning(
                "corporate_action_retry_failed",
                extra={"isin": pending.isin, "action_id": pending.id,
                       "error_type": type(exc).__name__},
                exc_info=True,
            )

    result = {
        "fetched": len(feed_rows),
        "verified": verified,
        "exceptions": counts[_EXCEPTION],
        "conflicts": counts[_CONFLICT],
    }
    if errors:
        from app.ingest.action_source import ActionIngestionError
        raise ActionIngestionError(f"{errors} corporate-action rows failed; retry required")
    return result


def record_yahoo_rejection(
    conn: Any,
    *,
    isin: str,
    symbol: str | None,
    classification: str,
    reason_code: str,
    reason: str | None,
    rights_reference: str | None,
    detail: dict[str, Any] | None = None,
) -> str:
    """Record one Yahoo discovery failure (an ISIN without a usable symbol, a failed
    lookup, or a candidate that could not be stored) as a REJECTED observation and
    commit it, so the run can continue and the failure stays visible. Returns the
    observation ID; the caller raises ``ActionBatchFailed`` at the end of its run."""
    from app.db.action_ingestion import record_observation
    from app.ingest.action_source import PARSER_VERSION
    from app.ingest.corporate_action_yfinance import YFINANCE_SOURCE_NAME

    raw = {"isin": isin, "symbol": symbol, "rejection": reason_code, **(detail or {})}
    observation_id = record_observation(
        conn, provider=YFINANCE_SOURCE_NAME, raw=raw, parser_version=PARSER_VERSION,
        classification=classification, result="REJECTED", reason_code=reason_code,
        reason=reason, isin=isin, rights_reference=rights_reference,
    )
    conn.commit()
    logger.warning(
        "yahoo_discovery_rejected",
        extra={"provider": YFINANCE_SOURCE_NAME, "isin": isin,
               "observation_id": observation_id, "reason_code": reason_code},
    )
    return observation_id


def current_identifiers(conn: Any, isins: list[str]) -> list[str]:
    """``isins`` without the RETIRED side of any VERIFIED alias. A retired ISIN's
    history is the current ISIN's history, so the current one covers it; skipping it is
    not a rejection and records nothing."""
    from app.db.security_alias import verified_map

    retired = verified_map(conn)
    return [isin for isin in isins if isin not in retired]


def yahoo_symbol_rejection(meta: dict[str, dict[str, Any]], isin: str) -> str | None:
    """The reason code when ``isin`` cannot be looked up on Yahoo, else ``None``."""
    if isin not in meta:
        return "SECURITY_NOT_REGISTERED"
    if not meta[isin].get("symbol"):
        return "MISSING_SYMBOL"
    return None


def discover_from_yfinance(
    conn: Any,
    isins: list[str] | None = None,
    source: Any | None = None,
    actor_uid: str | None = None,
    rights_reference: str | None = None,
) -> dict[str, Any]:
    """Record each Yahoo split-history factor as an exact UNKNOWN_ADJUSTMENT observation.

    A bare factor cannot say whether an event was a bonus or a split, so nothing here
    writes ``corporate_action`` and nothing can be applied to a ledger; an issuer notice
    has to establish the event. The factor is stored as text, never rounded.

    BSE-ONLY (owner decision 2026-10-31). For an NSE-listed security, NSE's notices and the
    bhavcopy price check already find splits and bonuses, so Yahoo is asked only about
    securities whose newest close is not NSE's; the rest are counted in
    ``skipped_nse_listed``.

    ``isins`` defaults to every ISIN this system has ever held; the retired side of a
    VERIFIED alias is skipped silently (``current_identifiers``). An ISIN with no usable
    symbol, or whose lookup fails, is recorded REJECTED and the run continues with the
    rest; ``ActionBatchFailed`` is raised at the end if any ISIN was rejected.
    ``rights_reference`` is copied onto every observation; when ``source`` is omitted
    the real Yahoo source is built here and the reference is ``YAHOO_DATA_RIGHTS_REFERENCE``.
    """
    from app.config import get_settings
    from app.db.action_ingestion import payload_digest, record_observation
    from app.db.prices import held_isins, nse_listed_isins
    from app.ingest.action_source import PARSER_VERSION, ActionBatchFailed
    from app.ingest.corporate_action_yfinance import (
        YFINANCE_SOURCE_NAME,
        YfinanceSource,
        discover_candidates,
    )
    from app.securities import lookup

    yf_source = source
    if yf_source is None:
        yf_source = YfinanceSource()
        rights_reference = get_settings().yahoo_data_rights_reference
    candidates_isins = current_identifiers(
        conn, isins if isins is not None else held_isins(conn)
    )
    nse_listed = nse_listed_isins(conn, candidates_isins)
    target_isins = [isin for isin in candidates_isins if isin not in nse_listed]
    meta = lookup(conn, target_isins)
    discovered = duplicates = 0
    rejected: list[str] = []
    for isin in target_isins:
        symbol = (meta.get(isin) or {}).get("symbol")
        reason_code = yahoo_symbol_rejection(meta, isin)
        if reason_code is not None:
            rejected.append(record_yahoo_rejection(
                conn, isin=isin, symbol=symbol, classification="UNKNOWN_ADJUSTMENT",
                reason_code=reason_code, reason=None, rights_reference=rights_reference,
            ))
            continue
        try:
            candidates = discover_candidates(yf_source, isin, symbol)
        except Exception as exc:
            rejected.append(record_yahoo_rejection(
                conn, isin=isin, symbol=symbol, classification="UNKNOWN_ADJUSTMENT",
                reason_code="SOURCE_LOOKUP_FAILED",
                reason=f"{type(exc).__name__}: {str(exc)[:200]}",
                rights_reference=rights_reference,
            ))
            continue
        for candidate in candidates:
            raw = {"isin": isin, "ex_date": candidate.ex_date.isoformat(),
                   "adjustment_factor": str(candidate.adjustment_factor),
                   "action_type": "UNKNOWN_ADJUSTMENT",
                   "source_url": candidate.source_url}
            existed = conn.execute(
                "SELECT 1 FROM corporate_action_observation "
                "WHERE provider=%s AND payload_sha256=%s AND parser_version=%s",
                (YFINANCE_SOURCE_NAME, payload_digest(raw), PARSER_VERSION),
            ).fetchone()
            record_observation(
                conn, provider=YFINANCE_SOURCE_NAME, raw=raw, parser_version=PARSER_VERSION,
                classification="UNKNOWN_ADJUSTMENT", result="REVIEW",
                reason_code="UNCLASSIFIED_ADJUSTMENT", isin=isin, ex_date=candidate.ex_date,
                rights_reference=rights_reference,
            )
            discovered += existed is None
            duplicates += existed is not None
        conn.commit()  # per ISIN: a later ISIN's failure must not discard these
    if rejected:
        raise ActionBatchFailed(rejected, [])
    return {"isins_checked": len(target_isins), "discovered": discovered, "verified": 0,
            "exceptions": discovered, "duplicates": duplicates, "errors": {},
            "skipped_nse_listed": len(nse_listed)}
