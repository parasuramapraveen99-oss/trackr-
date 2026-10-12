"""Deterministic monitor scheduling: due instants, event matching and honest states (BE 36-37).

BLUEPRINT_BACKEND.md section 6.4 ("Scheduled occurrence", "Schedule vs thesis", "Event
occurrence", "Closed-browser execution"). PURE: no database, no clock of its own, no network, no
model. ``app/db/monitors.py`` reads the rows and records what this module decides.

**BE-Q01 IS OPEN, SO NOTHING HERE EVER DECIDES "RUN".** A due occurrence whose commitment is
still in force is ``BLOCKED_EXECUTION_POLICY`` with a stated reason; there is no code path that
yields ``QUEUED``. Catch-up, notification delivery and unattended execution are BE 38's, and only
once the owner records the decision. A clock firing is a fact about the calendar, not a run.

SCHEDULED INSTANTS. ``interval_days`` means LOCAL CALENDAR DAYS at the original ``starts_at``
wall time in the schedule's zone: 09:00 stays 09:00 across every interval instead of drifting by
elapsed hours. Only ``Asia/Kolkata`` is accepted. It has no daylight transitions; a zone that has
them needs explicit gap and fold semantics (which local time a skipped hour resolves to, which of
a repeated hour fires), and inventing that policy silently would produce a duplicate or a missing
run twice a year. Other zones are refused with that reason until the semantics are specified.

EVENT OCCURRENCES (BE-V06). An event occurrence needs a research_event (migration 055) that a
person VERIFIED, that is OBSERVED (it happened; a scheduled date is not the event), with an EXACT
date, of exactly the declared type, in the company's verified ISIN family, for an exact period,
from a source whose use is permitted. Its identity is (type, period) -- several rows of one filing
(republished, mirrored, corrected, delivered twice) are one event and one occurrence. Nothing
here reads a price, a volume, a quantity, a holding or the ledger: none of those is evidence that
a results event occurred.

WHAT IS RECORDED AND WHAT IS ONLY SHOWN. An event group with no verified observed row -- an
unverified extraction awaiting a person's recheck, a CLAIMED date, an imprecise date -- records
NO occurrence. Recording one would consume the event's identity, and the genuine verified filing
that usually follows a press claim could then never produce its occurrence. Those groups are
returned as PENDING with their reason, and the listing shows them ("no qualifying event yet",
"awaiting verification", "cannot be verified"). A VERIFIED observed event that fails a definitive
check (use not permitted, ambiguous period, inconsistent dates) IS recorded, as BLOCKED_EVENT
with its reason, and that record is final for that event: a late copy does not re-open it.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.research.errors import WorkspaceError

# --- the gate --------------------------------------------------------------------------------

#: BE-Q01. Shown on every schedule listing, not only on run rows, so the gate is visible where a
#: person decides whether their monitor is doing anything.
EXECUTION_POLICY_STATUS = "EXECUTION_POLICY_UNRESOLVED"
EXECUTION_POLICY_REASON = (
    "Not run: no supported way exists yet to run a scheduled model check while your provider "
    "key stays in your browser (open decision BE-Q01). This occurrence is recorded so the gap "
    "is visible; nothing was checked and no report exists."
)
#: The manual-run refusal. Same gate, same words.
MANUAL_RUN_REASON = (
    "Manual monitor runs are not available: how a scheduled model check runs, and what a manual "
    "run would mean for due occurrences, is an open decision (BE-Q01)."
)

# --- configuration bounds ----------------------------------------------------------------------

SUPPORTED_TIMEZONES = frozenset({"Asia/Kolkata"})
TIMEZONE_UNSUPPORTED = "TIMEZONE_UNSUPPORTED"
#: A bound on configuration, not a financial rule: a yearly check is the longest cadence offered.
MAX_INTERVAL_DAYS = 366
#: Occurrences recorded per schedule per tick, oldest first; the next tick continues. A worker
#: that was down for a long time records its backlog in bounded steps rather than in one burst.
MAX_DUE_PER_TICK = 100
#: Display only: an occurrence recorded this long after its due instant is shown as recorded
#: late. It changes no disposition -- lateness is a fact about the worker, not the monitor.
LATE_AFTER = timedelta(minutes=15)

KINDS = ("SCHEDULED", "POST_RESULTS", "POST_CONCALL")
#: The exact research_event type each event kind waits for. An adjacent disclosure (a board
#: meeting notice, a results DATE announcement, a transcript of some other call) does not qualify.
KIND_EVENT_TYPE = {"POST_RESULTS": "FINANCIAL_RESULTS", "POST_CONCALL": "EARNINGS_CALL"}

#: An event occurrence uses only the event's citation metadata (type, date, period, source). No
#: text is read, stored or disclosed to anyone by deciding that an event is due.
MONITOR_SOURCE_USE = "METADATA"

# --- dispositions and display states -----------------------------------------------------------

QUEUED = "QUEUED"
BLOCKED_EXECUTION_POLICY = "BLOCKED_EXECUTION_POLICY"
BLOCKED_EVENT = "BLOCKED_EVENT"
SUPERSEDED_THESIS = "SUPERSEDED_THESIS"

#: Pending (unrecorded) event states for the listing. Distinct from every recorded disposition.
NO_QUALIFYING_EVENT = "NO_QUALIFYING_EVENT"
EVENT_AWAITING_VERIFICATION = "EVENT_AWAITING_VERIFICATION"
EVENT_UNVERIFIABLE = "EVENT_UNVERIFIABLE"

#: BLOCKED_EVENT reason codes.
SOURCE_USE_NOT_PERMITTED = "SOURCE_USE_NOT_PERMITTED"
PERIOD_AMBIGUOUS = "PERIOD_AMBIGUOUS"
IDENTITY_INCONSISTENT = "IDENTITY_INCONSISTENT"

IST = ZoneInfo("Asia/Kolkata")
_KEY_EPOCH = date(2000, 1, 1)


def check_timezone(name: str) -> ZoneInfo:
    """The zone, or 422 TIMEZONE_UNSUPPORTED with the reason."""
    if name not in SUPPORTED_TIMEZONES:
        raise WorkspaceError(
            422, TIMEZONE_UNSUPPORTED,
            "only Asia/Kolkata is supported: a zone with daylight-saving transitions needs "
            "explicit rules for skipped and repeated hours, and none have been specified",
            {"timezone": TIMEZONE_UNSUPPORTED},
        )
    return ZoneInfo(name)


# =============================================================================================
# scheduled instants
# =============================================================================================


def nth_due(starts_at: datetime, zone: ZoneInfo, interval_days: int, n: int) -> datetime:
    """The n-th due instant (n >= 0), in UTC: local date + n*interval at the original wall time.

    Built from the LOCAL date and wall time, never ``starts_at + n * timedelta(days=...)`` --
    elapsed-time arithmetic is exactly the drift the contract forbids. In Asia/Kolkata the local
    wall time always exists exactly once; that is why it is the only zone accepted.
    """
    if starts_at.tzinfo is None:
        raise ValueError("starts_at must be timezone-aware")
    local = starts_at.astimezone(zone)
    day = local.date() + timedelta(days=n * interval_days)
    wall = datetime.combine(day, time(local.hour, local.minute, local.second, local.microsecond),
                            tzinfo=zone)
    return wall.astimezone(UTC)


def _first_index(starts_at: datetime, zone: ZoneInfo, interval_days: int,
                 lower: datetime, inclusive: bool) -> int:
    """Smallest n whose instant is >= lower (or > lower)."""
    elapsed = (lower - starts_at).days
    n = max(0, elapsed // interval_days - 1)
    while True:
        instant = nth_due(starts_at, zone, interval_days, n)
        if instant > lower or (inclusive and instant == lower):
            return n
        n += 1


def due_instants(
    starts_at: datetime, timezone_name: str, interval_days: int, *,
    after: datetime | None, not_before: datetime, until: datetime,
    limit: int = MAX_DUE_PER_TICK,
) -> list[datetime]:
    """Due instants t with t > after, t >= not_before and t <= until, oldest first, at most limit.

    ``after`` is the schedule's last recorded due occurrence (any version): reconfiguration
    starts STRICTLY after it, so a new version never regenerates a recorded moment.
    ``not_before`` is when the version was recorded: a schedule saved today with a start date in
    the past does not manufacture a history of occurrences from before it existed.
    """
    zone = check_timezone(timezone_name)
    if interval_days < 1:
        raise ValueError("interval_days must be positive")
    n_floor = _first_index(starts_at, zone, interval_days, not_before, inclusive=True)
    n_after = (_first_index(starts_at, zone, interval_days, after, inclusive=False)
               if after is not None else 0)
    n = max(n_floor, n_after)
    out: list[datetime] = []
    while len(out) < limit:
        instant = nth_due(starts_at, zone, interval_days, n)
        if instant > until:
            break
        out.append(instant)
        n += 1
    return out


def next_due(starts_at: datetime, timezone_name: str, interval_days: int, *,
             after: datetime | None, not_before: datetime, now: datetime) -> datetime:
    """The next instant a tick will record: strictly after max(after, now), >= not_before."""
    zone = check_timezone(timezone_name)
    lower = max(after, now) if after is not None else now
    n = max(_first_index(starts_at, zone, interval_days, not_before, inclusive=True),
            _first_index(starts_at, zone, interval_days, lower, inclusive=False))
    return nth_due(starts_at, zone, interval_days, n)


# =============================================================================================
# the commitment check
# =============================================================================================


def commitment_disposition(selection_state: str, selected_thesis_id: str | None,
                           pinned_thesis_id: str) -> tuple[str, str]:
    """(disposition, reason) at the due cutoff.

    The pinned thesis must be exactly the ACTIVE commitment in force at the cutoff. A newer
    DRAFT does not displace it (``engine.thesis_versions.in_force`` skips drafts). Otherwise the
    occurrence is SUPERSEDED_THESIS and rebinding is left to the person as a new schedule
    version -- a worker never silently swaps the object being monitored.
    """
    if selection_state == "ACTIVE" and selected_thesis_id == pinned_thesis_id:
        return BLOCKED_EXECUTION_POLICY, EXECUTION_POLICY_REASON
    if selection_state == "CONFLICT":
        reason = ("Not run: verified identifiers for this company hold more than one active "
                  "commitment at this moment. Resolve them, then rebind this monitor.")
    elif selection_state == "NONE":
        reason = ("Not run: the thesis this monitor watches was no longer an active commitment "
                  "at this moment (retired, or never in force). Rebind it to continue.")
    else:
        reason = ("Not run: a newer thesis version was the commitment in force at this moment. "
                  "This monitor still watches the older one; rebind it to continue.")
    return SUPERSEDED_THESIS, reason


# =============================================================================================
# event occurrences
# =============================================================================================


@dataclass(frozen=True)
class EventRow:
    """One research_event row and the source facts the matcher needs. Nothing else."""

    id: str
    isin: str
    event_type: str
    status: str
    verification: str
    date_precision: str
    event_date: date | None
    period_start: date | None
    period_end: date | None
    source_id: str
    source_type: str
    publication_date: date
    retrieved_at: datetime
    seq: int


@dataclass(frozen=True)
class EventDecision:
    """An occurrence to record: MATCHED (commitment check follows) or BLOCKED_EVENT."""

    identity: str
    event_id: str
    due_at: datetime
    matched: bool
    code: str | None
    reason: str | None


@dataclass(frozen=True)
class PendingEvent:
    """An event group that records nothing yet, with the reason it does not qualify."""

    identity: str | None
    event_id: str
    code: str
    reason: str


def event_identity(event_type: str, period_start: date, period_end: date) -> str:
    """TYPE|start|end -- migration 057's CHECK spells the same format."""
    return f"{event_type}|{period_start.isoformat()}|{period_end.isoformat()}"


