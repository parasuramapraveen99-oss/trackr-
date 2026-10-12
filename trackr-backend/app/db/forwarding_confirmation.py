"""Persistence for ``forwarding_confirmation`` -- Gmail's forwarding verification codes.

Narrow and typed, the same shape as ``app/db/index_levels.py``: one writer called by the
inbound-email webhook, one reader called by the router that shows the code to the person
who has to paste it into Gmail. See migration 037 for why this is a table rather than a
column on ``investor``, and why a one-time forwarding code is not a credential.
"""

from __future__ import annotations

import re
from typing import Any

#: Gmail's own sender for a forwarding verification. Matched exactly, and only after the
#: provider credential has already been verified -- an inbound message is not trusted
#: because of what it claims in its From line, it is trusted because the webhook
#: signature checked out (app/ingest/webhook_providers.py).
GMAIL_FORWARDING_SENDER = "forwarding-noreply@google.com"

#: Gmail's confirmation codes are nine digits. Anchored on a word boundary so a longer
#: run of digits elsewhere in the body -- a message id, a phone number -- cannot be
#: mistaken for one.
_CODE_RE = re.compile(r"\b(\d{9})\b")

#: How long a code is worth showing. Gmail's own codes do not carry a documented expiry,
#: but a code from last month is far more likely to be a stale row than the one the
#: person is waiting for, and showing it would send them to paste a value Gmail has
#: already forgotten. One day is comfortably longer than the minute this flow takes.
MAX_CODE_AGE_HOURS = 24


def extract_code(body_text: str) -> str | None:
    """The nine-digit confirmation code in a Gmail verification body, or ``None``.

    ``None`` is a perfectly ordinary answer and never an error: Gmail also sends
    non-verification mail from the same address, and a body we cannot read a code out of
    is one we simply do not act on. The caller returns 200 either way -- see
    ``app.ingest.webhook.handle_inbound_email``, and defect 9 in docs/ONBOARDING_FLOW.md
    for why a mail provider must not be handed a retryable status for a permanent
    non-event.
    """
    match = _CODE_RE.search(body_text or "")
    return match.group(1) if match else None


def record_confirmation_code(
    conn: Any, investor_id: str, broker: str, doctype: str, code: str
) -> None:
    """Append one code. Never updates: Gmail re-sends on request and each send is its own
    piece of evidence, so the log keeps them all and ``latest_confirmation_codes`` decides
    which one is current."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO forwarding_confirmation (investor_id, broker, doctype, code)
            VALUES (%s, %s, %s, %s)
            """,
            (investor_id, broker.strip().lower(), doctype.strip().lower(), code),
        )


def latest_confirmation_codes(
    conn: Any, investor_id: str, max_age_hours: int = MAX_CODE_AGE_HOURS
) -> list[dict[str, Any]]:
    """The newest unexpired code per ``(broker, doctype)`` for this investor.

    ``DISTINCT ON`` rather than a ``GROUP BY``/re-join: one index-ordered pass over
    ``forwarding_confirmation_investor_idx`` answers it, and the ordering that picks the
    winner is the same one the index already provides.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (broker, doctype) broker, doctype, code, received_at
            FROM forwarding_confirmation
            WHERE investor_id = %s
              AND received_at > now() - make_interval(hours => %s)
            ORDER BY broker, doctype, received_at DESC
            """,
            (investor_id, max_age_hours),
        )
        rows = cur.fetchall()
    return [
        {
            "broker": row[0],
            "doctype": row[1],
            "code": row[2],
            "received_at": row[3],
        }
        for row in rows
    ]
