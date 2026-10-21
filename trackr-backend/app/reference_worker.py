"""Free-source reference worker: python -m app.reference_worker [--once]."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
from dataclasses import replace
from datetime import date, timedelta

from app.config import Settings, get_settings
from app.db import corporate_actions as ca
from app.db import identity_evidence as identity
from app.db import reference_sync as queue
from app.db.action_ingestion import record_observation, write_coverage
from app.db.connection import connect
from app.db.gap_resolution import ResearchDeferred
from app.db.isin_change_circulars import refresh_isin_change_feed
from app.db.security_alias import promote_draft_aliases
from app.engine.corporate_actions import Verification, check_price_consistency
from app.ingest.action_source import ActionSourceError
from app.ingest.corporate_action_feed import (
    NSE_CORP_ACTIONS_URL,
    NSE_PARSER_VERSION,
    CorporateActionFeedFormatError,
    NseCorporateActionFeedSource,
    classify_subject,
    is_plain_notice,
    parse_corporate_action_feed,
    parse_nse_date,
)
from app.ingest.isin_change_feed import NseCircularListSource, parse_circular_listing
from app.ingest.nsdl import NsdlParticipantSource
from app.prices.sources import NotPublished, bse_source, nse_source

logger = logging.getLogger("reference_worker")
_WORKER_LOCK = 804732916  # session lock: survives commits, released on connection death

#: Observation parser version for NSE ISIN-change circular listing rows.
CIRCULAR_PARSER_VERSION = "nse-isin-circular-listing-v1"

#: Notice classes that the row parsers read. Every other class is review-only.
_PARSED_CLASSES = ("BONUS", "SPLIT", "DIVIDEND")


class ReferenceBatchFailed(ActionSourceError):
    """Keep committed progress available when unrelated source tasks fail."""

    def __init__(self, result: dict):
        self.result = result
        super().__init__(
            f"{result['failed']} reference task(s) failed and remain queued; "
            "inspect operator status"
        )


class RecordingActionSource:
    """Wraps the NSE notice feed. Every row of a fetch is recorded as a classified
    observation before any parser reads the payload, and one fetch serves both the
    split/bonus and the dividend parsers."""

    def __init__(self, conn, settings: Settings, source=None):
        self.conn = conn
        self.rights_reference = settings.nse_automated_access_rights_reference
        self.source = source or NseCorporateActionFeedSource()
        self._payloads = {}

    def fetch(self, start, end):
        if (start, end) in self._payloads:
            return self._payloads[start, end]
        payload = self.source.fetch(start, end)
        try:
            rows = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CorporateActionFeedFormatError(f"not valid JSON: {exc}") from exc
        if not isinstance(rows, list):
            raise CorporateActionFeedFormatError("NSE corporate actions did not return a list")
        source_url = NSE_CORP_ACTIONS_URL.format(start=start, end=end)
        with self.conn.cursor() as cur:
            for i, row in enumerate(rows):
                if not isinstance(row, dict):
                    raise CorporateActionFeedFormatError(f"row {i}: not a JSON object")
                subject = str(row.get("subject") or "").strip()
                isin = str(row.get("isin") or "").strip() or None
                classification = classify_subject(subject)
                if classification in _PARSED_CLASSES:
                    result, reason_code = "PENDING", "AWAITING_PARSER"
                elif classification == "UNKNOWN":
                    result, reason_code = "REVIEW", "UNCLASSIFIED_NOTICE"
                else:
                    result, reason_code = "REVIEW", "UNSUPPORTED_ACTION_CLASS"
                record_observation(
                    self.conn,
                    provider="NSE",
                    raw=row,
                    parser_version=NSE_PARSER_VERSION,
                    classification=classification,
                    result=result,
                    reason_code=reason_code,
                    isin=isin,
                    ex_date=parse_nse_date(str(row.get("exDate", "")).strip()),
                    rights_reference=self.rights_reference,
                )
                unsupported = classification not in _PARSED_CLASSES + ("UNKNOWN",)
                if isin and (unsupported or not is_plain_notice(subject)):
                    # Investor-facing review signal for notices nothing here can apply.
                    raw = json.dumps(row, sort_keys=True)
                    cur.execute(
                        """INSERT INTO reference_notice
                           (notice_hash,isin,symbol,ex_date,subject,source_url,payload)
                           VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING""",
                        (
                            hashlib.sha256(raw.encode()).hexdigest(),
                            isin,
                            str(row.get("symbol", "")),
                            str(row.get("exDate", "")),
                            subject,
                            source_url,
                            raw,
                        ),
                    )
        self.conn.commit()
        self._payloads[start, end] = payload
        return payload


class RecordingCircularSource:
    """Wraps the NSE circular listing: every ISIN-change listing row is recorded as an
    IDENTITY observation. Detection only; aliases still come from resolve_split."""

    def __init__(self, conn, settings: Settings, source):
        self.conn = conn
        self.rights_reference = settings.nse_automated_access_rights_reference
        self.source = source

    def list_circulars(self, start, end):
        payload = self.source.list_circulars(start, end)
        for listing in parse_circular_listing(payload):
            record_observation(
                self.conn,
                provider="NSE",
                raw={
                    "circular_number": listing.circular_number,
                    "circular_ref": listing.circular_ref,
                    "circular_date": listing.circular_date.isoformat(),
                    "subject": listing.subject,
                    "pdf_url": listing.pdf_url,
                },
                parser_version=CIRCULAR_PARSER_VERSION,
                classification="IDENTITY",
                result="ACCEPTED",
                reason_code="IDENTITY_CIRCULAR",
                rights_reference=self.rights_reference,
            )
        self.conn.commit()
        return payload

    def fetch_pdf(self, url):
        return self.source.fetch_pdf(url)


def _schedule_actions(conn):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT t.account_id::text,c.id::text FROM corporate_action c
               JOIN transaction t ON left(t.isin,7)=left(c.isin,7)
               WHERE c.action_type IN ('BONUS','SPLIT') AND c.verification='DRAFT'"""
        )
        pairs = cur.fetchall()
    for account, action_id in pairs:
        queue.enqueue(conn, "ACTION", action_id, {"action_id": action_id})
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO reference_sync_dependency VALUES (%s,'ACTION',%s) "
                "ON CONFLICT DO NOTHING",
                (account, action_id),
            )


