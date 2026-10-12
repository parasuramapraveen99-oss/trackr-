"""DPDP coverage for the twenty-five Workspace research tables.

**Why this module exists separately from ``privacy.py``.** The rights are the same rights and
there is exactly one export endpoint and one erasure endpoint -- ``privacy.py`` calls into
here, and no parallel privacy route is added. What is different is the *inventory*: twenty-five
tables, each needing a decision about how it is exported, what it depends on for deletion, and
which approved retention category covers it. That inventory is data, it will be read by a human
during a compliance review, and it belongs in one place where it can be read as a table rather
than inferred from five functions.

**TWO OPEN DECISIONS GATE LIVE USE, AND THIS MODULE REFUSES TO GUESS EITHER.**

*BE-Q05 (retention).* No category mapping or duration has been approved for research records.
Every entry below therefore carries ``retention=UNMAPPED``, and ``readiness_blockers`` reports
that as a blocker. It deliberately does NOT inherit the existing five-year investor default:
research notes are not advisory records under SEBI Reg. 19 and nobody has decided that they
are. An invented duration would be a compliance claim written by a program.

*BE-Q04 (source rights).* No per-source fetching/quoting/export verdict has been recorded.
``EXPORT_VERDICTS`` therefore marks every source type ``UNDECIDED``, and the export excludes
those rows *with a stated reason* rather than either shipping them or silently dropping them.
The mechanism is complete and tested; only the verdicts are missing, and they are a human's to
supply.

The distinction that matters: **the machinery is implemented, the policy is not.** Fixture
work can proceed against this; live persistent research writes cannot be enabled until
``readiness_blockers`` returns empty.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.db import audit
from app.db.connection import WorkspaceCapability
from app.db.migrate import WORKSPACE_TABLES

#: An approved retention category has not been assigned. The only value any research table
#: carries today. See BE-Q05.
RETENTION_UNMAPPED = "UNMAPPED"

#: A source type whose export rights have not been decided. See BE-Q04.
VERDICT_UNDECIDED = "UNDECIDED"
#: Exportable in full, once somebody records that verdict.
VERDICT_EXPORT_ALLOWED = "EXPORT_ALLOWED"
#: Metadata may be exported; the retained text or bytes may not.
VERDICT_METADATA_ONLY = "METADATA_ONLY"

#: source_type -> export verdict. EVERY ENTRY IS UNDECIDED, and that is the current truth
#: rather than a placeholder to be filled in by whoever next needs the feature to work.
#:
#: A licensing verdict is per source and per USE: fetching, storing, extracting, quoting,
#: disclosing to a model and exporting are six separate permissions, and only the last one is
#: this table's business. `USER_DOCUMENT` looks obviously exportable -- it is the investor's own
#: upload -- but "obviously" is how an unreviewed verdict enters a compliance document, so it
#: waits with the rest.
EXPORT_VERDICTS: dict[str, str] = {
    "EXCHANGE_FILING": VERDICT_UNDECIDED,
    "ANNUAL_REPORT": VERDICT_UNDECIDED,
    "RESULTS": VERDICT_UNDECIDED,
    "PRESENTATION": VERDICT_UNDECIDED,
    "TRANSCRIPT": VERDICT_UNDECIDED,
    "SHAREHOLDING": VERDICT_UNDECIDED,
    "NEWS": VERDICT_UNDECIDED,
    "MANAGEMENT_COMMENTARY": VERDICT_UNDECIDED,
    "USER_DOCUMENT": VERDICT_UNDECIDED,
    "BOOK": VERDICT_UNDECIDED,
}


@dataclass(frozen=True)
class TableCoverage:
    """How one workspace table is exported, deleted and retained.

    ``columns`` is an explicit allowlist, never ``SELECT *``. Two reasons, and the second is
    the one that bites: internal fields must not leak into a portability export --
    ``creation_xid`` is a transaction id, ``dedupe_key`` is idempotency material that can
    encode request content -- and a ``SELECT *`` would silently start exporting every column
    a future migration adds, which is a privacy change nobody reviewed.
    """

    table: str
    #: Export key in the returned document, or None when the table is not exported at all.
    export_key: str | None
    #: Columns to export, in order. Empty when ``export_key`` is None.
    columns: tuple[str, ...]
    #: Why this table is deleted when it is -- the dependency a reader needs to understand.
    deletion: str
    #: Approved retention category. UNMAPPED until BE-Q05 is answered.
    retention: str = RETENTION_UNMAPPED
    #: Privacy data category, for the compliance reader and for purpose checks. Not a retention
    #: decision. INVESTOR_NOTEBOOK is the sensitive BT 12 category: observations about a
    #: person's recorded research process. HOSTED_JOB is BT-P02 job/budget/checkpoint records.
    category: str = "RESEARCH_RECORD"


#: EVERY ONE OF THE TWENTY-FIVE TABLES, including the ones nobody would think to ask for:
#: superseded notes and theses, rejected and guardrail-blocked output, sample attempts, sealed
#: child collections, paused schedules and blocked monitor occurrences. An export that quietly
#: omitted a rejected result would be an export of the flattering half of the record.
#:
#: Deletion is uniform and is stated per row anyway, because "cascades from investor" is a fact
#: a compliance reader should be able to confirm table by table rather than take on trust.
_CASCADE = "Cascades from investor (ON DELETE CASCADE on investor_id)."

COVERAGE: tuple[TableCoverage, ...] = (
    TableCoverage("company", "research_companies", ("isin", "created_at"), _CASCADE),
    TableCoverage(
        "research_source",
        "research_sources",
        (
            "id",
            "isin",
            "source",
            "source_type",
            "source_url",
            "publication_date",
            "period",
            "retrieved_at",
            "document_reference",
            "document_id",
            "adapter_id",
            "origin_item_id",
            "revises_source_id",
            "recorded_at",
        ),
        _CASCADE + " Its document rows cascade in the same statement; see erase_workspace.",
    ),
    TableCoverage(
        "research_fact",
        "research_facts",
        (
            "id",
            "isin",
            "source_id",
            "metric",
            "basis",
            "unit",
            "period_start",
            "period_end",
            "cadence",
            "numeric_value",
            "boolean_value",
            "text_value",
            "verification",
            "verification_reference",
            "supersedes_id",
            "registry_version",
            "recorded_at",
        ),
        _CASCADE,
    ),
    TableCoverage(
        "note",
        "research_notes",
        (
            "id",
            "isin",
            "kind",
            "title",
            "body",
            "written_on",
            "recorded_at",
            "actor_uid",
            "authorship",
            "primary_source_id",
            "previous_id",
        ),
        _CASCADE,
    ),
    TableCoverage(
        "note_link",
        "research_note_links",
        (
            "id",
            "from_note_id",
            "to_note_id",
            "source_id",
            "thesis_id",
            "relation",
            "recorded_at",
            "actor_uid",
        ),
        _CASCADE,
    ),
    TableCoverage(
        "ritual", "research_rituals", ("id", "isin", "actor_uid", "recorded_at"), _CASCADE
    ),
    TableCoverage(
        "question",
        "research_questions",
        ("id", "isin", "note_id", "ritual_id", "blocking", "recorded_at"),
        _CASCADE,
    ),
    TableCoverage(
        "question_answer",
        "research_answers",
        ("id", "question_id", "answer", "actor_uid", "answered_on", "recorded_at", "previous_id"),
        _CASCADE,
    ),
    TableCoverage(
        "thesis",
        "research_theses",
        (
            "id",
            "isin",
            "version",
            "status",
            "formed_on",
            "version_on",
            "recorded_at",
            "actor_uid",
            "argument",
            "anti_thesis",
            "pre_mortem",
            "intended_holding_period",
            "variables",
            "exits",
            "draft_reasons",
        ),
        _CASCADE,
    ),
    TableCoverage(
        "trigger_evaluation",
        "research_evaluations",
        (
            "id",
            "thesis_id",
            "evaluated_at",
            "evidence_cutoff",
            "engine_version",
            "input_digest",
            "state",
            "rule_results",
        ),
        _CASCADE,
    ),
    TableCoverage(
        "evaluation_fact",
        "research_evaluation_inputs",
        ("evaluation_id", "fact_id"),
        _CASCADE,
    ),
    TableCoverage(
        "thesis_event",
        "research_thesis_events",
        (
            "id",
            "thesis_id",
            "kind",
            "conviction",
            "reason",
            "evaluation_id",
            "actor_uid",
            "occurred_on",
            "recorded_at",
        ),
        _CASCADE,
    ),
    TableCoverage(
        "skill",
        "research_skills",
        (
            "id",
            "version",
            "name",
            "summary",
            "method",
            "reads",
            "citation_rules",
            "permitted_tools",
            "forbidden_conclusions",
            "output_contract",
            "house_rules_version",
            "enabled",
            "recorded_at",
        ),
        _CASCADE,
    ),
    TableCoverage(
        "agent",
        "research_agents",
        # No credential column exists to omit -- there is none in the schema. base_url and
        # model are configuration the investor chose and are theirs to take with them.
        (
            "id",
            "version",
            "name",
            "provider",
            "model",
            "base_url",
            "role_description",
            "skill_id",
            "skill_version",
            "enabled",
            "actor_uid",
            "recorded_at",
        ),
        _CASCADE,
    ),
    TableCoverage(
        "task",
        "research_tasks",
        (
            "id",
            "isin",
            "purpose",
            "title",
            "brief",
            "skill_id",
            "skill_version",
            "agent_id",
            "agent_version",
            "thesis_id",
            "answer_id",
            "chat_thread_id",
            "parent_task_id",
            "created_by",
            "created_at",
            "context_cutoff",
            "context_digest",
            "context_manifest",
            "house_rules_version",
        ),
        _CASCADE,
    ),
    TableCoverage(
        "task_context",
        "research_task_context",
        (
            "id",
            "task_id",
            "ordinal",
            "note_id",
            "thesis_id",
            "answer_id",
            "source_id",
            "fact_id",
            "evaluation_id",
        ),
        _CASCADE,
    ),
    TableCoverage(
        "task_attempt",
        "research_task_attempts",
        # mode and sample_ref are exported deliberately: an export that hid which answers came
        # from a stored fixture would misrepresent the record.
        ("id", "task_id", "attempt_no", "mode", "sample_ref", "started_at", "lease_until"),
        _CASCADE,
    ),
    TableCoverage(
        "task_result",
        "research_task_results",
        # Includes FAILED, CANCELLED, EXPIRED and guardrail-BLOCKED rows.
        (
            "id",
            "task_id",
            "attempt_id",
            "outcome",
            "output",
            "structured_output",
            "guardrail_status",
            "error_code",
            "finished_at",
            "duration_ms",
        ),
        _CASCADE,
    ),
    TableCoverage(
        "result_citation",
        "research_result_citations",
        ("task_id", "result_id", "context_id", "claim_ref"),
        _CASCADE,
    ),
    TableCoverage(
        "task_review",
        "research_task_reviews",
        (
            "id",
            "task_id",
            "result_id",
            "decision",
            "reason",
            "accepted_note_id",
            "reviewer_uid",
            "reviewed_at",
        ),
        _CASCADE,
    ),
    TableCoverage(
        "chat_thread",
        "research_chat_threads",
        ("id", "isin", "title", "actor_uid", "created_at"),
        _CASCADE,
    ),
    TableCoverage(
        "chat_message",
        "research_chat_messages",
        # body is '' for ASSISTANT turns by construction; the text is in the referenced result,
        # which is exported above. The export preserves that relationship rather than inlining
        # a second copy the schema deliberately does not keep.
        ("id", "thread_id", "role", "body", "actor_uid", "result_id", "recorded_at"),
        _CASCADE,
    ),
    TableCoverage(
        "monitor_schedule",
        "research_monitor_schedules",
        (
            "id",
            "version",
            "isin",
            "thesis_id",
            "skill_id",
            "skill_version",
            "agent_id",
            "agent_version",
            "kind",
            "timezone",
            "starts_at",
            "interval_days",
            "enabled",
            "actor_uid",
            "recorded_at",
        ),
        _CASCADE,
    ),
    TableCoverage(
        "monitor_run",
        "research_monitor_runs",
        (
            "id",
            "isin",
            "schedule_id",
            "schedule_version",
            "thesis_id",
            "due_at",
            "recorded_at",
            "event_fact_id",
            "task_id",
            "disposition",
            "reason",
        ),
        _CASCADE,
    ),
    TableCoverage(
        "monitor_report",
        "research_monitor_reports",
        ("id", "run_id", "result_id", "evaluation_id", "title", "lede", "recorded_at"),
        _CASCADE,
    ),
    # 047. NOT EXPORTED: an idempotency receipt is operation name, key, payload digest and the
    # identifiers a write produced. Every identifier it names is exported through its own
    # table above; the key and digest are request-handling material, not facts about the
    # investor -- the same reasoning that keeps `dedupe_key` in NEVER_EXPORTED.
    TableCoverage("ws_receipt", None, (), _CASCADE),
    # 048. Exported, and restricted by its source's export verdict through `source_id` like
    # every other source-derived row. Names the retained text artifact's document row, which
    # the existing document erasure path removes with the investor's other objects.
    TableCoverage(
        "research_source_extraction",
        "research_source_extractions",
        (
            "id",
            "source_id",
            "isin",
            "status",
            "unavailable_reason",
            "extractor_id",
            "extractor_version",
            "normalization",
            "content_sha256",
            "text_sha256",
            "text_document_id",
            "page_count",
            "recorded_at",
        ),
        _CASCADE + " Its text_document_id row is a `document` removed by the document "
        "erasure path; the FK has no ON DELETE action, so both go in the investor cascade.",
    ),    # 051 -- Butler (BT 01). Every Butler record is exported, including forgotten memories
    # (the tombstone is itself a fact about what the investor asked), dismissed and snoozed
    # inbox items, superseded reviews and failed worker effects. The outbox, its claims and
    # the tick receipts are exported too: they are the evidence of what the worker did and
    # did not deliver, and an export showing only delivered cards would be the flattering
    # half. Retention is UNMAPPED like every research table until BE-Q05 is decided.
    TableCoverage(
        "butler_preference",
        "butler_preferences",
        ("id", "version", "timezone", "quiet_start", "quiet_end", "digest_limit", "actor_uid",
         "recorded_at"),
        _CASCADE,
    ),
    TableCoverage(
        "butler_subscription",
        "butler_subscriptions",
        ("id", "subject_kind", "isin", "topic", "topic_aliases", "actor_uid", "recorded_at"),
        _CASCADE,
    ),
    TableCoverage(
        "butler_subscription_event",
        "butler_subscription_events",
        ("id", "subscription_id", "version", "action", "checkpoint_days", "actor_uid",
         "recorded_at"),
        _CASCADE,
    ),
    TableCoverage(
        "butler_memory",
        "butler_memories",
        ("id", "isin", "ref_kind", "note_id", "answer_id", "thesis_id", "chat_message_id",
         "actor_uid", "recorded_at"),
        _CASCADE,
    ),
    TableCoverage(
        "butler_memory_event",
        "butler_memory_events",
        ("id", "memory_id", "action", "superseded_by", "actor_uid", "recorded_at"),
        _CASCADE,
    ),
    TableCoverage(
        "butler_source_retraction",
        "butler_source_retractions",
        ("id", "isin", "source_id", "reason_code", "replacement_source_id", "actor_uid",
         "recorded_at"),
        _CASCADE,
    ),
    TableCoverage(
        "butler_outbox",
        "butler_outbox_events",
        ("id", "event_kind", "isin", "record_id", "recorded_at"),
        _CASCADE,
    ),
    TableCoverage(
        "butler_outbox_claim",
        "butler_outbox_claims",
        ("id", "outbox_id", "attempt_no", "claimed_at", "lease_until"),
        _CASCADE,
    ),
    TableCoverage(
        "butler_outbox_effect",
        "butler_outbox_effects",
        ("id", "outbox_id", "claim_id", "outcome", "reason_code", "inbox_item_id",
         "recorded_at"),
        _CASCADE,
    ),
    TableCoverage(
        "butler_inbox_item",
        "butler_inbox_items",
        ("id", "card_kind", "priority", "isin", "subscription_id", "template_id",
         "template_version", "rule_version", "issue_key", "evidence_version", "args",
         "source_id", "thesis_id", "evaluation_id", "supersedes_item_id", "deliver_after",
         "recorded_at"),
        _CASCADE,
    ),
    TableCoverage(
        "butler_inbox_event",
        "butler_inbox_events",
        ("id", "item_id", "version", "action", "snooze_until", "actor_uid", "recorded_at"),
        _CASCADE,
    ),
    TableCoverage(
        "butler_review",
        "butler_reviews",
        ("id", "isin", "decision_kind", "thesis_id", "checklist_id", "checklist_version",
         "question_ids", "trigger_item_id", "supersedes_review_id", "actor_uid", "recorded_at"),
        _CASCADE,
    ),
    TableCoverage(
        "butler_review_response",
        "butler_review_responses",
        ("id", "review_id", "question_id", "answer", "attested", "not_applicable_reason",
         "milestone_on", "previous_id", "actor_uid", "recorded_at"),
        _CASCADE,
    ),
    TableCoverage(
        "butler_review_evidence",
        "butler_review_evidence",
        ("id", "response_id", "ordinal", "source_id", "note_id"),
        _CASCADE,
    ),
    TableCoverage(
        "butler_review_completion",
        "butler_review_completions",
        ("id", "review_id", "choice", "reason", "unresolved", "actor_uid", "recorded_at"),
        _CASCADE,
    ),
    TableCoverage(
        "butler_tick",
        "butler_worker_ticks",
        ("id", "started_at", "finished_at", "outcome", "processed", "cards", "suppressed",
         "failed", "oldest_pending_at"),
        _CASCADE,
    ),
    # 052_research_execution.sql (BT 09): explicit choices, exported in full, erased with the
    # investor. Neither holds free text.
    TableCoverage(
        "butler_teaching_preference",
        "butler_teaching_preferences",
        ("id", "version", "detail", "lesson_cadence", "follow_ups", "actor_uid", "recorded_at"),
        _CASCADE,
    ),
    TableCoverage(
        "butler_feedback",
        "butler_item_feedback",
        ("id", "subject_kind", "subject_id", "subject_version", "inbox_item_id", "rating",
         "actor_uid", "recorded_at"),
        _CASCADE,
    ),
)

#: 053 -- the investor notebook (BT 12-15). A SENSITIVE category (BUTLER_INVESTOR_MEMORY section
#: 7): exported in full to the person it describes -- the authorised-export exception -- and to no
#: one else. BE-Q05 must map it before live writes; it must not inherit a research-notes label.
#: The exclusion rows are the replay fence and are exported too: they show what was deleted.
_NOTEBOOK = "INVESTOR_NOTEBOOK"
_JOB = "HOSTED_JOB"
COVERAGE = COVERAGE + (
    TableCoverage(
        "butler_learning_consent", "butler_notebook_consent",
        ("id", "version", "action", "disclosure_version", "sources", "include_from",
         "reflection_enabled", "actor_uid", "recorded_at"),
        _CASCADE, category=_NOTEBOOK,
    ),
    TableCoverage(
        "butler_self_description", "butler_notebook_self_descriptions",
        ("id", "isin", "statement", "previous_id", "actor_uid", "recorded_at"),
        _CASCADE + " Also removed by the notebook purge.", category=_NOTEBOOK,
    ),
    TableCoverage(
        "butler_episode", "butler_notebook_episodes",
        ("id", "catalogue_id", "polarity", "observation_code", "source_category",
         "comparability_key", "isin", "occurred_on", "knowable_at", "anchor_kind", "anchor_id",
         "review_id", "thesis_id", "prior_thesis_id", "response_id", "evaluation_id",
         "limitations", "builder_version", "consent_version", "supersedes_id", "recorded_at"),
        _CASCADE + " Also removed by the notebook purge.", category=_NOTEBOOK,
    ),
    TableCoverage(
        "butler_pattern", "butler_notebook_patterns",
        ("id", "catalogue_id", "scope_key", "recorded_at"),
        _CASCADE + " Also removed by the notebook purge.", category=_NOTEBOOK,
    ),
    TableCoverage(
        "butler_pattern_revision", "butler_notebook_pattern_revisions",
        ("id", "pattern_id", "revision", "status", "origin", "supporting_episode_ids",
         "counter_episode_ids", "eligible_count", "supporting_count", "counter_count",
         "undetermined_count", "distinct_dates", "denominator_known", "window_start",
         "window_end", "alternative_ids", "question_ids", "limitations", "reason_code",
         "consent_version", "reflection_job_id", "rule_version", "evaluated_at",
         "recorded_at"),
        _CASCADE + " Also removed by the notebook purge.", category=_NOTEBOOK,
    ),
    TableCoverage(
        "butler_notebook_event", "butler_notebook_controls",
        ("id", "action", "pattern_id", "episode_id", "self_description_id", "correction_code",
         "note", "actor_uid", "recorded_at"),
        _CASCADE + " Also removed by the notebook purge.", category=_NOTEBOOK,
    ),
    TableCoverage(
        "butler_notebook_exclusion", "butler_notebook_exclusions",
        ("id", "target_kind", "anchor_id", "catalogue_id", "scope_key", "before_at",
         "reason_code", "actor_uid", "recorded_at"),
        _CASCADE + " KEPT by the notebook purge: identifiers only, it is what stops a rebuild "
        "resurrecting deleted material.", category=_NOTEBOOK,
    ),
    TableCoverage(
        "butler_coaching_use", "butler_notebook_influence",
        ("id", "question_id", "pattern_revision_id", "review_id", "consent_version",
         "recorded_at"),
        _CASCADE + " Also removed by the notebook purge.", category=_NOTEBOOK,
    ),
    # 055 -- dated corporate events (BE 29/34). Excluded per source like facts: an event
    # inherits its source's export verdict through source_id.
    TableCoverage(
        "research_event", "research_events",
        ("id", "isin", "source_id", "event_type", "status", "event_date", "date_precision",
         "period_start", "period_end", "description", "locator", "verification",
         "verified_by", "verification_reference", "supersedes_id", "reschedules_id",
         "recorded_at"),
        _CASCADE,
    ),
    # 056 -- published report documents and artifacts (RP 05/06). The export carries their
    # identity, status, digests and counts. The document TEXT is withheld from the export while
    # BE-Q04 export verdicts are undecided, because it quotes source material; the artifact
    # bytes are in the document store and follow the document export path.
    TableCoverage(
        "report_document", "report_documents",
        ("id", "job_id", "isin", "report_version", "status", "contract_version",
         "document_sha256", "ledger_sha256", "claims_published", "claims_withheld",
         "recorded_at"),
        _CASCADE, category=_JOB,
    ),
    TableCoverage(
        "report_artifact", "report_artifacts",
        ("id", "report_document_id", "format", "sha256", "byte_size", "page_count",
         "document_id", "renderer", "recorded_at"),
        _CASCADE + " Its bytes are a document row, erased with the investor's documents.",
        category=_JOB,
    ),
    TableCoverage(
        "report_retraction", "report_retractions",
        ("id", "report_document_id", "reason", "detail", "actor_uid", "recorded_at"),
        _CASCADE, category=_JOB,
    ),
    # 054 -- hosted jobs (RP 01). Exported in full including failed, cancelled and budget-paused
    # jobs and every charge row: spend and failures are part of the record. No credential exists
    # in any of them to omit.
    TableCoverage(
        "hosted_job", "hosted_jobs",
        ("id", "purpose", "trigger_kind", "isin", "request", "request_digest",
         "contract_version", "revision_of", "consent_version", "provider", "model",
         "price_version", "currency", "budget_limit", "max_input_tokens", "max_output_tokens",
         "max_stage_attempts", "deadline_at", "local_day", "actor_uid", "recorded_at"),
        _CASCADE, category=_JOB,
    ),
    TableCoverage(
        "hosted_job_event", "hosted_job_events",
        ("id", "job_id", "version", "state", "stage", "reason_code", "detail", "actor_uid",
         "recorded_at"),
        _CASCADE, category=_JOB,
    ),
    TableCoverage(
        "hosted_job_lease", "hosted_job_leases",
        ("id", "job_id", "attempt_no", "stage", "lease_seconds", "claimed_at", "lease_until"),
        _CASCADE, category=_JOB,
    ),
    TableCoverage(
        "hosted_job_checkpoint", "hosted_job_checkpoints",
        ("id", "job_id", "lease_id", "stage", "stage_version", "outcome", "input_digest",
         "output_digest", "output", "recorded_at"),
        _CASCADE, category=_JOB,
    ),
    TableCoverage(
        "hosted_job_charge", "hosted_job_charges",
        ("id", "job_id", "lease_id", "kind", "reserve_id", "amount", "input_tokens",
         "output_tokens", "recorded_at"),
        _CASCADE, category=_JOB,
    ),
)

#: The categories a report-generation packet, a support-assistant context or a share flow may
#: never read. Enforced in app/research/report_contract.py and by `make lint`.
PURPOSE_EXCLUDED_FROM_REPORTS: frozenset[str] = frozenset({_NOTEBOOK})

#: Fields that must never appear in an export, asserted rather than assumed. ``creation_xid`` is
#: an internal transaction id; ``dedupe_key`` is idempotency material that can encode request
#: content and is not a fact about the investor.
NEVER_EXPORTED: frozenset[str] = frozenset({"creation_xid", "dedupe_key", "seq"})


def _assert_inventory_complete() -> None:
    """Every workspace table appears exactly once, and no export leaks an internal field.

    Import-time, not test-time. A table added by a future migration and not added here would
    silently fall out of both the export and the compliance inventory -- an absence nobody
    would notice until a data-principal request arrived. Failing at import is noisy on purpose.
    """
    covered = [entry.table for entry in COVERAGE]
    missing = sorted(set(WORKSPACE_TABLES) - set(covered))
    unknown = sorted(set(covered) - set(WORKSPACE_TABLES))
    duplicated = sorted({t for t in covered if covered.count(t) > 1})
    if missing or unknown or duplicated:
        raise RuntimeError(
            "app/db/workspace_privacy.py COVERAGE is out of step with "
            f"app/db/migrate.py WORKSPACE_TABLES: missing={missing}, unknown={unknown}, "
            f"duplicated={duplicated}. Every workspace table needs an export, deletion and "
            "retention decision before it can hold data."
        )
    for entry in COVERAGE:
        leaked = sorted(NEVER_EXPORTED & set(entry.columns))
        if leaked:
            raise RuntimeError(
                f"{entry.table} would export internal field(s) {leaked}; these are not facts "
                "about the investor."
            )


_assert_inventory_complete()


def _apply_approved_policy() -> None:
    """Load the owner-approved BE-Q04 export verdicts and BE-Q05 retention mapping, if any.

    ``app/research/policy_approval.py`` binds the approval to the proposal's exact digest. With
    no valid approval nothing changes: every export verdict stays UNDECIDED and every table
    UNMAPPED, so ``readiness_blockers`` keeps research writes closed."""
    global COVERAGE
    from dataclasses import replace

    from app.research.policy_approval import approved_policy

    policy = approved_policy()
    if policy is None:
        return
    for source_type, entry in policy.source_rights.items():
        verdict = entry["export"]
        if verdict not in (VERDICT_EXPORT_ALLOWED, VERDICT_METADATA_ONLY):
            raise ValueError(f"approved export verdict {source_type}={verdict} is not valid")
        EXPORT_VERDICTS[source_type] = verdict
    mapping = {cat: entry["entity_type"] for cat, entry in policy.retention.items()}
    missing = sorted({e.category for e in COVERAGE} - set(mapping))
    if missing:
        raise ValueError(f"approved retention mapping omits categor(ies) {missing}")
    COVERAGE = tuple(replace(e, retention=mapping[e.category]) for e in COVERAGE)


_apply_approved_policy()


def readiness_blockers(conn: Any) -> list[str]:
    """What must be decided before live persistent research writes may be enabled. Empty is go.

    **A missing policy fails readiness; it never defaults.** The tempting shortcut is for
    unmapped research to inherit the existing five-year investor default -- which would be this
    program deciding a retention period, and would also be wrong: research notes are not
    advisory records under SEBI Reg. 19 and nobody has decided what they are.

    Read this as the answer to "can we turn research on", not as a health check. It is cheap and
    takes a connection only so the retention_policy table can be consulted for categories that
    HAVE been approved, once any are.
    """
    blockers: list[str] = []

    unmapped = sorted({e.table for e in COVERAGE if e.retention == RETENTION_UNMAPPED})
    if unmapped:
        blockers.append(
            f"BE-Q05 unresolved: {len(unmapped)} workspace table(s) have no approved retention "
            f"category ({', '.join(unmapped[:4])}"
            f"{', ...' if len(unmapped) > 4 else ''}). A policy owner must map each to a "
            "category and a start event. Research must not inherit the five-year investor "
            "default by omission."
        )
    else:
        with conn.cursor() as cur:
            cur.execute("SELECT entity_type FROM retention_policy")
            known = {row[0] for row in cur.fetchall()}
        absent = sorted({e.retention for e in COVERAGE} - known)
        from app.research.policy_approval import approved_policy

        policy = approved_policy()
        if policy is not None and not absent:
            with conn.cursor() as cur:
                cur.execute("SELECT entity_type, retain_years FROM retention_policy")
                years = {row[0]: row[1] for row in cur.fetchall()}
            drift = sorted(e["entity_type"] for e in policy.retention.values()
                           if years.get(e["entity_type"]) != int(e["retain_years"]))
            if drift:
                blockers.append(
                    f"retention_policy years for {drift} differ from the approved mapping; "
                    "the database and the approval must agree before research writes open.")
        if absent:
            blockers.append(
                f"retention_policy has no row for approved categor(ies) {absent}; the mapping "
                "names a category the database does not define."
            )

    undecided = sorted(k for k, v in EXPORT_VERDICTS.items() if v == VERDICT_UNDECIDED)
    if undecided:
        blockers.append(
            f"BE-Q04 unresolved: no export verdict recorded for source type(s) {undecided}. "
            "Their rows are excluded from exports with a stated reason, which is honest but is "
            "not coverage."
        )
    return blockers


def export_verdict(source_type: str) -> str:
    """The recorded export verdict for a source type. Unknown types are UNDECIDED.

    An unrecognised source type resolves to UNDECIDED rather than raising, because a source
    type added by a future migration must exclude-with-a-reason, not break an investor's
    export of everything else.
    """
    return EXPORT_VERDICTS.get(source_type, VERDICT_UNDECIDED)


def export_workspace(conn: Any, investor_id: str) -> dict[str, Any]:
    """Every exportable workspace row for one investor, plus what was excluded and why.

    Returns a mapping of export keys to row lists, with two extra keys:

    * ``research_export_exclusions`` -- one entry per excluded row, naming the table, the row
      id, the source type and the verdict that excluded it. **An export that silently omitted
      restricted material would be indistinguishable from an export of an investor who had
      none**, so an exclusion is a positive statement, not a gap.
    * ``research_export_readiness`` -- the open blockers, so a reader of the export file knows
      the policy position at the time it was produced.

    Requires a transaction-local tenant scope to be set (``app/db/workspace_context.py``): RLS
    returns nothing without it, and an export that returned nothing because the scope was
    missing would look exactly like an investor with no research. ``privacy.py`` establishes the
    scope; this function verifies it rather than trusting it.

    Reads are ordered deterministically so two exports of unchanged data compare equal.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT nullif(current_setting('app.workspace_investor', true), '')")
        scope = cur.fetchone()[0]
    if scope is None:
        raise RuntimeError(
            "export_workspace requires a transaction-local tenant scope; without it RLS "
            "returns no rows and the export would falsely report an investor with no research."
        )
    if str(scope) != str(investor_id):
        raise RuntimeError(
            f"tenant scope is {scope} but the export is for {investor_id}; refusing to export "
            "one investor's research under another's name."
        )

    export: dict[str, Any] = {}
    exclusions: list[dict[str, Any]] = []

    # Which sources are excluded, resolved once. Facts, notes and citations that hang off an
    # excluded source are excluded with it: exporting a quoted figure while withholding the
    # filing it came from would export the restricted material and lose the provenance, which
    # is the worst of both. Checking raw file downloads alone would miss exactly this.
    restricted_sources: dict[str, str] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, source_type FROM research_source WHERE investor_id = %s ORDER BY id",
            (investor_id,),
        )
        for source_id, source_type in cur.fetchall():
            verdict = export_verdict(source_type)
            if verdict != VERDICT_EXPORT_ALLOWED:
                restricted_sources[str(source_id)] = verdict

    for entry in COVERAGE:
        if entry.export_key is None:
            continue
        columns = ", ".join(entry.columns)
        order = entry.columns[0]
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {columns} FROM {entry.table} WHERE investor_id = %s ORDER BY {order}",
                (investor_id,),
            )
            names = [c.name for c in cur.description]
            rows = [dict(zip(names, r, strict=True)) for r in cur.fetchall()]

        kept: list[dict[str, Any]] = []
        for row in rows:
            blocking = _restricting_source(entry.table, row, restricted_sources)
            if blocking is None:
                kept.append(row)
                continue
            source_id, verdict = blocking
            exclusions.append(
                {
                    "table": entry.table,
                    "row_id": str(row.get("id") or row.get("result_id") or ""),
                    "source_id": source_id,
                    "verdict": verdict,
                    "reason": (
                        "no export verdict has been recorded for this source (BE-Q04 open)"
                        if verdict == VERDICT_UNDECIDED
                        else f"source export verdict is {verdict}"
                    ),
                }
            )
        export[entry.export_key] = kept

    export["research_export_exclusions"] = exclusions
    export["research_export_readiness"] = readiness_blockers(conn)
    return export


