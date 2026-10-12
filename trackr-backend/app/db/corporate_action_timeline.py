"""Per-investor corporate-action timeline -- GET /investors/{id}/corporate-actions/timeline.

DERIVED ON READ, NEVER STORED (docs/CORPORATE_ACTIONS_COMPLETION_PLAN.md section 3.5).
Built from the reference tables (``corporate_action``, ``dividend_event``,
``security_alias``, review-class ``corporate_action_observation`` rows, observed
``corporate_action_coverage``) and from each account's OWN ledger, loaded exactly once
per account per request (defect D8). Quantities are never combined across accounts.

Every state is decided here by deterministic checks, in a fixed order, from a fixed
vocabulary (``STATES``/``app.schemas.corporate_actions.TIMELINE_REASON_CODES``). An empty
reference table yields no events: nothing is projected that a table does not hold.
Expected dividends come only from ``app.engine.dividends.compute_expected_dividends``
(invariants 18/19); nothing here reaches REALISED.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any

from app import securities
from app.database import money
from app.db.corporate_actions import list_corporate_actions
from app.db.dividend_events import list_dividend_events
from app.db.repository import load_transactions
from app.db.security_alias import list_aliases, verified_map
from app.engine.corporate_actions import (
    CorpActionType,
    CorporateAction,
    Verification,
    screen,
)
from app.engine.dividends import (
    EVIDENCE_WITHDRAWN,
    compute_expected_dividends,
    drop_unverified_twins,
    parse_event_source_ref,
)
from app.engine.holdings import compute_holdings_partitioned
from app.engine.lots import EVIDENCE_WITHDRAWN_KEY, bonus_acquisition_date
from app.engine.types import ZERO, Source, Transaction, TxnType
from app.ingest.corporate_action_feed import classify_subject, parse_nse_date

logger = logging.getLogger(__name__)

#: Most significant first: an event's state is its most significant account state.
STATES = ("CONFLICT", "REVIEW", "EXCLUDED", "PENDING", "APPLIED", "CANCELLED", "NOT_HELD")

#: Notice classes that are detection plus REVIEW only (OD-7). Never applied.
REVIEW_CLASSES = ("RIGHTS", "BUYBACK", "MERGER", "DEMERGER", "CAPITAL_REDUCTION", "CONSOLIDATION")

#: Provider reported on a ``reference_notice``-only event. That table is written by the
#: NSE feed and nothing else (``app.reference_worker.RecordingActionSource``), and it
#: stores no provider column -- but an event's ``source`` is not allowed to be null just
#: because its source table predates the provider column.
_NOTICE_PROVIDER = "NSE"

#: Dividend screen codes that mean "not verified yet" rather than "evidence problem".
_AWAITING = {"UNVERIFIED"}

_EFFECT_TYPE = {
    CorpActionType.BONUS: TxnType.BONUS,
    CorpActionType.SPLIT: TxnType.SPLIT_ADJ,
    CorpActionType.DEMERGER: TxnType.DEMERGER_IN,
}


class SecurityNotOwned(LookupError):
    """The requested ISIN is not in (or alias-related to) this investor's ledger."""


@dataclass(frozen=True)
class TimelineAccount:
    id: str
    label: str


def investor_accounts(conn: Any, investor_id: str, account_id: str | None) -> list[TimelineAccount]:
    """The investor's accounts (or the one named), with a human label: the account's own
    label, else ``broker/client_id``, else ``broker/investor code`` (ISIN_AUTOMATION
    section 4). Always scoped by ``investor_id``; a foreign ``account_id`` matches nothing."""
    rows = conn.execute(
        "SELECT a.id::text, a.broker, a.client_id, a.label, i.code "
        "FROM account a JOIN investor i ON i.id = a.investor_id "
        "WHERE a.investor_id = %s AND (%s::uuid IS NULL OR a.id = %s::uuid) "
        "ORDER BY a.broker, a.id",
        (investor_id, account_id, account_id),
    ).fetchall()
    return [
        TimelineAccount(id=r[0], label=r[3] or f"{r[1]}/{r[2] or r[4]}") for r in rows
    ]