def event_due_at(event_date: date, period_start: date, period_end: date) -> datetime:
    """The occurrence's due instant, derived from the EVENT, never from when it arrived.

    Midnight Asia/Kolkata on the event's date, plus a deterministic intra-day key: seconds from
    the period's end date and microseconds from its length. The key exists because a company can
    publish several periods on one day (Q4 and the full year; a delinquent filer's two quarters)
    and each is its own event, while 043's UNIQUE (investor, schedule, due_at) still holds. The
    reader-facing date is the event's own ``event_date``; this instant is an ordering key.
    """
    midnight = datetime.combine(event_date, time(0), tzinfo=IST)
    key = timedelta(seconds=(period_end - _KEY_EPOCH).days % 86400,
                    microseconds=min((period_end - period_start).days, 999_999))
    return (midnight + key).astimezone(UTC)


def _verified_observed(r: EventRow) -> bool:
    return (r.verification == "VERIFIED" and r.status == "OBSERVED"
            and r.date_precision == "EXACT" and r.event_date is not None)


def _definitive_failure(r: EventRow, allows: Callable[[str], bool]) -> tuple[str, str] | None:
    """The first definitive reason a VERIFIED observed row cannot produce a matched occurrence."""
    assert r.event_date is not None and r.period_start is not None and r.period_end is not None
    if not allows(r.source_type):
        return (SOURCE_USE_NOT_PERMITTED,
                f"The only verified record of this event comes from a {r.source_type} source "
                "whose use has not been approved (open decision BE-Q04), so it cannot start a "
                "monitor check.")
    if r.period_end >= r.event_date:
        return (PERIOD_AMBIGUOUS,
                "The verified record reports on a period that had not ended when it was "
                "published, so which period it covers is ambiguous. No nearby period is "
                "substituted.")
    if r.event_date > r.publication_date or r.publication_date > r.retrieved_at.date():
        return (IDENTITY_INCONSISTENT,
                "The verified record's dates are inconsistent with its source (the event is "
                "dated after its source was published, or the source was retrieved before it "
                "was published), so the event cannot be identified with confidence.")
    return None


