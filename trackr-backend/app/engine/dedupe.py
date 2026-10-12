"""Deduplication -- docs/DATA_MODEL.md, ``dedupe_key``.

Both functions here are pure, which is why they sit in the engine: the DATABASE is what
refuses a duplicate, but the KEY is a deterministic property of a transaction and must
be computable without touching one.
"""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal

from app.engine.fno_types import FnoContract
from app.engine.types import Transaction

#: Field separator for the hashed key. A character that cannot appear in any of the
#: components, so ("AB", "C") and ("A", "BC") cannot collide.
SEPARATOR = "|"


def _normalise(value: object) -> str:
    """Render one component so the same transaction always hashes identically.

    Decimals are normalised first: ``100`` and ``100.00`` are the same quantity, and a
    re-import that formats them differently must not look like a new row.
    """
    if value is None:
        return ""
    if isinstance(value, Decimal):
        # normalize() sends 100.00 -> 1E+2, so restore plain notation afterwards.
        return format(value.normalize(), "f")
    return str(value)


def make_dedupe_key(txn: Transaction) -> str:
    """Derive the stable identity of a transaction.

    ``sha256(account_id | source | source_ref | isin | txn_date | txn_type | quantity
    | price)``

    Stable across imports of the same underlying row, so the same trade parsed from a
    tradebook today and a contract note tomorrow collides rather than double-counting.

    Note what is ABSENT: ``charges``, ``dividend_amount``, ``raw_payload``, ``created_at``.
    Two parsers can legitimately disagree about how to apportion charges on a multi-line
    contract note; that disagreement must not manufacture a second copy of one trade.
    """
    components = [
        txn.account_id,
        txn.source,
        txn.source_ref,
        txn.isin,
        txn.txn_date,
        txn.txn_type,
        txn.quantity,
        txn.price,
    ]
    joined = SEPARATOR.join(_normalise(c) for c in components)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def make_fno_dedupe_key(
    *,
    account_id: str,
    source: object,
    source_ref: str | None,
    contract: FnoContract,
    trade_date: date,
    side: object,
    quantity: Decimal,
    price: Decimal,
) -> str:
    """The stable identity of an F&O trade (``fno_trade.dedupe_key``, rule 21(b)).

    ``sha256("FNO" | account_id | source | source_ref | exchange | underlying |
    instrument_type | expiry | strike | option_type | trade_date | side | quantity |
    price)``

    Built like ``make_dedupe_key``, with the contract's economic identity in place of the
    ISIN -- never its database id or exchange token, which are not the identity (rule
    21(a)). The leading ``FNO`` keeps the two key spaces disjoint by construction. Charges
    and ``kind`` are absent for the same reason charges are absent from the equity key.
    ``contract`` needs only the identity attributes, so the repository's contract row
    serves as well as ``app.engine.fno_types.FnoContract``.
    """
    components = [
        "FNO",
        account_id,
        source,
        source_ref,
        contract.exchange,
        contract.underlying,
        contract.instrument_type,
        contract.expiry,
        contract.strike,
        contract.option_type,
        trade_date,
        side,
        quantity,
        price,
    ]
    joined = SEPARATOR.join(_normalise(c) for c in components)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def dedupe(
    transactions: list[Transaction], existing_keys: set[str] | None = None
) -> tuple[list[Transaction], int]:
    """Split incoming rows into those to insert and a count of duplicates skipped.

    Returns:
        ``(to_insert, skipped_count)``.

    Duplicates are skipped and counted, never raised as errors. Re-importing a file must
    be a no-op that still produces an honest import summary.

    Deduplicates WITHIN the batch as well as against ``existing_keys``. Uploading a full
    history as five overlapping tradebook exports is the normal case, not the exception --
    Console caps each download at 365 days -- so the same trade arrives twice in one call.

    A UNIQUE INDEX on ``dedupe_key`` is the real enforcement. This keeps the common case
    cheap; it is not the guarantee, because application-level checks are the kind of thing
    someone later forgets to call.
    """
    seen = set(existing_keys or ())
    to_insert: list[Transaction] = []
    skipped = 0

    for txn in transactions:
        key = txn.dedupe_key or make_dedupe_key(txn)
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        to_insert.append(txn)

    return to_insert, skipped