class _Ledger:
    """One account's ledger, loaded once, with a memo of the partitioned replay per cutoff."""

    def __init__(self, account: TimelineAccount, transactions: list[Transaction]):
        self.account = account
        self.transactions = transactions
        self._at: dict[date, tuple[dict, dict]] = {}

    def before(self, isin: str, ex_date: date):
        """(quantity held at the close before ``ex_date``, skip reason code or None)."""
        cutoff = ex_date - timedelta(days=1)
        if cutoff not in self._at:
            holdings, skipped = compute_holdings_partitioned(self.transactions, cutoff)
            self._at[cutoff] = (
                {h.isin: h.quantity for h in holdings},
                {s.isin: s.reason_code or "LEDGER_INCOMPLETE" for s in skipped},
            )
        quantities, skipped = self._at[cutoff]
        return quantities.get(isin, ZERO), skipped.get(isin)

    def ever_held(self, isin: str) -> bool:
        return any(t.isin == isin for t in self.transactions)


def _effect(txn: Transaction) -> dict:
    return {"txn_type": txn.txn_type.value, "isin": txn.isin, "quantity": money(txn.quantity)}


def _account(ledger: _Ledger, state: str, reason: str | None, quantity=None, **extra) -> dict:
    return {
        "account_id": ledger.account.id,
        "account_label": ledger.account.label,
        "state": state,
        "reason_code": reason,
        "eligible_quantity": money(quantity) if quantity is not None else None,
        "effect": extra.get("effect"),
        "acquisition_date_basis": extra.get("acquisition_date_basis"),
    }


def _latest_conflicts(conn: Any, column: str, ids: list[str]) -> dict[str, str]:
    """Reference row id -> reason code, where the most recent source evidence for the row
    is a CONFLICT observation (never merged by guesswork; plan section 3.3). Links come
    from the observation itself or, for the operator import, through the event's
    revisions. A later accepted revision or non-conflicting observation supersedes it."""
    if not ids:
        return {}
    conflicts = conn.execute(
        f"""SELECT o.{column}::text, o.reason_code, o.fetched_at
              FROM corporate_action_observation o
             WHERE o.result = 'CONFLICT' AND o.{column} = ANY(%s::uuid[])
            UNION ALL
            SELECT r.{column}::text, o.reason_code, o.fetched_at
              FROM corporate_action_observation o
              JOIN corporate_action_revision r ON r.provider = o.provider
               AND r.event_id = o.event_id AND r.component_id = o.component_id
             WHERE o.result = 'CONFLICT' AND r.{column} = ANY(%s::uuid[])""",
        (ids, ids),
    ).fetchall()
    if not conflicts:
        return {}
    settled = dict(
        conn.execute(
            f"""SELECT id, max(at) FROM (
                    SELECT {column}::text AS id, fetched_at AS at
                      FROM corporate_action_observation
                     WHERE result <> 'CONFLICT' AND {column} = ANY(%s::uuid[])
                    UNION ALL
                    SELECT {column}::text, recorded_at FROM corporate_action_revision
                     WHERE {column} = ANY(%s::uuid[])) s GROUP BY id""",
            (ids, ids),
        ).fetchall()
    )
    found: dict[str, tuple[str, Any]] = {}
    for row_id, reason, at in conflicts:
        if settled.get(row_id) is not None and settled[row_id] >= at:
            continue
        if row_id not in found or found[row_id][1] < at:
            found[row_id] = (reason, at)
    return {row_id: reason for row_id, (reason, _) in found.items()}


def _observed_at(conn: Any, column: str, ids: list[str]) -> dict[str, Any]:
    if not ids:
        return {}
    return dict(
        conn.execute(
            f"SELECT {column}::text, max(fetched_at) FROM corporate_action_observation "
            f"WHERE {column} = ANY(%s::uuid[]) GROUP BY 1",
            (ids,),
        ).fetchall()
    )


