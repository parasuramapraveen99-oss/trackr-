"""The BT-P02 hosted worker: bounded report and reflection jobs (RP 01).

docs/BUTLER_REPORTS.md section 3. A job kind of the EXISTING durable queue (``hosted_job_tick``
via app/jobs/drain.py) -- not a second scheduler, not an HTTP request. Runnable directly too::

    python -m app.jobs.hosted [--investor UUID]

WHAT ONE PASS DOES, PER RUNNABLE JOB:
  1. Recheck authority on the financial connection: the owner still holds Workspace and the
     investor still exists. Otherwise the job is cancelled with that reason.
  2. Honour CANCEL_REQUESTED and the deadline FIRST: settle any open reservation as UNKNOWN
     (charged in full -- the call may have gone out) and write CANCELLED / EXPIRED.
  3. Take a lease (committed) with a short-lived signed delegation, then run exactly ONE stage.
     The stage re-verifies the delegation before every disclosure and every write; 054 refuses
     its checkpoint if the lease went stale, the job was cancelled, the deadline passed or --
     for reflection -- consent changed.
  4. A crash anywhere leaves either nothing (before the lease commits), an expired lease and
     maybe an open reservation (recovered conservatively by the next pass), or a finished
     checkpoint (never repeated). Bounded: per-stage attempts are capped by the job, and there
     is no loop that keeps researching "until perfect".

REPORT STAGES BEYOND PLAN ARE NOT INSTALLED. RP 02 (sources) through RP 06 (PDF) are later
sessions. A report job therefore completes its deterministic PLAN (the coverage checklist) and
then FAILS with REPORT_STAGES_NOT_INSTALLED -- a truthful blocked explanation with no report
body, never a model-written stand-in. No model is invoked for a report in this session.

THE REPORT PATH NEVER IMPORTS NOTEBOOK CODE. Reflection handlers are imported lazily, inside
the reflection branch only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import psycopg

from app.config import Settings, get_settings
from app.database import workspace_connection
from app.db import hosted_jobs as hj
from app.db.capabilities import has_capability
from app.db.connection import WorkspaceCapability
from app.db.workspace import lock_tenant
from app.db.workspace_context import enter_workspace
from app.research import canonical

logger = logging.getLogger(__name__)

KIND_HOSTED_TICK = "hosted_job_tick"
ENGINE = WorkspaceCapability.ENGINE

REPORT_STAGES = ("PLAN", "SOURCE_DISCOVERY", "NORMALIZE", "DRAFT", "CHECK", "RENDER")
REPORT_CALL_TIMEOUT_SECONDS = 180.0
REFLECTION_STAGES = ("BUILD_EPISODES", "MODEL_PROPOSAL", "VALIDATE_AND_PERSIST")


@dataclass
class StageContext:
    conn: Any
    investor_id: str
    job: dict[str, Any]
    lease_id: str
    delegation: hj.Delegation
    signature: str
    settings: Settings
    now: datetime
    prior: list[dict[str, Any]] = field(default_factory=list)
    #: The financial connection (document store rows, authority). Report stages only.
    fin_conn: Any = None

    def prior_output(self, stage: str) -> dict[str, Any]:
        """A prior stage's raw checkpoint output (PLAN stores plain string fields)."""
        for c in reversed(self.prior):
            if c["stage"] == stage and c["outcome"] == "ACCEPTED":
                return c["output"]
        raise RuntimeError(f"stage {stage} has no accepted checkpoint")

    def authorised(self) -> bool:
        """Re-verify the signed delegation. Called before every disclosure and write."""
        key = self.settings.hosted_delegation_signing_key or ""
        return bool(key) and hj.verify(self.delegation, self.signature, key,
                                       datetime.now(UTC))


@dataclass
class StageOutcome:
    checkpoint_outcome: str  # ACCEPTED / ABSTAINED / REJECTED / BLOCKED
    output: dict[str, Any]
    input_digest: str
    #: Terminal (state, reason, detail) after this stage, or None to continue next pass.
    final: tuple[str, str, dict[str, Any]] | None = None


