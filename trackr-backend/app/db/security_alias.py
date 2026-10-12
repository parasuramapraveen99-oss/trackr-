"""Retired ISIN -> its replacement, for the SAME security -- see
``app/db/migrations/027_security_alias.sql`` for the full reasoning.

THE GOVERNING RULE, and the reason this module is shaped the way it is:

    An alias is an IDENTITY ASSERTION, NOT AN INFERENCE. It may be created only from
    verified evidence. The structural checks below can REJECT an unsafe alias; they can
    never create one.

Its corollary, which is the sharper form of "detect, don't infer": DETECTION MAY USE
INFERENCE, RESOLUTION MAY NOT. ``detect_candidates`` is free to be aggressive -- matching
issuers, dangling positions, suspicious arithmetic -- because everything it returns is a
hypothesis for a person. ``add_alias``/``verify_alias`` are the opposite: they demand
evidence and refuse anything they cannot check.

Nothing here writes to ``transaction``. An alias is applied by
``app.db.repository.load_transactions`` at the READ boundary, which is what preserves FIFO
acquisition dates across an identifier change -- a synthetic TRANSFER_OUT/TRANSFER_IN pair
would date the new lot at the change and silently turn a long-term holding short-term.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.db import audit
from app.engine.corporate_actions import Verification

logger = logging.getLogger("security_alias")

#: NSDL's ISIN structure: characters 1-3 are country + instrument type (INE equity, INF
#: mutual fund), 4-7 the issuer/company, 8-9 the security within that issuer, 12 a check
#: digit. Comparing the first 7 therefore compares "same type, same company" in one slice
#: while leaving the security code -- the part that legitimately differs between an
#: issuer's several instruments -- deliberately out of it.
_IDENTITY_PREFIX = 7

_SELECT_COLUMNS = (
    "old_isin, current_isin, effective_date, source, source_url, "
    "verification::text, notes"
)


@dataclass(frozen=True, slots=True)
class SecurityAlias:
    old_isin: str
    current_isin: str
    effective_date: date
    source: str
    source_url: str
    verification: Verification = Verification.DRAFT
    notes: str = ""


class SecurityAliasError(ValueError):
    """An alias that must not be written. Distinct from a lookup miss so a router can
    turn it into a 422 rather than a 404."""


def _row_to_alias(row: tuple) -> SecurityAlias:
    return SecurityAlias(
        old_isin=row[0],
        current_isin=row[1],
        effective_date=row[2],
        source=row[3],
        source_url=row[4],
        verification=Verification(row[5]),
        notes=row[6] or "",
    )


def _assert_same_identity(old_isin: str, current_isin: str) -> None:
    """Country+type and issuer must match -- REJECTION ONLY.

    A different issuer means a merger or amalgamation: a different event, with its own
    prescribed cost allocation and its own tax treatment (``app.engine.demerger``), which
    must not be smuggled in through an identity alias. A different instrument type would
    alias an equity onto a mutual fund.

    Passing this check PROVES NOTHING. One issuer legitimately has many securities,
    distinguished by the security code at characters 8-9, so "same issuer" is necessary
    and nowhere near sufficient -- only verified evidence creates an alias.
    """
    if old_isin == current_isin:
        raise SecurityAliasError("an ISIN cannot alias to itself")
    if old_isin[:_IDENTITY_PREFIX] != current_isin[:_IDENTITY_PREFIX]:
        raise SecurityAliasError(
            f"{old_isin} and {current_isin} are different issuers or instrument types "
            "(ISIN characters 1-7). That is a merger or amalgamation, not an identifier "
            "change -- it needs a corporate action with its own cost allocation, not an "
            "identity alias"
        )


def _assert_acyclic(conn: Any, old_isin: str, current_isin: str) -> None:
    """Reject any edge that would close a cycle or fork the chain.

    Two separate hazards, both of which make "what is this security now?" unanswerable:
      - a CYCLE (A -> B -> A) would make ``verified_map`` loop forever;
      - re-pointing an ISIN that is already some other row's ``current_isin`` while it
        also has its own outgoing edge would fork resolution.

    Walking forward from ``current_isin`` and refusing if we arrive back at ``old_isin``
    catches both, and terminates: each hop moves to a distinct ISIN and the table is
    finite.
    """
    seen = {old_isin}
    cursor_isin = current_isin
    while cursor_isin is not None:
        if cursor_isin in seen:
            raise SecurityAliasError(
                f"aliasing {old_isin} -> {current_isin} would create a cycle "
                f"(reached {cursor_isin} again)"
            )
        seen.add(cursor_isin)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT current_isin FROM security_alias WHERE old_isin = %s", (cursor_isin,)
            )
            row = cur.fetchone()
        cursor_isin = row[0] if row else None


def add_alias(conn: Any, alias: SecurityAlias, actor_uid: str | None = None) -> None:
    """Record one alias. Structural gates reject; they never vouch.

    ``alias.verification`` is honoured as given -- a caller with real evidence may write
    VERIFIED directly, exactly as ``add_corporate_action`` allows, and anything less
    certain lands DRAFT and inert.
    """
    if not alias.source or not alias.source_url:
        raise SecurityAliasError("an alias needs a source and a source_url")
    _assert_same_identity(alias.old_isin, alias.current_isin)
    _assert_acyclic(conn, alias.old_isin, alias.current_isin)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO security_alias
                (old_isin, current_isin, effective_date, source, source_url,
                 verification, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                alias.old_isin,
                alias.current_isin,
                alias.effective_date,
                alias.source,
                alias.source_url,
                alias.verification.value,
                alias.notes or None,
            ),
        )

    # entity_id is uuid-typed and an alias is keyed by ISIN, so the identity lives in
    # the diff payload rather than being given a surrogate uuid it has no other use for.
    audit.record(
        conn,
        audit.ACTION_SECURITY_ALIAS_ADDED,
        "security_alias",
        None,
        actor_uid,
        {
            "old_isin": alias.old_isin,
            "current_isin": alias.current_isin,
            "effective_date": alias.effective_date.isoformat(),
            "verification": alias.verification.value,
            "source": alias.source,
        },
    )


def list_aliases(conn: Any, verification: Verification | None = None) -> list[SecurityAlias]:
    query = f"SELECT {_SELECT_COLUMNS} FROM security_alias"
    params: list[Any] = []
    if verification is not None:
        query += " WHERE verification = %s"
        params.append(verification.value)
    query += " ORDER BY old_isin"
    with conn.cursor() as cur:
        cur.execute(query, params)
        return [_row_to_alias(row) for row in cur.fetchall()]


def verified_map(conn: Any, as_of: date | None = None) -> dict[str, str]:
    """Every VERIFIED alias, already resolved to its terminal ISIN.

    Returned fully collapsed (A -> C, not A -> B -> C) so the read path is a single dict
    lookup per row and can never see a half-resolved identity. DRAFT and DISPUTED rows
    are absent entirely -- that is what "inert" means.
    """
    direct = {
        a.old_isin: a.current_isin
        for a in list_aliases(conn, verification=Verification.VERIFIED)
        if a.effective_date <= (as_of or date.today())
    }
    resolved: dict[str, str] = {}
    for old in direct:
        seen = {old}
        target = direct[old]
        # Terminates: _assert_acyclic refuses cycles at write, and this guard means even
        # a row inserted around this module cannot hang a read.
        while target in direct and target not in seen:
            seen.add(target)
            target = direct[target]
        resolved[old] = target
    return resolved


def verify_alias(conn: Any, old_isin: str, actor_uid: str | None = None) -> None:
    """Promote a DRAFT alias to VERIFIED. The caller is asserting it has evidence; this
    function does not and cannot check that -- which is exactly why the post-application
    reconciliation gate (``reconcile_after_alias``) exists."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE security_alias SET verification = %s WHERE old_isin = %s "
            "AND verification != %s",
            (Verification.VERIFIED.value, old_isin, Verification.DISPUTED.value),
        )
        if cur.rowcount == 0:
            return  # no such row, or DISPUTED -- a rejection is not undone by a verify

    audit.record(
        conn,
        audit.ACTION_SECURITY_ALIAS_VERIFIED,
        "security_alias",
        None,
        actor_uid,
        {"old_isin": old_isin},
    )