def _restricting_source(
    table: str, row: dict[str, Any], restricted: dict[str, str]
) -> tuple[str, str] | None:
    """The source restricting this row, or None. Only source-derived tables can be restricted.

    Notes and results are the interesting cases: a note that cites a restricted filing may
    quote it, and a task result may quote whatever was pinned into its context. Both are
    withheld with their source rather than exported on the grounds that the file itself was
    not attached.
    """
    if table == "research_source":
        source_id = str(row.get("id") or "")
        verdict = restricted.get(source_id)
        return (source_id, verdict) if verdict else None
    for column in ("source_id", "primary_source_id"):
        source_id = row.get(column)
        if source_id is None:
            continue
        verdict = restricted.get(str(source_id))
        if verdict:
            return str(source_id), verdict
    return None


def workspace_row_counts(conn: Any, investor_id: str) -> dict[str, int]:
    """Row counts per workspace table for one investor. Used by erasure to report and verify.

    Counted BEFORE deletion and compared after, because "the DELETE statement succeeded" and
    "the rows are gone" are different claims: with no tenant scope selected, RLS makes every
    workspace table look empty and a DELETE removes nothing while returning success.
    """
    counts: dict[str, int] = {}
    with conn.cursor() as cur:
        for table in WORKSPACE_TABLES:
            cur.execute(f"SELECT count(*) FROM {table} WHERE investor_id = %s", (investor_id,))
            counts[table] = cur.fetchone()[0]
    return counts