# =========================================================================================
# report stages (RP 01 installs PLAN only)
# =========================================================================================


def _report_plan(ctx: StageContext) -> StageOutcome:
    """The coverage checklist the later stages must fill: periods x statements x basis, all
    PENDING. Deterministic from the sealed request; no source is read and no model is called."""
    req = ctx.job["request"]
    latest = int(req.get("latest_fiscal_year") or _latest_fiscal_year(ctx.now.date()))
    years = int(req["window_years"])
    cells = [{"period": f"FY{y}", "statement": st, "basis": req["basis"], "status": "PENDING"}
             for y in range(latest - years + 1, latest + 1) for st in req["statements"]]
    output = {"coverage": {"isin": ctx.job["isin"], "requested_years": str(years),
                           "latest_fiscal_year": str(latest), "basis": req["basis"],
                           "cells": cells},
              "scope": req["scope"], "mode": req["mode"]}
    return StageOutcome("ACCEPTED", output, canonical.digest(req))


def _payload(ctx: StageContext, stage: str) -> dict[str, Any]:
    """A prior stage's output. Stored as one JSON STRING inside the checkpoint so integers in
    it never become JSON numbers (054's ws_string_numbers)."""
    for c in reversed(ctx.prior):
        if c["stage"] == stage and c["outcome"] == "ACCEPTED":
            return json.loads(c["output"]["payload"])
    raise RuntimeError(f"stage {stage} has no accepted checkpoint")


def _accept(body: dict[str, Any], final: tuple[str, str, dict[str, Any]] | None = None
            ) -> StageOutcome:
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return StageOutcome("ACCEPTED", {"payload": text,
                                     "sha256": hashlib.sha256(text.encode()).hexdigest()},
                        hashlib.sha256(text.encode()).hexdigest(), final=final)


def _policy() -> tuple[Any, Any, Any]:
    from app.db.workspace_privacy import export_verdict
    from app.research import source_policy as sp
    from app.research import sources as srcs

    return sp.LIVE_SOURCE_POLICY, export_verdict, srcs.LIVE_ADAPTERS


def _report_sources(ctx: StageContext) -> StageOutcome:
    """RP 02. Held, permitted issuer evidence only; a missing source is a gap, not a zero."""
    from app.research import report_evidence as rev

    policy, export_verdict, adapters = _policy()
    found = rev.discover(ctx.conn, ctx.investor_id, ctx.job["isin"], ctx.job["request"],
                         policy, export_verdict, adapters)
    return _accept(found)


def _report_normalize(ctx: StageContext) -> StageOutcome:
    """RP 03. Exact comparable cells and approved calculations (pure)."""
    from app.engine import research_metrics as rm
    from app.research import registry as reg
    from app.research import report_evidence as rev

    plan = ctx.prior_output("PLAN")
    latest = int(plan["coverage"]["latest_fiscal_year"])
    normal = rev.normalise(_payload(ctx, "SOURCE_DISCOVERY"), ctx.job["request"], latest,
                           reg.LIVE_REGISTRY, rm.LIVE_FORMULAS)
    return _accept(normal)


def _texts(ctx: StageContext, discovery: dict[str, Any]) -> dict[str, str]:
    """Retained extracted text for sources whose READ verdict allows it, rehashed against the
    extraction pin. A missing or changed text is simply absent (claims resting on it will be
    withheld as not reproducible); it is never re-extracted or guessed."""
    from app.research.source_policy import LIVE_SOURCE_POLICY, USE_READ
    from app.storage.factory import get_store

    if ctx.fin_conn is None:
        return {}
    store = None
    out: dict[str, str] = {}
    for s in discovery["sources"]:
        if not s.get("_text_document_id") or not s.get("_text_sha256") \
                or not LIVE_SOURCE_POLICY.allows(s["_source_type"], USE_READ):
            continue
        with ctx.fin_conn.cursor() as cur:
            cur.execute("SELECT storage_key FROM document WHERE investor_id = %s AND id = %s",
                        (ctx.investor_id, s["_text_document_id"]))
            row = cur.fetchone()
        if row is None:
            continue
        store = store or get_store(ctx.settings)
        try:
            data = store.get(row[0])
        except Exception:  # noqa: BLE001 - missing bytes are an absence, not a crash
            continue
        if hashlib.sha256(data).hexdigest() == s["_text_sha256"]:
            out[s["source_id"]] = data.decode("utf-8", errors="replace")
    return out