def quarantine_alias(conn: Any, old_isin: str, reason: str, actor_uid: str | None = None) -> None:
    """Disable an alias that broke the ledger it was applied to. An alias that breaks the
    ledger is a wrong alias, so this is automatic, not advisory -- see
    ``reconcile_after_alias``. The row is never deleted: it stays as the record of what
    was tried and why it was withdrawn."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE security_alias SET verification = %s, "
            "notes = COALESCE(NULLIF(notes, ''), '') || %s WHERE old_isin = %s",
            (Verification.DISPUTED.value, f"\nQUARANTINED: {reason}", old_isin),
        )
    audit.record(
        conn,
        audit.ACTION_SECURITY_ALIAS_QUARANTINED,
        "security_alias",
        None,
        actor_uid,
        {"old_isin": old_isin, "reason": reason},
    )


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """What applying an alias actually did to one account's ledger."""

    ok: bool
    failures: tuple[str, ...] = ()


def reconcile_after_alias(
    conn: Any, account_id: str, old_isin: str, current_isin: str, as_of: date
) -> ReconcileResult:
    """Prove the ledger an alias produces still holds its own invariants.

    An alias is not finished when it is verified; it is finished when what it produces
    reconciles. ``verify_alias`` cannot check the evidence behind an identity claim, so
    this is the check that can actually fail: it replays the account both ways and
    compares.

    Asserted, all against the same account and date:
      - the transaction COUNT is unchanged (an alias renames, it never adds or drops);
      - no NEW negative holding appears (if merging the two identities makes the position
        impossible, the alias is wrong, or a quantity-changing corporate action is missing
        and must be entered first -- either way, do not apply this);
      - every surviving FIFO lot's ``open_date`` is one that existed before (the whole
        point: an identifier change must not restart a holding period);
      - the combined cost pool equals the sum of the two separate pools.

    Returns rather than raises: the caller decides between quarantine and a retry, and a
    failed reconciliation is an expected outcome here, not an exception.
    """
    from app.db.repository import load_transactions
    from app.engine.holdings import AVG_COST_PLACES, compute_holdings_partitioned
    from app.engine.lots import compute_lots
    from app.engine.types import ACQUIRING_TYPES, DISPOSING_TYPES, FREE_SHARE_TYPES, ZERO

    raw = load_transactions(conn, account_id=account_id, apply_aliases=False)
    before_lots = {
        (lot.isin, lot.open_date) for lot in _safe_lots(compute_lots, raw, as_of)
    }
    before_holdings, before_skipped = compute_holdings_partitioned(raw, as_of)
    before_pool = sum(
        (h.invested for h in before_holdings if h.isin in (old_isin, current_isin)), ZERO
    )
    before_broken = {s.isin for s in before_skipped}

    aliased = [
        t if t.isin != old_isin else _with_isin(t, current_isin) for t in raw
    ]
    after_holdings, after_skipped = compute_holdings_partitioned(aliased, as_of)
    after_pool = sum((h.invested for h in after_holdings if h.isin == current_isin), ZERO)
    after_broken = {s.isin for s in after_skipped}

    failures: list[str] = []
    if len(aliased) != len(raw):
        failures.append("transaction count changed")

    newly_broken = after_broken - before_broken - {old_isin, current_isin}
    if newly_broken:
        failures.append(f"introduced a negative holding in {sorted(newly_broken)}")
    if current_isin in after_broken:
        # Checked regardless of whether it was already broken BEFORE, because fixing
        # exactly that is what the alias was for. Still broken after merging means the
        # identity is wrong, or a quantity-changing corporate action is missing -- the
        # GUJTHEM shape, where 65 held cannot cover 487 sold until its split is on file.
        # Either way the alias must not be left applied.
        failures.append(
            f"{current_isin} still does not reconcile once merged -- a quantity-changing "
            "corporate action is probably missing and must be entered first"
        )

    after_dates = {
        lot.open_date for lot in _safe_lots(compute_lots, aliased, as_of)
        if lot.isin == current_isin
    }
    known_dates = {d for (isin, d) in before_lots if isin in (old_isin, current_isin)}
    known_dates.update(
        t.txn_date for t in raw if t.isin in (old_isin, current_isin)
        and t.txn_type in ACQUIRING_TYPES | FREE_SHARE_TYPES and t.txn_date <= as_of
    )
    invented = after_dates - known_dates
    if invented:
        failures.append(
            f"acquisition dates not present before the alias: {sorted(map(str, invented))}"
        )

    # Rule B (app.engine.holdings) rounds the average to 4dp ONCE at emission and derives
    # `invested` from it, so merging two pools into one re-rounds a single average instead
    # of two and moves the total by up to half a paisa-of-a-paisa per unit. That drift is
    # arithmetic, not a cost change -- demanding exact equality would fail every real
    # alias. Anything LARGER than Rule B's own precision allows is a genuine move and
    # still fails.
    merged_quantity = sum(
        (h.quantity for h in after_holdings if h.isin == current_isin), ZERO
    )
    tolerance = AVG_COST_PLACES * merged_quantity
    disposals = any(t.isin in (old_isin, current_isin) and t.txn_type in DISPOSING_TYPES
                    and t.txn_date <= as_of for t in raw)
    # Joining identifier histories can legitimately repair a previously unmatched sale
    # or change the average cost assigned to it. Compare open pools only without sales.
    # Likewise when either side was broken BEFORE the merge: a broken holding is
    # partitioned out and contributes nothing to before_pool, so the comparison measures
    # the repair, not a cost move. Real case, SKMEGGPROD: the split's SPLIT_ADJ lands on
    # the new ISIN, which has no base until merged (194.30 before vs 2520.57 after). The
    # checks that matter here still ran above: reconciles after, breaks nothing else,
    # invents no acquisition date.
    repaired = bool({old_isin, current_isin} & before_broken)
    if not failures and not disposals and not repaired and (
        abs(before_pool - after_pool) > tolerance
    ):
        failures.append(f"cost pool moved: {before_pool} -> {after_pool}")

    return ReconcileResult(ok=not failures, failures=tuple(failures))


