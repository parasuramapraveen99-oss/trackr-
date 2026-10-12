"""DTOs for ``app/routers/monitors.py`` (BLUEPRINT_BACKEND.md section 7.5, sessions 36-37).

Same rules as ``app/schemas/workspace.py``: requests forbid extra fields and use strict scalars;
the server derives actor, version numbers, timestamps, dispositions and every state label.

THE STATES ARE ENUMERATED ON PURPOSE. A client renders ``display_state`` and never infers
"healthy" from the absence of an alert: blocked, superseded, event-blocked, queued, failed and
completed are different facts, and while BE-Q01 is open a schedule is at most
``SAVED_EXECUTION_BLOCKED`` -- there is no ACTIVE or RUNNING schedule state to render.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

Kind = Literal["SCHEDULED", "POST_RESULTS", "POST_CONCALL"]
Disposition = Literal["QUEUED", "BLOCKED_EXECUTION_POLICY", "BLOCKED_EVENT", "SUPERSEDED_THESIS"]
RunDisplayState = Literal[
    "BLOCKED_EXECUTION_POLICY",        # a timer came due; nothing ran (BE-Q01)
    "EVENT_MATCHED_EXECUTION_BLOCKED",  # a verified event matched; nothing ran (BE-Q01)
    "EVENT_BLOCKED",                   # a verified event failed a definitive check
    "SUPERSEDED_THESIS",               # the pinned thesis was not in force when due
    "QUEUED", "EVENT_MATCHED_QUEUED",  # BE 38 only
    "RUNNING", "AWAITING_REVIEW", "REVIEWED", "FAILED", "COMPLETED",
]
ScheduleDisplayState = Literal["PAUSED", "NEEDS_REBIND", "SAVED_EXECUTION_BLOCKED"]
EventWatchState = Literal[
    "NO_QUALIFYING_EVENT", "EVENT_AWAITING_VERIFICATION", "EVENT_UNVERIFIABLE",
]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScheduleCreateIn(_In):
    thesis_id: StrictStr
    skill_id: StrictStr
    skill_version: StrictInt = Field(ge=1)
    agent_id: StrictStr
    agent_version: StrictInt = Field(ge=1)
    kind: Kind
    #: Only "Asia/Kolkata" is accepted (422 TIMEZONE_UNSUPPORTED otherwise). No default is
    #: applied silently: the zone decides the wall time every interval keeps.
    timezone: StrictStr
    #: An ISO instant WITH an offset. A naive time is refused rather than assumed to be IST.
    starts_at: datetime
    interval_days: StrictInt | None = None
    enabled: StrictBool


class ScheduleVersionIn(ScheduleCreateIn):
    expected_version: StrictInt = Field(ge=1)


class ManualRunIn(_In):
    #: No due time: a manual run can never claim a historical moment (and is refused anyway
    #: while BE-Q01 is open).
    schedule_version: StrictInt = Field(ge=1)


class RunEvent(BaseModel):
    event_id: str
    identity: str
    event_type: str | None
    event_date: str | None
    period_start: str | None
    period_end: str | None
    source_id: str | None
    verification: str | None
    status: str | None


class Run(BaseModel):
    id: str
    isin: str
    schedule_id: str
    schedule_version: int
    thesis_id: str
    due_at: str
    recorded_at: str
    disposition: Disposition
    blocked_reason: str | None
    task_id: str | None
    task_state: str | None
    report_id: str | None
    display_state: RunDisplayState
    recorded_late: bool
    recorded_after_due_seconds: int
    executed: bool
    event: RunEvent | None


class Commitment(BaseModel):
    state: Literal["ACTIVE", "NONE", "CONFLICT"]
    in_force_thesis_id: str | None
    pinned_is_in_force: bool


class Occurrences(BaseModel):
    total: int
    executed: int
    blocked_execution_policy: int
    blocked_event: int
    superseded_thesis: int


class PendingEvent(BaseModel):
    event_id: str
    identity: str | None
    code: str
    reason: str


class EventWatch(BaseModel):
    event_type: str
    state: EventWatchState
    pending: list[PendingEvent]
    awaiting_tick: int
    watching_from: str


class Schedule(BaseModel):
    id: str
    version: int
    isin: str
    thesis_id: str
    skill_id: str
    skill_version: int
    agent_id: str
    agent_version: int
    kind: Kind
    timezone: str
    starts_at: str
    interval_days: int | None
    enabled: bool
    actor_uid: str
    recorded_at: str
    created_at: str
    versions: int
    execution_policy_status: Literal["EXECUTION_POLICY_UNRESOLVED"]
    execution_policy_reason: str
    commitment: Commitment
    display_state: ScheduleDisplayState
    next_due_at: str | None
    occurrences: Occurrences
    latest_run: Run | None
    event_watch: EventWatch | None


class ReportRef(BaseModel):
    id: str
    title: str
    lede: str
    recorded_at: str


class RunDetail(BaseModel):
    run: Run
    #: The existing Task DTO (app/schemas/tasks.py) when a task exists; always null while
    #: BE-Q01 is open.
    task: dict[str, Any] | None
    #: Always null until BE 39 (blocked on BE-Q01). Never a placeholder body.
    report: ReportRef | None
