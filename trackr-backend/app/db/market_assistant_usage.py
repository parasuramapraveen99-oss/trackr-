"""Shared daily counters for Ask about markets (migration 078).

The ONLY database module the market-assistant router may import
(tests/test_market_assistant_boundary.py). It touches one table, which holds a day, a kind
and a count -- no user, no question, no topic. ``app.market_assistant`` itself stays
database-free: it sees this through ``limits.UsageStore``.

Fail closed: if the database cannot be reached, a reserve is refused, so an outage can
cost an answer its official excerpts but can never overspend the external budget.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from app.db.connection import connect

logger = logging.getLogger("db.market_assistant_usage")

KINDS = frozenset({"search", "model"})

_RESERVE = """
INSERT INTO market_assistant_usage (day, kind, used) VALUES (%s, %s, 1)
ON CONFLICT (day, kind) DO UPDATE SET used = market_assistant_usage.used + 1
    WHERE market_assistant_usage.used < %s
RETURNING used
"""


def reserve(conn: Any, kind: str, day: date, limit: int) -> bool:
    """Take one unit of ``kind`` for ``day`` if fewer than ``limit`` are used. Atomic: two
    concurrent callers at ``limit - 1`` cannot both succeed (the row lock serialises them,
    and the second sees the updated count in the WHERE)."""
    if kind not in KINDS:
        raise ValueError(f"unknown usage kind {kind!r}")
    if limit <= 0:
        return False
    with conn.cursor() as cur:
        cur.execute(_RESERVE, (day, kind, limit))
        return cur.fetchone() is not None


def used(conn: Any, kind: str, day: date) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT used FROM market_assistant_usage WHERE day = %s AND kind = %s", (day, kind)
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0


class PostgresUsageStore:
    """``limits.UsageStore`` over its own short connection per call, committed at once, so
    a reservation is durable before the external call it pays for is made."""

    def __init__(self, url: str, kind: str):
        if kind not in KINDS:
            raise ValueError(f"unknown usage kind {kind!r}")
        self._url = url
        self.kind = kind

    def reserve(self, day: date, limit: int) -> bool:
        try:
            with connect(self._url) as conn:
                return reserve(conn, self.kind, day, limit)
        except Exception as exc:  # any database failure refuses the external call
            logger.warning("market_assistant usage reserve failed: %s", type(exc).__name__)
            return False

    def used(self, day: date) -> int:
        try:
            with connect(self._url) as conn:
                return used(conn, self.kind, day)
        except Exception as exc:
            logger.warning("market_assistant usage read failed: %s", type(exc).__name__)
            return 0


class MessageUsageUnavailable(Exception):
    """The daily message allowance could not be read or reserved."""


class PostgresMessageStore:
    """Atomic daily allowance for one opaque subject; no conversation is stored."""

    def __init__(self, url: str, subject_hash: str):
        self._url = url
        self._subject = subject_hash

    def used(self, day: date) -> int:
        try:
            with connect(self._url) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT used FROM market_assistant_message_usage "
                    "WHERE subject_hash = %s AND day = %s", (self._subject, day),
                )
                row = cur.fetchone()
                return int(row[0]) if row else 0
        except Exception as exc:
            logger.warning("market_assistant message read failed: %s", type(exc).__name__)
            raise MessageUsageUnavailable from None

    def reserve(self, day: date, limit: int) -> int | None:
        if limit <= 0:
            return None
        try:
            with connect(self._url) as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO market_assistant_message_usage (subject_hash, day, used) "
                    "VALUES (%s, %s, 1) ON CONFLICT (subject_hash, day) DO UPDATE "
                    "SET used = market_assistant_message_usage.used + 1 "
                    "WHERE market_assistant_message_usage.used < %s RETURNING used",
                    (self._subject, day, limit),
                )
                row = cur.fetchone()
                return int(row[0]) if row else None
        except Exception as exc:
            logger.warning("market_assistant message reserve failed: %s", type(exc).__name__)
            raise MessageUsageUnavailable from None

    def release(self, day: date) -> None:
        try:
            with connect(self._url) as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE market_assistant_message_usage SET used = greatest(0, used - 1) "
                    "WHERE subject_hash = %s AND day = %s", (self._subject, day),
                )
        except Exception as exc:
            logger.warning("market_assistant message release failed: %s", type(exc).__name__)
            raise MessageUsageUnavailable from None
