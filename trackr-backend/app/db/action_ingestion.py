"""Corporate-action observations, provider revisions and observed coverage.

``record_observation`` is the one writer of ``corporate_action_observation`` and
``write_coverage`` the one writer of ``corporate_action_coverage``. ``ingest_batch`` is the
back end of the operator file import. Only the reference worker and ops/cron routes may
call this module: these are global reference tables, never written by an investor route.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import replace
from datetime import date
from decimal import Decimal

from pydantic import ValidationError

from app.db import corporate_actions as ca
from app.db import dividend_events as dividends
from app.db.reference_sync import enqueue
from app.engine.corporate_actions import CorpActionType, Verification
from app.engine.dividend_events import DividendEvent, DividendKind
from app.ingest.action_source import (
    PARSER_VERSION,
    ActionBatch,
    ActionBatchFailed,
    ActionConflictError,
    ActionIngestionError,
    ActionNotice,
    canonical_json,
)

logger = logging.getLogger(__name__)

#: Mirrors the CHECK on corporate_action_observation.classification (migration 061).
CLASSIFICATIONS = frozenset(
    {
        "BONUS",
        "SPLIT",
        "DIVIDEND",
        "RIGHTS",
        "BUYBACK",
        "MERGER",
        "DEMERGER",
        "CAPITAL_REDUCTION",
        "CONSOLIDATION",
        "IDENTITY",
        "UNKNOWN_ADJUSTMENT",
        "UNKNOWN",
    }
)

#: The fixed set of observation reason codes. Readers map these to copy; never match text.
REASON_CODES = frozenset(
    {
        # Operator file import.
        "SUPERSEDED_REVISION",
        "ALREADY_PROCESSED",
        "CANCELLATION_RECORDED",
        "MISSING_EX_DATE",
        "NOT_CONFIRMED",
        "SECURITY_NOT_REGISTERED",
        "AWAITING_VERIFICATION",
        "MISSING_DIVIDEND_TERMS",
        "DECLARATION_ONLY",
        "TERMS_CONFLICT",
        "IDENTITY_CONFLICT",
        "TYPE_CONFLICT",
        "SAME_DAY_EVENT_EXISTS",
        "INVALID_NOTICE",
        "UNEXPECTED_ERROR",
        # Any source.
        "UNSUPPORTED_ACTION_CLASS",
        # NSE notices, NSE ISIN-change circulars, Yahoo history.
        "AWAITING_PARSER",
        "UNCLASSIFIED_NOTICE",
        "IDENTITY_CIRCULAR",
        "UNCLASSIFIED_ADJUSTMENT",
        # NSE notice rows after the bonus/split writer has read them.
        "PRICE_CORROBORATED",
        # Yahoo discovery, per ISIN.
        "MISSING_SYMBOL",
        "SOURCE_LOOKUP_FAILED",
        # A second exchange's copy of an event already stored with identical terms.
        "CROSS_EXCHANGE_DUPLICATE",
        # BSE export rows whose scrip code the BSE bhavcopy does not map to one ISIN.
        "SCRIP_CODE_UNRESOLVED",
    }
)


def payload_digest(raw: dict) -> str:
    return hashlib.sha256(canonical_json(raw).encode()).hexdigest()


def record_observation(
    conn,
    *,
    provider: str,
    raw: dict,
    parser_version: str,
    classification: str,
    result: str,
    reason_code: str,
    isin: str | None = None,
    ex_date: date | None = None,
    action_id: str | None = None,
    dividend_id: str | None = None,
    reason: str | None = None,
    rights_reference: str | None = None,
    event_id: str | None = None,
    component_id: str | None = None,
) -> str:
    """Record one source payload; return its observation ID. Does not commit.

    Idempotent on (provider, payload bytes, parser version). Recording the same payload
    again refreshes its outcome; links and provenance already stored are never cleared
    by a later call that does not supply them.
    """
    if classification not in CLASSIFICATIONS:
        raise ValueError(f"Unknown observation classification: {classification}")
    if reason_code not in REASON_CODES:
        raise ValueError(f"Unknown observation reason code: {reason_code}")
    row = conn.execute(
        """INSERT INTO corporate_action_observation AS o
           (provider,payload_sha256,raw_payload,parser_version,rights_reference,
            classification,result,reason_code,reason,isin,ex_date,event_id,component_id,
            action_id,dividend_id)
           VALUES (%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT(provider,payload_sha256,parser_version) DO UPDATE SET
               fetched_at=now(),
               classification=EXCLUDED.classification,
               result=EXCLUDED.result,
               reason_code=EXCLUDED.reason_code,
               reason=EXCLUDED.reason,
               rights_reference=COALESCE(EXCLUDED.rights_reference,o.rights_reference),
               isin=COALESCE(EXCLUDED.isin,o.isin),
               ex_date=COALESCE(EXCLUDED.ex_date,o.ex_date),
               event_id=COALESCE(EXCLUDED.event_id,o.event_id),
               component_id=COALESCE(EXCLUDED.component_id,o.component_id),
               action_id=COALESCE(EXCLUDED.action_id,o.action_id),
               dividend_id=COALESCE(EXCLUDED.dividend_id,o.dividend_id)
           RETURNING id""",
        (
            provider,
            payload_digest(raw),
            canonical_json(raw),
            parser_version,
            rights_reference,
            classification,
            result,
            reason_code,
            reason,
            isin,
            ex_date,
            event_id,
            component_id,
            action_id,
            dividend_id,
        ),
    ).fetchone()
    return str(row[0])


def write_coverage(
    conn, *, provider: str, start: date, end: date, exchanges: list[str], parser_version: str
) -> None:
    """Record that ``[start, end]`` was observed. Never a claim of completeness.
    Does not commit."""
    conn.execute(
        """INSERT INTO corporate_action_coverage
           (provider,start_date,end_date,exchanges,parser_version) VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT(provider,start_date,end_date,parser_version) DO UPDATE
           SET checked_at=now(),exchanges=EXCLUDED.exchanges""",
        (provider, start, end, sorted(exchanges), parser_version),
    )


def _material_terms(terms: dict) -> dict:
    # Dual exchange listings are evidence of the same event, not two entitlements.
    return {k: v for k, v in terms.items() if k not in ("exchange", "source_url", "subject")}


def _apply_notice(conn, notice: ActionNotice, provider: str, previous, auto_apply: bool):
    """Return (action_id, dividend_id, result, reason_code). Raises ActionConflictError."""
    action_id = str(previous[2]) if previous and previous[2] else None
    dividend_id = str(previous[3]) if previous and previous[3] else None
    if previous and previous[1]["isin"] != notice.isin:
        raise ActionConflictError(
            "An event revision cannot silently replace security identity", "IDENTITY_CONFLICT"
        )
    if previous and previous[1]["action_type"] != notice.action_type:
        raise ActionConflictError("Changed action type requires explicit review", "TYPE_CONFLICT")
    if notice.status == "CANCELLED":
        # An explicit cancellation is the one source event that may set DISPUTED.
        if action_id:
            conn.execute(
                "UPDATE corporate_action SET cancelled=true,verification='DISPUTED' WHERE id=%s",
                (action_id,),
            )
        if dividend_id:
            conn.execute(
                "UPDATE dividend_event SET verification='DISPUTED' WHERE id=%s", (dividend_id,)
            )
        return action_id, dividend_id, "ACCEPTED", "CANCELLATION_RECORDED"
    if notice.ex_date is None:
        return action_id, dividend_id, "REVIEW", "MISSING_EX_DATE"
    if notice.status != "CONFIRMED":
        return action_id, dividend_id, "REVIEW", "NOT_CONFIRMED"
    if not conn.execute("SELECT 1 FROM security WHERE isin=%s", (notice.isin,)).fetchone():
        return action_id, dividend_id, "REVIEW", "SECURITY_NOT_REGISTERED"
    if notice.action_type in ("BONUS", "SPLIT"):
        action = ca._evaluate_bonus_split(
            conn,
            notice.isin,
            CorpActionType(notice.action_type),
            notice.ex_date,
            notice.bonus_new,
            notice.bonus_for,
            Decimal(notice.face_value_before) if notice.face_value_before else None,
            Decimal(notice.face_value_after) if notice.face_value_after else None,
            notice.subject,
            source=provider,
            source_url=notice.source_url,
        )
        # Source confirmation is not our verification. The worker's price/identity check
        # must verify the candidate; a configured licence alone never verifies a ratio.
        action = replace(
            action,
            verification=Verification.DRAFT,
            allotment_date=notice.allotment_date,
            automatic_application=auto_apply,
        )
        if action_id:
            conn.execute(
                """UPDATE corporate_action SET ex_date=%s,bonus_new=%s,bonus_for=%s,
                   face_value_before=%s,face_value_after=%s,allotment_date=%s,
                   verification='DRAFT',cancelled=false,price_cum=%s,price_ex=%s,
                   source=%s,source_url=%s,notes=%s,price_waived=false,
                   price_waiver_reason=NULL,automatic_application=%s WHERE id=%s""",
                (
                    action.ex_date,
                    action.bonus_new,
                    action.bonus_for,
                    action.face_value_before,
                    action.face_value_after,
                    action.allotment_date,
                    action.price_cum,
                    action.price_ex,
                    provider,
                    action.source_url,
                    action.notes,
                    auto_apply,
                    action_id,
                ),
            )
        else:
            existing = conn.execute(
                """SELECT id,source,bonus_new,bonus_for,face_value_before,face_value_after
                   FROM corporate_action WHERE isin=%s AND action_type=%s AND ex_date=%s""",
                (notice.isin, notice.action_type, notice.ex_date),
            ).fetchone()
            if existing:
                if existing[1] != provider:
                    # Another source (the NSE feed, for a dual-listed security) already
                    # holds this (isin, type, ex-date). Identical terms are the same event
                    # seen on a second exchange: link it, never a second entitlement.
                    # Different terms are a conflict; the stored row is left untouched.
                    if (existing[2], existing[3]) == (action.bonus_new, action.bonus_for) and (
                        existing[4] == action.face_value_before
                        and existing[5] == action.face_value_after
                    ):
                        return str(existing[0]), None, "ACCEPTED", "CROSS_EXCHANGE_DUPLICATE"
                    raise ActionConflictError(
                        "Another source holds different terms for this action", "TERMS_CONFLICT"
                    )
                # An unlinked notice is not proven to be the existing economic event.
                # Never combine a second same-day component or independent event by guess.
                raise ActionConflictError(
                    "Same-day action already exists under another event ID",
                    "SAME_DAY_EVENT_EXISTS",
                )
            action_id = ca.add_corporate_action(conn, action)
        return action_id, None, "ACCEPTED", "AWAITING_VERIFICATION"
    if notice.action_type == "DIVIDEND":
        if not notice.amount_per_share or notice.currency != "INR" or not notice.dividend_kind:
            return None, dividend_id, "REVIEW", "MISSING_DIVIDEND_TERMS"
        event = DividendEvent(
            isin=notice.isin,
            ex_date=notice.ex_date,
            amount_per_share=Decimal(notice.amount_per_share),
            kind=DividendKind(notice.dividend_kind),
            record_date=notice.record_date,
            payment_date=notice.payment_date,
            source=provider,
            source_url=notice.source_url,
            notes=notice.subject,
        )
        dividends.validate_dates(event)
        if dividend_id:
            conn.execute(
                """UPDATE dividend_event SET ex_date=%s,amount_per_share=%s,kind=%s,
                   record_date=%s,payment_date=%s,source=%s,source_url=%s,notes=%s,
                   verification='DRAFT',corroborated_by=NULL,corroborated_url=NULL WHERE id=%s""",
                (
                    event.ex_date,
                    event.amount_per_share,
                    event.kind.value,
                    event.record_date,
                    event.payment_date,
                    provider,
                    event.source_url,
                    event.notes,
                    dividend_id,
                ),
            )
        else:
            same_day = conn.execute(
                "SELECT id,source,amount_per_share,kind FROM dividend_event "
                "WHERE isin=%s AND ex_date=%s",
                (notice.isin, notice.ex_date),
            ).fetchall()
            for row in same_day:
                if row[1] != provider and row[2] == event.amount_per_share:
                    # The same declaration from a second exchange. Kinds can be labelled
                    # differently by the two exchanges; the amount is what is projected.
                    return None, str(row[0]), "ACCEPTED", "CROSS_EXCHANGE_DUPLICATE"
            if any(row[1] != provider or row[3] == event.kind.value for row in same_day):
                # A different amount on the same day from another source may be the same
                # declaration read differently: a second row would double the expected
                # income, so it is held for review instead.
                raise ActionConflictError(
                    "A different same-day dividend is already stored", "SAME_DAY_EVENT_EXISTS"
                )
            dividend_id = dividends.add_dividend_event(conn, event)
        return None, dividend_id, "ACCEPTED", "DECLARATION_ONLY"
    return action_id, dividend_id, "REVIEW", "UNSUPPORTED_ACTION_CLASS"


def _raw_text(raw: dict, key: str) -> str | None:
    value = raw.get(key)
    return value if isinstance(value, str) else None


def _invalidate_holders(conn, notice: ActionNotice, previous, batch: ActionBatch) -> None:
    """Invalidate only the affected holders' snapshots and queue their replay."""
    conn.execute(
        """UPDATE reference_sync_task SET state='pending',available_at=now()
           WHERE kind='REPLAY' AND task_key IN
           (SELECT DISTINCT account_id::text FROM transaction WHERE isin=%s)""",
        (notice.isin,),
    )
    earliest = [notice.ex_date]
    if previous and previous[1].get("ex_date"):
        earliest.append(date.fromisoformat(previous[1]["ex_date"]))
    conn.execute(
        """DELETE FROM portfolio_snapshot WHERE investor_id IN
           (SELECT a.investor_id FROM account a JOIN transaction t
            ON t.account_id=a.id WHERE t.isin=%s) AND as_of >= %s""",
        (notice.isin, min(filter(None, earliest), default=batch.start)),
    )


def _validate(batch: ActionBatch, raw: dict) -> ActionNotice:
    try:
        notice = ActionNotice.model_validate(raw)
    except ValidationError as exc:
        # Field locations and messages only: never echo provider content.
        detail = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise ActionIngestionError(detail[:1000]) from exc
    if notice.exchange not in batch.exchanges:
        raise ActionIngestionError("Notice exchange is absent from batch coverage")
    if notice.ex_date and not batch.start <= notice.ex_date <= batch.end:
        raise ActionIngestionError("Notice ex-date is outside requested coverage")
    return notice


def _latest_revision(conn, provider: str, notice: ActionNotice):
    """Serialize work on one provider event, then read its latest revision."""
    lock_key = canonical_json(
        {"provider": provider, "event": notice.event_id, "component": notice.component_id}
    )
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (lock_key,))
    return conn.execute(
        """SELECT revision,terms,action_id,dividend_id,id FROM corporate_action_revision
           WHERE provider=%s AND event_id=%s AND component_id=%s
           ORDER BY revision DESC LIMIT 1""",
        (provider, notice.event_id, notice.component_id),
    ).fetchone()