def research_last_activity(conn: Any, investor_id: str) -> Any:
    """The most recent research receipt for one investor, or None.

    WHY RETENTION CANNOT KEEP USING TRANSACTIONS ALONE. ``retention.due_for_deletion`` measures
    inactivity from the last transaction date or the investor's creation date. An investor who
    reads filings, writes notes and revises a thesis every week but has not traded in six years
    is *active*, and under the transaction-only rule would appear on a deletion report. Whether
    research activity resets the retention clock is part of BE-Q05 and is not settled here; this
    function supplies the fact that decision needs, and ``retention.py`` uses it to keep such an
    investor off the report rather than to invent a period.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT max(latest) FROM (
                SELECT max(recorded_at) AS latest FROM note WHERE investor_id = %(i)s
                UNION ALL
                SELECT max(recorded_at) FROM question_answer WHERE investor_id = %(i)s
                UNION ALL
                SELECT max(recorded_at) FROM thesis WHERE investor_id = %(i)s
                UNION ALL
                SELECT max(recorded_at) FROM research_source WHERE investor_id = %(i)s
                UNION ALL
                SELECT max(recorded_at) FROM thesis_event WHERE investor_id = %(i)s
                UNION ALL
                SELECT max(created_at) FROM task WHERE investor_id = %(i)s
            ) activity
            """,
            {"i": investor_id},
        )
        return cur.fetchone()[0]