@dataclass(frozen=True, slots=True)
class PromotionSummary:
    """What one ``promote_draft_aliases`` run did, for a log line or a review command.

    ``inconclusive`` is not a fourth alias state -- an alias counted here is still
    ``DRAFT`` on disk, exactly as it was before the run. It exists only so an unexpected
    failure is visible in the summary rather than silently indistinguishable from "still
    correctly waiting to be checked."
    """

    verified: int = 0
    quarantined: int = 0
    inconclusive: int = 0
    details: tuple[str, ...] = ()


def _affected_accounts(conn: Any, old_isin: str, current_isin: str) -> list[tuple[str, str]]:
    """Every account that has ever transacted in either ISIN, paired with a label a
    human can act on without a second lookup -- ``broker/investor.code``, the same
    identifier ``app.db.repository.list_accounts`` exists to show instead of a raw
    UUID "nobody remembers" (that function's own words). An account with neither ISIN
    is excluded rather than reconciled against trivially-true output.

    Returns ``(account_id, label)`` rather than the label alone: the UUID is kept in
    every failure string alongside the label for an exact lookup, since ``label`` is
    not guaranteed unique (an investor can hold two accounts at the same broker)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT a.id, a.broker, i.code
            FROM transaction t
            JOIN account a ON a.id = t.account_id
            JOIN investor i ON i.id = a.investor_id
            WHERE t.isin IN (%s, %s)
            """,
            (old_isin, current_isin),
        )
        return [
            (str(account_id), f"{broker}/{code}") for account_id, broker, code in cur.fetchall()
        ]


