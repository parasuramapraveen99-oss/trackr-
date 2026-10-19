"""The repository -- ``save_transactions`` and ``load_transactions``. Nothing else.

Deliberately small. There is no update path and no delete path, because the ledger is
append-only; and no holdings persistence, because holdings are computed on read. If this
module ever grows an ``update_transaction`` or a ``save_holdings``, the architecture has
been broken.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from typing import Any

from app.db import audit, crypto
from app.engine.dedupe import make_dedupe_key
from app.engine.types import ImportSummary, Source, Transaction, TxnType

#: Postgres SQLSTATE for a unique-index violation.
UNIQUE_VIOLATION = "23505"

_INSERT = """
INSERT INTO transaction (
    account_id, isin, txn_date, txn_type, quantity, price, charges, stt,
    dividend_amount, tds_amount, cost_transfer, source, source_ref, dedupe_key, raw_payload
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (dedupe_key) DO NOTHING
RETURNING id
"""


class DuplicateInvestorCodeError(ValueError):
    """``investor.code`` is UNIQUE (docs/DATA_MODEL.md) -- this one is already taken."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"investor code {code!r} is already in use")


class InvestorNotFoundError(LookupError):
    """No investor row with this id -- checked explicitly (see create_account) so the
    router can answer 404 instead of a raw foreign-key-violation 500."""

    def __init__(self, investor_id: str) -> None:
        self.investor_id = investor_id
        super().__init__(f"no investor with id {investor_id!r}")


def save_transactions(
    conn: Any,
    transactions: list[Transaction],
    actor_uid: str | None = None,
) -> ImportSummary:
    """Append transactions, skipping duplicates.

    A duplicate is NOT an error. Re-importing a file must be a no-op that still produces
    an honest summary, because email forwarding will deliver the same contract note twice
    and a client uploading a full history sends overlapping tradebook exports by
    necessity -- Console caps each download at 365 days.

    ``ON CONFLICT (dedupe_key) DO NOTHING`` puts the enforcement in the UNIQUE INDEX
    rather than in a prior SELECT. A check-then-insert would race: two concurrent imports
    of the same file could both find nothing and both insert.

    ``transaction.isin`` is a foreign key into ``security``, so every ISIN this batch
    references must exist there first -- a bare, symbol-less upsert here is the backstop
    for any caller that has not already done the richer version itself (``app.db.imports``
    upserts with a real ``symbol`` from the parsed row before calling this; manual entry
    via ``routers/transactions.py`` has no symbol to offer at all). ``ON CONFLICT DO
    NOTHING`` means a caller that already inserted a richer row keeps it -- this can only
    add a bare placeholder, never overwrite one.

    The audit row shares this transaction. If it fails, the inserts roll back with it.
    """
    inserted = 0
    skipped = 0

    with conn.cursor() as cur:
        isins = sorted({txn.isin for txn in transactions})
        for isin in isins:
            cur.execute(
                "INSERT INTO security (isin) VALUES (%s) ON CONFLICT (isin) DO NOTHING",
                (isin,),
            )
        for txn in transactions:
            key = txn.dedupe_key or make_dedupe_key(txn)
            cur.execute(
                _INSERT,
                (
                    txn.account_id,
                    txn.isin,
                    txn.txn_date,
                    str(txn.txn_type),
                    txn.quantity,
                    txn.price,
                    txn.charges,
                    txn.stt,
                    txn.dividend_amount,
                    txn.tds_amount,
                    txn.cost_transfer,
                    str(txn.source) if txn.source else Source.MANUAL.value,
                    txn.source_ref,
                    key,
                    json.dumps(txn.raw_payload) if txn.raw_payload else None,
                ),
            )
            if cur.fetchone() is None:
                skipped += 1
            else:
                inserted += 1

    audit.record(
        conn,
        action=audit.ACTION_IMPORT,
        entity_type="transaction",
        actor_uid=actor_uid,
        diff={"inserted": inserted, "skipped_duplicates": skipped, "offered": len(transactions)},
    )

    return ImportSummary(inserted=inserted, skipped_duplicates=skipped, rejected=[])


def load_transactions(
    conn: Any,
    investor_id: str | None = None,
    account_id: str | None = None,
    isin: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    advised_only: bool = False,
    apply_aliases: bool = True,
) -> list[Transaction]:
    """Read the ledger back as engine objects.

    Returns ``Transaction`` objects, not rows: the engine must not know that a database
    exists, and the boundary is enforced by what crosses it. ``NUMERIC`` arrives as
    ``Decimal`` from psycopg, so no conversion is needed and none is done -- a float
    round-trip here would silently undo the entire precision discipline.

    ORDERED BY ``(txn_date, seq)``, and the second half of that is load-bearing.
    ``engine.ledger.ordered`` sorts on ``txn_date`` alone and depends on a stable sort to
    keep same-date rows in the order they were inserted. ``created_at`` cannot supply that
    -- ``now()`` is the transaction timestamp, so every row of a bulk import shares it --
    and the previous fallback to ``id`` was a random uuid. See migration 007: replaying the
    golden fixture out of the database raised ``NegativeHoldingError`` on a SELL that had
    overtaken its own BUY.

    ``advised_only`` restricts to accounts flagged ``account.is_advised`` (docs/
    DATA_MODEL.md) -- the ``account`` join this already runs, so no new join or query path
    is added for it. Exists so a SEBI-registered adviser can report on the mandated
    account only, per contracts/openapi.yaml's ``advised_only`` parameter.

    ISIN ALIASES ARE APPLIED HERE, and only here. A security whose identifier was retired
    and replaced (``security_alias``, migration 027) has its trades reported by the broker
    under whichever ISIN was current at the time; a VERIFIED alias maps the retired one
    onto its replacement as rows cross into the engine, so both halves replay as the one
    security they always were.

    This is the read boundary ON PURPOSE, for two reasons. The stored rows keep the ISIN
    the broker actually reported -- the ledger is append-only and a wrong alias is revoked,
    never un-migrated. And doing it here rather than as a synthetic TRANSFER_OUT/
    TRANSFER_IN pair is what preserves FIFO acquisition dates: those would open a new lot
    dated at the identifier change (``app.engine.lots``) and silently turn a long-term
    holding short-term. An ISIN change is not a taxable event.

    So replaying the ledger now also depends on ``security_alias``, the same standing
    ``price`` and ``corporate_action`` already have. ``apply_aliases=False`` returns the
    raw, un-normalised rows -- used by ``app.db.security_alias.reconcile_after_alias`` to
    compare a ledger before and after an alias, and by nothing else.
    """
    # A reversal (migration 076) voids the row it names: replay drops BOTH, so the engine
    # never sees a corrected row or its correction. The stored rows stay -- append-only.
    clauses: list[str] = [
        "t.reverses_id IS NULL",
        "NOT EXISTS (SELECT 1 FROM transaction r WHERE r.reverses_id = t.id)",
    ]
    params: list[Any] = []

    if investor_id:
        clauses.append("a.investor_id = %s")
        params.append(investor_id)
    if account_id:
        clauses.append("t.account_id = %s")
        params.append(account_id)
    if date_to:
        # A revision may move an identified effect into this historical window.
        # Load its immutable anchor and filter on the replayed effective date below.
        clauses.append("(t.txn_date <= %s OR (t.source='CORP_ACTION' "
                       "AND t.raw_payload ? 'auto_corporate_action_id'))")
        params.append(date_to)
    if advised_only:
        clauses.append("a.is_advised = true")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT t.id, t.account_id, t.isin, t.txn_date, t.txn_type, t.quantity,
                   t.price, t.charges, t.stt, t.dividend_amount, t.tds_amount,
                   t.cost_transfer,
                   t.source, t.source_ref, t.dedupe_key, t.raw_payload, t.created_at
            FROM transaction t
            JOIN account a ON a.id = t.account_id
            {where}
            ORDER BY t.txn_date, t.seq
            """,
            params,
        )
        rows = cur.fetchall()

    # Resolved once per read, not per row: verified_map returns each alias already
    # collapsed to its terminal ISIN, so this is a single dict lookup below and can never
    # hand the engine a half-resolved identity.
    aliases = _alias_map(conn, date_to) if apply_aliases else {}
    transactions = [
            Transaction(
                id=str(row[0]),
                account_id=str(row[1]),
                isin=aliases.get(row[2], row[2]),
                txn_date=row[3],
                txn_type=TxnType(row[4]),
                quantity=row[5],
                price=row[6],
                charges=row[7],
                stt=row[8],
                dividend_amount=row[9],
                tds_amount=row[10],
                cost_transfer=row[11],
                source=Source(row[12]),
                source_ref=row[13],
                dedupe_key=row[14],
                raw_payload=row[15],
                created_at=row[16],
            )
            for row in rows
        ]
    if apply_aliases and any(
        (t.raw_payload or {}).get("auto_corporate_action_id") for t in transactions
    ):
        from app.db.corporate_actions import list_corporate_actions
        from app.engine.action_replay import replay_derived_actions

        actions = [replace(a, isin=aliases.get(a.isin, a.isin),
                           resulting_isin=aliases.get(a.resulting_isin, a.resulting_isin))
                   for a in list_corporate_actions(conn)]
        transactions = replay_derived_actions(transactions, actions, as_of=date_to)
    selected_isin = aliases.get(isin, isin)
    return [t for t in transactions if (not isin or t.isin == selected_isin)
            and (date_from is None or t.txn_date >= date_from)
            and (date_to is None or t.txn_date <= date_to)]


def _alias_map(conn: Any, as_of: date | None = None) -> dict[str, str]:
    """VERIFIED ISIN aliases.

    A database that has not run migration 027 raises ``UndefinedTable``. That is a
    deployment fault and is surfaced as one: reading it as "no aliases" valued a renamed
    holding as two unrelated securities with no error anywhere.
    """
    from app.db.security_alias import verified_map

    return verified_map(conn, as_of)


def create_investor(
    conn: Any,
    code: str,
    display_name: str,
    family_group_id: str | None = None,
    pan: str | None = None,
    onboarding_date: date | None = None,
    owner_user_id: str | None = None,
    actor_uid: str | None = None,
    relationship: str = "client",
) -> dict[str, Any]:
    """Onboard a new investor. The only way an investor row is created outside
    ``app.db.load_golden``'s fixture seed -- see docs/FULL_VISION_BACKEND_ROADMAP.md A5.

    ``pan``, when given, never lands on the ``investor`` row: docs/DATA_MODEL.md is
    explicit that PAN lives only as a peppered lookup hash plus Fernet ciphertext in
    ``investor_secret``, via the same ``app.db.crypto`` module ``app.ingest.webhook``
    already routes the contract-note-password lookup through. Validated and normalised
    before the investor insert, so a bad PAN format fails the whole request without
    leaving a half-onboarded investor behind.

    ``onboarding_date`` feeds ``compute_period_xirr``'s ``start_date`` (see
    ``app.routers.analytical._window_xirr``) -- optional, nullable, never defaulted.

    Raises:
        app.db.crypto.InvalidPANError: ``pan`` does not match the mandated PAN format.
        DuplicateInvestorCodeError: ``code`` is already in use.
    """
    import psycopg

    normalised_pan = crypto.normalise_pan(pan) if pan else None

    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                INSERT INTO investor (code, display_name, family_group_id,
                                      onboarding_date, owner_user_id, relationship)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id, code, display_name, family_group_id, onboarding_date, created_at,
                          relationship
                """,
                (code, display_name, family_group_id, onboarding_date, owner_user_id, relationship),
            )
        except psycopg.errors.UniqueViolation as exc:
            raise DuplicateInvestorCodeError(code) from exc
        row = cur.fetchone()

    investor = {
        "id": str(row[0]),
        "code": row[1],
        "display_name": row[2],
        "family_group_id": str(row[3]) if row[3] else None,
        "onboarding_date": row[4],
        "created_at": row[5],
        "relationship": row[6],
    }

    audit.record(
        conn,
        action=audit.ACTION_INSERT,
        entity_type="investor",
        entity_id=investor["id"],
        actor_uid=actor_uid,
        diff={"code": code},
    )

    if normalised_pan:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO investor_secret (investor_id, pan_hash, pan_enc) VALUES (%s, %s, %s)",
                (investor["id"], crypto.hash_pan(normalised_pan), crypto.encrypt(normalised_pan)),
            )
        audit.record(
            conn,
            action=audit.ACTION_SECRET_WRITTEN,
            entity_type="investor",
            entity_id=investor["id"],
            actor_uid=actor_uid,
        )

    return investor