def _model_call(ctx: StageContext, system: str, packet: dict[str, Any],
                schema: dict[str, Any], tag: str) -> tuple[Any, dict[str, str], str | None]:
    """ONE bounded, budgeted platform call (the reflection pattern): reserve and COMMIT the
    worst case, re-check authority/state, call, settle at once. Returns (payload, usage,
    unavailable_reason). Never retried here; the stage's attempt cap bounds it."""
    from app.research import platform_inference as pi

    inv, job, job_id = ctx.investor_id, ctx.job, str(ctx.job["id"])
    try:
        pricing = pi.ready(ctx.settings)
    except pi.PlatformNotConfigured as exc:
        return None, {}, exc.reason
    content = json.dumps(packet, sort_keys=True)
    if len(content) // 2 > int(job["max_input_tokens"]):
        return None, {}, "PACKET_EXCEEDS_INPUT_CAP"
    amount = pricing.worst_case(int(job["max_input_tokens"]), int(job["max_output_tokens"]))
    reserve_id = hj.reserve(ctx.conn, inv, job_id, ctx.lease_id, amount)
    ctx.conn.commit()
    with enter_workspace(ctx.conn, ENGINE, inv):
        lock_tenant(ctx.conn, inv)
        state = hj.current_state(ctx.conn, inv, job_id)["state"]
        if not ctx.authorised() or state != "RUNNING" or _authority(ctx.fin_conn, inv):
            hj.settle(ctx.conn, inv, job_id, ctx.lease_id, reserve_id, "RELEASE", 0, None, None)
            ctx.conn.commit()
            return None, {}, "STOPPED_BEFORE_DISCLOSURE"
    ctx.conn.commit()
    result = pi.invoke(ctx.settings, "COMPANY_REPORT", system, content, schema,
                       int(job["max_output_tokens"]), REPORT_CALL_TIMEOUT_SECONDS)
    if hj._one(ctx.conn, "SELECT 1 AS x FROM hosted_job_charge WHERE investor_id = %s AND "
                         "reserve_id = %s", (inv, reserve_id)) is not None:
        ctx.conn.rollback()
        return None, {}, "CANCELLED_DURING_CALL"
    with enter_workspace(ctx.conn, ENGINE, inv):
        if result.outcome == "NOT_SENT":
            hj.settle(ctx.conn, inv, job_id, ctx.lease_id, reserve_id, "RELEASE", 0, None, None)
        elif result.outcome == "AMBIGUOUS":
            hj.settle(ctx.conn, inv, job_id, ctx.lease_id, reserve_id, "UNKNOWN", amount, None,
                      None)
        else:
            hj.settle(ctx.conn, inv, job_id, ctx.lease_id, reserve_id, "SETTLE",
                      pricing.actual(result.input_tokens or 0, result.output_tokens or 0),
                      result.input_tokens, result.output_tokens)
    ctx.conn.commit()
    usage = {"call": tag, "outcome": result.outcome,
             "input_tokens": str(result.input_tokens or 0),
             "output_tokens": str(result.output_tokens or 0)}
    if result.outcome != "OK" or result.payload is None:
        return None, usage, result.error_code or "MODEL_UNAVAILABLE"
    return result.payload, usage, None