def evaluate_events(
    kind: str,
    rows: Iterable[EventRow],
    *,
    family: frozenset[str],
    allows: Callable[[str], bool],
    effective_from: date,
    today: date,
    recorded_identities: frozenset[str],
) -> tuple[list[EventDecision], list[PendingEvent]]:
    """Occurrences to record and groups to show as pending, for one event schedule.

    ``family`` is the schedule company's VERIFIED ISIN family: an event filed under a retired
    ISIN still matches, one filed under an unverified alias or another company never does, and
    no ticker is consulted anywhere. ``effective_from`` (Asia/Kolkata date) is when this schedule
    version began watching: an event whose first verified date is earlier is history and records
    nothing -- no backfill. ``recorded_identities`` makes a re-scan, a late copy and a new version
    no-ops.
    """
    wanted = KIND_EVENT_TYPE[kind]
    groups: dict[str, list[EventRow]] = defaultdict(list)
    pending: list[PendingEvent] = []
    for r in rows:
        if r.event_type != wanted or r.isin not in family:
            continue  # a related disclosure or another company: not this event at all
        if r.period_start is None or r.period_end is None:
            pending.append(PendingEvent(None, r.id, EVENT_UNVERIFIABLE,
                                        "This record does not state which period it covers."))
            continue
        groups[event_identity(r.event_type, r.period_start, r.period_end)].append(r)

    decisions: list[EventDecision] = []
    for identity in sorted(groups):
        if identity in recorded_identities:
            continue
        group = sorted(groups[identity], key=lambda r: r.seq)
        verified = [r for r in group if _verified_observed(r)]
        if verified:
            first = min(r.event_date for r in verified if r.event_date is not None)
            if first < effective_from or first > today:
                continue  # history before this monitor began, or dated in the future
            ps, pe = group[0].period_start, group[0].period_end
            assert ps is not None and pe is not None
            due_at = event_due_at(first, ps, pe)
            failures: list[tuple[EventRow, tuple[str, str]]] = []
            passing: EventRow | None = None
            for r in sorted(verified, key=lambda r: (r.event_date, r.seq)):
                failure = _definitive_failure(r, allows)
                if failure is None:
                    passing = r
                    break
                failures.append((r, failure))
            if passing is not None:
                decisions.append(EventDecision(identity, passing.id, due_at, True, None, None))
            else:
                row, (code, reason) = failures[0]
                decisions.append(EventDecision(identity, row.id, due_at, False, code, reason))
            continue
        # No verified observed row: nothing is recorded. Say why.
        latest = group[-1]
        if latest.event_date is not None and latest.event_date < effective_from:
            continue
        if all(r.status == "CLAIMED" or r.date_precision != "EXACT" for r in group):
            pending.append(PendingEvent(
                identity, latest.id, EVENT_UNVERIFIABLE,
                "Only a claim or an imprecise date is held for this event; it cannot be "
                "verified, so no check was started."))
        elif any(r.status == "OBSERVED" for r in group):
            pending.append(PendingEvent(
                identity, latest.id, EVENT_AWAITING_VERIFICATION,
                "An extracted record of this event is waiting for you to verify it against "
                "its source. Nothing starts until you do."))
        # SCHEDULED/CANCELLED-only groups: a future date is not an event that happened.
    return decisions, pending