class ActionTermsConflict(identity.EvidencePending):
    """A DRAFT whose latest revision is a contradicting notice. Not a worker failure:
    it waits, with backoff, until a human resolves the conflict."""

    reason_code = "TERMS_CONFLICT"


def _refile_under_traded_identifier(
    conn, action, traded, before, after, before_receipt, after_receipt
):
    """Re-file a stale-ISIN notice under the identifier the exchange traded.

    The ratio is still corroborated independently, by the same price-consistency check
    any notice must pass; identity alone never verifies anything. The stale row is
    DISPUTED with the reason (never deleted), so the record of what NSE sent survives.
    """
    cum = next(r for r in before if r[0] == traded)
    ex = next(r for r in after if r[0] == traded)
    reason = (
        f"Notice ISIN {action.isin} did not trade on {after_receipt['date']} or the "
        f"preceding session; NSE bhavcopy lists {cum[1]} as {traded} on both "
        f"(sha256 {before_receipt['sha256'][:12]}, {after_receipt['sha256'][:12]})."
    )
    candidate = replace(
        action,
        id=None,
        isin=traded,
        price_cum=cum[3],
        price_ex=ex[3],
        verification=Verification.VERIFIED,
        notes=f"{action.notes or ''}\nREFILED from stale notice ISIN {action.isin}: {reason}",
    )
    if check_price_consistency(candidate):
        raise identity.EvidencePending("Exchange prices contradict the announced ratio")
    existing = [
        a
        for a in ca.list_corporate_actions(conn, [traded])
        if a.isin == traded and a.action_type == action.action_type and a.ex_date == action.ex_date
    ]
    if existing and existing[0].multiplier() != candidate.multiplier():
        raise identity.EvidencePending("Traded identifier already carries different terms")
    if not existing:
        new_id = ca.add_corporate_action(conn, candidate)
        evidence = json.dumps(
            {
                "before": before_receipt,
                "after": after_receipt,
                "announcement": action.source_url,
                "stale_notice_isin": action.isin,
                "traded_isin": traded,
            }
        )
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO reference_action_evidence VALUES (%s,%s::jsonb,now()) "
                "ON CONFLICT(action_id) DO NOTHING",
                (new_id, evidence),
            )
    ca.dispute_corporate_action(conn, action.id, reason=f"Superseded. {reason}")
    conn.commit()


def _verify_action(conn, action_id, source=None):
    action = ca.get_corporate_action(conn, action_id)
    if action is not None and action.cancelled:
        return  # An explicit cancellation is processed by account replay.
    if action is None or action.verification is Verification.DISPUTED:
        raise identity.EvidencePending("Action is absent or disputed")
    if action.verification is Verification.VERIFIED:
        return
    if action.ex_date > date.today():
        raise identity.EvidencePending("Action is not effective yet")
    if ca.revision_blocks_verification(conn, action_id):
        raise ActionTermsConflict("A contradicting notice for this action awaits review")
    listed = conn.execute("SELECT exchange FROM security WHERE isin=%s", (action.isin,)).fetchone()
    # A BSE-only listing verifies from BSE closes. NSE, dual-listed ('BOTH') and unknown
    # listings use NSE closes, as before exchange-tagged evidence existed.
    exchange = "BSE" if listed and listed[0] == "BSE" else "NSE"
    source = source or (bse_source() if exchange == "BSE" else nse_source())
    after, after_receipt = identity._day(conn, action.ex_date, source, exchange)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT old_isin FROM security_alias WHERE current_isin=%s AND verification='VERIFIED'",
            (action.isin,),
        )
        identifiers = {action.isin, *(r[0] for r in cur.fetchall())}
    prior_rows = []
    before = []
    before_receipt = None
    for offset in range(1, 8):
        on = action.ex_date - timedelta(days=offset)
        if on.weekday() >= 5:
            continue
        try:
            before, before_receipt = identity._day(conn, on, source, exchange)
        except NotPublished:
            continue
        prior_rows = [r for r in before if r[0] in identifiers]
        break
    ex_rows = [r for r in after if r[0] == action.isin]
    if not prior_rows and not ex_rows and before_receipt is not None:
        traded = identity.traded_identifier(action.isin, before, after)
        if traded is not None:
            _refile_under_traded_identifier(
                conn, action, traded, before, after, before_receipt, after_receipt
            )
            return
    if len(prior_rows) != 1 or len(ex_rows) != 1:
        raise identity.EvidencePending("Waiting for unique cum/ex exchange prices or ISIN link")
    candidate = replace(action, price_cum=prior_rows[0][3], price_ex=ex_rows[0][3])
    if check_price_consistency(candidate):
        raise identity.EvidencePending("Exchange prices contradict the announced ratio")
    evidence = json.dumps(
        {"before": before_receipt, "after": after_receipt, "announcement": action.source_url}
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE corporate_action SET price_cum=%s,price_ex=%s WHERE id=%s",
            (candidate.price_cum, candidate.price_ex, action_id),
        )
        cur.execute(
            "INSERT INTO reference_action_evidence VALUES (%s,%s::jsonb,now()) "
            "ON CONFLICT(action_id) DO UPDATE SET evidence=EXCLUDED.evidence,"
            "recorded_at=EXCLUDED.recorded_at",
            (action_id, evidence),
        )
        cur.execute(
            "UPDATE corporate_action_revision SET verification_evidence=%s::jsonb WHERE id IN "
            "(SELECT id FROM corporate_action_revision WHERE action_id=%s "
            "ORDER BY revision DESC LIMIT 1)",
            (evidence, action_id),
        )
    ca.verify_corporate_action(conn, action_id)
    conn.commit()