def promote_draft_aliases(
    conn: Any, as_of: date | None = None, actor_uid: str | None = None
) -> PromotionSummary:
    """Promote effective, evidenced drafts only after every affected account reconciles.

    Ledger arithmetic is a rejection gate, never proof of identity. Missing evidence
    or incomplete personal history leaves a draft pending for subsequent retries.
    DISPUTED aliases remain excluded and require an explicit correction.
    """
    as_of = as_of or date.today()
    verified = quarantined = inconclusive = 0
    details: list[str] = []

    for alias in list_aliases(conn, verification=Verification.DRAFT):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM security_alias_evidence WHERE old_isin=%s "
                "AND current_isin=%s AND effective_date=%s",
                (alias.old_isin, alias.current_isin, alias.effective_date),
            )
            evidence = cur.fetchone()
        if evidence is None or alias.effective_date > as_of:
            inconclusive += 1
            details.append(f"{alias.old_isin}: waiting for dated identity evidence")
            continue
        affected = _affected_accounts(conn, alias.old_isin, alias.current_isin)

        failures: list[str] = []
        raised = False
        for account_id, label in affected:
            try:
                result = reconcile_after_alias(
                    conn, account_id, alias.old_isin, alias.current_isin, as_of
                )
            except Exception:
                logger.warning(
                    "reconcile_after_alias raised for %s -> %s, account %s (%s) -- "
                    "leaving DRAFT rather than quarantining on an unrelated failure",
                    alias.old_isin, alias.current_isin, account_id, label, exc_info=True,
                )
                raised = True
                continue
            if not result.ok:
                # Both the human-readable label and the exact UUID: label alone is not
                # guaranteed unique (an investor can hold two accounts at one broker).
                failures.append(
                    f"{label} (account {account_id}): {'; '.join(result.failures)}"
                )

        if raised:
            # An exception on ANY account makes the round's picture incomplete -- even
            # if every OTHER account reconciled cleanly, that is not the same as knowing
            # this alias is safe. Left DRAFT; retried in full next run.
            inconclusive += 1
            details.append(
                f"{alias.old_isin} -> {alias.current_isin}: inconclusive "
                "(reconcile_after_alias raised for at least one account)"
            )
            continue

        if failures:
            reason = "; ".join(failures)
            # Incomplete personal history can fail this gate without disproving the
            # exchange's identity evidence. Retry after subsequent uploads.
            inconclusive += 1
            details.append(f"{alias.old_isin} -> {alias.current_isin}: pending -- {reason}")
        else:
            verify_alias(conn, alias.old_isin, actor_uid=actor_uid)
            conn.commit()
            verified += 1
            details.append(
                f"{alias.old_isin} -> {alias.current_isin}: verified "
                f"({len(affected)} account(s) checked)"
            )

    return PromotionSummary(
        verified=verified, quarantined=quarantined, inconclusive=inconclusive,
        details=tuple(details),
    )