# =============================================================================================
# display states
# =============================================================================================


def run_display_state(disposition: str, is_event: bool, task_state: str | None,
                      task_error: str | None, has_report: bool) -> str:
    """One label per distinct fact. Nothing that did not execute can read as a completed run."""
    if disposition == BLOCKED_EVENT:
        return "EVENT_BLOCKED"
    if disposition == SUPERSEDED_THESIS:
        return "SUPERSEDED_THESIS"
    if disposition == BLOCKED_EXECUTION_POLICY:
        return "EVENT_MATCHED_EXECUTION_BLOCKED" if is_event else "BLOCKED_EXECUTION_POLICY"
    # QUEUED: only once BE 38 exists. Read from the task, never assumed.
    if has_report:
        return "COMPLETED"
    if task_state == "running":
        return "RUNNING"
    if task_state == "review":
        return "AWAITING_REVIEW"
    if task_state in ("done", "rejected"):
        return "REVIEWED"
    if task_state == "assigned" and task_error:
        return "FAILED"
    return "EVENT_MATCHED_QUEUED" if is_event else "QUEUED"


def schedule_display_state(enabled: bool, commitment_state: str, in_force_matches: bool) -> str:
    """Never ACTIVE, RUNNING or HEALTHY while BE-Q01 is open: a saved schedule is only saved."""
    if not enabled:
        return "PAUSED"
    if commitment_state != "ACTIVE" or not in_force_matches:
        return "NEEDS_REBIND"
    return "SAVED_EXECUTION_BLOCKED"