def process(conn, kind: str, key: str, payload: dict, settings: Settings) -> dict | None:
    """Return a checkpoint for more work, or None when this task is complete."""
    if kind == "NSDL":
        if len(payload["query"]) != 12:
            # Legacy issuer-prefix task. NSDL's prefix pagination overlaps and skips rows,
            # so it can never complete; exact-ISIN tasks replace it. Retiring it records
            # nothing: unresolved identities are derived from security_reference itself.
            logger.info("Retiring legacy NSDL prefix task %s", key)
            return None
        page_no = payload["page"]
        if page_no > settings.reference_nsdl_max_pages:
            raise ValueError("NSDL pagination exceeded configured page limit")
        page = NsdlParticipantSource(timeout=settings.reference_http_timeout_seconds).search(
            payload["query"],
            page=page_no,
        )
        seen = set(payload.get("seen", []))
        new = {r.isin for r in page.records}
        if seen & new:
            raise ValueError("NSDL returned overlapping pages; refusing incomplete success")
        if page_no > 1 and page.count != payload["count"]:
            # The source changed mid-pagination: restart the next attempt, preserving
            # observations already stored, and do not declare the traversal complete.
            return {"query": payload["query"], "page": 1, "seen": []}
        queue.store_nsdl_page(conn, page, payload["query"])
        seen |= new
        if len(seen) < page.count:
            return {**payload, "page": page_no + 1, "seen": sorted(seen), "count": page.count}
        if len(seen) != page.count:
            raise ValueError("NSDL count did not reconcile")
    elif kind == "NSE":
        start, end = date.fromisoformat(payload["start"]), date.fromisoformat(payload["end"])
        circulars = refresh_isin_change_feed(
            conn,
            start,
            end,
            source=RecordingCircularSource(
                conn,
                settings,
                NseCircularListSource(timeout=settings.reference_http_timeout_seconds),
            ),
            max_documents=10,
        )
        identity.schedule_identity(conn)
        if circulars.get("error") or circulars.get("breaker_open"):
            raise ValueError(circulars.get("error") or "NSE circular circuit breaker is open")
        if circulars.get("remaining"):
            return payload
        from app.db.dividend_events import refresh_dividend_feed

        source = RecordingActionSource(conn, settings)
        # Both raise on a fetch, format or row failure, so coverage below is written
        # only for a month whose notices were all processed.
        ca.refresh_corporate_action_feed(conn, start, end, source=source)
        refresh_dividend_feed(conn, start, end, source=source)
        _schedule_actions(conn)
        write_coverage(
            conn,
            provider="NSE",
            start=start,
            end=end,
            exchanges=["NSE"],
            parser_version=NSE_PARSER_VERSION,
        )
    elif kind == "IDENTITY":
        identity.resolve_split(conn, payload["circular_number"])
    elif kind == "ACTION":
        _verify_action(conn, payload["action_id"])
    elif kind == "REPLAY":
        ca.apply_known_actions_for_account(conn, payload["account_id"])
        # Source observations can requeue REPLAY without changing this ledger.
        # Invalidate only when the normalized financial inputs changed, so repeated
        # checks do not erase a chart that the previous cycle just rebuilt.
        fingerprint, inputs, scope = _ledger_state(conn, payload["account_id"])
        changed = payload.get("snapshot_fingerprint") != fingerprint
        if changed:
            previous = payload.get("snapshot_inputs")
            dirty_from = None
            if previous is not None and payload.get("snapshot_scope") == scope:
                affected = [
                    value[0]
                    for key in previous.keys() | inputs.keys()
                    if previous.get(key) != inputs.get(key)
                    for value in (previous.get(key), inputs.get(key))
                    if value
                ]
                if affected:
                    dirty_from = date.fromisoformat(min(affected))
            _invalidate_snapshots(conn, payload["account_id"], dirty_from)
        payload["snapshot_fingerprint"] = fingerprint
        payload["snapshot_inputs"] = inputs
        payload["snapshot_scope"] = scope
        # Rule 20 (owner amendment 2026-10-08): whatever still does not reconcile after
        # the known actions are applied is a gap for public records to close.
        gap_payload = {"account_id": payload["account_id"]}
        queue.enqueue(conn, "GAP", payload["account_id"], gap_payload, refresh=changed)
        if not changed:
            with conn.cursor() as cur:
                # New public observations may explain a previously open gap, but an
                # unchanged replay must not erase an in-progress research checkpoint
                # or bypass its source-outage backoff.
                cur.execute(
                    """UPDATE reference_sync_task SET state='pending', available_at=now(),
                           payload=%s::jsonb WHERE kind='GAP' AND task_key=%s AND state='done'""",
                    (json.dumps(gap_payload), payload["account_id"]),
                )
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO reference_sync_dependency VALUES (%s,'GAP',%s) ON CONFLICT DO NOTHING",
                (payload["account_id"], payload["account_id"]),
            )
    elif kind == "GAP":
        from app.db.gap_resolution import resolve_account_gaps

        remaining = payload.get("research_isins")
        researching = remaining is not None
        result = resolve_account_gaps(
            conn,
            payload["account_id"],
            researcher=_researcher(settings),
            defer_research=not researching,
            research_isins=remaining[:1] if researching else None,
        )
        if result["resolved"]:
            # New ledger rows: replay so snapshots and every derived figure catch up.
            # Terminates: a replay re-queues GAP, which resolves nothing new the second
            # time (the rows are on file; a fit that already closed cannot fit again).
            queue.enqueue(conn, "REPLAY", payload["account_id"], payload, refresh=True)
        if result["deferred"]:
            # What pass 1 fixed is committed; only research waits for the quota to recover.
            conn.commit()
            raise ResearchDeferred(f"{len(result['deferred'])} gap(s) wait for research")
        pending = remaining[1:] if researching else result.get("research_pending", [])
        if pending:
            # One researched security per claim. Yield the worker to snapshots and
            # other accounts between slow source/model calls. The remaining ISINs
            # survive a worker restart in the existing durable task payload.
            return {"account_id": payload["account_id"], "research_isins": pending}
    elif kind == "SNAPSHOT":
        if payload.get("repair"):
            # An interior-hole repair (app.routers.analytical.enqueue_interior_holes). A day
            # that still cannot be priced is recorded on the task and finished; the payload
            # written back by run_once carries the reason, and the hole is offered again
            # only after HOLE_RETRY_INTERVAL.
            from app.engine.errors import MissingPriceError, NegativeHoldingError

            try:
                _rebuild_snapshot(conn, payload)
                payload.pop("unrepairable", None)
            except (MissingPriceError, NegativeHoldingError) as exc:
                conn.rollback()
                payload["unrepairable"] = str(exc)[:300]
        else:
            _rebuild_snapshot(conn, payload)
    elif kind == "AMFI_NAV_WINDOW":
        from app.prices.amfi_jobs import process_nav_window

        return process_nav_window(conn, payload, settings.amfi_http_timeout_seconds)
    elif kind == "AMFI_AMC_DISCOVERY":
        from app.prices.amfi_jobs import process_amc_discovery

        return process_amc_discovery(
            conn, payload, settings.amfi_http_timeout_seconds, budget_seconds=60.0
        )
    else:
        raise ValueError(f"Unknown reference task kind: {kind}")
    return None