def _report_draft(ctx: StageContext) -> StageOutcome:
    """RP 04. Controlled renderers first; then, if configured, ONE model call for labelled
    interpretation. The draft is retained as attributed output until CHECK."""
    from app.db import reports as rdb
    from app.engine import research_metrics as rm
    from app.research import report_claims as rc

    discovery = _payload(ctx, "SOURCE_DISCOVERY")
    normal = _payload(ctx, "NORMALIZE")
    texts = _texts(ctx, discovery)
    version = rdb.report_version(ctx.conn, ctx.investor_id, ctx.job)
    as_of = ctx.job["recorded_at"]
    draft = rc.build_document(ctx.job, ctx.job["request"], discovery, normal, texts,
                              rm.LIVE_FORMULAS, version, as_of)
    packet = rc.model_packet(draft, ctx.job["request"])
    packet_id = hashlib.sha256(json.dumps(packet, sort_keys=True).encode()).hexdigest()[:32]
    model_claims: list[dict[str, Any]] = []
    rejected: list[str] = []
    usage: dict[str, str] = {}
    limitation = None
    if not packet["facts"]:
        limitation = "INTERPRETATION_NOT_DRAFTED_NO_DISCLOSABLE_EVIDENCE"
    else:
        payload, usage, reason = _model_call(
            ctx, rc.REPORT_PROMPT, {**packet, "packet_id": packet_id}, rc.OUTPUT_SCHEMA,
            "DRAFT")
        if reason == "STOPPED_BEFORE_DISCLOSURE":
            return StageOutcome("BLOCKED", {"reason": reason}, packet_id,
                                final=("CANCELLED", reason, {}))
        if payload is None:
            limitation = "INTERPRETATION_NOT_DRAFTED"
        else:
            model_claims, rejected = rc.accept_model_claims(payload, packet, packet_id)
    if model_claims or limitation:
        draft = rc.build_document(ctx.job, ctx.job["request"], discovery, normal, texts,
                                  rm.LIVE_FORMULAS, version, as_of, model_claims, limitation)
    return _accept({"document": draft, "model": {"usage": usage, "rejected": rejected,
                                                 "packet_id": packet_id,
                                                 "prompt_version": rc.PROMPT_VERSION,
                                                 "accepted": str(len(model_claims))}})


CRITIQUE_PROMPT = (
    "You are an independent critic of a company research draft. You receive the claim ledger "
    "and source metadata only -- not the author's reasoning. Flag claims that are unsupported "
    "by their premises, overstate causation, omit contrary evidence present in the ledger, mix "
    "periods, units or bases, disguise a recommendation, or present an issuer's own claim as "
    "independent fact. Return only JSON matching the schema. You cannot approve anything: an "
    "unflagged claim is not verified."
)

CRITIQUE_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["packet_id", "flags"],
    "properties": {"packet_id": {"type": "string"}, "flags": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["claim_id", "code"],
        "properties": {"claim_id": {"type": "string"},
                       "code": {"type": "string", "enum": [
                           "UNSUPPORTED_INTERPRETATION", "CAUSAL_OVERREACH",
                           "MISSING_COUNTEREVIDENCE", "TEMPORAL_OR_UNIT_ERROR",
                           "DISGUISED_RECOMMENDATION", "MISLABELLED_ISSUER_CLAIM"]}}}}}}


def _current_rights(discovery: dict[str, Any]) -> dict[str, dict[str, str]]:
    from app.research.source_policy import (
        USE_EXTRACT,
        USE_METADATA,
        USE_MODEL_DISCLOSE,
        USE_STORE,
    )

    policy, export_verdict, _ = _policy()
    return {s["source_id"]: {"FETCH": policy.verdict(s["_source_type"], USE_METADATA),
                             "STORE": policy.verdict(s["_source_type"], USE_STORE),
                             "EXTRACT": policy.verdict(s["_source_type"], USE_EXTRACT),
                             "MODEL": policy.verdict(s["_source_type"], USE_MODEL_DISCLOSE),
                             "EXPORT": export_verdict(s["_source_type"])}
            for s in discovery["sources"]}