def _process(conn, batch, raw, notice, previous, rights_reference, auto_apply) -> str:
    """Apply one validated notice inside the caller's transaction; return its result.
    Raises ActionConflictError when the notice contradicts what is stored."""
    terms = notice.model_dump(mode="json")
    observed = dict(
        provider=batch.provider,
        raw=raw,
        parser_version=PARSER_VERSION,
        classification=notice.action_type,
        isin=notice.isin,
        ex_date=notice.ex_date,
        rights_reference=rights_reference,
        event_id=notice.event_id,
        component_id=notice.component_id,
    )
    if previous and notice.revision == previous[0]:
        if _material_terms(previous[1]) != _material_terms(terms):
            raise ActionConflictError(
                "Conflicting terms for the same provider revision", "TERMS_CONFLICT"
            )
    if previous and notice.revision < previous[0]:
        record_observation(conn, **observed, result="ACCEPTED", reason_code="SUPERSEDED_REVISION")
        return "ACCEPTED"
    if previous and notice.revision == previous[0] and (previous[2] or previous[3]):
        # An identical re-import (or a second exchange's copy) changes nothing. An
        # observation already on file is left exactly as it is.
        exists = conn.execute(
            "SELECT 1 FROM corporate_action_observation "
            "WHERE provider=%s AND payload_sha256=%s AND parser_version=%s",
            (batch.provider, payload_digest(raw), PARSER_VERSION),
        ).fetchone()
        if not exists:
            record_observation(
                conn,
                **observed,
                result="ACCEPTED",
                reason_code="ALREADY_PROCESSED",
                action_id=str(previous[2]) if previous[2] else None,
                dividend_id=str(previous[3]) if previous[3] else None,
            )
        return "ACCEPTED"
    # A newer revision invalidates the old effect before the new terms are evaluated,
    # so an incomplete newer revision can never leave the old terms verified.
    if previous and notice.revision > previous[0]:
        if previous[2]:
            conn.execute(
                "UPDATE corporate_action SET verification='DRAFT' WHERE id=%s", (previous[2],)
            )
        if previous[3]:
            conn.execute(
                "UPDATE dividend_event SET verification='DRAFT' WHERE id=%s", (previous[3],)
            )
    action_id, dividend_id, result, reason_code = _apply_notice(
        conn, notice, batch.provider, previous, auto_apply
    )
    observation_id = record_observation(
        conn,
        **observed,
        result=result,
        reason_code=reason_code,
        action_id=action_id,
        dividend_id=dividend_id,
    )
    conn.execute(
        """INSERT INTO corporate_action_revision
           (provider,event_id,component_id,revision,observation_id,terms,
            action_id,dividend_id) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
           ON CONFLICT(provider,event_id,component_id,revision) DO UPDATE
           SET action_id=EXCLUDED.action_id,dividend_id=EXCLUDED.dividend_id""",
        (
            batch.provider,
            notice.event_id,
            notice.component_id,
            notice.revision,
            observation_id,
            canonical_json(terms),
            action_id,
            dividend_id,
        ),
    )
    if reason_code == "CROSS_EXCHANGE_DUPLICATE":
        # Nothing changed on the stored row: no re-verification, no replay.
        return result
    if action_id:
        enqueue(conn, "ACTION", action_id, {"action_id": action_id}, refresh=True)
    _invalidate_holders(conn, notice, previous, batch)
    return result