#: Tries per research LLM call. A free model's 429 rarely clears within seconds, and
#: every further try spends the account's shared daily quota.
RESEARCH_LLM_ATTEMPTS = 2
#: After the model or the search refuses (rate limit, daily quota, outage), no research call
#: is made from this process for this long, for ANY account -- the quota is shared, so the
#: next account's call would only be refused too -- and a deferred GAP task waits at least
#: this long before it runs again.
RESEARCH_PAUSE_SECONDS = 1800
_research_paused_until = float("-inf")


def _researcher(settings: Settings):
    """Rule-20 research (Exa + OpenRouter), or None when either key is unset -- then BSE-only
    gaps simply stay open, excluded and named, as before."""
    if not (settings.exa_api_key and settings.openrouter_api_key):
        return None
    from app.ai.llm import LlmClient, LlmUnavailable
    from app.db.gap_research import research_gap
    from app.ingest.exa_search import ExaSource, ExaUnavailable

    exa = ExaSource(settings.exa_api_key)
    llm = LlmClient(
        settings.openrouter_api_key,
        settings.openrouter_model,
        max_attempts=RESEARCH_LLM_ATTEMPTS,
    )

    def run(conn, account_id, gap, meta):
        global _research_paused_until
        if time.monotonic() < _research_paused_until:
            raise ResearchDeferred("research paused after a rate limit or outage")
        try:
            return research_gap(conn, account_id, gap, meta, exa=exa, llm=llm)
        except (LlmUnavailable, ExaUnavailable) as exc:
            _research_paused_until = time.monotonic() + RESEARCH_PAUSE_SECONDS
            raise ResearchDeferred(str(exc)) from exc

    return run


def _ledger_fingerprint(conn, account_id):
    return _ledger_state(conn, account_id)[0]


def _ledger_state(conn, account_id):
    from app.db.repository import load_transactions

    rows = load_transactions(conn, account_id=account_id)
    digest = hashlib.sha256()
    with conn.cursor() as cur:
        cur.execute("SELECT investor_id::text, is_advised FROM account WHERE id=%s", (account_id,))
        scope = list(cur.fetchone())
        digest.update(json.dumps(scope).encode())
    inputs = {}
    for row in rows:
        values = {
            name: getattr(row, name)
            for name in (
                "id",
                "isin",
                "account_id",
                "txn_date",
                "txn_type",
                "quantity",
                "price",
                "charges",
                "stt",
                "dividend_amount",
                "tds_amount",
                "cost_transfer",
                "raw_payload",
            )
        }
        encoded = json.dumps(values, sort_keys=True, default=str).encode()
        digest.update(encoded)
        digest.update(b"\n")
        inputs[row.id] = [row.txn_date.isoformat(), hashlib.sha256(encoded).hexdigest()]
    return digest.hexdigest(), inputs, scope


