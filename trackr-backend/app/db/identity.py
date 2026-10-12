"""Does this document belong to the account it is being staged into?

A broker document names its owner. The destination account is chosen by something else
entirely -- a dropdown an advisor clicked, or an inbound address a stranger could guess
(``app.ingest.webhook.parse_to_address``; investor codes are short and human-readable, so
the address is not a secret). When those two disagree, one investor's trades are about to
enter another investor's ledger. This module is the only place that compares them.

Two identifiers, both of which already have a home in the schema -- this check needed no
migration:

- **PAN** vs ``investor_secret.pan_hash``. Compared as peppered HMAC, never decrypted:
  ``app.db.crypto.hash_pan`` turns the document's PAN into the same hash the row already
  stores, so a mismatch is detected without either value being read back into memory in
  the clear. Catches wrong-investor.
- **UCC** vs ``account.client_id``. The broker's own code for the account. Catches
  right-investor-wrong-account, which PAN cannot -- one person's two Zerodha accounts
  share a PAN.

**Refuse before storing, not after.** ``stage_batch`` calls this ahead of
``store_document`` deliberately: a note that belongs to somebody else must not be written
into this investor's document store on the way to being rejected. Refusing early means the
only trace left is an audit row naming a redacted PAN, which is the correct amount of
another person's data to keep.

Nothing here is a substitute for the webhook's own credential check
(``app.ingest.webhook_providers``). That establishes the message came from the mail
provider; this establishes the document inside it belongs to the account it names.
"""

from __future__ import annotations

from typing import Any

from app.db import audit, crypto
from app.ingest.base import DocumentIdentity

#: Audit action for a document refused because it named somebody else. Distinct from a
#: parse failure: this file was readable and well-formed, and was turned away on identity.
ACTION_IDENTITY_REFUSED = "IDENTITY_REFUSED"


class IdentityMismatchError(PermissionError):
    """The document names an owner that is not this account's.

    ``PermissionError`` rather than ``ValueError``: nothing about the file is malformed,
    and re-uploading it will not help. It is a well-formed document belonging to someone
    else, and the router answers 409.
    """

    def __init__(self, field: str, expected: str, found: str) -> None:
        self.field = field
        self.expected = expected
        self.found = found
        super().__init__(
            f"this document's {field} is not this account's: "
            f"document says {found}, account is {expected}"
        )


def _stored_pan_hashes(conn: Any, investor_id: str) -> list[str]:
    """Both hashes for this investor -- ``pan_hash`` and, during a pepper rotation,
    ``pan_hash_next``.

    docs/KEY_ROTATION.md §4 is explicit that a lookup during the rotation window must
    match either ("look up by either hash ... for the duration"). Checking only
    ``pan_hash`` would turn every legitimate contract note into an identity refusal for
    exactly as long as a rotation takes to run.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pan_hash, pan_hash_next FROM investor_secret WHERE investor_id = %s",
            (investor_id,),
        )
        row = cur.fetchone()
    if row is None:
        return []
    return [h for h in row if h]


def _account_client_id(conn: Any, account_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT client_id FROM account WHERE id = %s", (account_id,))
        row = cur.fetchone()
    return (row[0] or "").strip().upper() if row else ""


def verify(
    conn: Any,
    identity: DocumentIdentity,
    investor_id: str,
    account_id: str,
    actor_uid: str | None = None,
) -> list[str]:
    """Compare a document's stated owner against the destination account.

    Returns notes to attach to the import batch -- one per identifier that could NOT be
    checked. They are not decoration: an advisor looking at a staged batch needs to know
    the difference between "verified as yours" and "nothing on file to verify against",
    and the review screen is the last human gate before promotion. Silence would read as
    the former while meaning the latter.

    Raises:
        IdentityMismatchError: an identifier was stated on the document, was on file for
            the account, and the two differ. Never raised on absence -- an unverifiable
            document is a note, a contradicted one is a refusal.
    """
    notes: list[str] = []

    if not identity:
        return ["identity not verified: this document states no PAN or client code"]

    if identity.pan:
        stored = _stored_pan_hashes(conn, investor_id)
        if not stored:
            notes.append(
                "identity NOT verified: no PAN on file for this investor, so the PAN "
                "printed on this document could not be checked against anything"
            )
        else:
            try:
                presented = crypto.hash_pan(identity.pan)
            except crypto.InvalidPANError:
                # Read off the page but not a structurally valid PAN. Treat as unstated
                # rather than as a mismatch: the likely cause is an OCR or layout slip,
                # not a document belonging to somebody else.
                notes.append(
                    "identity NOT verified: the PAN read from this document is not "
                    "structurally valid"
                )
                presented = ""
            if presented and presented not in stored:
                _record_refusal(conn, investor_id, account_id, actor_uid, "PAN", identity)
                raise IdentityMismatchError(
                    "PAN",
                    expected="the PAN on file for this investor",
                    found=crypto.redact_pan(identity.pan),
                )
    else:
        notes.append("identity partly verified: this document states no PAN")

    if identity.client_code:
        on_file = _account_client_id(conn, account_id)
        if not on_file:
            notes.append(
                f"identity partly verified: no client code on file for this account, so "
                f"the document's {identity.client_code} could not be checked"
            )
        elif on_file != identity.client_code:
            _record_refusal(conn, investor_id, account_id, actor_uid, "client code", identity)
            raise IdentityMismatchError(
                "client code", expected=on_file, found=identity.client_code
            )

    return notes


def _record_refusal(
    conn: Any,
    investor_id: str,
    account_id: str,
    actor_uid: str | None,
    field: str,
    identity: DocumentIdentity,
) -> None:
    """Leave a trace of a refused document, since the document itself is not kept.

    The diff carries a REDACTED PAN and never the raw one -- ``app.db.audit.record``'s
    own contract ("``diff`` must never contain a PAN, a demat number or ciphertext").
    A refusal is exactly the case where somebody will want to know later what arrived and
    was turned away, and exactly the case where retaining it in full would be keeping a
    third party's identifier for no lawful reason.

    DURABILITY IS NOT UNIFORM, deliberately. This writes on the caller's connection, per
    ``audit.record``'s design, so it survives only when the caller catches the refusal and
    the request goes on to commit. That is the inbound-email path, which catches
    per-attachment (``app.ingest.webhook.handle_inbound_email``) -- and which is the
    untrusted public surface where a record of refusals actually matters. On the upload
    path the refusal reaches a human as a 409 immediately and the row is rolled back with
    the rest of the request. Making it durable there needs a second connection, which
    ``audit.record`` explicitly warns against; that trade is worth deciding on its own
    merits rather than as a side effect of this check.
    """
    audit.record(
        conn,
        action=ACTION_IDENTITY_REFUSED,
        entity_type="account",
        entity_id=account_id,
        actor_uid=actor_uid,
        diff={
            "mismatched_field": field,
            "investor_id": investor_id,
            "document_pan": crypto.redact_pan(identity.pan) if identity.pan else None,
            "document_client_code": identity.client_code or None,
        },
    )