def _report_check(ctx: StageContext) -> StageOutcome:
    """RP 05. Independent checks, optional critique, rights fence, derived status."""
    from app.db import reports as rdb
    from app.db.migrate import report_documents_schema_ready
    from app.engine import research_metrics as rm
    from app.research import report_publish as rp

    discovery = _payload(ctx, "SOURCE_DISCOVERY")
    normal = _payload(ctx, "NORMALIZE")
    drafted = _payload(ctx, "DRAFT")
    draft = drafted["document"]
    texts = _texts(ctx, discovery)
    critique: dict[str, list[str]] = {}
    provenance: dict[str, Any] = {"ran": False}
    interpretations = [c for c in draft["claims"]
                       if c["claim_class"] in ("INTERPRETATION", "HYPOTHETICAL_RISK")]
    if interpretations:
        ledger = [{"claim_id": c["claim_id"], "claim_class": c["claim_class"],
                   "text": c["text"], "premise_claim_ids": c.get("premise_claim_ids", [])}
                  for c in draft["claims"]]
        packet = {"packet_id": drafted["model"]["packet_id"] + "-critique", "claims": ledger}
        payload, usage, reason = _model_call(ctx, CRITIQUE_PROMPT, packet, CRITIQUE_SCHEMA,
                                             "CRITIQUE")
        provenance = {"ran": payload is not None, "usage": usage, "unavailable": reason}
        if payload is not None and payload.get("packet_id") == packet["packet_id"]:
            for flag in payload.get("flags", [])[:200]:
                if isinstance(flag, dict) and isinstance(flag.get("claim_id"), str):
                    critique.setdefault(flag["claim_id"], []).append(str(flag.get("code")))
        else:
            # No independent critique ran. The interpretations stay LABELLED AI interpretation
            # (never a fact), and the cover says the critique did not run.
            draft["limitations"] = draft["limitations"] + [
                {"code": "CRITIQUE_NOT_RUN", "applies_to": "COVER"}]
    snaps = rp.snapshots(draft, normal, texts)
    result = rp.publish(draft, snaps, rm.LIVE_FORMULAS, _current_rights(discovery), critique,
                        provenance)
    counts = {"withheld": str(len(result.withheld)), "status": result.status}
    if result.document is None:
        why = "REPORT_BLOCKED" if result.status == "BLOCKED" else "REPORT_NEEDS_REVIEW"
        return _accept({"published": False, "status": result.status, "ledger": result.ledger},
                       final=("FAILED", why, {**counts, "note": "no report body was published; "
                                              "the reasons are in the job record"}))
    if not report_documents_schema_ready(ctx.fin_conn):
        return _accept({"published": False, "status": result.status, "ledger": result.ledger},
                       final=("FAILED", "REPORT_STORAGE_NOT_INSTALLED", counts))
    # The source TYPE of every registered source, so a later download can recheck the CURRENT
    # export verdict exactly (the published source record carries origin, not type).
    result.ledger["source_types"] = {s["source_id"]: s["_source_type"]
                                     for s in discovery["sources"]}
    doc_text = rp.canonical_text(result.document)
    ledger_text = rp.canonical_text(result.ledger)
    document_id = rdb.insert_document(
        ctx.conn, ctx.investor_id, str(ctx.job["id"]), ctx.job["isin"],
        int(result.document["report_version"]), result.status, ctx.job["contract_version"],
        doc_text, rp.sha256_text(doc_text), ledger_text, rp.sha256_text(ledger_text),
        len(result.document["claims"]), len(result.withheld))
    return _accept({"published": True, "status": result.status,
                    "report_document_id": document_id, "document_sha256": rp.sha256_text(doc_text),
                    "withheld": {k: v for k, v in result.withheld.items()}})


