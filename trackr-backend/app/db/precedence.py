"""Source precedence -- which document wins when two describe the same trade.

THE PROBLEM THIS EXISTS FOR. A tradebook export and a contract note describe the SAME
trades. `dedupe_key` does not catch that, for two independent reasons:

  1. `source` is part of the key (docs/DATA_MODEL.md specifies it), so TRADEBOOK and
     CONTRACT_NOTE rows differ by construction;
  2. prices differ in precision -- the tradebook exports 43.119999 where the note prints
     43.12 -- so the keys would differ even without `source`.

Measured on this project's own files: 146 of 259 tradebook trades are also described by a
contract note. Importing both would insert every one of them twice.

THIS IS AN INTERIM MEASURE. The real fix is a dedupe_key that collides across sources --
see the TODO in docs/DATA_MODEL.md. Arbitrating in the ingestion layer means every future
adapter has to know about this rule, which is exactly the kind of knowledge that belongs in
a key rather than in a policy.

THE RULE. A contract note beats a tradebook, because the note carries CHARGES and the
tradebook does not. Preferring the tradebook would import the same trade with a cost basis
understated by its brokerage, STT, GST and stamp duty -- 8,278.12 across the 40 real notes
here.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, NamedTuple

from app.engine.types import Source, Transaction

#: Lower wins. A source absent from this map is ranked last: an unknown document should
#: never silently outrank one whose contents are understood.
SOURCE_PRECEDENCE: dict[str, int] = {
    Source.CONTRACT_NOTE.value: 1,  # carries charges
    Source.MANUAL.value: 2,  # a human typed it deliberately
    Source.TRADEBOOK.value: 3,  # no charges
    Source.CSV.value: 4,
    Source.EMAIL.value: 5,
}

UNRANKED = 99


def rank(source: str | None) -> int:
    return SOURCE_PRECEDENCE.get(str(source) if source else "", UNRANKED)


class TradeIdentity(NamedTuple):
    """What makes two rows the SAME TRADE, regardless of which document described it.

    PRICE IS DELIBERATELY ABSENT. The tradebook exports 43.119999 where the contract note
    prints 43.12 -- the same fill, rendered by two systems with different precision. Any
    identity including price would treat them as different trades and defeat the whole
    rule.

    `source` is absent for the same reason, and that is the point: this identity is what
    `dedupe_key` would be if it were source-agnostic.
    """

    account_id: str
    source_ref: str
    isin: str
    txn_date: Any
    txn_type: str
    quantity: Decimal


def trade_identity(txn: Transaction) -> TradeIdentity | None:
    """The cross-source identity of a trade, or None if it cannot be established.

    Returns None without a ``source_ref``: with no broker trade number there is nothing
    stable to match on, and guessing from price and quantity would collapse the genuinely
    distinct same-price fills that Session 6 proved exist (16 of them).
    """
    if not txn.source_ref or not txn.account_id:
        return None
    return TradeIdentity(
        account_id=str(txn.account_id),
        source_ref=str(txn.source_ref),
        isin=txn.isin,
        txn_date=txn.txn_date,
        txn_type=str(txn.txn_type),
        quantity=txn.quantity,
    )


class Decision(NamedTuple):
    """What to do with one incoming row, and why -- always why."""

    action: str  # INSERT | EXCLUDE | SUPERSEDE
    reason: str
    superseded_id: str | None = None


ACTION_INSERT = "INSERT"
ACTION_EXCLUDE = "EXCLUDE"
ACTION_SUPERSEDE = "SUPERSEDE"


def decide(incoming: Transaction, existing_source: str | None, existing_id: str | None) -> Decision:
    """Arbitrate one incoming row against whatever already describes that trade.

    Never silent: every outcome carries a sentence naming the trade and the reason.
    """
    if existing_source is None:
        return Decision(ACTION_INSERT, "no other source describes this trade")

    if str(existing_source) == str(incoming.source):
        # SAME SOURCE. Not this rule's business: re-importing a file is already handled by
        # the UNIQUE INDEX on dedupe_key, which catches it exactly and counts it honestly
        # as a duplicate. Precedence arbitrates only BETWEEN different documents.
        return Decision(ACTION_INSERT, "same source; dedupe_key handles this")

    incoming_rank = rank(incoming.source)
    existing_rank = rank(existing_source)

    if existing_rank < incoming_rank:
        return Decision(
            ACTION_EXCLUDE,
            f"trade {incoming.source_ref} ({incoming.isin} on {incoming.txn_date}) is "
            f"already in the ledger from {existing_source}, which outranks "
            f"{incoming.source}. Skipped to avoid double-counting.",
        )
    if existing_rank > incoming_rank:
        return Decision(
            ACTION_SUPERSEDE,
            f"trade {incoming.source_ref} ({incoming.isin} on {incoming.txn_date}) was "
            f"recorded from {existing_source}; {incoming.source} outranks it and carries "
            f"charges, so the earlier row is reversed and replaced.",
            superseded_id=existing_id,
        )
    return Decision(
        ACTION_EXCLUDE,
        f"trade {incoming.source_ref} is already in the ledger from a different but "
        f"equally ranked source ({existing_source}). Kept the first; neither outranks "
        f"the other, so preferring the newcomer would be arbitrary.",
    )


def existing_identities(conn: Any, account_id: str) -> dict[TradeIdentity, tuple[str, str]]:
    """Every trade already in the ledger for this account, by cross-source identity.

    Maps identity -> (source, transaction id), so an incoming row can be arbitrated
    against what is actually there rather than against what a batch claims.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, source_ref, isin, txn_date, txn_type, quantity, source
            FROM transaction t
            WHERE account_id = %s AND source_ref IS NOT NULL
              AND t.reverses_id IS NULL
              AND NOT EXISTS (SELECT 1 FROM transaction r WHERE r.reverses_id = t.id)
            """,
            (account_id,),
        )
        found: dict[TradeIdentity, tuple[str, str]] = {}
        for row in cur.fetchall():
            identity = TradeIdentity(
                account_id=str(account_id),
                source_ref=str(row[1]),
                isin=row[2],
                txn_date=row[3],
                txn_type=str(row[4]),
                quantity=row[5],
            )
            found[identity] = (str(row[6]), str(row[0]))
        return found
