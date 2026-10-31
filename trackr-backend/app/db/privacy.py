"""DPDP data-principal rights: export and erasure.

Both are rights, not favours. They have to work on demand, which means they have to be
built and tested before client data lands rather than written in a hurry when the first
request arrives.
"""

from __future__ import annotations

from typing import Any

from app.db import audit, crypto, documents, workspace_privacy

#: Session flag the append-only trigger checks. Erasure is the ONE path allowed to delete
#: from the ledger, and it has to announce itself to do so -- see 001_core.sql. Migration 044's
#: ``ws_append_only`` reads the same setting, so the ledger and the twenty-five workspace tables
#: agree on one word for "this transaction is an authorised erasure" rather than having two.
ERASURE_FLAG = "app.erasure_authorised"

#: Transaction-local tenant scope. Workspace tables are under RLS (migration 045), so a read or
#: a delete with this unset sees nothing -- which is safe but indistinguishable from an investor
#: with no research, and is therefore checked explicitly rather than relied on.
TENANT_SETTING = "app.workspace_investor"


class LegalHoldError(PermissionError):
    """Erasure refused: an open legal hold covers this investor."""

    def __init__(self, investor_id: str, reasons: list[str]) -> None:
        self.investor_id = investor_id
        self.reasons = reasons
        super().__init__(
            f"Erasure refused for investor {investor_id}: open legal hold "
            f"({'; '.join(reasons)}). Where a dispute has been raised, records are kept "
            "until it resolves or SEBI directs otherwise."
        )


def open_holds(conn: Any, investor_id: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT reason FROM legal_hold WHERE investor_id = %s AND released_at IS NULL",
            (investor_id,),
        )
        return [row[0] for row in cur.fetchall()]


def place_legal_hold(conn: Any, investor_id: str, reason: str, actor_uid: str | None = None) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO legal_hold (investor_id, reason) VALUES (%s, %s) RETURNING id",
            (investor_id, reason),
        )
        hold_id = str(cur.fetchone()[0])
    audit.record(
        conn,
        audit.ACTION_LEGAL_HOLD_PLACED,
        "investor",
        investor_id,
        actor_uid,
        {"hold_id": hold_id, "reason": reason},
    )
    return hold_id


def release_legal_hold(conn: Any, hold_id: str, actor_uid: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE legal_hold SET released_at = now() WHERE id = %s AND released_at IS NULL",
            (hold_id,),
        )
    audit.record(conn, audit.ACTION_LEGAL_HOLD_RELEASED, "legal_hold", hold_id, actor_uid)