def _report_render(ctx: StageContext) -> StageOutcome:
    """RP 06. HTML and PDF from the ONE published document; manifests checked; bytes stored."""
    import uuid

    from app.db import reports as rdb
    from app.db.documents import store_document
    from app.research import report_contract as ct
    from app.research import report_render as rr
    from app.schemas.reports import ReportDocument
    from app.storage.factory import get_store

    checked = _payload(ctx, "CHECK")
    row = rdb.document_for_job(ctx.conn, ctx.investor_id, str(ctx.job["id"]))
    if row is None or row["id"] != checked["report_document_id"]:
        return StageOutcome("BLOCKED", {"reason": "DOCUMENT_MISSING"},
                            hashlib.sha256(b"missing").hexdigest(),
                            final=("FAILED", "DOCUMENT_MISSING", {}))
    doc = json.loads(row["document_json"])
    if ct.document_digest(ReportDocument.model_validate(doc)) is None:  # pragma: no cover
        raise RuntimeError("unreachable")
    # The document was fully checked and published by CHECK; the artifact check needs its
    # published status and (published) claim set, which is exactly what it carries.
    findings = ct.Findings(status=doc["status"])
    code = _investor_code(ctx.fin_conn, ctx.investor_id)
    store = get_store(ctx.settings)
    out = {}
    for fmt in ("HTML", "PDF"):
        if fmt == "HTML":
            data, masked = rr.render_html(doc)
            pages, replaced = None, 0
        else:
            data, pages, replaced, masked = rr.render_pdf(doc)
        artifact_id = str(uuid.uuid4())
        manifest, problems = rr.manifest(doc, fmt, data, pages, artifact_id, findings)
        if problems:
            return StageOutcome("BLOCKED", {"reason": "ARTIFACT_REFUSED",
                                            "problems": problems},
                                hashlib.sha256(data).hexdigest(),
                                final=("FAILED", "ARTIFACT_REFUSED", {"format": fmt}))
        manifest = {**manifest, "withheld_from_export": masked,
                    "characters_replaced": str(replaced)}
        stored = store_document(
            ctx.fin_conn, store, ctx.investor_id, code, "RESEARCH_REPORT", data,
            ctx.now.date(), filename=f"report-{ctx.job['isin']}-v{doc['report_version']}."
            f"{fmt.lower()}", content_type="application/pdf" if fmt == "PDF"
            else "text/html; charset=utf-8", actor_uid=None, parse_status="RESEARCH_ONLY")
        ctx.fin_conn.commit()
        rdb.insert_artifact(ctx.conn, ctx.investor_id, row["id"], fmt, manifest["sha256"],
                            len(data), pages, str(stored["id"]),
                            rr.RENDERER_PDF if fmt == "PDF" else rr.RENDERER_HTML, manifest,
                            artifact_id)
        out[fmt] = {"artifact_id": artifact_id, "sha256": manifest["sha256"],
                    "pages": str(pages) if pages else None, "withheld": str(len(masked))}
    return _accept({"artifacts": out},
                   final=(doc["status"], "REPORT_PUBLISHED",
                          {"report_document_id": row["id"]}))


def _investor_code(fin_conn: Any, investor_id: str) -> str:
    with fin_conn.cursor() as cur:
        cur.execute("SELECT code FROM investor WHERE id = %s", (investor_id,))
        row = cur.fetchone()
    if row is None or not (row[0] or "").strip():
        raise RuntimeError("investor has no storage code")
    return str(row[0])


def _latest_fiscal_year(today: date) -> int:
    """Indian fiscal years end 31 March; the latest COMPLETED one as of today."""
    return today.year if today.month > 3 else today.year - 1


REPORT_HANDLERS = {"PLAN": _report_plan, "SOURCE_DISCOVERY": _report_sources,
                   "NORMALIZE": _report_normalize, "DRAFT": _report_draft,
                   "CHECK": _report_check, "RENDER": _report_render}


# =========================================================================================
# the pass
# =========================================================================================


def _authority(fin_conn: Any, investor_id: str) -> str | None:
    """None when the job may continue, else the cancellation reason."""
    with fin_conn.cursor() as cur:
        cur.execute("SELECT owner_user_id::text FROM investor WHERE id = %s", (investor_id,))
        row = cur.fetchone()
    if row is None or row[0] is None:
        return "INVESTOR_DELETED"
    if not has_capability(fin_conn, row[0], "workspace"):
        return "OWNER_ACCESS_REVOKED"
    return None


def _settle_open_unknown(conn: Any, inv: str, job_id: str) -> int:
    latest = hj._one(conn, "SELECT id FROM hosted_job_lease WHERE investor_id = %s AND "
                           "job_id = %s ORDER BY attempt_no DESC LIMIT 1", (inv, job_id))
    n = 0
    for r in hj.open_reservations(conn, inv, job_id):
        hj.settle(conn, inv, job_id, str(latest["id"]), str(r["id"]), "UNKNOWN", r["amount"],
                  None, None)
        n += 1
    return n