def set_investor_pan(conn: Any, investor_id: str, pan: str, actor_uid: str | None = None) -> None:
    """Set or replace an investor's PAN after onboarding.

    ``create_investor`` only ever writes a PAN at creation time -- most investors are
    onboarded before this system needs their PAN at all (it exists to auto-unlock a
    password-protected Zerodha/Groww/Upstox contract-note PDF), so there was no path
    to add one later until this. Same encryption (``app.db.crypto``), same
    ``investor_secret`` row every other PAN-consuming path already reads
    (``app.ingest.webhook.raw_pan``).

    Upserts rather than requiring the row to already exist: an investor onboarded
    without a PAN has no ``investor_secret`` row at all (``create_investor`` only
    inserts one ``if normalised_pan``), so ``ON CONFLICT`` alone would silently do
    nothing for the common case this function exists to handle.

    Raises:
        app.db.crypto.InvalidPANError: ``pan`` does not match the mandated PAN format.
            Raised before any write, same as ``create_investor``.
    """
    normalised_pan = crypto.normalise_pan(pan)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO investor_secret (investor_id, pan_hash, pan_enc)
            VALUES (%s, %s, %s)
            ON CONFLICT (investor_id) DO UPDATE
            SET pan_hash = EXCLUDED.pan_hash, pan_enc = EXCLUDED.pan_enc, updated_at = now()
            """,
            (investor_id, crypto.hash_pan(normalised_pan), crypto.encrypt(normalised_pan)),
        )
    audit.record(
        conn,
        action=audit.ACTION_SECRET_WRITTEN,
        entity_type="investor",
        entity_id=investor_id,
        actor_uid=actor_uid,
    )


def list_investor_ids(conn: Any) -> list[str]:
    """Every investor id -- what ``POST /ops/snapshots/run`` iterates over.

    Mirrors ``app.db.prices.held_isins``'s unscoped shape: that endpoint, like this one,
    runs across every investor in one call rather than one at a time.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM investor ORDER BY id")
        return [str(row[0]) for row in cur.fetchall()]


