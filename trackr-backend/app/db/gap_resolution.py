"""Close an account's ledger gaps from public records -- rule 20, the orchestration.

Runs as reference-worker task ``GAP`` (see ``app.reference_worker``) after an account's
REPLAY leaves securities that do not reconcile. For each gap it gathers candidate events
from free exchange records, asks ``app.engine.gap_resolution`` whether each one closes the
gap exactly, and writes the single fitting row -- append-only, ``source=CORP_ACTION``,
labelled ``raw_payload.resolution='AUTO_RESOLVED'`` with its evidence and rule-20 clauses.

  - Rights: the NSE corporate-actions notice (ratio; matched by SYMBOL, because NSE files
    some notices under a retired ISIN -- SOUTHBANK's under INE683A01015) + the NSE "further
    issues" listing circular (current ISIN, price, allotment date).
  - IPO: ``app.ingest.nse_ipo`` (issue price, lot, listing date, allotment date).
  - BSE-only bonus/split, ISIN change: ``researcher`` (Exa + LLM, ``app.db.gap_research``).

Two passes, in that order of trust and cost. PASS 1 runs the structured exchange fixes for
EVERY gap and commits each fit at once; PASS 2 researches only the gaps pass 1 left. So a
rate-limited or unreachable model can delay research, but never the free, structured fixes
of another stock in the same account (it did: one 429 on one stock failed the whole task,
2026-10-10). A research outage is ``deferred``, not a refusal: the task is retried later.

Two or more fitting candidates resolve nothing (rule 20c): both are reported and the gap
stays open. A source outage is not a refusal; the task retries.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import date, timedelta
from typing import Any

from app.engine.gap_resolution import (
    AUTO_RESOLVED,
    Gap,
    Resolution,
    RightsTerms,
    find_gaps,
    ipo_fits,
    rights_fits,
)

logger = logging.getLogger(__name__)


class ResearchDeferred(Exception):
    """Research could not run now -- the model or the search is rate-limited, out of its
    daily quota, or unreachable. Not a refusal: the gap stays open and the GAP task is
    retried later (``app.reference_worker``)."""


#: How long after a rights ex-date NSE's further-issue listing is looked for. SOUTHBANK:
#: ex 27 Feb 2024, allotted 27 Mar, listed in the 1 Apr circular -- 34 days.
RIGHTS_LISTING_WINDOW_DAYS = 75

#: Published NSE circulars never change, so what they say is kept for the life of the
#: process: every account holding the same stock reuses one download instead of fetching
#: the same PDFs again (~40 s per account, 2026-10-10). A listing is kept only for a
#: window wholly in the past -- a window reaching today can still gain circulars.
_LISTING_CACHE: dict[tuple[date, date], Any] = {}
_ISSUES_CACHE: dict[str, list] = {}


def _listing(circulars: Any, start: date, end: date) -> Any:
    if (start, end) in _LISTING_CACHE:
        return _LISTING_CACHE[(start, end)]
    listing = json.loads(circulars.list_circulars(start, end))
    if end < date.today():
        _LISTING_CACHE[(start, end)] = listing
    return listing


def _further_issues(circulars: Any, url: str) -> list:
    from app.ingest.isin_change_feed import extract_pdf_text
    from app.ingest.nse_further_issue import parse_further_issues

    if url not in _ISSUES_CACHE:
        _ISSUES_CACHE[url] = parse_further_issues(extract_pdf_text(circulars.fetch_pdf(url)))
    return _ISSUES_CACHE[url]


def _rights_candidates(conn: Any, gap: Gap, symbol: str, circulars: Any) -> list[RightsTerms]:
    from app.ingest.nse_further_issue import parse_rights_ratio

    notices = conn.execute(
        """SELECT DISTINCT ex_date, raw_payload->>'subject' FROM corporate_action_observation
           WHERE provider='NSE' AND classification='RIGHTS'
             AND (raw_payload->>'symbol' = %s OR isin = %s)
             AND ex_date IS NOT NULL AND ex_date <= %s
           ORDER BY ex_date""",
        (symbol, gap.isin, gap.first_negative),
    ).fetchall()
    out: list[RightsTerms] = []
    for ex_date, subject in notices:
        ratio = parse_rights_ratio(subject or "")
        if ratio is None:
            continue
        listing = _listing(circulars, ex_date, ex_date + timedelta(days=RIGHTS_LISTING_WINDOW_DAYS))
        rows = listing.get("data", listing) if isinstance(listing, dict) else listing
        for row in rows:
            if "further issue" not in str(row.get("sub", "")).lower():
                continue
            if str(row.get("fileExt", "")).lower() != "pdf":
                continue
            entries = _further_issues(circulars, row["circFilelink"])
            hit = [e for e in entries if e.symbol == symbol and e.is_rights and e.issue_price]
            if not hit:
                continue
            entry = hit[0]
            out.append(
                RightsTerms(
                    isin=entry.isin,
                    ratio_new=ratio[0],
                    ratio_held=ratio[1],
                    price=entry.issue_price,
                    ex_date=ex_date,
                    allotment_date=entry.allotment_date,
                    evidence={
                        "notice": {"subject": subject, "ex_date": ex_date.isoformat()},
                        "listing_circular": {
                            "url": row["circFilelink"],
                            "number": row.get("circDisplayNo"),
                        },
                    },
                )
            )
            break
    return out


def resolve_account_gaps(
    conn: Any,
    account_id: str,
    *,
    ipo_source: Any = None,
    circulars: Any = None,
    researcher: Any = None,
) -> dict[str, Any]:
    """Resolve what can be resolved for one account; report the rest. Commits per row."""
    from app import securities
    from app.ai.llm import LlmResponseError, LlmUnavailable
    from app.db.auto_reversal import check_auto_rows, reversed_refs
    from app.db.repository import load_transactions, save_transactions
    from app.ingest.exa_search import ExaUnavailable
    from app.ingest.nse_ipo import NseIpoFormatError

    # PASS 0 -- rule 20(f): the investor's own documents may now confirm an earlier
    # auto-resolved row, show it is no longer needed, or show the shares never arrived.
    checked = check_auto_rows(conn, account_id)
    refused = reversed_refs(conn, account_id)

    transactions = load_transactions(conn, account_id=account_id)
    gaps = find_gaps(transactions)
    if not gaps:
        return {"gaps": 0, "resolved": [], "open": [], "deferred": [], **checked}
    meta = securities.lookup(conn, [g.isin for g in gaps])
    if circulars is None:
        from app.ingest.isin_change_feed import NseCircularListSource

        circulars = NseCircularListSource()
    if ipo_source is None:
        from app.ingest.nse_ipo import NseIpoSource

        ipo_source = NseIpoSource(circulars=circulars)

    resolved, still_open, deferred = [], [], []
    leftovers: list[tuple[Gap, dict, list[str]]] = []
    # PASS 1 -- structured exchange records for every gap; each fit is committed at once.
    for gap in gaps:
        info = meta.get(gap.isin) or {}
        symbol, company = info.get("symbol"), info.get("name")
        fits: list[Resolution] = []
        refusals: list[str] = []
        if symbol:
            for terms in _rights_candidates(conn, gap, symbol, circulars):
                result = rights_fits(gap, terms, transactions)
                if isinstance(result, Resolution):
                    fits.append(_with_evidence(result, terms.evidence))
                else:
                    refusals.append(result.reason)
            if company:
                try:
                    ipo = ipo_source.terms(symbol, company)
                except NseIpoFormatError as exc:
                    refusals.append(f"no NSE IPO record: {exc}")
                else:
                    result = ipo_fits(gap, ipo, transactions)
                    if isinstance(result, Resolution):
                        fits.append(_with_evidence(result, ipo.evidence))
                    else:
                        refusals.append(result.reason)
        # A resolution the investor's documents already reversed is never written again:
        # the gap stays open and named, rather than looping add -> reverse -> add.
        undone = [f for f in fits if f.row.source_ref in refused]
        fits = [f for f in fits if f.row.source_ref not in refused]
        if undone and not fits:
            still_open.append(
                {
                    "isin": gap.isin,
                    "symbol": symbol,
                    "shortfall": str(gap.shortfall),
                    "reason": "reversed by your document -- import the real history",
                }
            )
            continue
        if len(fits) == 1:
            (fit,) = fits
            save_transactions(conn, [fit.row])
            conn.commit()
            resolved.append(
                {
                    "isin": gap.isin,
                    "symbol": symbol,
                    "kind": fit.kind,
                    "quantity": str(fit.row.quantity),
                    "price": str(fit.row.price),
                    "date": fit.row.txn_date.isoformat(),
                }
            )
        elif fits:
            still_open.append(
                {
                    "isin": gap.isin,
                    "symbol": symbol,
                    "shortfall": str(gap.shortfall),
                    "reason": "several public events fit -- left open (rule 20c): "
                    + ", ".join(f.kind for f in fits),
                }
            )
        else:
            leftovers.append((gap, info, refusals))

    # PASS 2 -- research (Exa + LLM) only for what no structured record explained.
    paused: str | None = None
    for gap, info, refusals in leftovers:
        symbol = info.get("symbol")
        open_row = {"isin": gap.isin, "symbol": symbol, "shortfall": str(gap.shortfall)}
        if researcher is None:
            still_open.append(
                {**open_row, "reason": "; ".join(refusals) or "no public event found yet"}
            )
            continue
        if paused is None:
            try:
                outcome = researcher(conn, account_id, gap, info)
            except (ResearchDeferred, LlmUnavailable, ExaUnavailable) as exc:
                # The quota and the services are shared by every gap: once one call is
                # refused, the next would be too. Stop calling; retry the task later.
                conn.rollback()
                paused = str(exc)
            except LlmResponseError as exc:
                conn.rollback()
                still_open.append({**open_row, "reason": f"research answer unusable: {exc}"})
                continue
        if paused is not None:
            deferred.append({**open_row, "reason": f"automatic research paused: {paused}"})
            continue
        if outcome.get("status") == "resolved":
            resolved.append(
                {
                    "isin": gap.isin,
                    "symbol": symbol,
                    "kind": "RESEARCHED",
                    "events": outcome.get("events", []),
                }
            )
        else:
            still_open.append(
                {**open_row, "reason": outcome.get("reason", "research found nothing")}
            )
    return {
        "gaps": len(gaps),
        "resolved": resolved,
        "open": still_open,
        "deferred": deferred,
        **checked,
    }


def _with_evidence(result: Resolution, evidence: dict) -> Resolution:
    payload = {
        "resolution": AUTO_RESOLVED,
        "kind": result.kind,
        "rule_20": result.clauses,
        "evidence": evidence,
    }
    return replace(result, row=replace(result.row, raw_payload=payload))
