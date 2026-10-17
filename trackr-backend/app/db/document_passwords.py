"""Saved statement passwords -- one per source, used exactly as typed (migration 082).

The ONLY module that reads or writes ``document_password.password_enc`` (the Makefile lint
enforces it), so the decrypted value has exactly one way out: ``saved_passwords``, which
exists to open a file and nothing else. Nothing here returns a password to a client.

A password reaches ``save`` only after it has opened a real file (app/routers/ingest.py);
this module does not check that itself, because it has no file to check against.
"""

from __future__ import annotations

from typing import Any

from app.db import audit, crypto

#: Every source a password can be saved for, in the order Settings lists them. Mirrors the
#: CHECK constraint in 082_document_password.sql.
SOURCES: tuple[str, ...] = ("zerodha", "groww", "upstox", "cdsl", "nsdl", "cams", "kfintech")

#: Shown to the investor, never used to open anything: the password is what they typed.
SOURCE_LABELS: dict[str, str] = {
    "zerodha": "Zerodha contract notes",
    "groww": "Groww contract notes",
    "upstox": "Upstox contract notes",
    "cdsl": "CDSL CAS (monthly statement)",
    "nsdl": "NSDL CAS",
    "cams": "CAMS mutual-fund CAS",
    "kfintech": "KFintech mutual-fund CAS",
}
SOURCE_HINTS: dict[str, str] = {
    "zerodha": "Usually your PAN in capitals",
    "groww": "Usually your PAN in capitals",
    "upstox": "Usually your PAN in small letters",
    "cdsl": "Your PAN in capitals",
    "nsdl": "Your PAN in capitals",
    "cams": "The password you set when you requested it",
    "kfintech": "The password you set when you requested it",
}


class UnknownSourceError(ValueError):
    """Not one of ``SOURCES``."""


def _check_source(source: str) -> str:
    if source not in SOURCES:
        raise UnknownSourceError(f"unknown statement source: {source!r}")
    return source


def save(conn: Any, investor_id: str, source: str, password: str, actor_uid: str | None) -> None:
    """Store (or replace) the password for one source, exactly as given."""
    _check_source(source)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO document_password (investor_id, source, password_enc)
            VALUES (%s, %s, %s)
            ON CONFLICT (investor_id, source) DO UPDATE
            SET password_enc = EXCLUDED.password_enc, updated_at = now()
            """,
            (investor_id, source, crypto.encrypt(password)),
        )
    audit.record(
        conn,
        action=audit.ACTION_SECRET_WRITTEN,
        entity_type="document_password",
        entity_id=investor_id,
        actor_uid=actor_uid,
        diff={"source": source},
    )
    # Reuse archived originals when a verified password becomes available; sending
    # the same file again must not be necessary for automatic email recovery.
    from app.db.email_ingestion import retry_blocked

    retry_blocked(conn, investor_id)


def remove(conn: Any, investor_id: str, source: str, actor_uid: str | None) -> bool:
    """Delete one source's password. False when none was saved."""
    _check_source(source)
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM document_password WHERE investor_id = %s AND source = %s",
            (investor_id, source),
        )
        deleted = cur.rowcount > 0
    if deleted:
        audit.record(
            conn,
            action=audit.ACTION_DELETE,
            entity_type="document_password",
            entity_id=investor_id,
            actor_uid=actor_uid,
            diff={"source": source},
        )
    return deleted


def saved_sources(conn: Any, investor_id: str) -> dict[str, Any]:
    """``{source: updated_at}`` for every saved password. Never the value."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source, updated_at FROM document_password WHERE investor_id = %s",
            (investor_id,),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def saved_passwords(conn: Any, investor_id: str) -> list[tuple[str, str]]:
    """``[(source, password)]``, decrypted, in ``SOURCES`` order -- to open a file, nothing
    else. Raises if the encryption key is missing or wrong: a password we cannot read is a
    configuration fault to fix, not a reason to quietly try nothing."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source, password_enc FROM document_password WHERE investor_id = %s",
            (investor_id,),
        )
        rows = {row[0]: crypto.decrypt(row[1]) for row in cur.fetchall()}
    return [(source, rows[source]) for source in SOURCES if source in rows]


#: Not a saved-password source: the PAN lives in ``investor_secret`` (set in Settings or at
#: onboarding) and is tried automatically. Named so ``Unlock.source`` can say it opened a file.
PAN_SOURCE = "pan"


def stored_pan(conn: Any, investor_id: str) -> str:
    """The investor's PAN as stored (normalised to capitals), or "" when none is on file."""
    with conn.cursor() as cur:
        cur.execute("SELECT pan_enc FROM investor_secret WHERE investor_id = %s", (investor_id,))
        row = cur.fetchone()
    if row is None or row[0] is None:
        return ""
    return crypto.decrypt(row[0])


def unlock_candidates(conn: Any, investor_id: str) -> list[tuple[str, str]]:
    """Every password to try on a locked file before asking the investor, in order:

    1. their PAN, in capitals then small letters -- the two forms brokers and depositories
       lock statements with (CDSL/NSDL/Zerodha/Groww: capitals; Upstox: small letters).
       Only when a PAN is on file; nothing else is ever derived from it;
    2. each password they saved for a statement source, exactly as typed.

    (No password at all is tried before any of these, by ``unlock_pdf`` itself.)
    """
    pan = stored_pan(conn, investor_id)
    pan_forms = list(dict.fromkeys((pan.upper(), pan.lower()))) if pan else []
    return [(PAN_SOURCE, form) for form in pan_forms] + saved_passwords(conn, investor_id)
