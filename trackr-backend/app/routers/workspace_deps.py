"""Shared FastAPI plumbing for the Workspace research routers.

Everything a research route needs besides its own logic, in one place so both routers
(``workspace.py`` and ``research.py``) apply it identically:

* **Gates.** ``require_workspace_enabled`` (the operator flag AND a schema that can support
  it) guards every route; ``require_research_writes_ready`` additionally guards every WRITE
  until ``workspace_privacy.readiness_blockers`` is empty (BE-Q04/BE-Q05). Route registration
  alone never enables live research writes.
* **Sessions.** ``WorkspaceSessions.open`` opens one capability connection and scopes it to
  one investor/actor via ``enter_workspace``. The capability is chosen by the SERVER
  OPERATION (reader for reads, human for a person's writes, engine for receipts), never by
  anything in the request.
* **Idempotency-Key.** Parsed and canonicalised here; every WRITE requires a UUID.
* **Overridable definitions.** The metric registry, reporting calendar and source-use
  policy are dependencies so tests supply synthetic fixtures; production gets the LIVE
  objects: the BE-Q03 registry/calendar, and source verdicts only once BE-Q04 is approved.
* **Error translation.** A database guard's refusal (a blocking question, a version race,
  a forked chain) becomes the documented 409/422 instead of a 500. Anything unrecognised
  is re-raised untouched.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

import psycopg
from fastapi import Depends, Header
from psycopg import IsolationLevel

from app.config import Settings, get_settings
from app.database import get_conn, workspace_connection
from app.db.connection import (
    WorkspaceCapability,
    WorkspaceConnectionError,
    WorkspaceConnectionNotConfiguredError,
)
from app.db.migrate import assert_workspace_ready
from app.db.workspace_context import WorkspaceContextError, enter_workspace
from app.db.workspace_privacy import readiness_blockers
from app.research.errors import (
    BLOCKING_QUESTION_OPEN,
    EVALUATION_NOT_TRIGGERED,
    IDEMPOTENCY_CONFLICT,
    IDEMPOTENCY_KEY_REQUIRED,
    INVALID_INPUT,
    INVALID_TRANSITION,
    REVIEW_CLOSED,
    STALE_PREDECESSOR,
    VERSION_CONFLICT,
    WORKSPACE_DISABLED,
    WORKSPACE_UNAVAILABLE,
    WORKSPACE_WRITES_NOT_READY,
    WorkspaceError,
)
from app.research.registry import LIVE_CALENDAR, LIVE_REGISTRY, CalendarProvider
from app.research.source_policy import LIVE_SOURCE_POLICY, SourcePolicy

logger = logging.getLogger(__name__)

#: Set once the schema check has passed in this process. Enablement is checked at the
#: point of use, not per statement; a schema cannot become LESS ready while running.
_schema_ready = False


def require_workspace_enabled(
    settings: Settings = Depends(get_settings),
    conn: Any = Depends(get_conn),
) -> None:
    """503 WORKSPACE_DISABLED unless the operator enabled Workspace AND the complete,
    correctly-owned schema is applied (``assert_workspace_ready``)."""
    global _schema_ready
    if not settings.workspace_enabled:
        raise WorkspaceError(503, WORKSPACE_DISABLED, "research is not enabled on this server")
    if not _schema_ready:
        try:
            assert_workspace_ready(conn)
        except Exception as exc:
            logger.error("workspace enabled but not ready: %s", exc)
            raise WorkspaceError(503, WORKSPACE_UNAVAILABLE,
                                 "research storage is not ready on this server") from exc
        _schema_ready = True


def require_research_writes_ready(conn: Any = Depends(get_conn)) -> None:
    """503 WORKSPACE_WRITES_NOT_READY while retention (BE-Q05) or source-rights (BE-Q04)
    decisions are open. Reads stay available; persistent research writes do not."""
    blockers = readiness_blockers(conn)
    if blockers:
        raise WorkspaceError(
            503,
            WORKSPACE_WRITES_NOT_READY,
            "research writes are disabled until privacy decisions are recorded",
            {"blockers": [b.split(":", 1)[0] for b in blockers]},
        )


def idempotency_key(
    key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> str:
    """The canonical UUID from the ``Idempotency-Key`` header. Required on every WRITE."""
    if key is None or not key.strip():
        raise WorkspaceError(422, IDEMPOTENCY_KEY_REQUIRED,
                             "an Idempotency-Key header (UUID) is required",
                             {"Idempotency-Key": "REQUIRED"})
    try:
        return str(UUID(key.strip()))
    except ValueError as exc:
        raise WorkspaceError(422, IDEMPOTENCY_KEY_REQUIRED,
                             "Idempotency-Key must be a UUID",
                             {"Idempotency-Key": "FORMAT"}) from exc


class WorkspaceSessions:
    """Opens scoped capability connections. A dependency so tests can substitute
    fixture-owned connections without touching the routes."""

    @contextmanager
    def open(
        self,
        capability: WorkspaceCapability,
        investor_id: str,
        actor_uid: str | None = None,
        *,
        snapshot: bool = False,
    ) -> Iterator[Any]:
        """``snapshot=True`` makes every statement read one REPEATABLE READ snapshot --
        used by read-only evaluation so facts and aliases cannot change mid-assembly."""
        with workspace_connection(capability) as conn:
            if snapshot:
                conn.isolation_level = IsolationLevel.REPEATABLE_READ
            with enter_workspace(conn, capability, investor_id, actor_uid):
                yield conn


def get_workspace_sessions() -> WorkspaceSessions:
    return WorkspaceSessions()


def get_metric_registry() -> Any:
    return LIVE_REGISTRY


def get_formula_registry() -> Any:
    """The approved calculator registry (BE 27-28). Live: the BE-Q03 registry decided
    2026-09-29. Tests override this dependency with a synthetic registry built with
    ``synthetic=True``."""
    from app.engine.research_metrics import LIVE_FORMULAS

    return LIVE_FORMULAS


def get_source_adapters() -> dict[str, Any]:
    """Approved external-evidence adapters by id (BE 29). Live: none (BE-Q04). Tests override
    this dependency with ``app.research.sources.SyntheticAdapter`` instances."""
    from app.research.sources import LIVE_ADAPTERS

    return LIVE_ADAPTERS


def get_calendar_provider() -> CalendarProvider:
    return LIVE_CALENDAR


def get_source_policy() -> SourcePolicy:
    return LIVE_SOURCE_POLICY


# --- database error translation ----------------------------------------------------------

#: Guard messages raised by migrations 044/047-050, mapped to the documented contract.
_RAISE_MAP: tuple[tuple[str, int, str], ...] = (
    ("blocking question remains open", 409, BLOCKING_QUESTION_OPEN),
    ("thesis version conflict", 409, VERSION_CONFLICT),
    ("formation date is immutable", 409, VERSION_CONFLICT),
    ("invalid note revision", 409, STALE_PREDECESSOR),
    ("answer revision must name predecessor", 409, STALE_PREDECESSOR),
    ("override must name triggered evaluation", 409, EVALUATION_NOT_TRIGGERED),
    ("ritual needs at least one saved human question", 422, INVALID_INPUT),
    ("supersession must stay within one series", 422, INVALID_INPUT),
    ("only a same-source revision may supersede a fact", 422, INVALID_INPUT),
    ("verification must copy the checked value unchanged", 422, INVALID_INPUT),
    # 051_butler.sql
    ("butler version conflict", 409, VERSION_CONFLICT),
    ("butler invalid subscription transition", 409, INVALID_TRANSITION),
    ("butler review is closed", 409, REVIEW_CLOSED),
    ("butler response revision must name predecessor", 409, STALE_PREDECESSOR),
    ("butler question not in review", 422, INVALID_INPUT),
    ("butler memory reference mismatch", 422, INVALID_INPUT),
    ("butler snooze out of range", 422, INVALID_INPUT),
    ("butler unknown timezone", 422, INVALID_INPUT),
    ("butler subscription needs a FOLLOW event", 422, INVALID_INPUT),
    ("butler response evidence is sealed", 422, INVALID_INPUT),
    # 044 execution guards (BE 19-25). The repositories check these first; the triggers are
    # the race-proof second line, and a race must surface as its 409, never a 500.
    ("configuration version conflict", 409, VERSION_CONFLICT),
    ("late result refused; expire and retry", 409, "LEASE_EXPIRED"),
    ("attempt has not expired", 409, "ATTEMPT_ACTIVE"),
    ("previous attempt still running", 409, "ATTEMPT_ACTIVE"),
    ("attempt order conflict", 409, "ATTEMPT_ACTIVE"),
    ("task already has a proposal or review", 409, "TASK_CLOSED"),
    ("task context is sealed", 409, "CONTEXT_SEALED"),
    ("result citations are sealed", 409, "CONTEXT_SEALED"),
    ("no reviewable proposal", 409, "NOT_REVIEWABLE"),
    ("blocked proposal", 409, "BLOCKED_PROPOSAL"),
    ("acceptance needs a new attributed note in this transaction", 409, "NOT_REVIEWABLE"),
    ("agent note needs explicit human acceptance", 409, "NOT_REVIEWABLE"),
    ("only an authenticated human can review", 403, "HUMAN_REVIEW_REQUIRED"),
    ("answer review requires own answer for this company", 409, "OWN_ANSWER_FIRST"),
    ("chat task belongs to a different company", 409, "CROSS_COMPANY"),
    ("chat result mismatch", 409, "CROSS_COMPANY"),
    ("runner cannot author a human chat turn", 403, "HUMAN_REVIEW_REQUIRED"),
    # 053_butler_notebook.sql (BT 12-15)
    ("butler invalid consent transition", 409, INVALID_TRANSITION),
    ("butler consent date out of range", 422, INVALID_INPUT),
    ("butler notebook consent not current", 409, "CONSENT_NOT_CURRENT"),
    ("butler notebook source not consented", 409, "CONSENT_NOT_CURRENT"),
    ("butler notebook record excluded", 409, "NOTEBOOK_RECORD_EXCLUDED"),
    ("butler notebook system write required", 403, "SYSTEM_WRITE_REQUIRED"),
    ("butler episode already recorded", 409, VERSION_CONFLICT),
    ("butler pattern cites an ineligible episode", 422, INVALID_INPUT),
    ("butler pattern counts do not match cited episodes", 422, INVALID_INPUT),
    # 054_hosted_jobs.sql (RP 01)
    ("hosted job already running", 409, "JOB_ALREADY_RUNNING"),
    ("hosted job quota exhausted", 429, "QUOTA_EXHAUSTED"),
    ("hosted job deadline out of range", 422, INVALID_INPUT),
    ("hosted job invalid transition", 409, INVALID_TRANSITION),
    ("hosted job cancellation pending", 409, INVALID_TRANSITION),
    ("hosted job version conflict", 409, VERSION_CONFLICT),
    ("hosted job attribution mismatch", 403, "SYSTEM_WRITE_REQUIRED"),
    ("hosted job not runnable", 409, "JOB_NOT_RUNNABLE"),
)


def translate_db_error(exc: psycopg.Error) -> WorkspaceError | None:
    message = str(getattr(exc.diag, "message_primary", "") or exc)
    if isinstance(exc, psycopg.errors.RaiseException):
        for needle, status, code in _RAISE_MAP:
            if needle in message:
                return WorkspaceError(status, code, needle)
        return None
    constraint = getattr(exc.diag, "constraint_name", "") or ""
    if isinstance(exc, psycopg.errors.UniqueViolation):
        if "previous_id" in constraint or "one_successor" in constraint:
            return WorkspaceError(409, STALE_PREDECESSOR,
                                  "another revision was saved first; reload")
        if "version" in constraint:
            return WorkspaceError(409, VERSION_CONFLICT, "another version was saved first")
        # One review per task/result and one terminal result per attempt (042).
        if constraint.startswith("task_review_investor_id_task_id") or \
                constraint.startswith("task_review_investor_id_result_id"):
            return WorkspaceError(409, "REVIEW_EXISTS", "this task has already been reviewed")
        if constraint.startswith("task_result_investor_id_attempt_id"):
            return WorkspaceError(409, "RESULT_EXISTS", "this attempt already has a result")
        if "dedupe_key" in constraint or "idem_key" in constraint or "ws_receipt" in constraint:
            return WorkspaceError(409, IDEMPOTENCY_CONFLICT,
                                  "a concurrent request with this Idempotency-Key committed "
                                  "first; retry to receive its result")
        return None
    if isinstance(exc, psycopg.errors.CheckViolation):
        return WorkspaceError(422, INVALID_INPUT, "the request violates a stored invariant",
                              {"constraint": constraint or "CHECK"})
    if isinstance(exc, psycopg.errors.ForeignKeyViolation):
        return WorkspaceError(422, INVALID_INPUT, "a referenced record does not exist here",
                              {"constraint": constraint or "FOREIGN_KEY"})
    return None


@contextmanager
def db_errors() -> Iterator[None]:
    """Wrap a whole session (including its COMMIT) so deferred-constraint failures at
    commit are translated too -- a commit failure is never reported as success."""
    try:
        yield
    except (WorkspaceConnectionNotConfiguredError, WorkspaceConnectionError) as exc:
        logger.error("workspace connection unavailable: %s", exc)
        raise WorkspaceError(503, WORKSPACE_UNAVAILABLE,
                             "research storage is unavailable") from exc
    except WorkspaceContextError as exc:
        raise WorkspaceError(403, "FORBIDDEN", "this investor cannot be opened") from exc
    except psycopg.Error as exc:
        mapped = translate_db_error(exc)
        if mapped is None:
            raise
        raise mapped from exc