def _action_account(
    ledger: _Ledger,
    action: CorporateAction,
    failure: str | None,
    conflict: str | None,
    combined: bool,
    today: date,
) -> dict:
    """One account's state for one corporate action. Order is the contract."""
    effect_type = _EFFECT_TYPE[action.action_type]
    effect_isin = (
        action.resulting_isin if action.action_type is CorpActionType.DEMERGER else action.isin
    )
    # Tagged rows (auto-application) first; an untagged CORP_ACTION row at the same
    # (isin, ex_date, type) is the same application by the manual/staged path -- the same
    # content check apply_known_actions_for_account uses.
    applied = next(
        (t for t in ledger.transactions
         if t.source is Source.CORP_ACTION
         and (t.raw_payload or {}).get("auto_corporate_action_id") == action.id),
        None,
    ) or next(
        (t for t in ledger.transactions
         if t.source is Source.CORP_ACTION
         and not (t.raw_payload or {}).get("auto_corporate_action_id")
         and t.isin == effect_isin and t.txn_date == action.ex_date
         and t.txn_type is effect_type),
        None,
    )
    quantity, skipped = ledger.before(action.isin, action.ex_date)
    extra: dict[str, Any] = {}
    if applied is not None:
        extra["effect"] = _effect(applied)
        if applied.txn_type is TxnType.BONUS:
            extra["acquisition_date_basis"] = bonus_acquisition_date(applied)[1].value

    def out(state: str, reason: str | None) -> dict:
        return _account(ledger, state, reason, quantity, **extra)

    if action.cancelled:
        return out("CANCELLED", "CANCELLED_BY_SOURCE")
    if conflict:
        return out("CONFLICT", conflict)
    if applied is not None:
        withdrawn = (applied.raw_payload or {}).get(EVIDENCE_WITHDRAWN_KEY)
        return out("EXCLUDED", withdrawn) if withdrawn else out("APPLIED", None)
    if skipped:
        return out("EXCLUDED", skipped)
    if quantity <= ZERO:
        return out("NOT_HELD", None)
    if action.ex_date > today:
        return out("PENDING", "UPCOMING")
    if action.verification is Verification.DISPUTED:
        return out("EXCLUDED", "REFERENCE_DISPUTED")
    if action.verification is Verification.DRAFT:
        return out("PENDING", "AWAITING_VERIFICATION")
    if failure:
        return out("REVIEW", failure)
    if action.action_type is CorpActionType.DEMERGER:
        return out("REVIEW", "MANUAL_LEDGER_ENTRY_REQUIRED")
    if combined:
        return out("REVIEW", "COMBINED_ACTIONS")
    if not action.automatic_application:
        return out("PENDING", "AUTOMATIC_APPLICATION_OFF")
    return out("PENDING", "AWAITING_APPLICATION")


def _event(kind: str, accounts: list[dict], **fields: Any) -> dict:
    lead = min(accounts, key=lambda a: STATES.index(a["state"]))
    return {"kind": kind, "state": lead["state"], "reason_code": lead["reason_code"],
            "accounts": accounts, **fields}