def ingest_batch(
    conn,
    batch: ActionBatch,
    *,
    rights_reference: str,
    auto_apply: bool,
    prior_rejected: list[str] | None = None,
) -> dict:
    """Import one operator file. Each row commits on its own, so one bad row never
    discards good rows; a batch with any rejected or conflicting row raises
    ``ActionBatchFailed`` at the end and writes no coverage. ``prior_rejected`` are
    observations an adapter already rejected for this file before building the batch."""
    if not rights_reference.strip():
        raise ActionIngestionError("A data rights reference is required")
    rejected: list[str] = list(prior_rejected or [])
    conflicts: list[str] = []
    accepted = review = 0
    for raw in batch.events:
        notice = previous = None
        try:
            notice = _validate(batch, raw)
            previous = _latest_revision(conn, batch.provider, notice)
            result = _process(conn, batch, raw, notice, previous, rights_reference, auto_apply)
            conn.commit()
            accepted += result == "ACCEPTED"
            review += result == "REVIEW"
        except ActionConflictError as exc:
            conn.rollback()
            # The stored row is no longer corroborated by its source: back to DRAFT so it
            # is retried, never DISPUTED (terminal; only a cancellation or a human).
            if previous and previous[2]:
                conn.execute(
                    "UPDATE corporate_action SET verification='DRAFT' "
                    "WHERE id=%s AND verification<>'DISPUTED'",
                    (previous[2],),
                )
            if previous and previous[3]:
                conn.execute(
                    "UPDATE dividend_event SET verification='DRAFT' "
                    "WHERE id=%s AND verification<>'DISPUTED'",
                    (previous[3],),
                )
            observation_id = record_observation(
                conn,
                provider=batch.provider,
                raw=raw,
                parser_version=PARSER_VERSION,
                classification=notice.action_type,
                result="CONFLICT",
                reason_code=exc.reason_code,
                reason=str(exc),
                isin=notice.isin,
                ex_date=notice.ex_date,
                rights_reference=rights_reference,
                event_id=notice.event_id,
                component_id=notice.component_id,
            )
            conn.commit()
            conflicts.append(observation_id)
            logger.warning(
                "corporate_action_observation_conflict",
                extra={
                    "provider": batch.provider,
                    "isin": notice.isin,
                    "observation_id": observation_id,
                    "reason_code": exc.reason_code,
                },
            )
        except Exception as exc:
            # Invalid or unexpected: nothing from this row survives the rollback, so no
            # reference row is mutated. The evidence is kept as REJECTED.
            conn.rollback()
            invalid = isinstance(exc, ActionIngestionError)
            reason_code = "INVALID_NOTICE" if invalid else "UNEXPECTED_ERROR"
            observation_id = record_observation(
                conn,
                provider=batch.provider,
                raw=raw,
                parser_version=PARSER_VERSION,
                classification=(
                    raw["action_type"] if raw.get("action_type") in CLASSIFICATIONS else "UNKNOWN"
                ),
                result="REJECTED",
                reason_code=reason_code,
                reason=str(exc) if invalid else type(exc).__name__,
                isin=_raw_text(raw, "isin"),
                rights_reference=rights_reference,
                event_id=_raw_text(raw, "event_id"),
                component_id=_raw_text(raw, "component_id"),
            )
            conn.commit()
            rejected.append(observation_id)
            context = {
                "provider": batch.provider,
                "isin": _raw_text(raw, "isin"),
                "observation_id": observation_id,
                "reason_code": reason_code,
                "error_type": type(exc).__name__,
            }
            if invalid:
                logger.warning("corporate_action_observation_rejected", extra=context)
            else:
                logger.error("corporate_action_observation_rejected", exc_info=True, extra=context)
    if rejected or conflicts:
        raise ActionBatchFailed(rejected, conflicts)
    write_coverage(
        conn,
        provider=batch.provider,
        start=batch.start,
        end=batch.end,
        exchanges=list(batch.exchanges),
        parser_version=PARSER_VERSION,
    )
    conn.commit()
    return {"accepted": accepted, "review": review, "observations": len(batch.events)}