def list_investors_for_user(conn: Any, user_id: str) -> list[dict[str, Any]]:
    """Every investor this login owns, oldest first.

    The read behind a portfolio switcher, and the reason one can exist at all. Until this
    existed the API could create a second investor (POST /investors) and report exactly
    one (``first_investor_id_for_user``, which /auth/login echoes), so a login that owned
    two had no way to name the second -- creating it switched to it, and the next login
    switched silently back to the oldest. The data was never lost; there was no endpoint
    that would admit it was there.

    Ordered by ``created_at, id`` so the first row is the same investor
    ``first_investor_id_for_user`` returns -- the switcher's default and the session's
    default must agree, or logging out would appear to move you.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, code, display_name, family_group_id, onboarding_date, created_at,
                   relationship
            FROM investor
            WHERE owner_user_id = %s
            ORDER BY created_at, id
            """,
            (user_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "id": str(row[0]),
            "code": row[1],
            "display_name": row[2],
            "family_group_id": row[3],
            "onboarding_date": row[4],
            "created_at": row[5],
            "relationship": row[6],
        }
        for row in rows
    ]


#: A code is read aloud and copied by hand, so no look-alikes: no 0/O, no 1/I/L.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def new_investor_code() -> str:
    """A random investor code such as ``TP-7K3QX9``.

    Never derived from a name or an email. ``investor.code`` ends up in forwarding
    addresses, storage paths and logs ("use this in files and logs, NEVER the name or
    PAN"), so it must carry nothing about the person. 31^6 (~887 million) codes.
    """
    import secrets

    return "TP-" + "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))


