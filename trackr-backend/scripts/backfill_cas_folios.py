"""Recover direct-fund balances in an explicitly tagged scratch database (dry run by default).

Does not re-promote batches or create/reverse transactions. The same parser and record
boundary handle new uploads, email documents and this source-evidence repair.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

from dotenv import dotenv_values

from app.config import get_settings
from app.db import audit, cas_folios, document_passwords, identity
from app.db.scratch import connect_scratch
from app.ingest.cas_pdf import CasPdfAdapter
from app.ingest.file_validation import UNLOCK_OPENED, unlock_pdf
from app.storage.factory import get_store


def recover(conn, store, batch, *, apply=False):
    batch_id, investor_id, account_id, document_owner, storage_key, filename, notes, owner = batch
    if str(investor_id) != str(document_owner):
        raise ValueError("CAS source document and import batch have different owners.")
    content = store.get(storage_key)
    unlocked = unlock_pdf(content, document_passwords.unlock_candidates(conn, str(investor_id)))
    if unlocked.outcome != UNLOCK_OPENED:
        raise ValueError("The stored CAS cannot be opened with the saved statement passwords.")
    adapter = CasPdfAdapter(password=unlocked.password)
    identity.verify(
        conn,
        adapter.identity(content),
        str(investor_id),
        str(account_id),
        str(owner) if owner else None,
    )
    rows = adapter.parse_declared_folios(content)
    if apply:
        cas_folios.record(conn, str(investor_id), str(batch_id), rows)
        if rows:
            note = (
                f"{len(rows)} direct mutual fund folio balance(s) recorded as dated CAS "
                "evidence. Fund transaction history is needed for cost basis and returns."
            )
            updated = [
                n for n in (notes or []) if not re.match(r"\d+ direct mutual fund folio", n)
            ] + [note]
            if updated != notes:
                conn.execute(
                    "UPDATE import_batch SET notes=%s WHERE id=%s", (json.dumps(updated), batch_id)
                )
                audit.record(
                    conn,
                    audit.ACTION_UPDATE,
                    "cas_fund_snapshot",
                    str(batch_id),
                    str(owner) if owner else None,
                    {
                        "source_batch_id": str(batch_id),
                        "rows": len(rows),
                        "reason": "Re-extracted direct fund balances from stored CAS",
                    },
                )
    return dict(
        batch_id=str(batch_id),
        filename=filename,
        folios=len(rows),
        statement_dates=sorted({r.snap_date.isoformat() for r in rows}),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scratch-url", required=True, help="Explicit tagged disposable database URL"
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--investor-id")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    for key, value in dotenv_values(args.env_file).items():
        if value is not None:
            os.environ.setdefault(key, value)
    settings = get_settings()
    store = get_store(settings)
    results = []
    with connect_scratch("CAS folio recovery probe", url=args.scratch_url) as conn:
        if not args.apply:
            conn.read_only = True
        if args.apply and not cas_folios.tables_present(conn):
            raise SystemExit("Apply migration 084_cas_fund_snapshot.sql before --apply.")
        rows = conn.execute(
            """SELECT b.id,b.investor_id,b.account_id,d.investor_id,d.storage_key,b.filename,
                      b.notes,i.owner_user_id
               FROM import_batch b JOIN document d ON d.id=b.document_id
               JOIN investor i ON i.id=b.investor_id
               WHERE b.adapter='cas_pdf' AND b.status<>'DISCARDED'
                 AND (%s::uuid IS NULL OR b.investor_id=%s::uuid)
               ORDER BY b.uploaded_at""",
            (args.investor_id, args.investor_id),
        ).fetchall()
        for batch in rows:
            try:
                with conn.transaction():
                    results.append(recover(conn, store, batch, apply=args.apply))
            except Exception as exc:
                results.append(
                    dict(
                        batch_id=str(batch[0]),
                        filename=batch[5],
                        error=type(exc).__name__ + ": " + str(exc),
                    )
                )
        if not args.apply:
            conn.rollback()
    print(json.dumps(dict(applied=args.apply, results=results), indent=2))
    return 1 if any("error" in result for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