#: The capability erasure runs as. Named here so a reader of this module sees which identity the
#: privacy workflow needs, and so there is one place to look when it fails closed.
ERASURE_CAPABILITY = WorkspaceCapability.ERASER

#: audit_log action for a research-artifact deletion that is still incomplete and retryable.
ACTION_RESEARCH_ARTIFACTS_PENDING = "RESEARCH_ARTIFACTS_PENDING"


def record_pending_artifacts(
    conn: Any, investor_id: str, keys: list[str], actor_uid: str | None = None
) -> None:
    """Record storage keys that could not be deleted, so the work stays discoverable.

    STORAGE AND POSTGRES ARE SEPARATE FAILURE DOMAINS, and this is the consequence. A database
    rollback cannot un-delete an object, and an object deletion cannot be rolled back by a
    later SQL failure -- so the only safe order is objects first, rows second, and the only
    honest report of a partial object deletion is a durable, retryable list of what is left.

    Written to the existing ``audit_log`` rather than to a new retry table. A new table would
    need its own retention category and its own privacy coverage -- the very thing this module
    exists to enumerate -- and would be a job framework introduced on a guess. Storage KEYS
    only: never a note body, an excerpt or a signed URL.
    """
    if not keys:
        return
    audit.record(
        conn,
        ACTION_RESEARCH_ARTIFACTS_PENDING,
        "investor",
        entity_id=str(investor_id),
        actor_uid=actor_uid,
        diff={"pending_storage_keys": sorted(keys), "count": len(keys)},
    )