def export_investor(
    conn: Any,
    investor_id: str,
    include_identifiers: bool = False,
    actor_uid: str | None = None,
) -> dict[str, Any]:
    """Everything held about one investor -- the DPDP portability right.

    ``include_identifiers`` decrypts PAN and demat. Off by default: an export is usually
    wanted for its portfolio content, and decrypting identifiers that nobody asked for
    widens the blast radius of wherever the export file ends up. Turning it on is logged
    as a distinct access event, because reading personal data is itself something the
    principal is entitled to know about.
    """
    export: dict[str, Any] = {}

    with conn.cursor() as cur:
        cur.execute(
            "SELECT i.id, i.code, i.display_name, i.family_group_id, "
            "f.name AS family_name, i.onboarding_date, i.created_at "
            "FROM investor i LEFT JOIN family_group f ON f.id = i.family_group_id "
            "WHERE i.id = %s",
            (investor_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"no such investor: {investor_id}")
        export["investor"] = dict(zip([c.name for c in cur.description], row, strict=True))

        for table, sql in (
            ("accounts", "SELECT * FROM account WHERE investor_id = %s"),
            ("reconciliation_runs", "SELECT * FROM reconciliation_run WHERE investor_id = %s"),
            (
                "reconciliation_statements",
                "SELECT s.* FROM reconciliation_statement s "
                "JOIN account a ON a.id=s.account_id WHERE a.investor_id = %s",
            ),
            ("consents", "SELECT * FROM client_consent WHERE investor_id = %s"),
            ("legal_holds", "SELECT * FROM legal_hold WHERE investor_id = %s"),
            (
                "transactions",
                "SELECT t.* FROM transaction t JOIN account a ON a.id = t.account_id "
                "WHERE a.investor_id = %s ORDER BY t.txn_date",
            ),
            (
                # The equity F&O ledger (migration 064): a separate table, the same right.
                "fno_trades",
                "SELECT t.* FROM fno_trade t JOIN account a ON a.id = t.account_id "
                "WHERE a.investor_id = %s ORDER BY t.trade_date, t.seq",
            ),
            (
                # A registrar statement's own facts for each fund ledger row (migration 095):
                # printed amount, stamp duty, STT, TDS. Part of the same portability right.
                "mf_transaction_details",
                "SELECT * FROM mf_transaction_detail WHERE investor_id = %s "
                "ORDER BY created_at, transaction_id",
            ),
        ):
            cur.execute(sql, (investor_id,))
            columns = [c.name for c in cur.description]
            export[table] = [dict(zip(columns, r, strict=True)) for r in cur.fetchall()]

        if include_identifiers:
            cur.execute(
                "SELECT pan_enc, demat_enc FROM investor_secret WHERE investor_id = %s",
                (investor_id,),
            )
            secret = cur.fetchone()
            if secret:
                export["identifiers"] = {
                    "pan": crypto.decrypt(secret[0]) if secret[0] else None,
                    "demat": crypto.decrypt(secret[1]) if secret[1] else None,
                }
            audit.record(
                conn,
                audit.ACTION_SECRET_READ,
                "investor",
                investor_id,
                actor_uid,
                {"reason": "export with identifiers"},
            )

    # Documents are LISTED, never embedded. A portability export carrying megabytes of
    # decrypted PDF is a second copy of the most sensitive thing in the system, in a file
    # that then travels by email. The list plus a signed URL is the safer shape.
    export["documents"] = documents.documents_for_investor(conn, investor_id)

    from app.db import cas_folios

    if cas_folios.tables_present(conn):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM cas_fund_snapshot WHERE investor_id=%s ORDER BY snap_date",
                (investor_id,),
            )
            columns = [c.name for c in cur.description]
            export["cas_fund_statements"] = [
                dict(zip(columns, r, strict=True)) for r in cur.fetchall()
            ]

    # RESEARCH, when this database has the schema for it. Added in place rather than behind a
    # second endpoint: portability is one right, and an investor should not have to know that
    # their notes live in different tables from their trades to get a complete copy.
    #
    # Skipped silently ONLY when the workspace schema is absent, which is every deployment
    # today. That is not an omission of data -- there is no data -- and the alternative, raising
    # on a database that has never had research, would break the existing export.
    research_rows = 0
    if _workspace_schema_present(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT set_config(%s, %s, true)", (TENANT_SETTING, str(investor_id)))
        research = workspace_privacy.export_workspace(conn, investor_id)
        export.update(research)
        research_rows = sum(
            len(v)
            for k, v in research.items()
            if isinstance(v, list) and k != "research_export_exclusions"
        )

    audit.record(
        conn,
        audit.ACTION_EXPORT,
        "investor",
        investor_id,
        actor_uid,
        {
            "documents": len(export["documents"]),
            "include_identifiers": include_identifiers,
            "transactions": len(export["transactions"]),
            "fno_trades": len(export["fno_trades"]),
            "mf_transaction_details": len(export["mf_transaction_details"]),
            "accounts": len(export["accounts"]),
            # Counts, never content. An audit row recording what an export CONTAINED would be a
            # second copy of it in the one table erasure deliberately does not delete.
            "research_rows": research_rows,
            "research_exclusions": len(export.get("research_export_exclusions", [])),
        },
    )
    return export


def _workspace_schema_present(conn: Any) -> bool:
    """Does this database have the workspace tables at all?

    One cheap catalogue read, not a migration-state check. ``app/db/migrate.py`` owns whether
    the bundle is complete and deployable; this only answers "is there anywhere for research
    rows to be", so that export and erasure work unchanged on the databases that have no
    research schema -- which is all of them today.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'note')"
        )
        return bool(cur.fetchone()[0])


def erase_investor(
    conn: Any, investor_id: str, actor_uid: str | None = None, store: Any = None
) -> dict[str, int]:
    """Delete everything about one investor. Returns what was removed.

    Refuses while a legal hold is open. Otherwise sets ``app.erasure_authorised`` LOCAL --
    the one thing that lets the append-only trigger permit a delete -- and cascades from
    ``investor``.

    THE AUDIT TRAIL SURVIVES. The ``audit_log`` rows for this investor are deliberately
    not deleted: erasing the evidence that you erased would defeat both DPDP's access-log
    duty and SEBI's five-year record. Those rows reference an investor id that no longer
    resolves, which is the point -- they record that something happened without retaining
    what it was about.

    Raises:
        LegalHoldError: an open hold covers this investor.
    """
    holds = open_holds(conn, investor_id)
    if holds:
        raise LegalHoldError(investor_id, holds)

    # Documents first, and before any row is touched. A PDF holding the PAN and the full
    # holdings that outlives the record of it is not an erasure - it is a leak with no
    # index. Passing no store is allowed only where no documents exist, and is asserted.
    docs_removed = 0
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM document WHERE investor_id = %s", (investor_id,))
        doc_count = cur.fetchone()[0]
        cur.execute("SELECT code, family_group_id FROM investor WHERE id = %s", (investor_id,))
        row = cur.fetchone()
        investor_code = row[0] if row else ""
        family_group_id = row[1] if row else None

    if doc_count and store is None:
        raise ValueError(
            f"investor {investor_id} has {doc_count} stored documents but no store was "
            "given. Refusing to erase database rows while leaving the files behind."
        )

    workspace_present = _workspace_schema_present(conn)
    workspace_counts: dict[str, int] = {}
    if workspace_present:
        with conn.cursor() as cur:
            cur.execute("SELECT set_config(%s, %s, true)", (TENANT_SETTING, str(investor_id)))
        workspace_counts = workspace_privacy.workspace_row_counts(conn, investor_id)

    if store is not None:
        # BYTES ONLY. ``delete_documents_for_investor`` used to delete the storage objects AND
        # the `document` rows, then let `DELETE FROM investor` cascade the rest. That order
        # BREAKS as soon as research exists: `research_source.document_id` references
        # `document (investor_id, id)` with no ON DELETE action, so deleting document rows while
        # a research source cites one fails with a foreign-key violation -- verified, not
        # theorised.
        #
        # The fix is to separate artifact deletion from row deletion. Objects go now; the
        # `document` rows go in the SAME cascading statement as `research_source` below.
        # PostgreSQL checks NO ACTION constraints at end of statement rather than per row, so
        # both disappear together and no intermediate orphan is ever visible.
        #
        # What is NOT done: nulling `research_source.document_id`, or relabelling a document's
        # parse state to make it deletable. Either would rewrite an immutable research record to
        # make a deletion convenient.
        docs_removed = documents.delete_artifacts_for_investor(
            conn, store, investor_id, investor_code, actor_uid
        )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM transaction t JOIN account a ON a.id = t.account_id "
            "WHERE a.investor_id = %s",
            (investor_id,),
        )
        txn_count = cur.fetchone()[0]
        # The separate F&O ledger (migration 064) cascades from account the same way and is
        # counted the same way, so the audit names every ledger row erased.
        cur.execute(
            "SELECT count(*) FROM fno_trade f JOIN account a ON a.id = f.account_id "
            "WHERE a.investor_id = %s",
            (investor_id,),
        )
        fno_count = cur.fetchone()[0]

        # RE-CHECK THE HOLD, inside the same transaction as the delete. The first check above
        # happened before object deletion; a hold placed in between must not be missed, and
        # `place_legal_hold` and this statement contend for the same tenant advisory lock below,
        # so the two orderings are both valid and both safe.
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(investor_id),))
        cur.execute(
            "SELECT reason FROM legal_hold WHERE investor_id = %s AND released_at IS NULL",
            (investor_id,),
        )
        late = [r[0] for r in cur.fetchall()]
        if late:
            raise LegalHoldError(investor_id, late)

        # Announce the erasure to the append-only trigger. LOCAL, so it expires with this
        # transaction and cannot leak authorisation into any later statement.
        cur.execute(f"SET LOCAL {ERASURE_FLAG} = 'on'")
        if workspace_present:
            # Migration 044's ws_append_only also requires the row's investor_id to equal
            # ws_tenant(), so the scope has to be set for the cascade to be permitted.
            cur.execute("SELECT set_config(%s, %s, true)", (TENANT_SETTING, str(investor_id)))
        # ONE cascading statement removes accounts, transactions, documents and every workspace
        # row, in an order PostgreSQL works out and the FK checks confirm at statement end.
        cur.execute("DELETE FROM investor WHERE id = %s", (investor_id,))
        removed = cur.rowcount
    if removed and family_group_id is not None:
        # A family left with no members is the last trace of this person's family name.
        from app.db.families import delete_if_empty

        delete_if_empty(conn, family_group_id)

    # DID IT ACTUALLY DELETE ANYTHING? A DELETE that matched no rows succeeds, and under RLS
    # with no tenant selected every workspace table looks empty -- so "the statement ran" is not
    # "the data is gone". Counted before and verified after, because reporting a successful
    # erasure that removed nothing is the one failure a data principal cannot detect.
    remaining: dict[str, int] = {}
    if workspace_present:
        with conn.cursor() as cur:
            cur.execute("SELECT set_config(%s, %s, true)", (TENANT_SETTING, str(investor_id)))
        remaining = {
            table: count
            for table, count in workspace_privacy.workspace_row_counts(conn, investor_id).items()
            if count
        }
    if removed == 0:
        raise RuntimeError(
            f"erasure of investor {investor_id} removed no investor row. Refusing to report a "
            "successful erasure: check the investor exists and that this connection holds the "
            "eraser capability with a matching tenant scope."
        )
    if remaining:
        raise RuntimeError(
            f"erasure of investor {investor_id} left workspace rows behind: {remaining}. The "
            "transaction is being rolled back; storage objects already deleted are recorded as "
            "pending in the audit log and the operation is safe to retry."
        )

    # Recorded AFTER the delete and outside the flag's usefulness, so the trail is the
    # last thing written and cannot itself be cascaded away. Audit and deletion share this
    # transaction: an audit failure rolls the deletion back rather than leaving an unrecorded
    # erasure, which is app/db/audit.py's rule and is unchanged here.
    audit.record(
        conn,
        audit.ACTION_ERASURE,
        "investor",
        investor_id,
        actor_uid,
        {
            "transactions_deleted": txn_count,
            "fno_trades_deleted": fno_count,
            "investors_deleted": removed,
            "documents_deleted": docs_removed,
            # Counts per table, never content. Someone auditing a disputed erasure needs to know
            # twelve notes were removed, not what they said.
            "workspace_rows_deleted": sum(workspace_counts.values()),
            "workspace_tables_affected": sorted(t for t, n in workspace_counts.items() if n),
        },
    )
    return {
        "investors": removed,
        "transactions": txn_count,
        "fno_trades": fno_count,
        "documents": docs_removed,
        "workspace_rows": sum(workspace_counts.values()),
    }