def _invalidate_snapshots(conn, account_id, dirty_from=None):
    """Invalidate derived caches atomically with durable rebuild requests."""
    with conn.cursor() as cur:
        cur.execute("SELECT investor_id::text FROM account WHERE id=%s", (account_id,))
        owner = cur.fetchone()
        if owner is None:  # the account may have been erased while work was queued
            return
        investor_id = owner[0]
        cur.execute(
            "SELECT as_of,advised_only FROM portfolio_snapshot WHERE investor_id=%s "
            "AND (%s::date IS NULL OR as_of >= %s)",
            (investor_id, dirty_from, dirty_from),
        )
        dates = cur.fetchall()
        tasks = [
            {
                "key": f"{investor_id}:{on}:{advised}",
                "payload": {
                    "investor_id": investor_id,
                    "on": on.isoformat(),
                    "advised_only": advised,
                },
            }
            for on, advised in dates
        ]
        if tasks:
            cur.execute(
                """INSERT INTO reference_sync_task(kind,task_key,payload)
                   SELECT 'SNAPSHOT',item->>'key',item->'payload'
                   FROM jsonb_array_elements(%s::jsonb) item
                   ON CONFLICT(kind,task_key) DO UPDATE SET payload=EXCLUDED.payload,
                     state='pending',available_at=now()""",
                (json.dumps(tasks),),
            )
            cur.execute(
                "INSERT INTO reference_sync_dependency(account_id,kind,task_key) "
                "SELECT %s,'SNAPSHOT',unnest(%s::text[]) ON CONFLICT DO NOTHING",
                (account_id, [task["key"] for task in tasks]),
            )
        cur.execute(
            "DELETE FROM portfolio_snapshot WHERE investor_id=%s "
            "AND (%s::date IS NULL OR as_of >= %s)",
            (investor_id, dirty_from, dirty_from),
        )


def _rebuild_snapshot(conn, payload):
    """Replay using date-effective identities and observed prices; never retain stale totals."""
    from app.db.snapshot_batch import snapshot_batch
    from app.db.snapshots import upsert_snapshot

    if not conn.execute("SELECT 1 FROM investor WHERE id=%s", (payload["investor_id"],)).fetchone():
        return
    on = date.fromisoformat(payload["on"])
    with snapshot_batch(conn, on, on) as batch:
        snapshot = batch.build(payload["investor_id"], payload["advised_only"], on)
        upsert_snapshot(conn, payload["investor_id"], payload["advised_only"], snapshot)


def _process_snapshot_claim(conn, rows):
    """Share reads across portfolios and atomically publish a bounded claim."""
    from uuid import UUID

    from app.db.snapshot_batch import snapshot_batch
    from app.db.snapshots import upsert_snapshots
    from app.engine.errors import MissingPriceError, NegativeHoldingError

    done, failed, points = [], [], []
    candidates = []
    for key, payload, attempts in rows:
        try:
            investor = str(UUID(payload["investor_id"]))
        except Exception as exc:
            failed.append((key, attempts, exc))
            continue
        candidates.append((key, payload, attempts, investor))
    existing = {
        row[0]
        for row in conn.execute(
            "SELECT id::text FROM investor WHERE id=ANY(%s::uuid[])",
            (sorted({row[3] for row in candidates}),),
        ).fetchall()
    }
    live = []
    for key, payload, attempts, investor in candidates:
        if investor not in existing:
            payload.pop("unrepairable", None)
            done.append({"key": key, "payload": payload})
            continue
        try:
            on = date.fromisoformat(payload["on"])
            advised = payload["advised_only"]
            live.append((key, payload, attempts, investor, advised, on))
        except Exception as exc:
            failed.append((key, attempts, exc))
    if live:
        dates = [row[5] for row in live]
        with snapshot_batch(conn, min(dates), max(dates)) as batch:
            batch.prefetch({(row[3], row[4]) for row in live})
            for key, payload, attempts, investor, advised, on in live:
                try:
                    point = batch.build(investor, advised, on)
                    points.append((investor, advised, point))
                    payload.pop("unrepairable", None)
                except (MissingPriceError, NegativeHoldingError) as exc:
                    if not payload.get("repair"):
                        failed.append((key, attempts, exc))
                        continue
                    payload["unrepairable"] = str(exc)[:300]
                except Exception as exc:
                    failed.append((key, attempts, exc))
                    continue
                done.append({"key": key, "payload": payload})
    with conn.pipeline():
        upsert_snapshots(conn, points)
        if done:
            conn.execute(
                """UPDATE reference_sync_task t SET state='done',payload=item->'payload',
                   last_error=NULL,updated_at=clock_timestamp(),attempts=0,
                   last_success_at=clock_timestamp(),available_at=now()+interval '1 second'
                   FROM jsonb_array_elements(%s::jsonb) item
                   WHERE t.kind='SNAPSHOT' AND t.task_key=item->>'key'""",
                (json.dumps(done),),
            )
        conn.commit()
    for key, attempts, exc in failed:
        _requeue(conn, "SNAPSHOT", key, attempts, exc)
    return len(done), len(failed)


#: Task kinds that call nseindia.com/api. Left pending on a deployment with
#: ``nse_api_enabled`` false, for ``app.nse_worker`` to run where NSE is reachable.
NSE_TASK_KINDS = ("NSE", "GAP")


