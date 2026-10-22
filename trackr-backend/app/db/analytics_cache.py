"""``analytics_report_cache``: serve a computed advisor response only if nothing changed.

THE VERSION. ``data_version`` = (count, min id, max id) of ``analytics_input_change``
(migration 089) + a hash of the backend source tree. A statement trigger on every
analytics-input table writes one marker row per writing transaction, INSIDE that
transaction, so a marker becomes visible atomically with the data it describes:

  * a write committed before the version is read is visible to the computation;
  * a write committed after it adds a marker -> the next read differs -> recompute.

The set of visible markers only grows (pruning removes the oldest, which raises the
minimum id), so equal (count, min, max) means the identical set: no stale hit.

The first design read ``pg_stat_user_tables`` write counters. Measured on scratch they
are published asynchronously: a request right after an in-place price correction saw
the old counters and was served the old report. That design is rejected.

The source hash makes every deploy or local code change invalidate every row, so no
one has to remember to bump a version when the projection or engine changes.

COVERAGE. The trigger is attached to every public table except EXCLUDED_* below, which
must match migration 089. ``tests/test_db_analytics_cache.py`` fails if a non-excluded
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

#: Bump when the projection in app/routers/analytics_advisor.py changes shape or meaning.
PROJECTION_VERSION = "advisor-v2-projection-2026-10-22"
CACHE_METHOD = f"{METHOD_VERSION}+{PROJECTION_VERSION}"

#: Tables that never feed the advisor report. Prefixes cover whole feature families.
EXCLUDED_TABLES = frozenset(
    {
        "analytics_input_change",  # the markers themselves
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


def data_version(conn: Any) -> str:
    """Input-change markers + backend source hash. Read BEFORE computing a report."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*), min(id), max(id) FROM analytics_input_change")
        count, low, high = cur.fetchone()
    payload = json.dumps([source_version(), count, low, high], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def is_excluded(table: str) -> bool:
    return table in EXCLUDED_TABLES or table.startswith(EXCLUDED_PREFIXES)


def get(conn: Any, key: CacheKey, version: str) -> dict | None:
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
    return row[0] if row else None


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
                Jsonb(report),
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