def ingest_bse_export(
    conn,
    payload: bytes,
    *,
    start: date,
    end: date,
    scrip_to_isin: dict[str, str],
    rights_reference: str,
    auto_apply: bool,
) -> dict:
    """Import one BSE corporate-actions export (``app.ingest.bse_corporate_actions``).
    A row whose scrip code does not resolve is kept as a REJECTED observation with
    ``SCRIP_CODE_UNRESOLVED`` -- never matched by name -- and fails the batch like any
    other bad row, after every resolvable row has been imported."""
    from app.ingest.bse_corporate_actions import BSE_EXPORT_PROVIDER, build_batch

    if not rights_reference.strip():
        raise ActionIngestionError("A data rights reference is required")
    batch, unresolved = build_batch(payload, scrip_to_isin, start=start, end=end)
    rejected: list[str] = []
    for row in unresolved:
        observation_id = record_observation(
            conn,
            provider=BSE_EXPORT_PROVIDER,
            raw=row.raw,
            parser_version=PARSER_VERSION,
            classification="UNKNOWN",
            result="REJECTED",
            reason_code="SCRIP_CODE_UNRESOLVED",
            reason=f"BSE scrip code {row.scrip_code!r} is not in the BSE bhavcopy",
            rights_reference=rights_reference,
        )
        conn.commit()
        rejected.append(observation_id)
        logger.warning(
            "corporate_action_observation_rejected",
            extra={
                "provider": BSE_EXPORT_PROVIDER,
                "isin": None,
                "observation_id": observation_id,
                "reason_code": "SCRIP_CODE_UNRESOLVED",
            },
        )
    return ingest_batch(
        conn,
        batch,
        rights_reference=rights_reference,
        auto_apply=auto_apply,
        prior_rejected=rejected,
    )
