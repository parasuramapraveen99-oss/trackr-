"""Corrections to the append-only F&O ledger, and the trade list a user picks one from.

docs/ENGINE_SPEC.md section 15 rule 15, docs/ARCHITECTURE_INVARIANTS.md rule 21(b),
migration 066. A wrong ``fno_trade`` row is never edited or deleted: ``reverse_fno_trade``
appends a ``kind = REVERSAL`` row whose ``reverses_id`` names it, and the engine drops both
from matching. Raw SQL, no ORM. The mutation and its audit row share one transaction; the
caller commits.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from app.db import audit

#: audit_log.action for a reversal written here.
ACTION_FNO_TRADE_REVERSED = "FNO_TRADE_REVERSED"

_TRADE_COLUMNS = (
    "t.id, t.account_id, t.contract_id, t.trade_date, t.side, t.quantity, t.price, "
    "t.charges, t.stt, t.kind, t.reverses_id, t.source, t.seq"
)
_TRADE_KEYS = (
    "id", "account_id", "contract_id", "trade_date", "side", "quantity", "price",
    "charges", "stt", "kind", "reverses_id", "source", "seq",
)


class FnoTradeNotFoundError(LookupError):
    """No such F&O trade in this investor's accounts. Rendered 404."""

    def __init__(self, trade_id: str) -> None:
        self.trade_id = trade_id
        super().__init__(f"no F&O trade {trade_id} for this investor")


class FnoTradeNotReversibleError(ValueError):
    """The target is itself a reversal. Rendered 409."""

    def __init__(self, trade_id: str, why: str) -> None:
        self.trade_id = trade_id
        super().__init__(f"F&O trade {trade_id} cannot be reversed: {why}")


def reversal_dedupe_key(target_id: str) -> str:
    """Deterministic from the target, so a second request is a no-op (CLAUDE.md rule 3)."""
    return f"fno-reversal|{target_id}"


def _row(record: tuple) -> dict[str, Any]:
    out = dict(zip(_TRADE_KEYS, record, strict=True))
    for key in ("id", "account_id", "contract_id", "reverses_id"):
        if out[key] is not None:
            out[key] = str(out[key])
    out["source"] = str(out["source"])
    return out


def _owned_trade(cur: Any, investor_id: str, trade_id: str) -> dict[str, Any] | None:
    cur.execute(
        f"""
        SELECT {_TRADE_COLUMNS} FROM fno_trade t JOIN account a ON a.id = t.account_id
        WHERE a.investor_id = %s AND t.id = %s
        """,
        (investor_id, trade_id),
    )
    record = cur.fetchone()
    return _row(record) if record else None


def reverse_fno_trade(
    conn: Any, investor_id: str, trade_id: str, reason: str, actor_uid: str | None = None
) -> tuple[dict[str, Any], bool]:
    """Void one of the investor's F&O trades with a mirror REVERSAL row. Returns
    ``(reversal_row, created)``.

    The mirror: same account and contract, the opposite side, the same quantity and price,
    charges and STT 0 (rule 15 voids both rows; the original's charges leave expenses),
    ``trade_date = max(today UTC, target date)``, source ``CORRECTION``, ``raw_payload``
    holding the reason. Idempotent: a trade already reversed returns its existing
    reversal with ``created = False`` and writes nothing.

    Raises:
        FnoTradeNotFoundError: not a trade in this investor's accounts (also for an id
            that is not a uuid).
        FnoTradeNotReversibleError: the target is itself a REVERSAL.
    """
    try:
        uuid.UUID(trade_id)
    except ValueError:
        raise FnoTradeNotFoundError(trade_id) from None
    with conn.cursor() as cur:
        target = _owned_trade(cur, investor_id, trade_id)
        if target is None:
            raise FnoTradeNotFoundError(trade_id)
        if target["kind"] == "REVERSAL":
            raise FnoTradeNotReversibleError(trade_id, "it is itself a reversal")

        cur.execute(
            f"SELECT {_TRADE_COLUMNS} FROM fno_trade t WHERE t.reverses_id = %s", (trade_id,)
        )
        existing = cur.fetchone()
        if existing:
            return _row(existing), False

        today = datetime.now(UTC).date()
        cur.execute(
            f"""
            INSERT INTO fno_trade AS t (account_id, contract_id, trade_date, side, quantity,
                                        price, charges, stt, kind, reverses_id, source,
                                        source_ref, dedupe_key, raw_payload)
            VALUES (%s, %s, %s, %s, %s, %s, 0, 0, 'REVERSAL', %s, 'CORRECTION', %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING {_TRADE_COLUMNS}
            """,
            (
                target["account_id"],
                target["contract_id"],
                max(today, target["trade_date"]),
                "SELL" if target["side"] == "BUY" else "BUY",
                target["quantity"],
                target["price"],
                trade_id,
                trade_id,
                reversal_dedupe_key(trade_id),
                json.dumps({"reason": reason, "reverses_id": trade_id}),
            ),
        )
        inserted = cur.fetchone()
        if inserted is None:
            # A concurrent request won the unique index; return its row.
            cur.execute(
                f"SELECT {_TRADE_COLUMNS} FROM fno_trade t WHERE t.reverses_id = %s",
                (trade_id,),
            )
            return _row(cur.fetchone()), False

    reversal = _row(inserted)
    audit.record(
        conn,
        ACTION_FNO_TRADE_REVERSED,
        "fno_trade",
        reversal["id"],
        actor_uid,
        {"reverses_id": trade_id, "reason": reason},
    )
    return reversal, True


def list_fno_trades(conn: Any, investor_id: str) -> list[dict[str, Any]]:
    """Every F&O trade of the investor's accounts, ``(trade_date, seq)`` order, with the
    contract identity and ``reversed`` (a REVERSAL row names it)."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_TRADE_COLUMNS}, c.exchange, c.instrument_type, c.underlying, c.expiry,
                   c.strike, c.option_type,
                   EXISTS (SELECT 1 FROM fno_trade r WHERE r.reverses_id = t.id)
            FROM fno_trade t
            JOIN account a ON a.id = t.account_id
            JOIN fno_contract c ON c.id = t.contract_id
            WHERE a.investor_id = %s
            ORDER BY t.trade_date, t.seq
            """,
            (investor_id,),
        )
        rows = []
        n = len(_TRADE_KEYS)
        for record in cur.fetchall():
            row = _row(record[:n])
            row.update(
                zip(
                    ("exchange", "instrument_type", "underlying", "expiry", "strike",
                     "option_type", "reversed"),
                    record[n:],
                    strict=True,
                )
            )
            rows.append(row)
    return rows