def _finish(conn: Any, inv: str, job_id: str, state: str, reason: str,
            detail: dict[str, Any]) -> None:
    unknown = _settle_open_unknown(conn, inv, job_id)
    if unknown:
        detail = {**detail, "unknown_charges": str(unknown)}
    hj.append_event(conn, inv, job_id, state, None, reason, detail)


def _handlers(purpose: str) -> tuple[tuple[str, ...], dict[str, Any]]:
    if purpose == "COMPANY_REPORT":
        return REPORT_STAGES, REPORT_HANDLERS
    from app.research import butler_reflection as br  # reflection branch only

    return REFLECTION_STAGES, br.STAGE_HANDLERS


def step_job(conn: Any, fin_conn: Any, inv: str, job_id: str, settings: Settings,
             now: datetime | None = None) -> str:
    """Advance one job by at most one stage. Returns what happened (for the pass summary)."""
    now = now or datetime.now(UTC)
    with enter_workspace(conn, ENGINE, inv):
        lock_tenant(conn, inv)
        job = hj.job_row(conn, inv, job_id)
        state = hj.current_state(conn, inv, job_id)["state"]
        if state in hj.TERMINAL:
            conn.rollback()
            return "TERMINAL"
        reason = _authority(fin_conn, inv)
        if reason is not None and state != "CANCEL_REQUESTED":
            hj.append_event(conn, inv, job_id, "CANCEL_REQUESTED", None, reason, {})
            state = "CANCEL_REQUESTED"
        if state == "CANCEL_REQUESTED":
            _finish(conn, inv, job_id, "CANCELLED", reason or "CANCELLED_ON_REQUEST", {})
            conn.commit()
            return "CANCELLED"
        if now >= job["deadline_at"]:
            _finish(conn, inv, job_id, "EXPIRED", "DEADLINE_REACHED", {})
            conn.commit()
            return "EXPIRED"
        if hj.open_reservations(conn, inv, job_id):
            # Crash recovery: a call may or may not have gone out. Charge it; do not resend.
            _settle_open_unknown(conn, inv, job_id)
        stages, handlers = _handlers(job["purpose"])
        done = {c["stage"] for c in hj.checkpoints(conn, inv, job_id)}
        pending = [s for s in stages if s not in done]
        if not pending:
            _finish(conn, inv, job_id, "FAILED", "NO_TERMINAL_STAGE", {})
            conn.commit()
            return "FAILED"
        stage = pending[0]
        key = settings.hosted_delegation_signing_key
        if not key:
            _finish(conn, inv, job_id, "FAILED", "DELEGATION_KEY_NOT_CONFIGURED", {})
            conn.commit()
            return "FAILED"
        try:
            lease_id, delegation, signature = hj.take_lease(conn, inv, job_id, stage, key)
        except psycopg.errors.RaiseException as exc:
            conn.rollback()
            msg = str(exc)
            if "attempts exhausted" in msg:
                with enter_workspace(conn, ENGINE, inv):
                    _finish(conn, inv, job_id, "FAILED", "STAGE_ATTEMPTS_EXHAUSTED",
                            {"stage": stage})
                conn.commit()
                return "FAILED"
            return "LEASE_REFUSED"
        if state != "RUNNING":
            hj.append_event(conn, inv, job_id, "RUNNING", stage, "STAGE_STARTED", {})
    conn.commit()  # the lease is durable BEFORE any work

    ctx = StageContext(conn, inv, job, lease_id, delegation, signature, settings, now,
                       fin_conn=fin_conn)
    try:
        with enter_workspace(conn, ENGINE, inv):
            ctx.prior = hj.checkpoints(conn, inv, job_id)
            if not ctx.authorised():
                raise PermissionError("delegation invalid or expired")
            result = handlers[stage](ctx)
        # A stage may have committed mid-way (reflection commits its budget reservation before
        # the call), which ends the transaction-local tenant scope -- so persist under a fresh
        # scope. Re-entering within the same transaction is harmless.
        with enter_workspace(conn, ENGINE, inv):
            lock_tenant(conn, inv)
            if not ctx.authorised():
                raise PermissionError("delegation expired before persist")
            hj.write_checkpoint(conn, inv, job_id, lease_id, stage, result.checkpoint_outcome,
                                result.input_digest, result.output)
            if result.final is not None:
                st, why, detail = result.final
                _finish(conn, inv, job_id, st, why, {**detail, "stage": stage})
        conn.commit()
        return f"{stage}:{result.checkpoint_outcome}"
    except psycopg.errors.RaiseException as exc:
        # 054/053 refused the output: cancelled, stale lease, deadline, consent changed or
        # budget exhausted. Nothing landed; the next pass records the terminal state.
        conn.rollback()
        msg = str(exc)
        try:
            with enter_workspace(conn, ENGINE, inv):
                lock_tenant(conn, inv)
                if "budget exhausted" in msg:
                    hj.append_event(conn, inv, job_id, "BUDGET_PAUSED", stage,
                                    "BUDGET_EXHAUSTED", {})
                    _finish(conn, inv, job_id, "FAILED", "BUDGET_EXHAUSTED", {"stage": stage})
                elif "consent not current" in msg:
                    hj.append_event(conn, inv, job_id, "CANCEL_REQUESTED", stage,
                                    "CONSENT_REVOKED", {})
            conn.commit()
        except psycopg.Error:
            conn.rollback()  # already cancelled/terminal: the next pass settles it
        return "OUTPUT_REFUSED"
    except Exception:
        conn.rollback()
        logger.exception("hosted job %s stage %s failed", job_id, stage)
        return "STAGE_ERROR"  # lease expires; bounded retry by max_stage_attempts


