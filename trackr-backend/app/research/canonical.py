"""Canonical request digests and opaque pagination cursors.

TWO THINGS THAT MUST BE BYTE-STABLE ACROSS PROCESSES. An idempotency receipt compares a
retry's payload digest with the one recorded by a process that may since have restarted,
and a cursor issued by one worker is redeemed by another. Neither may depend on dict
ordering, ``repr``, ``hash()`` randomisation or a float's shortest-repr -- so both go
through ``canonical_json``: sorted keys, no whitespace, UTF-8, and a refusal to encode a
float at all.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.research.errors import INVALID_CURSOR, WorkspaceError

#: Page size contract, BLUEPRINT_BACKEND.md section 7.1.
DEFAULT_LIMIT = 50
MAX_LIMIT = 100


def _default(value: Any) -> Any:
    if isinstance(value, bool):  # pragma: no cover - json handles bool before default
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal cannot be canonicalised")
        return format(value, "f")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive datetime cannot be canonicalised")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    raise TypeError(f"cannot canonicalise {type(value).__name__}")


def _refuse_floats(value: Any) -> None:
    if isinstance(value, float):
        raise TypeError("a float cannot enter a canonical digest; use a decimal string")
    if isinstance(value, dict):
        for item in value.values():
            _refuse_floats(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _refuse_floats(item)


def canonical_json(value: Any) -> str:
    """Sorted-key, whitespace-free JSON. Floats are refused, not rounded."""
    _refuse_floats(value)
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default
    )


def digest(value: Any) -> str:
    """SHA-256 hex of ``canonical_json(value)``."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


# --- cursors ------------------------------------------------------------------------------


def scope_digest(investor_id: str, route: str, filters: dict[str, Any]) -> str:
    """Binds a cursor to one investor, one collection and one filter set.

    A cursor redeemed under a different investor or with different filters is refused
    rather than silently continuing some other listing -- that would mix scopes, and for a
    different investor it would be a probe.
    """
    return digest({"i": str(investor_id), "r": route, "f": filters})[:24]


def encode_cursor(scope: str, key: list[Any]) -> str:
    raw = canonical_json({"s": scope, "k": key}).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None, scope: str) -> list[Any] | None:
    """The order key a cursor continues from, or None for the first page.

    Raises 422 INVALID_CURSOR for anything malformed or issued for another scope.
    """
    if cursor is None or cursor == "":
        return None
    if len(cursor) > 512:
        raise WorkspaceError(422, INVALID_CURSOR, "cursor is not valid for this listing")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, binascii.Error, UnicodeError) as exc:
        raise WorkspaceError(422, INVALID_CURSOR, "cursor is not valid for this listing") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("s") != scope
        or not isinstance(payload.get("k"), list)
    ):
        raise WorkspaceError(422, INVALID_CURSOR, "cursor is not valid for this listing")
    return payload["k"]


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    if limit < 1 or limit > MAX_LIMIT:
        raise WorkspaceError(
            422, "INVALID_INPUT", f"limit must be between 1 and {MAX_LIMIT}", {"limit": "RANGE"}
        )
    return limit


def escape_like(text: str) -> str:
    """Literal-substring LIKE pattern. ``%``, ``_`` and ``\\`` in user input match
    themselves, never act as wildcards. Used with ``ILIKE ... ESCAPE '\\'``."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"
