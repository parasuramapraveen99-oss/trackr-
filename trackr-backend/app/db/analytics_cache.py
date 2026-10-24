"""``analytics_report_cache``: serve a computed advisor response only if nothing changed.

THE VERSION. ``data_version`` = (count, min id, max id) of ``analytics_input_change``
markers that apply to one investor -- the shared ones (investor_id NULL) and that
investor's own -- + a hash of the backend source tree. Triggers on every analytics-input
table write a marker INSIDE the writing transaction, only when rows actually changed, and
scoped to the owning investor where the table has one (migrations 089, 093). A marker
becomes visible atomically with the data it describes:

  * a write committed before the version is read is visible to the computation;
  * a write committed after it adds a marker -> the next read differs -> recompute.

Each set of visible markers (shared, and per investor) only grows (pruning removes the
oldest, which raises the minimum id), so equal (count, min, max) means the identical set:
no stale hit. One investor's snapshot rebuild therefore leaves every other investor's
cached report valid, and a statement that changed no rows invalidates nothing.

The first design read ``pg_stat_user_tables`` write counters. Measured on scratch they
are published asynchronously: a request right after an in-place price correction saw
the old counters and was served the old report. That design is rejected.

The source hash makes every deploy or local code change invalidate every row, so no
one has to remember to bump a version when the projection or engine changes.

COVERAGE. The triggers are attached to every public table except EXCLUDED_* below, which
must match migrations 089 and 093. ``tests/test_db_analytics_cache.py`` fails if a non-excluded
table lacks the trigger, so a table added later cannot silently escape invalidation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from functools import cache
from pathlib import Path
from typing import Any

from app.analytics._vendor.advisor.service import METHOD_VERSION
from app.analytics.transport import compact_report, expand_report

#: Bump when the projection in app/routers/analytics_advisor.py changes shape or meaning.
PROJECTION_VERSION = "advisor-v2-projection-2026-10-22"
CACHE_METHOD = f"{METHOD_VERSION}+{PROJECTION_VERSION}"

#: Tables that never feed the advisor report. Prefixes cover whole feature families.
EXCLUDED_TABLES = frozenset(
    {
        "analytics_input_change",  # the markers themselves
        "reconciliation_statement",
        "reconciliation_run",  # comparison evidence, not a financial input
        "analytics_report_cache",  # itself: every put would otherwise invalidate
        "schema_migrations",
        "scratch_database_tag",
        "job",
        "app_session",
        "audit_log",
        "ws_receipt",
        "feed_poll_state",
        "document",
        "document_password",
        "import_batch",  # staging; promotion writes `transaction`, which is included
        "import_row",
        # Background-queue bookkeeping (migration 093): the report reads no queue table,
        # and the catch-up cycle writes these on every run.
        "reference_sync_task",
        "reference_sync_dependency",
        "reference_sync_import",
        "agent",
        "skill",
        "ritual",
        "trigger_evaluation",
        "evaluation_fact",
        "user_capability",
        "grievance",
        "legal_hold",
        "retention_policy",
    }
)
EXCLUDED_PREFIXES = (
    "butler_",
    "chat_",
    "email_",
    "hosted_job",
    "market_assistant_",
    "monitor_",
    "narrative_",
    "note",
    "question",
    "report_",
    "research_",
    "task",
    "thesis",
)


@dataclass(frozen=True, slots=True)
class CacheKey:
    investor_id: str
    as_of: date
    advised_only: bool
    benchmark: str
    universe: str


@cache
def source_version() -> str:
    """SHA-256 over every backend Python source file, computed once per process."""
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256(CACHE_METHOD.encode())
    for path in sorted(root.rglob("*.py")):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def data_version(conn: Any, investor_id: str) -> str:
    """Shared + this investor's input-change markers + backend source hash. Read BEFORE
    computing a report."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FILTER (WHERE investor_id IS NULL), "
            "min(id) FILTER (WHERE investor_id IS NULL), "
            "max(id) FILTER (WHERE investor_id IS NULL), "
            "count(*) FILTER (WHERE investor_id = %s), "
            "min(id) FILTER (WHERE investor_id = %s), "
            "max(id) FILTER (WHERE investor_id = %s) "
            "FROM analytics_input_change WHERE investor_id IS NULL OR investor_id = %s",
            (investor_id,) * 4,
        )
        markers = list(cur.fetchone())
    payload = json.dumps([source_version(), *markers], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def is_excluded(table: str) -> bool:
    return table in EXCLUDED_TABLES or table.startswith(EXCLUDED_PREFIXES)


def get(conn: Any, key: CacheKey, version: str, *, compact: bool = False) -> dict | None:
    """The stored response for ``key`` only if computed under exactly ``version``."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT report FROM analytics_report_cache WHERE investor_id=%s AND as_of=%s "
            "AND advised_only=%s AND benchmark=%s AND universe=%s "
            "AND method_version=%s AND data_version=%s",
            (
                key.investor_id,
                key.as_of,
                key.advised_only,
                key.benchmark,
                key.universe,
                CACHE_METHOD,
                version,
            ),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return compact_report(row[0]) if compact else expand_report(row[0])


def put(conn: Any, key: CacheKey, version: str, report: dict, compute_ms: int) -> None:
    from psycopg.types.json import Jsonb

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO analytics_report_cache (investor_id, as_of, advised_only, benchmark, "
            "universe, method_version, data_version, report, compute_ms) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (investor_id, as_of, advised_only, benchmark, universe) DO UPDATE SET "
            "method_version=EXCLUDED.method_version, data_version=EXCLUDED.data_version, "
            "report=EXCLUDED.report, computed_at=now(), compute_ms=EXCLUDED.compute_ms",
            (
                key.investor_id,
                key.as_of,
                key.advised_only,
                key.benchmark,
                key.universe,
                CACHE_METHOD,
                version,
                Jsonb(compact_report(report)),
                compute_ms,
            ),
        )
    conn.commit()


def prune(conn: Any, before: date) -> dict[str, int]:
    """Drop past-date reports and markers older than ``before``.

    Removing markers raises the minimum id, which changes every version once: all rows
    recompute on next read. Run it right before the warm job, which recomputes them.
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM analytics_report_cache WHERE as_of < %s", (before,))
        reports = cur.rowcount
        cur.execute("DELETE FROM analytics_input_change WHERE changed_at < %s", (before,))
        markers = cur.rowcount
    conn.commit()
    return {"reports": reports, "markers": markers}
