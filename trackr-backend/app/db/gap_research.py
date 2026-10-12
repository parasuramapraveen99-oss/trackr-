"""Rule-20 research for gaps no structured exchange feed explains (BSE-only bonuses,
splits, ISIN changes) -- owner decision 2026-10-08.

The loop, for one open gap:

  1. REQUEST  -- what Trackr+ cannot compute and what would explain it, as JSON: the
     security, its sibling ISINs, the ledger window, and the price discontinuities the
     exchange's own bhavcopy shows in that window (``find_price_jumps``).
  2. SEARCH   -- ``app.ingest.exa_search``: nseindia.com / bseindia.com only.
  3. READ     -- ``app.ingest.corporate_action_llm``: the model fills fixed JSON from the
     fetched documents, with a verbatim quote. It never searches and never decides.
  4. VERIFY   -- deterministic (``app.engine.gap_resolution``): quote verbatim in the hashed
     document, the document naming this company (in the quote, if it names others), the
     quote naming the event type, every number in the quote, the date on an observed price
     jump whose size agrees with the ratio, an ISIN change visible as a handover in the
     bhavcopy, duplicate readings collapsed and contradictory ones refused, and all events
     together closing the gap exactly.
  4b. REVIEW  -- a second, independent LLM reading of each surviving event against its
     document (``review_event``). Veto only: it can stop a write, never cause one.
  5. WRITE    -- through the existing machinery: ``add_alias`` (VERIFIED, with evidence) for
     an ISIN change, ``add_corporate_action`` (VERIFIED, cum/ex prices from the jump) for
     a bonus/split, then ``apply_known_actions_for_account``. Committed only if the account
     then reconciles; otherwise everything is rolled back and the gap stays open.

Every record written carries ``AUTO_RESOLVED`` in its source and the document URL, hash
and quote in its evidence (rule 20e).
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from app.engine.gap_resolution import (
    AUTO_RESOLVED,
    JUMP_DATE_TOLERANCE_DAYS,
    Gap,
    check_event_against_document,
    consolidate_events,
    events_close_gap,
    find_price_jumps,
    isin_handover_observed,
    jump_for,
)

logger = logging.getLogger(__name__)

SOURCE_LABEL = f"{AUTO_RESOLVED} rule 20: exchange document via Exa"
#: How far before the first trade an event may lie and still matter (a pre-history split
#: whose shares were later sold); the window's other end is the first negative date.
LOOKBACK_DAYS = 60
MAX_DOCUMENTS = 15
#: Per search phrase, so one event's search cannot crowd out another event's documents.
DOCS_PER_QUERY = 3


def _cluster(conn: Any, isin: str, symbol: str | None) -> list[str]:
    """The security's own ISIN plus siblings: same trading symbol, or same issuer prefix
    (ISIN characters 1-7) -- candidates for an ISIN change, never proof of one (rule 10)."""
    rows = conn.execute(
        "SELECT isin FROM security WHERE isin = %s OR (symbol IS NOT NULL AND symbol = %s) "
        "OR left(isin, 7) = left(%s, 7)",
        (isin, symbol, isin),
    ).fetchall()
    return sorted({r[0] for r in rows} | {isin})


def _price_dates(conn: Any, isin: str | None) -> list[date]:
    if not isin:
        return []
    rows = conn.execute("SELECT DISTINCT price_date FROM price WHERE isin = %s", (isin,))
    return [r[0] for r in rows.fetchall()]


def _depository_isin_changes(
    conn: Any, gap: Gap, request: dict, verified: list, jumps: list, by_url: dict
) -> list:
    """ISIN changes the depository establishes for a verified split (rule 20a/20b) -- see
    ``isin_change_from_depository``. Returns events; registers their evidence in by_url."""
    import hashlib

    from app.engine.gap_resolution import DepositoryRecord, isin_change_from_depository
    from app.ingest.exa_search import ExchangeDocument

    if any(e.type == "ISIN_CHANGE" for e in verified) or not request["related_isins"]:
        return []
    splits = []
    for e in verified:
        jump = jump_for(e, jumps) if e.type == "SPLIT" else None
        if jump is not None:
            splits.append(replace(e, ex_date=jump.on))
    if not splits:
        return []

    def record(isin: str) -> DepositoryRecord | None:
        row = conn.execute(
            "SELECT isin, issuer, description, status, source_url, response_sha256 "
            "FROM security_reference WHERE isin = %s",
            (isin,),
        ).fetchone()
        return DepositoryRecord(*row) if row else None

    new = record(gap.isin)
    out = []
    for sibling in request["related_isins"]:
        old = record(sibling)
        if new is None or old is None:
            continue
        for split in splits:
            event, _why = isin_change_from_depository(
                old, new, split, _price_dates(conn, old.isin), _price_dates(conn, new.isin)
            )
            if event is None:
                continue
            text = json.dumps({"old": old.__dict__, "new": new.__dict__}, sort_keys=True)
            by_url[event.doc_url] = ExchangeDocument(
                url=event.doc_url,
                title="NSDL ISIN master (depository record)",
                published=None,
                text=text,
                sha256=hashlib.sha256(text.encode()).hexdigest(),
            )
            out.append(event)
            break
    return out


def _handovers(
    conn: Any, isin: str, cluster: list[str], start: date, end: date
) -> list[dict[str, str]]:
    """Sibling ISINs the bhavcopy shows handing over to or from ``isin`` inside the gap's
    window -- one stops trading before the other starts, the shape
    ``isin_handover_observed`` later requires of a researched ISIN change. A sibling that
    traded side by side is another instrument of the same issuer, not a predecessor; a
    handover outside the window cannot explain this gap."""
    own = _price_dates(conn, isin)
    out = []
    for sibling in cluster:
        theirs = _price_dates(conn, sibling) if sibling != isin else []
        if not own or not theirs:
            continue
        if max(theirs) < min(own):
            old, new, old_last, new_first = sibling, isin, max(theirs), min(own)
        elif max(own) < min(theirs):
            old, new, old_last, new_first = isin, sibling, max(own), min(theirs)
        else:
            continue
        if not start <= new_first <= end + timedelta(days=JUMP_DATE_TOLERANCE_DAYS):
            continue
        out.append(
            {
                "old_isin": old,
                "new_isin": new,
                "old_last_traded": old_last.isoformat(),
                "new_first_traded": new_first.isoformat(),
            }
        )
    return out


def build_request(conn: Any, gap: Gap, meta: dict) -> tuple[dict[str, Any], list]:
    cluster = _cluster(conn, gap.isin, meta.get("symbol"))
    # The window opens at the account's first trade under ANY of the issuer's ISINs: a
    # gap on the new ISIN (GUJTHEM, sold 2025) is explained by events that happened while
    # the shares sat under the old one (split 2023, bonus 2024).
    first = conn.execute(
        "SELECT min(txn_date) FROM transaction WHERE isin = ANY(%s) AND account_id = %s",
        (cluster, gap.account_id),
    ).fetchone()[0]
    start = min(first or gap.first_txn, gap.first_txn) - timedelta(days=LOOKBACK_DAYS)
    closes = conn.execute(
        "SELECT price_date, close FROM price WHERE isin = ANY(%s) "
        "AND price_date BETWEEN %s AND %s ORDER BY price_date",
        (cluster, start, gap.first_negative),
    ).fetchall()
    # One close per day across the cluster: an ISIN change hands over day to day, so two
    # ISINs never both trade on the same session in practice; keep the first if they do.
    per_day: dict[date, Decimal] = {}
    for on, close in closes:
        per_day.setdefault(on, Decimal(close))
    jumps = find_price_jumps(list(per_day.items()))
    handovers = _handovers(conn, gap.isin, cluster, start, gap.first_negative)
    # Only what the exchange's own prices make possible. A bonus or split the verifier
    # cannot pin to a price jump is always refused, and so is an ISIN change with no
    # handover -- so with neither, there is nothing research could ever write, and no
    # search or model call is made (``research_gap``).
    needed = (["ISIN_CHANGE"] if handovers else []) + (["BONUS", "SPLIT"] if jumps else [])
    request = {
        "company": meta.get("name"),
        "symbol": meta.get("symbol"),
        "isin": gap.isin,
        "related_isins": [i for i in cluster if i != gap.isin],
        "shares_missing": str(gap.shortfall),
        "window": {"from": start.isoformat(), "to": gap.first_negative.isoformat()},
        "observed_price_jumps": [
            {
                "date": j.on.isoformat(),
                "close_before": str(j.cum),
                "close_after": str(j.ex),
                "ratio": str(round(j.ratio, 3)),
            }
            for j in jumps
        ],
        "observed_isin_handovers": handovers,
        "prices_on_file": len(per_day),
        "needed": needed,
    }
    return request, jumps


def _queries(request: dict[str, Any]) -> list[str]:
    """Search phrases: a bonus and a split phrase per observed price jump, and an ISIN
    change phrase per observed handover -- each dated by the bhavcopy's own evidence.

    No date FILTER: Exa carries no publication date for BSE corporate filings, so a date
    window silently drops exactly the documents this path exists to find (checked live,
    2026-10-08). The month is put in the query text to rank instead. Dates are never taken
    from search metadata anyway -- the event date must sit on the bhavcopy's own price jump.
    """
    company = request["company"] or request["symbol"]
    out: list[str] = []
    for jump in request["observed_price_jumps"]:
        when = date.fromisoformat(jump["date"]).strftime("%B %Y")
        out.append(f"{company} bonus issue ratio record date {when}")
        out.append(f"{company} sub-division of equity shares face value {when}")
    for handover in request["observed_isin_handovers"]:
        when = date.fromisoformat(handover["new_first_traded"]).strftime("%B %Y")
        out.append(f"{company} change in ISIN new ISIN {when}")
    return list(dict.fromkeys(out))


def research_gap(
    conn: Any, account_id: str, gap: Gap, meta: dict, *, exa: Any, llm: Any
) -> dict[str, Any]:
    """Try to close one gap. Returns what happened; commits only a reconciling result."""
    from app.db.repository import load_transactions
    from app.ingest.corporate_action_llm import extract_events, review_event

    request, jumps = build_request(conn, gap, meta)
    if not request["needed"]:
        # Nothing in the exchange's prices that a bonus, split or ISIN change could explain:
        # no search, no model call -- anything they found would be refused by the verifier.
        return {
            "isin": gap.isin,
            "status": "open",
            "reason": (
                "exchange prices show no bonus, split or ISIN change in this period -- "
                "most likely a purchase missing from the imported history"
                if request["prices_on_file"]
                else "no exchange prices on file for this period yet, so no corporate "
                "action can be checked"
            ),
            "request": request,
        }
    documents, seen = [], set()
    for query in _queries(request):
        taken = 0
        for doc in exa.search(query):
            # BSE serves one filing under /AttachHis/ and /attachhis/.
            if doc.url.lower() in seen or taken >= DOCS_PER_QUERY:
                continue
            if len(documents) >= MAX_DOCUMENTS:
                break
            seen.add(doc.url.lower())
            documents.append(doc)
            taken += 1
    if not documents:
        return {
            "isin": gap.isin,
            "status": "open",
            "reason": "no exchange document found",
            "request": request,
        }

    by_url = {d.url: d for d in documents}
    company = tuple(
        x
        for x in (
            *_cluster(conn, gap.isin, meta.get("symbol")),
            meta.get("symbol"),
            meta.get("name"),
        )
        if x
    )
    verified, rejected = [], []
    for event in extract_events(llm, request, documents):
        problem = check_event_against_document(event, by_url[event.doc_url].text, company)
        if problem is None and event.type in ("BONUS", "SPLIT") and jump_for(event, jumps) is None:
            problem = "no matching price discontinuity in the exchange bhavcopy"
        if problem is None and event.type == "ISIN_CHANGE":
            problem = isin_handover_observed(
                _price_dates(conn, event.old_isin),
                _price_dates(conn, event.new_isin),
                event.ex_date,
            )
        (
            rejected.append({"type": event.type, "reason": problem})
            if problem
            else verified.append(event)
        )

    def still_open(reason: str, **extra: Any) -> dict[str, Any]:
        return {
            "isin": gap.isin,
            "status": "open",
            "request": request,
            "reason": reason,
            "rejected": rejected,
            **extra,
        }

    derived = _depository_isin_changes(conn, gap, request, verified, jumps, by_url)
    verified += derived

    transactions = load_transactions(conn, account_id=account_id)
    verified, conflict = consolidate_events(verified, jumps, transactions, gap.isin)
    if conflict:
        return still_open(f"documents disagree -- left open (rule 20c): {conflict}")
    if not verified or not events_close_gap(transactions, gap.isin, verified):
        return still_open(
            "verified events do not close the gap exactly", verified=[e.type for e in verified]
        )
    for event in verified:
        if event.doc_url in {d.doc_url for d in derived}:
            continue  # depository data checked by code, not a model reading to re-read
        veto = review_event(llm, event, by_url[event.doc_url], meta.get("name") or gap.isin)
        if veto:
            return still_open(f"{event.type} {event.ex_date}: {veto}")

    try:
        _write(conn, account_id, gap.isin, verified, jumps, by_url)
        from app.engine.holdings import compute_holdings_partitioned

        after = load_transactions(conn, account_id=account_id)
        broken = {s.isin for s in compute_holdings_partitioned(after)[1]}
        final = next((e.new_isin for e in verified if e.type == "ISIN_CHANGE"), gap.isin)
        if final in broken or gap.isin in broken:
            raise RuntimeError("account still does not reconcile after applying the events")
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.warning("gap_research_write_rolled_back", exc_info=True)
        return {"isin": gap.isin, "status": "open", "reason": f"rolled back: {exc}"}
    return {
        "isin": gap.isin,
        "status": "resolved",
        "events": [
            {"type": e.type, "ex_date": e.ex_date.isoformat(), "source": e.doc_url}
            for e in verified
        ],
    }


def _write(
    conn: Any, account_id: str, gap_isin: str, events: list, jumps: list, by_url: dict
) -> None:
    from app.db import corporate_actions as ca
    from app.db.security_alias import SecurityAlias, add_alias
    from app.engine.corporate_actions import CorpActionType, CorporateAction, Verification

    def evidence(event) -> dict:
        doc = by_url[event.doc_url]
        return {
            "resolution": AUTO_RESOLVED,
            "url": doc.url,
            "sha256": doc.sha256,
            "title": doc.title,
            "quote": event.quote,
        }

    for event in sorted(events, key=lambda e: (e.ex_date, e.type != "ISIN_CHANGE")):
        if event.type == "ISIN_CHANGE":
            conn.execute(
                "INSERT INTO security (isin, symbol, name, exchange, asset_class, sector) "
                "SELECT %s, symbol, name, exchange, asset_class, sector FROM security "
                "WHERE isin = %s ON CONFLICT (isin) DO NOTHING",
                (event.new_isin, event.old_isin),
            )
            add_alias(
                conn,
                SecurityAlias(
                    old_isin=event.old_isin,
                    current_isin=event.new_isin,
                    effective_date=event.ex_date,
                    source=SOURCE_LABEL,
                    source_url=event.doc_url,
                    verification=Verification.VERIFIED,
                    notes=event.quote[:500],
                ),
            )
            conn.execute(
                "INSERT INTO security_alias_evidence VALUES (%s,%s,%s,%s::jsonb,now()) "
                "ON CONFLICT (old_isin) DO NOTHING",
                (event.old_isin, event.new_isin, event.ex_date, json.dumps(evidence(event))),
            )
            continue
        jump = jump_for(event, jumps)
        action_id = ca.add_corporate_action(
            conn,
            CorporateAction(
                isin=event.new_isin or gap_isin,
                action_type=CorpActionType.BONUS if event.type == "BONUS" else CorpActionType.SPLIT,
                ex_date=event.ex_date,
                bonus_new=event.bonus_new,
                bonus_for=event.bonus_for,
                face_value_before=event.fv_before,
                face_value_after=event.fv_after,
                price_cum=jump.cum,
                price_ex=jump.ex,
                verification=Verification.VERIFIED,
                source=SOURCE_LABEL,
                source_url=event.doc_url,
                notes=event.quote[:500],
            ),
        )
        conn.execute(
            "INSERT INTO reference_action_evidence VALUES (%s,%s::jsonb,now()) "
            "ON CONFLICT (action_id) DO UPDATE SET evidence=EXCLUDED.evidence",
            (action_id, json.dumps(evidence(event))),
        )
    ca.apply_known_actions_for_account(conn, account_id)