def tick_investor(fin_conn: Any, inv: str, settings: Settings,
                  now: datetime | None = None, connection: Any = None) -> dict[str, Any]:
    def run(conn: Any) -> dict[str, Any]:
        with enter_workspace(conn, ENGINE, inv):
            jobs = hj.runnable_jobs(conn, inv)
        conn.commit()
        return {"investor_id": inv,
                "jobs": {j: step_job(conn, fin_conn, inv, j, settings, now) for j in jobs}}

    if connection is not None:
        return run(connection)
    with workspace_connection(ENGINE) as conn:
        return run(conn)


def tick(fin_conn: Any, investor_ids: list[str] | None = None,
         settings: Settings | None = None) -> list[dict[str, Any]]:
    from app.db.migrate import hosted_jobs_schema_ready

    settings = settings or get_settings()
    if not hosted_jobs_schema_ready(fin_conn):
        return [{"investor_id": None, "outcome": "SCHEMA_NOT_READY"}]
    with fin_conn.cursor() as cur:
        cur.execute("SELECT id::text FROM investor WHERE owner_user_id IS NOT NULL AND "
                    "(%s::uuid[] IS NULL OR id = ANY(%s::uuid[])) ORDER BY id",
                    (investor_ids, investor_ids))
        ids = [r[0] for r in cur.fetchall()]
    results = []
    for inv in ids:
        try:
            if settings.hosted_jobs_enabled:
                from app.research import butler_reflection as br

                br.admit_daily(fin_conn, inv, settings)
            results.append(tick_investor(fin_conn, inv, settings))
        except Exception:
            logger.exception("hosted tick failed for investor %s", inv)
            results.append({"investor_id": inv, "outcome": "FAILED"})
    return results


def handle_job(conn: Any, store: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """``app/jobs/drain.py`` handler for ``hosted_job_tick``. Payload: {"investor_id"?: uuid}."""
    from app.db.butler import parse_uuid

    investor = payload.get("investor_id")
    ids = [parse_uuid(investor)] if investor else None
    if investor and ids == [None]:
        from app.jobs.drain import PermanentJobError

        raise PermanentJobError(f"investor_id is not a uuid: {investor!r}")
    results = tick(conn, ids)
    return {"investors": len(results)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one hosted-job worker pass.")
    parser.add_argument("--investor", action="append", help="limit to this investor id")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    from app.db.connection import connect, database_url

    with connect(database_url()) as fin_conn:
        for r in tick(fin_conn, args.investor):
            print(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