def create_investor_with_generated_code(
    conn: Any,
    display_name: str,
    owner_user_id: str,
    family_group_id: str | None = None,
    pan: str | None = None,
    onboarding_date: date | None = None,
    relationship: str = "client",
) -> dict[str, Any]:
    """``create_investor`` with a code Trackr+ picks. Uniqueness comes from retrying
    against the UNIQUE index, never from a pre-SELECT (the same race discipline as
    app_user.email). Each attempt runs in its own savepoint, so a collision leaves the
    caller's transaction usable."""
    for _ in range(5):
        try:
            with conn.transaction():
                return create_investor(
                    conn,
                    code=new_investor_code(),
                    display_name=display_name,
                    family_group_id=family_group_id,
                    pan=pan,
                    onboarding_date=onboarding_date,
                    owner_user_id=owner_user_id,
                    actor_uid=owner_user_id,
                    relationship=relationship,
                )
        except DuplicateInvestorCodeError:
            continue
    raise DuplicateInvestorCodeError("TP-* (five random codes collided)")


def create_personal_investor(
    conn: Any, owner_user_id: str, display_name: str | None = None
) -> dict[str, Any]:
    """The investor a fresh registration lands in, with a generated code (no part of the
    email in it -- see ``new_investor_code``). Returns the created investor dict with
    ``id`` stringified.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT email FROM app_user WHERE id = %s", (owner_user_id,))
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"no such user: {owner_user_id}")
    return create_investor_with_generated_code(
        conn,
        display_name=display_name or row[0].split("@", 1)[0],
        owner_user_id=owner_user_id,
    )


def get_investor_owner(conn: Any, investor_id: str) -> str | None:
    """``investor.owner_user_id`` for this investor, stringified; ``None`` when the row
    exists but nobody owns it yet, and ``None`` when it does not exist at all --
    callers that must distinguish those two check existence separately."""
    with conn.cursor() as cur:
        cur.execute("SELECT owner_user_id FROM investor WHERE id = %s", (investor_id,))
        row = cur.fetchone()
    return str(row[0]) if row and row[0] else None


def investor_exists(conn: Any, investor_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM investor WHERE id = %s", (investor_id,))
        return cur.fetchone() is not None


def first_investor_id_for_user(conn: Any, user_id: str) -> str | None:
    """The login's oldest owned investor -- what /auth/login echoes back so a client
    stops carrying its own notion of "my investor" around."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM investor WHERE owner_user_id = %s ORDER BY created_at, id LIMIT 1",
            (user_id,),
        )
        row = cur.fetchone()
    return str(row[0]) if row else None