def build_timeline(
    conn: Any,
    accounts: list[TimelineAccount],
    *,
    isin: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict:
    """Every event touching the accounts' ISINs, newest ex-date first, plus observed
    coverage. Raises ``SecurityNotOwned`` when ``isin`` is not the accounts' own."""
    today = date.today()
    ledgers = [_Ledger(a, load_transactions(conn, account_id=a.id)) for a in accounts]
    aliases = verified_map(conn)

    def resolve(value: str | None) -> str | None:
        return aliases.get(value, value) if value else value

    held = {t.isin for ledger in ledgers for t in ledger.transactions}
    owned = held | {old for old, current in aliases.items() if current in held}
    if isin is not None:
        if isin not in owned:
            raise SecurityNotOwned(isin)
        owned = {i for i in owned if resolve(i) == resolve(isin)}
    scope = sorted(owned)
    events: list[dict] = []
    if scope:
        events += _action_events(conn, ledgers, scope, resolve, today)
        events += _dividend_events(conn, ledgers, scope, resolve, today)
        events += _alias_events(conn, ledgers, set(scope), resolve, today)
        events += _notice_events(conn, ledgers, scope, resolve, events)
        meta = securities.lookup(conn, sorted({e["isin"] for e in events}))
        for e in events:
            e["symbol"] = securities.describe(meta, e["isin"]).get("symbol")
    if date_from is not None or date_to is not None:
        events = [
            e for e in events
            if e["ex_date"] is not None
            and (date_from is None or e["ex_date"] >= date_from)
            and (date_to is None or e["ex_date"] <= date_to)
        ]
    events.sort(key=lambda e: (e["ex_date"] or date.min, e["kind"], e["isin"], e["id"]),
                reverse=True)
    # Undated events sort last.
    events.sort(key=lambda e: e["ex_date"] is None)
    logger.info(
        "corporate_action_timeline_built",
        extra={"accounts": len(accounts), "isins": len(scope), "events": len(events)},
    )
    return {"events": events, "coverage": _coverage(conn, date_from, date_to)}


def _action_events(conn, ledgers, scope, resolve, today) -> list[dict]:
    stored = list_corporate_actions(conn, scope)
    actions = [replace(a, isin=resolve(a.isin), resulting_isin=resolve(a.resulting_isin))
               for a in stored]
    _safe, failures = screen(actions)
    failure_code: dict[str | None, str] = {}
    for f in failures:
        failure_code.setdefault(f.action.id, f.code)
    safe_ids = {a.id for a in _safe}
    ids = [a.id for a in actions]
    conflicts = _latest_conflicts(conn, "action_id", ids)
    observed = _observed_at(conn, "action_id", ids)
    out = []
    for original, action in zip(stored, actions, strict=True):
        combined = sum(
            a.isin == action.isin and a.ex_date == action.ex_date and a.id in safe_ids
            for a in actions
        ) > 1
        accounts = [
            _action_account(ledger, action, failure_code.get(action.id),
                            conflicts.get(action.id), combined, today)
            for ledger in ledgers
        ]
        out.append(_event(
            action.action_type.value, accounts,
            id=action.id, isin=original.isin,
            related_isin=original.resulting_isin,
            ex_date=action.ex_date, record_date=action.record_date,
            allotment_date=action.allotment_date, payment_date=None,
            bonus_new=action.bonus_new, bonus_for=action.bonus_for,
            face_value_before=money(action.face_value_before),
            face_value_after=money(action.face_value_after),
            amount_per_share=None,
            source=action.source, source_url=action.source_url,
            observed_at=observed.get(action.id),
            reference_state=action.verification.value,
        ))
    return out


def _dividend_events(conn, ledgers, scope, resolve, today) -> list[dict]:
    stored = list_dividend_events(conn, isins=scope)
    if not stored:
        return []
    resolved = [replace(e, isin=resolve(e.isin)) for e in stored]
    # ONE DIVIDEND, TWO ROWS: an unverified twin of a VERIFIED declaration is the same
    # declaration once identities are resolved, not a second one awaiting confirmation.
    # The rule is app.engine.dividends.drop_unverified_twins, shared with the
    # /dividends/expected endpoint so the two cannot disagree; it runs AFTER resolution
    # because that is the space the key is compared in.
    kept = {e.id for e in drop_unverified_twins(resolved)}
    pairs = [(o, e) for o, e in zip(stored, resolved, strict=True) if e.id in kept]
    events = [e for _, e in pairs]
    actions = [replace(a, isin=resolve(a.isin)) for a in list_corporate_actions(conn, scope)]
    ids = [e.id for e in events]
    conflicts = _latest_conflicts(conn, "dividend_id", ids)
    observed = _observed_at(conn, "dividend_id", ids)
    per_account = []
    for ledger in ledgers:
        expected, unprojectable = compute_expected_dividends(ledger.transactions, events, actions)
        received: dict[str, Any] = {}
        for t in ledger.transactions:
            if t.txn_type is not TxnType.DIVIDEND:
                continue
            linked = parse_event_source_ref(t.source_ref)
            if linked is not None:
                received[linked[0]] = received.get(linked[0], ZERO) + (t.dividend_amount or ZERO)
        per_account.append((
            ledger,
            {e.event_id: e for e in expected},
            {(u.isin, u.ex_date): u.code for u in unprojectable},
            received,
        ))
    out = []
    for original, event in pairs:
        accounts = []
        for ledger, expected, unprojectable, received in per_account:
            quantity, skipped = ledger.before(event.isin, event.ex_date)
            projected = expected.get(event.id)
            code = unprojectable.get((event.isin, event.ex_date))
            if event.id in conflicts:
                row = _account(ledger, "CONFLICT", conflicts[event.id], quantity)
            elif event.id in received:
                row = _account(ledger, "APPLIED", None, quantity, effect={
                    "txn_type": "DIVIDEND", "isin": event.isin,
                    "amount": money(received[event.id])})
            elif skipped:
                row = _account(ledger, "EXCLUDED", skipped, quantity)
            elif quantity <= ZERO:
                row = _account(ledger, "NOT_HELD", None, quantity)
            elif event.verification is Verification.DISPUTED:
                row = _account(ledger, "EXCLUDED", "REFERENCE_DISPUTED", quantity)
            elif projected is not None:
                row = _account(
                    ledger, "PENDING",
                    "UPCOMING" if event.ex_date > today else "EXPECTED_NOT_RECEIVED",
                    quantity,
                    effect={"txn_type": "DIVIDEND", "isin": event.isin,
                            "amount": money(projected.gross_expected), "projected": True},
                )
            elif code == EVIDENCE_WITHDRAWN:
                row = _account(ledger, "EXCLUDED", code, quantity)
            elif code in _AWAITING:
                row = _account(ledger, "PENDING", "AWAITING_VERIFICATION", quantity)
            elif code is not None:
                row = _account(ledger, "REVIEW", code, quantity)
            else:
                # compute_expected_dividends places every held event in exactly one list.
                raise RuntimeError(f"dividend {event.id} has no timeline state")
            accounts.append(row)
        out.append(_event(
            "DIVIDEND", accounts,
            id=event.id, isin=original.isin, related_isin=None,
            ex_date=event.ex_date, record_date=event.record_date, allotment_date=None,
            payment_date=event.payment_date, bonus_new=None, bonus_for=None,
            face_value_before=None, face_value_after=None,
            amount_per_share=money(event.amount_per_share),
            source=event.source, source_url=event.source_url,
            observed_at=observed.get(event.id), reference_state=event.verification.value,
        ))
    return out


def _alias_events(conn, ledgers, scope: set[str], resolve, today) -> list[dict]:
    out = []
    for alias in list_aliases(conn):
        if alias.old_isin not in scope and alias.current_isin not in scope:
            continue
        accounts = []
        for ledger in ledgers:
            quantity, skipped = ledger.before(resolve(alias.old_isin), alias.effective_date)
            if skipped:
                row = _account(ledger, "EXCLUDED", skipped, quantity)
            elif quantity <= ZERO:
                row = _account(ledger, "NOT_HELD", None, quantity)
            elif alias.verification is Verification.DISPUTED:
                row = _account(ledger, "EXCLUDED", "REFERENCE_DISPUTED", quantity)
            elif alias.verification is Verification.DRAFT:
                row = _account(ledger, "PENDING", "AWAITING_VERIFICATION", quantity)
            elif alias.effective_date > today:
                row = _account(ledger, "PENDING", "UPCOMING", quantity)
            else:
                row = _account(ledger, "APPLIED", None, quantity)
            accounts.append(row)
        out.append(_event(
            "ISIN_CHANGE", accounts,
            id=f"{alias.old_isin}:{alias.current_isin}", isin=alias.old_isin,
            related_isin=alias.current_isin, ex_date=alias.effective_date,
            record_date=None, allotment_date=None, payment_date=None,
            bonus_new=None, bonus_for=None, face_value_before=None, face_value_after=None,
            amount_per_share=None, source=alias.source, source_url=alias.source_url,
            observed_at=None, reference_state=alias.verification.value,
        ))
    return out


def _notice_events(conn, ledgers, scope, resolve, already: list[dict]) -> list[dict]:
    """Merger/demerger/rights/buyback/capital-reduction/consolidation notices: REVIEW on
    an eligible holding, never applied (OD-7). A notice already represented by a
    reference row (a DEMERGER in corporate_action) is not repeated.

    TWO SOURCES, ONE EVENT, DE-DUPLICATED ON ONE KEY. ``corporate_action_observation``
    dates from migration 061, so every review notice the feed read BEFORE it exists only
    in ``reference_notice`` -- leaving that table unread silently erased every pre-061
    merger, rights and buyback from the timeline, with no error anywhere. Both sources
    are keyed ``(resolved isin, classification, ex_date)`` and an observation always
    wins: it is the richer row, carrying a provider, a result and a fetch time. A notice
    is suppressed by an observation with the same key whatever that observation's result
    -- a row the feed already judged is not resurrected from the raw table.
    """
    review = list(REVIEW_CLASSES)
    observed_keys = {
        (resolve(isin), classification, ex_date)
        for isin, classification, ex_date in conn.execute(
            "SELECT isin, classification, ex_date FROM corporate_action_observation "
            "WHERE classification = ANY(%s) AND isin = ANY(%s)",
            (review, scope),
        ).fetchall()
    }
    rows = conn.execute(
        """SELECT DISTINCT ON (isin, classification, ex_date)
                  id::text, isin, classification, ex_date, provider,
                  raw_payload->>'source_url', fetched_at
             FROM corporate_action_observation
            WHERE classification = ANY(%s) AND isin = ANY(%s)
              AND result IN ('PENDING', 'REVIEW', 'ACCEPTED')
            ORDER BY isin, classification, ex_date, fetched_at DESC, id""",
        (review, scope),
    ).fetchall()
    candidates = [
        (obs_id, isin, classification, ex_date, provider, source_url, fetched_at, False)
        for obs_id, isin, classification, ex_date, provider, source_url, fetched_at in rows
    ]
    for notice_hash, isin, subject, raw_ex, source_url, observed_at in conn.execute(
        "SELECT notice_hash, isin, subject, ex_date, source_url, observed_at "
        "FROM reference_notice WHERE isin = ANY(%s) ORDER BY notice_hash",
        (scope,),
    ).fetchall():
        classification = classify_subject(subject)
        if classification not in REVIEW_CLASSES:
            continue
        candidates.append((
            notice_hash, isin, classification, parse_nse_date(raw_ex),
            _NOTICE_PROVIDER, source_url, observed_at, True,
        ))

    emitted = {(resolve(e["isin"]), e["kind"], e["ex_date"]) for e in already}
    out = []
    for candidate in candidates:
        event_id, isin, classification, ex_date, source, source_url, observed_at, is_notice = (
            candidate
        )
        key = (resolve(isin), classification, ex_date)
        if key in emitted or (is_notice and key in observed_keys):
            continue
        emitted.add(key)
        accounts = []
        for ledger in ledgers:
            if ex_date is None:
                held = ledger.ever_held(resolve(isin))
                row = _account(ledger, "REVIEW" if held else "NOT_HELD",
                               "MISSING_EX_DATE" if held else None)
            else:
                quantity, skipped = ledger.before(resolve(isin), ex_date)
                if skipped:
                    row = _account(ledger, "EXCLUDED", skipped, quantity)
                elif quantity <= ZERO:
                    row = _account(ledger, "NOT_HELD", None, quantity)
                else:
                    row = _account(ledger, "REVIEW", "ACTION_NOT_AUTOMATED", quantity)
            accounts.append(row)
        out.append(_event(
            classification, accounts,
            id=event_id, isin=isin, related_isin=None, ex_date=ex_date,
            record_date=None, allotment_date=None, payment_date=None,
            bonus_new=None, bonus_for=None, face_value_before=None, face_value_after=None,
            amount_per_share=None, source=source, source_url=source_url,
            observed_at=observed_at, reference_state=None,
        ))
    return out


def _coverage(conn: Any, date_from: date | None, date_to: date | None) -> dict:
    """Observed ranges (global reference data), contiguous ranges merged per provider and
    exchange set. Never a completeness claim."""
    rows = conn.execute(
        """SELECT provider, start_date, end_date, exchanges, checked_at
             FROM corporate_action_coverage
            WHERE (%s::date IS NULL OR end_date >= %s::date)
              AND (%s::date IS NULL OR start_date <= %s::date)
            ORDER BY provider, exchanges, start_date""",
        (date_from, date_from, date_to, date_to),
    ).fetchall()
    ranges: list[dict] = []
    for provider, start, end, exchanges, checked_at in rows:
        last = ranges[-1] if ranges else None
        if (last and last["provider"] == provider and last["exchanges"] == list(exchanges)
                and start <= last["end"] + timedelta(days=1)):
            last["end"] = max(last["end"], end)
            last["checked_at"] = max(last["checked_at"], checked_at)
            continue
        ranges.append({"provider": provider, "start": start, "end": end,
                       "exchanges": list(exchanges), "checked_at": checked_at})
    return {
        "state": "OBSERVED_RANGES_ONLY" if ranges else "NO_OBSERVED_RANGES",
        "checked_through": max((r["end"] for r in ranges), default=None),
        "ranges": ranges,
    }