def _with_isin(txn: Any, isin: str) -> Any:
    from dataclasses import replace

    return replace(txn, isin=isin)


def _safe_lots(compute_lots: Any, transactions: list[Any], as_of: date) -> list[Any]:
    """FIFO lots, or nothing if the ledger does not reconcile well enough to build them.

    A ledger that already cannot produce lots is not evidence for or against an alias --
    the other checks still apply, and the lot-date comparison simply has nothing to say.
    """
    try:
        return list(compute_lots(transactions, as_of))
    except Exception:
        return []


@dataclass(frozen=True, slots=True)
class AliasCandidate:
    """A SUSPECTED identifier change, with the arithmetic that makes it suspicious.

    A hypothesis for a person, never an instruction to act -- the same posture
    ``app.engine.reconcile``'s ``likely_cause`` already takes. Nothing here has been
    verified, and nothing downstream may treat it as evidence.
    """

    old_isin: str
    current_isin: str
    symbol: str | None
    #: Quantity stranded on the suspected-retired ISIN.
    dangling_quantity: Any
    #: Net quantity disposed on the suspected-current ISIN with no acquisition behind it.
    unexplained_disposal: Any
    reason: str


def detect_candidates(conn: Any, investor_id: str | None = None) -> list[AliasCandidate]:
    """Find ISIN pairs that LOOK like an identifier change.

    DETECTION MAY USE INFERENCE -- this is the half of the rule where that is allowed, and
    it is deliberately aggressive: any two traded ISINs sharing country+type+issuer
    (characters 1-7) where one holds a stranded position and the other shows disposals
    with no acquisition behind them. That pattern has an innocent explanation (an issuer
    with two genuinely different instruments, characters 8-9 apart) which is exactly why
    this returns candidates and not aliases.

    RESOLUTION MAY NOT. Nothing here writes anything. A human, or an automated check
    against a trusted source, still has to establish the identity before
    ``add_alias``/``verify_alias`` will record it -- and the structural gates there can
    still refuse what this suggests.
    """
    from app.db.repository import load_transactions
    from app.engine.holdings import compute_holdings_partitioned
    from app.engine.types import DISPOSING_TYPES, ZERO

    transactions = load_transactions(conn, investor_id=investor_id, apply_aliases=False)
    if not transactions:
        return []

    traded = sorted({t.isin for t in transactions})
    by_identity: dict[str, list[str]] = {}
    for isin in traded:
        by_identity.setdefault(isin[:_IDENTITY_PREFIX], []).append(isin)

    existing = {a.old_isin for a in list_aliases(conn)}
    as_of = max(t.txn_date for t in transactions)
    last_traded: dict[str, date] = {}
    for t in transactions:
        if t.txn_date > last_traded.get(t.isin, date.min):
            last_traded[t.isin] = t.txn_date
    holdings, skipped = compute_holdings_partitioned(transactions, as_of)
    held = {h.isin: h.quantity for h in holdings}
    broken = {s.isin for s in skipped}

    with conn.cursor() as cur:
        cur.execute("SELECT isin, symbol FROM security WHERE isin = ANY(%s)", (traded,))
        symbols = dict(cur.fetchall())

    candidates: list[AliasCandidate] = []
    for group in by_identity.values():
        if len(group) < 2:
            continue
        for old_isin in group:
            for current_isin in group:
                if old_isin == current_isin or old_isin in existing:
                    continue
                # The signature: one side stranded, the other disposing of what it never
                # acquired. Both halves must be present -- either alone is ordinary.
                dangling = held.get(old_isin, ZERO)
                if dangling <= ZERO:
                    continue
                disposed = sum(
                    (t.quantity for t in transactions
                     if t.isin == current_isin and t.txn_type in DISPOSING_TYPES),
                    ZERO,
                )
                acquired = sum(
                    (t.quantity for t in transactions
                     if t.isin == current_isin and t.txn_type not in DISPOSING_TYPES),
                    ZERO,
                )
                unexplained = disposed - acquired

                if current_isin in broken and unexplained > ZERO:
                    # The DAMAGING shape, and the strong signal: one side strands a
                    # position while the other disposes of what it never acquired.
                    reason = (
                        f"{old_isin} strands {dangling} units while {current_isin} "
                        f"disposes {unexplained} it never acquired; same issuer. "
                        "SUSPECTED identifier change -- unverified"
                    )
                elif held.get(current_isin, ZERO) > ZERO and last_traded[old_isin] < last_traded[
                    current_isin
                ]:
                    # Direction matters and the quiet shape is symmetric, so it would
                    # otherwise report A->B and B->A and leave a reviewer guessing. The
                    # retired identifier is the one that stopped trading first.
                    # The QUIET shape: both identities hold a position and nothing
                    # contradicts, so no screen is broken and nobody would look. Weaker
                    # by construction -- an issuer may legitimately have two instruments
                    # (characters 8-9 apart) -- which is precisely why it is a candidate
                    # and not an alias. Worth surfacing anyway: left alone it silently
                    # splits one holding in two and strands the retired half at whatever
                    # price it last traded (SKMEGGPROD's shape).
                    reason = (
                        f"{old_isin} and {current_isin} are both held under one issuer "
                        f"({dangling} and {held[current_isin]} units). POSSIBLE "
                        "identifier change, or two genuinely different instruments -- "
                        "unverified either way"
                    )
                else:
                    continue

                candidates.append(
                    AliasCandidate(
                        old_isin=old_isin,
                        current_isin=current_isin,
                        symbol=symbols.get(current_isin),
                        dangling_quantity=dangling,
                        unexplained_disposal=max(unexplained, ZERO),
                        reason=reason,
                    )
                )
    return candidates
