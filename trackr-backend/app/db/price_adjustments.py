"""Exchange-discovered price factors for demergers -- analytics series only.

See migration 071 for why the factor is (ex-date discovered open / cum close) and why it
is never the tax cost ratio. Nothing here writes a transaction or reads a holding: the
only consumer is ``app.db.prices.adjusted_price_series``.

Evidence is the exchange's own two dated files, hashed. A notice NSE filed under a
retired ISIN is mapped with ``identity_evidence.traded_identifier`` -- the same rule the
bonus/split resolver uses -- or waits; it is never guessed.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from app.db.identity_evidence import traded_identifier

logger = logging.getLogger("db.price_adjustments")

#: Ten places in storage; a factor carries no more information than its two prices.
_QUANTUM = Decimal("0.0000000001")

#: (isin -> (symbol, series, open, close)), receipt
DayRows = tuple[dict[str, tuple[str, str, Decimal | None, Decimal]], dict[str, str]]


def demerger_factor(price_cum: Decimal, price_discovered: Decimal) -> Decimal | None:
    """Share of value the parent kept. None unless strictly between 0 and 1: a factor of
    1 or more is not a demerger's price effect, and is not something to apply."""
    if price_cum <= 0 or price_discovered <= 0:
        return None
    factor = (price_discovered / price_cum).quantize(_QUANTUM)
    return factor if Decimal(0) < factor < Decimal(1) else None


def _decimal(value: str | None) -> Decimal | None:
    try:
        number = Decimal((value or "").strip())
    except InvalidOperation:
        return None
    return number if number.is_finite() and number > 0 else None


def _rows(text: str, columns: tuple[str, str, str, str, str]) -> dict:
    """ISIN-keyed rows; an ISIN printed twice is ambiguous and dropped, never chosen."""
    isin_col, symbol_col, series_col, open_col, close_col = columns
    out: dict[str, tuple] = {}
    seen_twice: set[str] = set()
    for raw in csv.DictReader(io.StringIO(text)):
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        isin, close = row.get(isin_col, ""), _decimal(row.get(close_col))
        if not isin or close is None:
            continue
        if isin in out:
            seen_twice.add(isin)
        out[isin] = (
            row.get(symbol_col, ""),
            row.get(series_col, ""),
            _decimal(row.get(open_col)),
            close,
        )
    return {k: v for k, v in out.items() if k not in seen_twice}


def nse_day(on: date) -> DayRows:
    """One NSE cash-market session with OPEN prices, which the stored pipe does not keep.

    Raises ``NotPublished`` for a holiday, like every bhavcopy source.
    """
    from app.prices import sources
    from app.prices.udiff import _decompress

    if on >= sources.NSE_CURRENT_FORMAT_SINCE:
        payload = sources.nse_source().fetch(on)
        text = _decompress(payload)
        digest, url = hashlib.sha256(payload).hexdigest(), sources.NSE_URL.format(d=on)
        columns = ("ISIN", "TckrSymb", "SctySrs", "OpnPric", "ClsPric")
    else:
        legacy = sources.legacy_nse_source()
        url = legacy.url_for(on)
        text = sources.fetch_legacy_archive_text(
            "NSE (legacy archive)", url, on, legacy._warmup_url, legacy._opener_factory
        )
        # The archive is a zip; this hashes the CSV inside it, and says so.
        digest = "csv:" + hashlib.sha256(text.encode()).hexdigest()
        columns = ("ISIN", "SYMBOL", "SERIES", "OPEN", "CLOSE")
    return _rows(text, columns), {"date": on.isoformat(), "url": url, "sha256": digest}


def resolve_demerger_adjustments(
    conn: Any, fetch_day: Callable[[date], DayRows] = nse_day
) -> dict[str, int]:
    """Turn observed NSE DEMERGER notices into evidenced price factors. Never raises for
    one notice; each lands or waits on its own, and is retried on the next run."""
    from app.prices.sources import NotPublished

    notices = conn.execute(
        """SELECT DISTINCT o.isin, o.ex_date FROM corporate_action_observation o
           WHERE o.classification = 'DEMERGER' AND o.ex_date <= current_date
             AND o.isin IS NOT NULL AND o.ex_date IS NOT NULL
             AND EXISTS (SELECT 1 FROM "transaction" t WHERE left(t.isin, 9) = left(o.isin, 9))
             AND NOT EXISTS (SELECT 1 FROM price_adjustment p
                             WHERE p.notice_isin = o.isin AND p.ex_date = o.ex_date)"""
    ).fetchall()
    stored = waiting = 0
    for notice_isin, ex_date in notices:
        try:
            after, after_receipt = fetch_day(ex_date)
            before = before_receipt = None
            for offset in range(1, 8):
                on = ex_date - timedelta(days=offset)
                if on.weekday() >= 5:
                    continue
                try:
                    before, before_receipt = fetch_day(on)
                    break
                except NotPublished:
                    continue
            if before is None:
                waiting += 1
                continue
            isin = notice_isin
            if notice_isin not in after or notice_isin not in before:
                as_tuples = lambda rows: [(i, r[0], r[1], r[3]) for i, r in rows.items()]  # noqa: E731
                isin = traded_identifier(notice_isin, as_tuples(before), as_tuples(after))
            if isin is None or isin not in after or isin not in before:
                waiting += 1
                continue
            price_cum, price_discovered = before[isin][3], after[isin][2]
            factor = demerger_factor(price_cum, price_discovered) if price_discovered else None
            known = conn.execute("SELECT 1 FROM security WHERE isin=%s", (isin,)).fetchone()
            if factor is None or known is None:
                waiting += 1
                continue
            evidence = {
                "method": "ex-date special pre-open discovered OPEN / cum-session CLOSE",
                "cum_session": before_receipt,
                "ex_session": after_receipt,
                "symbol": after[isin][0],
                "series_cum": before[isin][1],
                "series_ex": after[isin][1],
                "ex_close": str(after[isin][3]),
            }
            conn.execute(
                """INSERT INTO price_adjustment (isin, ex_date, kind, factor, price_cum,
                       price_discovered, notice_isin, evidence)
                   VALUES (%s,%s,'DEMERGER',%s,%s,%s,%s,%s::jsonb)
                   ON CONFLICT (isin, ex_date, kind) DO NOTHING""",
                (
                    isin,
                    ex_date,
                    factor,
                    price_cum,
                    price_discovered,
                    notice_isin,
                    json.dumps(evidence),
                ),
            )
            conn.commit()
            stored += 1
        except NotPublished:
            waiting += 1
        except Exception:
            conn.rollback()
            waiting += 1
            logger.warning("demerger_adjustment_failed", extra={"isin": notice_isin}, exc_info=True)
    return {"notices": len(notices), "stored": stored, "waiting": waiting}


def factors(conn: Any, isins: list[str], start: date, end: date) -> list[tuple[str, date, Decimal]]:
    """(isin, ex_date, factor) for ex-dates in (start, end]. Raises ``UndefinedTable``
    when migration 071 is not applied -- a deployment fault, not "no demergers"."""
    return conn.execute(
        "SELECT isin, ex_date, factor FROM price_adjustment "
        "WHERE isin = ANY(%s) AND ex_date > %s AND ex_date <= %s",
        (list(isins), start, end),
    ).fetchall()
