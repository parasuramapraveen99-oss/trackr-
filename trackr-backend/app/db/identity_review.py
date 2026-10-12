"""Vendor identity questions and each investor's answers (migration 074).

The identity check withholds a vendor's ratios when its company name differs from ours.
This module turns that into a question: the review keeps the vendor's name and ratios;
an investor's "SAME" answer applies them to THAT investor's analytics only.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

from app.db.fundamentals import Fundamentals

DECISIONS = ("SAME", "DIFFERENT")


def upsert_review(
    conn: Any,
    isin: str,
    provider: str,
    ticker: str,
    vendor_name: str,
    reason: str,
    candidate: dict,
    observed_on: date,
) -> None:
    conn.execute(
        """INSERT INTO vendor_identity_review
               (isin, provider, ticker, vendor_name, reason, candidate, observed_on)
           VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s)
           ON CONFLICT (isin, provider, ticker, vendor_name) DO UPDATE SET
               reason=excluded.reason, candidate=excluded.candidate,
               observed_on=excluded.observed_on, updated_at=now()""",
        (isin, provider, ticker, vendor_name, reason, json.dumps(candidate), observed_on),
    )


def _withheld(conn: Any, isins: list[str]) -> dict[str, str]:
    """ISIN -> ticker, for ISINs whose newest fundamentals row is identity-withheld."""
    rows = conn.execute(
        """SELECT DISTINCT ON (isin) isin, source FROM security_fundamentals
           WHERE isin = ANY(%s) ORDER BY isin, as_of DESC, fetched_at DESC""",
        (list(isins),),
    ).fetchall()
    return {
        isin: src.split(":")[1] for isin, src in rows if "identity_" in src and src.count(":") >= 2
    }


def reviews_for(conn: Any, investor_id: str, isins: list[str]) -> list[dict]:
    """Open questions for these ISINs: the current withheld ticker's vendor record, with
    this investor's answer if any. Only ISINs still withheld today are listed."""
    withheld = _withheld(conn, isins)
    if not withheld:
        return []
    rows = conn.execute(
        """SELECT r.isin, r.provider, r.ticker, r.vendor_name, r.reason, r.observed_on,
                  s.name, s.symbol, d.decision, d.decided_at
           FROM vendor_identity_review r
           JOIN security s ON s.isin = r.isin
           LEFT JOIN vendor_identity_decision d ON d.investor_id = %s AND d.isin = r.isin
                AND d.provider = r.provider AND d.ticker = r.ticker
                AND d.vendor_name = r.vendor_name
           WHERE r.isin = ANY(%s)""",
        (investor_id, list(withheld)),
    ).fetchall()
    return [
        {
            "isin": r[0],
            "provider": r[1],
            "ticker": r[2],
            "vendor_name": r[3],
            "reason": r[4],
            "observed_on": r[5],
            "our_name": r[6],
            "symbol": r[7],
            "decision": r[8],
            "decided_at": r[9],
        }
        for r in rows
        if withheld.get(r[0]) == r[2]
    ]


def decide(
    conn: Any,
    investor_id: str,
    isin: str,
    provider: str,
    ticker: str,
    vendor_name: str,
    decision: str,
) -> bool:
    """Record (or change) one answer. False when no such open question exists."""
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}")
    exists = conn.execute(
        "SELECT 1 FROM vendor_identity_review WHERE isin=%s AND provider=%s AND ticker=%s "
        "AND vendor_name=%s",
        (isin, provider, ticker, vendor_name),
    ).fetchone()
    if not exists:
        return False
    conn.execute(
        """INSERT INTO vendor_identity_decision
               (investor_id, isin, provider, ticker, vendor_name, decision)
           VALUES (%s,%s,%s,%s,%s,%s)
           ON CONFLICT (investor_id, isin, provider, ticker, vendor_name) DO UPDATE SET
               decision=excluded.decision, decided_at=now()""",
        (investor_id, isin, provider, ticker, vendor_name, decision),
    )
    return True


def confirmed_overrides(conn: Any, investor_id: str, isins: list[str]) -> dict[str, Fundamentals]:
    """Fundamentals this investor has confirmed: the candidate of each open question they
    answered SAME. Labelled ``YFINANCE:<ticker>:investor_confirmed`` wherever shown."""
    out: dict[str, Fundamentals] = {}
    for item in reviews_for(conn, investor_id, isins):
        if item["decision"] != "SAME":
            continue
        candidate = conn.execute(
            "SELECT candidate FROM vendor_identity_review WHERE isin=%s AND provider=%s "
            "AND ticker=%s AND vendor_name=%s",
            (item["isin"], item["provider"], item["ticker"], item["vendor_name"]),
        ).fetchone()[0]
        decimals = {
            k: (Decimal(v) if v is not None else None)
            for k, v in candidate.items()
            if k not in ("roe_basis", "fiscal_period")
        }
        out[item["isin"]] = Fundamentals(
            isin=item["isin"],
            as_of=item["observed_on"],
            roe_basis=candidate.get("roe_basis"),
            fiscal_period=date.fromisoformat(candidate["fiscal_period"])
            if candidate.get("fiscal_period")
            else None,
            source=f"{item['provider']}:{item['ticker']}:investor_confirmed",
            **decimals,
        )
    return out
