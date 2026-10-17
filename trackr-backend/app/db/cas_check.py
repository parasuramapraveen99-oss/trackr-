"""CAS import diagnostics, read-only against the ledger; saved as import notes.

These notes record the check at import time. Reparse reruns it after new contract notes.
Quantity candidates are not verified settlement matches and never create transactions.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.db.repository import load_transactions
from app.db.security_alias import verified_map
from app.engine.cas_check import check_movements


def check_notes(conn: Any, adapter: Any, content: bytes, declared: list[Any]) -> list[str]:
    if not declared or not hasattr(adapter, "extract_movements") or content[:5] != b"%PDF-":
        return []
    accounts = {h.account_id for h in declared}
    if len(accounts) != 1:
        raise ValueError("CAS diagnostics require one resolved account")
    account_id = accounts.pop()
    on = declared[0].snap_date
    # Bound identity to the statement date; a future split must not rename old holdings.
    aliases = verified_map(conn, on)
    movements = [
        replace(m, isin=aliases.get(m.isin, m.isin)) for m in adapter.extract_movements(content)
    ]
    tx = load_transactions(conn, account_id=account_id, date_to=on)
    checks = check_movements(movements, tx, account_id)
    notes = []
    if checks:
        candidates = sum(c.status == "CANDIDATE" for c in checks)
        notes.append(
            f"CAS trade check: {candidates} of {len(checks)} settlement groups have a unique "
            "ledger quantity candidate. Settlement identifiers are not available in the "
            "ledger, so these are quantity checks, not confirmed trade matches."
        )
    for c in checks:
        if c.status == "CANDIDATE":
            continue
        if c.status == "AMBIGUOUS":
            detail = "ambiguous trade dates: " + ", ".join(d.isoformat() for d in c.candidate_dates)
        else:
            detail = (
                "no ledger quantity candidate; contract note or fund transaction evidence missing"
            )
        notes.append(f"{c.isin}: net {c.net_quantity} settled {c.settle_date}; {detail}.")
    # A holding can predate every movement in this file. Do not omit it from diagnostics.
    traded = {t.isin for t in tx}
    for h in declared:
        isin = aliases.get(h.isin, h.isin)
        if h.quantity > 0 and isin not in traded:
            notes.append(
                f"{h.isin}: CAS declares {h.quantity} units on {on}, but no acquisition "
                "history exists in this account's ledger. Cost basis and returns are unavailable."
            )
    return notes