def run_once(
    conn,
    settings: Settings,
    limit: int | None = None,
    time_budget_seconds: float | None = None,
) -> dict:
    from app.db.connection import session_connection

    with session_connection(conn, settings) as direct:
        return _run_once(direct, settings, limit, time_budget_seconds)


def _run_once(conn, settings, limit=None, time_budget_seconds=None):
    if not settings.reference_sync_enabled:
        return {"enabled": False, "processed": 0, "failed": 0}
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (_WORKER_LOCK,))
        acquired = cur.fetchone()[0]
    if not acquired:
        return {"enabled": True, "busy": True, "processed": 0, "failed": 0}
    from contextlib import ExitStack

    from app.db.snapshot_batch import snapshot_batch
    from app.prices.pipe import NSE_BHAVCOPY_EARLIEST

    batch_scope = ExitStack()
    batch_scope.enter_context(
        snapshot_batch(conn, NSE_BHAVCOPY_EARLIEST, date.today(), queued=True)
    )
    processed = waiting = failed = 0
    began = time.monotonic()
    timings = {"scheduling": 0.0, "preparation": 0.0, "snapshot_batches": 0.0}
    try:
        # We hold the sole worker lock: 'running' now means an earlier process died.
        with conn.cursor() as cur:
            cur.execute("UPDATE reference_sync_task SET state='pending' WHERE state='running'")
        queue.schedule(conn)
        identity.schedule_identity(conn)
        _schedule_actions(conn)
        conn.commit()
        timings["scheduling"] = (time.monotonic() - began) * 1000
        preparing = time.monotonic()
        try:
            # Demerger price factors for analytics series. Best-effort and self-retrying:
            # a notice whose exchange files are not reachable simply waits for next run.
            from app.db.price_adjustments import resolve_demerger_adjustments

            logger.info("Demerger price factors: %s", resolve_demerger_adjustments(conn))
        except Exception:
            conn.rollback()
            logger.warning("demerger_price_factors_failed", exc_info=True)
        _maybe_verify_quote_mappings(conn)
        timings["preparation"] = (time.monotonic() - preparing) * 1000
        # AMFI tasks run only on a host that can reach AMFI, with the feature on -- the
        # same capability split as NSE -- and at most AMFI_TASKS_PER_BATCH of every five
        # claims while other kinds are due (docs/MF_ETF_PLAN.md section 7.2).
        from app.prices.amfi_jobs import AMFI_TASK_KINDS, AMFI_TASKS_PER_BATCH

        amfi_runnable = settings.mf_reference_enabled and settings.amfi_fetch_enabled
        claims = amfi_claims = 0
        for _ in range(limit or settings.reference_worker_batch_size):
            # A serverless cron has a hard timeout; stop claiming new tasks once the
            # budget is spent. Unclaimed work stays pending for the next run.
            if time_budget_seconds is not None and (time.monotonic() - began > time_budget_seconds):
                break
            remaining = (
                (limit or settings.reference_worker_batch_size) - processed - waiting - failed
            )
            if remaining <= 0:
                break
            snapshot_rows = None
            with conn.cursor() as cur:
                over_quota = amfi_claims >= AMFI_TASKS_PER_BATCH * (claims // 5 + 1)
                row = None
                for allow_amfi in ((amfi_runnable and not over_quota), amfi_runnable):
                    cur.execute(
                        """SELECT kind,task_key,payload,attempts FROM reference_sync_task
                           WHERE state IN ('pending','retry') AND available_at <= now()
                             AND (%s OR kind <> ALL(%s))
                             AND (%s OR kind <> ALL(%s))
                           ORDER BY
                             CASE WHEN %s THEN
                               CASE WHEN kind IN ('IDENTITY','ACTION','REPLAY','SNAPSHOT')
                                      OR (kind='GAP' AND NOT (payload ? 'research_isins'))
                                    THEN 1 ELSE 0 END
                               ELSE 0 END,
                             CASE
                               WHEN kind IN ('IDENTITY','ACTION','REPLAY') THEN 0
                               WHEN kind='GAP' AND NOT (payload ? 'research_isins') THEN 0
                               WHEN kind='SNAPSHOT' THEN 1
                               WHEN kind='GAP' THEN 3
                               ELSE 2
                             END,
                             CASE WHEN kind='SNAPSHOT' THEN payload->>'on' END DESC,
                             available_at, kind, task_key
                           LIMIT 1 FOR UPDATE SKIP LOCKED""",
                        (
                            settings.nse_api_enabled,
                            list(NSE_TASK_KINDS),
                            allow_amfi,
                            list(AMFI_TASK_KINDS),
                            claims % 5 == 4,
                        ),
                    )
                    row = cur.fetchone()
                    # Over quota, AMFI work waits only while something else is due.
                    if row is not None or not over_quota:
                        break
                if row is None:
                    break
                kind, key, payload, attempts = row
                claims += 1
                amfi_claims += kind in AMFI_TASK_KINDS
                if kind == "SNAPSHOT" and payload.get("investor_id") and remaining > 1:
                    with conn.pipeline():
                        claimed = conn.execute(
                            """WITH picked AS (
                                 SELECT kind,task_key FROM reference_sync_task
                                 WHERE kind='SNAPSHOT' AND state IN ('pending','retry')
                                   AND available_at<=now()
                                 ORDER BY (task_key=%s) DESC,payload->>'on' DESC,task_key
                                 LIMIT %s FOR UPDATE SKIP LOCKED
                               )
                               UPDATE reference_sync_task t
                               SET state='running',updated_at=now()
                               FROM picked p WHERE t.kind=p.kind AND t.task_key=p.task_key
                               RETURNING t.task_key,t.payload,t.attempts""",
                            (key, min(64, remaining)),
                        )
                        conn.commit()
                    snapshot_rows = claimed.fetchall()
                else:
                    cur.execute(
                        "UPDATE reference_sync_task SET state='running',updated_at=now() "
                        "WHERE kind=%s AND task_key=%s",
                        (kind, key),
                    )
            if snapshot_rows is None:
                conn.commit()
            try:
                if snapshot_rows:
                    started = time.monotonic()
                    try:
                        complete, errors = _process_snapshot_claim(conn, snapshot_rows)
                    except Exception as exc:
                        conn.rollback()
                        for task_key, _payload, task_attempts in snapshot_rows:
                            _requeue(conn, "SNAPSHOT", task_key, task_attempts, exc)
                        failed += len(snapshot_rows)
                        continue
                    processed += complete
                    failed += errors
                    timings["snapshot_batches"] += (time.monotonic() - started) * 1000
                    logger.info(
                        "snapshot_batch completed=%s failed=%s elapsed_ms=%.1f",
                        complete,
                        errors,
                        (time.monotonic() - started) * 1000,
                    )
                    continue
                checkpoint = process(conn, kind, key, payload, settings)
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE reference_sync_task SET state=%s, payload=%s::jsonb,
                           last_error=NULL, updated_at=now(), attempts=0,
                           last_success_at=CASE WHEN %s THEN now() ELSE last_success_at END,
                           available_at=now()+interval '1 second' WHERE kind=%s AND task_key=%s""",
                        (
                            "pending" if checkpoint else "done",
                            json.dumps(checkpoint or payload),
                            checkpoint is None,
                            kind,
                            key,
                        ),
                    )
                    if kind in ("ACTION", "IDENTITY") and checkpoint is None:
                        # Apply newly verified facts to accounts already imported.
                        cur.execute(
                            "UPDATE reference_sync_task SET state='pending',available_at=now() "
                            "WHERE kind='REPLAY'"
                        )
                conn.commit()
                if kind in ("REPLAY", "GAP", "ACTION", "IDENTITY", "NSE"):
                    from app.db.snapshot_batch import invalidate_batch

                    invalidate_batch()
                processed += 1
            except ResearchDeferred as exc:
                # Not a failure: the structured fixes are committed and research resumes
                # once the quota can take it. A fixed floor, not exponential backoff from
                # one minute, which spent the quota on retries that were always refused.
                _requeue(conn, kind, key, attempts, exc, min_delay=RESEARCH_PAUSE_SECONDS)
                waiting += 1
                logger.info("reference_task_research_deferred", extra={"task_key": key})
            except (identity.EvidencePending, NotPublished) as exc:
                # Evidence not available yet is an unresolved exception that retries
                # itself (invariant 6), not a failure of the source or the worker.
                _requeue(conn, kind, key, attempts, exc)
                waiting += 1
                logger.info(
                    "reference_task_waiting",
                    extra={
                        "kind": kind,
                        "task_key": key,
                        "reason_code": getattr(exc, "reason_code", type(exc).__name__),
                    },
                )
            except Exception as exc:
                # A source's Retry-After (AMFI 429/5xx) is a floor on the backoff.
                _requeue(
                    conn,
                    kind,
                    key,
                    attempts,
                    exc,
                    min_delay=getattr(exc, "retry_after", None) or 0,
                )
                failed += 1
                logger.warning(
                    "reference_task_failed",
                    extra={"kind": kind, "task_key": key, "error_type": type(exc).__name__},
                )
        promote_draft_aliases(conn)
        conn.commit()
        result = {
            "enabled": True,
            "processed": processed,
            "waiting": waiting,
            "failed": failed,
            "timings_ms": {name: round(value, 3) for name, value in timings.items()},
            "elapsed_ms": round((time.monotonic() - began) * 1000, 3),
        }
        if failed:
            raise ReferenceBatchFailed(result)
        return result
    finally:
        batch_scope.close()
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (_WORKER_LOCK,))
        conn.commit()


def _requeue(conn, kind, key, attempts, exc, min_delay: int = 0) -> None:
    """Roll back the task's partial work and schedule a retry with backoff."""
    conn.rollback()
    delay = max(min_delay, min(21600, 60 * 2 ** min(attempts, 9)))
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE reference_sync_task SET state='retry',attempts=attempts+1,
               last_error=%s,updated_at=now(),
               available_at=now() + %s * interval '1 second'
               WHERE kind=%s AND task_key=%s""",
            (f"{type(exc).__name__}: {str(exc)[:500]}", delay, kind, key),
        )
    conn.commit()


#: One worker cycle works through up to this many due tasks, or this long, before it
#: re-checks the schedule -- then loops straight on if it did any work.
WORKER_CYCLE_MAX_TASKS = 500
WORKER_CYCLE_SECONDS = 60.0


#: The evening check needs a new official close to have anything to do, and one provider
#: call when it does; ten minutes keeps a provider outage from being retried every cycle.
_QUOTE_CHECK_SECONDS = 10 * 60
_last_quote_check = float("-inf")


def _maybe_verify_quote_mappings(conn) -> None:
    """Check quote mappings against the newest official close (app.prices.quote_check),
    at most every ten minutes. Runs after whatever stored that close, in any job that
    reaches run_once. Never raises: an indicative-price check must not stop the queue."""
    global _last_quote_check
    if time.monotonic() - _last_quote_check < _QUOTE_CHECK_SECONDS:
        return
    _last_quote_check = time.monotonic()
    try:
        from app.prices.quote_check import verify_quote_mappings

        logger.info("Quote mapping check: %s", verify_quote_mappings(conn))
        conn.commit()
    except Exception:
        conn.rollback()
        logger.warning("quote_mapping_check_failed", exc_info=True)


#: The daily snapshot job otherwise runs only from Vercel's cron, so a machine running this
#: worker and nothing else never gets a new day of value history. Thirty minutes picks up
#: each day's bhavcopy soon after it lands; an up-to-date check is a single query.
_SNAPSHOT_CATCH_UP_SECONDS = 30 * 60
_last_snapshot_catch_up = float("-inf")


def _maybe_catch_up_snapshots(conn, settings: Settings) -> None:
    """Run the snapshot catch-up at most every thirty minutes. Never raises: a snapshot failure
    must not stop the reference queue, which the loop drains on its own schedule."""
    global _last_snapshot_catch_up
    if time.monotonic() - _last_snapshot_catch_up < _SNAPSHOT_CATCH_UP_SECONDS:
        return
    _last_snapshot_catch_up = time.monotonic()
    try:
        from app.prices.alerts import get_notifier
        from app.routers.analytical import catch_up_snapshots

        result = catch_up_snapshots(conn, get_notifier(settings))
        logger.info(
            "Snapshot catch-up: %s",
            {k: v for k, v in result.items() if k != "skipped_missing_price"},
        )
    except Exception:
        conn.rollback()
        logger.exception("Snapshot catch-up failed; retried in thirty minutes")
    _maybe_refresh_funds(conn, settings)


def _maybe_refresh_funds(conn, settings: Settings) -> None:
    """On a host that runs AMFI work: the daily NAV refresh when no daily file has been
    published since today's 06:00 UTC run time (plan section 7.3 -- the worker uses its own
    scheduler when it hosts AMFI), then the fund series catch-up. Never raises."""
    from datetime import UTC, datetime

    try:
        from app.db.fund_reference import fund_tables_present

        if not fund_tables_present(conn):
            return
        if settings.mf_reference_enabled and settings.amfi_fetch_enabled:
            now = datetime.now(UTC)
            due = now.replace(hour=6, minute=0, second=0, microsecond=0)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT max(fetched_at) FROM amfi_batch WHERE layout='DAILY' "
                    "AND status IN ('PUBLISHED','QUARANTINED')"
                )
                last = cur.fetchone()[0]
            conn.commit()
            if now >= due and (last is None or last < due):
                from app.prices.amfi_jobs import refresh_daily

                result = refresh_daily(conn, timeout_seconds=settings.amfi_http_timeout_seconds)
                logger.info("AMFI daily refresh: %s", result.status)
        if settings.mf_valuation_enabled:
            from app.funds.series import catch_up_fund_snapshots

            fund = catch_up_fund_snapshots(conn, today=date.today(), time_budget_seconds=240)
            logger.info(
                "Fund snapshot catch-up: written=%s complete=%s", fund.written, fund.complete
            )
    except Exception:
        conn.rollback()
        logger.exception("Fund refresh/catch-up failed; retried in thirty minutes")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--probe-isin", help="Check live free sources for this ISIN; no database writes"
    )
    args = parser.parse_args()
    if args.probe_isin:
        # A deployment connectivity check, never a substitute for ingestion or proof
        # of historical coverage. The caller supplies the security; no sentinel list.
        end = date.today()
        start = end - timedelta(days=30)
        failed = False
        probes = {
            "NSDL": lambda: {
                "records": len(NsdlParticipantSource().search(args.probe_isin).records)
            },
            "NSE circulars": lambda: {
                "isin_notices": len(
                    parse_circular_listing(NseCircularListSource().list_circulars(start, end))
                )
            },
            "NSE actions": lambda: {
                "bonus_split_notices": len(
                    parse_corporate_action_feed(NseCorporateActionFeedSource().fetch(start, end))
                )
            },
        }
        for name, probe in probes.items():
            try:
                print(json.dumps({"source": name, "ok": True, **probe()}), flush=True)
            except Exception as exc:
                failed = True
                print(json.dumps({"source": name, "ok": False, "error": str(exc)}), flush=True)
        raise SystemExit(1 if failed else 0)
    settings = get_settings()
    logging.basicConfig(level=logging.INFO)
    while True:
        busy = False
        try:
            with connect(settings.database_url) as conn:
                result = run_once(
                    conn,
                    settings,
                    limit=WORKER_CYCLE_MAX_TASKS,
                    time_budget_seconds=WORKER_CYCLE_SECONDS,
                )
                _maybe_catch_up_snapshots(conn, settings)
            logger.info("Reference cycle: %s", result)
            busy = bool(result.get("processed"))
            if args.once:
                raise SystemExit(1 if result.get("failed") else 0)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            logger.exception("Reference cycle failed; work remains queued")
            if args.once:
                raise SystemExit(1) from None
        # Keep going while there is work; the poll interval is for an idle queue. Sleeping
        # between batches of five turned a one-minute snapshot rebuild (1,409 days at
        # ~0.014 s each) into two and a half hours of a half-drawn chart (2026-10-10).
        if not busy:
            time.sleep(settings.reference_worker_poll_seconds)


if __name__ == "__main__":
    main()