def get_investor_onboarding_date(conn: Any, investor_id: str) -> date | None:
    """The investor's adviser-onboarding date -- ``compute_period_xirr``'s
    ``start_date``, via ``app.routers.analytical._window_xirr``. ``None`` both when the
    column is unset (most investors today) and when ``investor_id`` doesn't exist --
    this endpoint has never enforced investor existence (``load_transactions`` just
    returns an empty ledger for an unknown id), and this feature doesn't newly start.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT onboarding_date FROM investor WHERE id = %s", (investor_id,))
        row = cur.fetchone()
    return row[0] if row else None


def get_investor_code(conn: Any, investor_id: str) -> str | None:
    """``investor.code`` -- what file names and forwarding addresses are built from."""
    with conn.cursor() as cur:
        cur.execute("SELECT code FROM investor WHERE id = %s", (investor_id,))
        row = cur.fetchone()
    return row[0] if row else None


def list_accounts(conn: Any, investor_id: str) -> list[dict[str, Any]]:
    """Every account under one investor -- what the Import screen's account selector
    offers instead of asking for a raw UUID nobody remembers. Ordered by broker then
    id: ``account`` has no created_at column to sort on, and this is at least stable."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, broker, dp_id, client_id, is_advised, label
            FROM account WHERE investor_id = %s ORDER BY broker, id
            """,
            (investor_id,),
        )
        return [
            {
                "id": str(row[0]),
                "broker": row[1],
                "dp_id": row[2],
                "client_id": row[3],
                "is_advised": row[4],
                "label": row[5],
            }
            for row in cur.fetchall()
        ]


def account_belongs_to(conn: Any, account_id: str, investor_id: str) -> bool:
    """True only when ``account_id`` is one of ``investor_id``'s own accounts. The
    ingest path checks this explicitly so a mistyped or foreign account id is a clean
    404 rather than a ForeignKeyViolation surfacing as a 500 mid-request."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM account WHERE id = %s AND investor_id = %s",
            (account_id, investor_id),
        )
        return cur.fetchone() is not None


def create_account(
    conn: Any,
    investor_id: str,
    broker: str,
    dp_id: str | None = None,
    client_id: str | None = None,
    is_advised: bool = True,
    label: str | None = None,
    actor_uid: str | None = None,
) -> dict[str, Any]:
    """Add a demat/broker account to an investor. One row per account (docs/
    DATA_MODEL.md) -- a client's CAS routinely reveals accounts nobody knew existed, so
    onboarding must support adding more than one per investor.

    The returned dict carries an ``investor_code`` key alongside the account columns --
    not part of ``account``, but the router needs it to compute this account's
    forwarding addresses (app.ingest.addresses.forwarding_addresses) without a second
    round trip, and the existence check below already has to read the investor row.

    Raises:
        InvestorNotFoundError: no investor with ``investor_id``. Checked with a SELECT
            rather than left to ``account.investor_id``'s foreign key, so the router
            can answer a clean 404 instead of a raw ForeignKeyViolation 500.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT code FROM investor WHERE id = %s", (investor_id,))
        investor_row = cur.fetchone()
        if investor_row is None:
            raise InvestorNotFoundError(investor_id)
        investor_code = investor_row[0]

        cur.execute(
            """
            INSERT INTO account (investor_id, broker, dp_id, client_id, is_advised, label)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id, investor_id, broker, dp_id, client_id, is_advised, label
            """,
            (investor_id, broker, dp_id, client_id, is_advised, label),
        )
        row = cur.fetchone()

    account = {
        "id": str(row[0]),
        "investor_id": str(row[1]),
        "broker": row[2],
        "dp_id": row[3],
        "client_id": row[4],
        "is_advised": row[5],
        "label": row[6],
        "investor_code": investor_code,
    }

    audit.record(
        conn,
        action=audit.ACTION_INSERT,
        entity_type="account",
        entity_id=account["id"],
        actor_uid=actor_uid,
        diff={"investor_id": investor_id, "broker": broker},
    )

    return account
