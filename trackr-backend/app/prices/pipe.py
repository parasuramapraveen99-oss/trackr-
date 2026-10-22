"""The price pipe: fetch both exchanges, merge, report.

A separate pipe from ingestion, on a separate schedule. On a day with no trades the price
job still runs and the dashboard still moves; on a day with no prices, ingestion is
unaffected. Neither blocks the other, which is why they are not the same code path.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from app.prices.alerts import LoggingNotifier, Notifier
from app.prices.sources import FetchFailed, NotPublished, bse_source, nse_source
from app.prices.udiff import BhavcopyFormatError, BhavcopyRow, parse

#: DATA_MODEL: NSE wins on conflict, for deeper liquidity. Order matters here and nowhere
#: else -- the first source to supply an ISIN is the one whose close is kept.
SOURCE_PRIORITY = ("NSE", "BSE")

SOURCE_LABEL = {"NSE": "NSE_BHAVCOPY", "BSE": "BSE_BHAVCOPY"}

#: The backfill floor this pipe will attempt. NOT the same thing as
#: app.prices.sources.NSE_CURRENT_FORMAT_SINCE (2024-01-01) any more -- nse_source()
#: (returns an app.prices.sources.TieredBhavcopySource) now falls back to NSE's legacy pre-2024
#: archive automatically, confirmed live against a real fetched 17-Oct-2023 file. This
#: floor is a separate, deliberately conservative choice: it matches the ISIN-mandated
#: settlement era and is a date this session actually stands behind, not a guess at how
#: far back the legacy archive really goes (WebSearch suggests NSE's own archive reaches
#: 1994; untested here -- lowering this further is a real, valid follow-up once someone
#: verifies a file that old still parses, not a limitation of the mechanism itself).
NSE_BHAVCOPY_EARLIEST = date(2015, 4, 1)


@dataclass(frozen=True, slots=True)
class MergedPrice:
    isin: str
    price_date: date
    close: Decimal
    source: str
    also_quoted_by: tuple[str, ...] = ()
    #: The winning feed's trading symbol. Carried so a security row can be created for a
    #: universe member the ledger has never seen -- NOT the input to symbol refresh, which
    #: takes NSE only (see PipeResult.nse_symbols).
    symbol: str | None = None


@dataclass
class PipeResult:
    price_date: date
    prices: list[MergedPrice] = field(default_factory=list)
    fetched: dict[str, int] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    not_published: list[str] = field(default_factory=list)
    #: ISIN -> NSE trading symbol. NSE ONLY, deliberately: BSE's legacy layout maps its
    #: symbol column to SC_NAME, which is the company NAME, and app.prices.sources
    #: re-shapes that into the UDiFF symbol field before it reaches the parser. Feeding
    #: that to a symbol refresh would write "GREAT EASTERN SHIPPING CO.LTD." into a column
    #: whose only consumer builds a Yahoo ticker out of it. NSE's SYMBOL is a real ticker
    #: in both the current and legacy layouts, so it is the one safe source.
    nse_symbols: dict[str, str] = field(default_factory=dict)
    #: ISIN -> (exchange, that exchange's own symbol): where an indicative quote for the
    #: ISIN is looked up (app.db.quote_mapping). Same priority as the price merge, so the
    #: quote and the official close come from one exchange. A BSE row counts only from a
    #: real UDiFF file (instrument id present) -- never legacy, whose symbol is a NAME.
    listings: dict[str, tuple[str, str]] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        """A run is a success only if BOTH exchanges answered.

        Deliberately strict. One feed alone still produces prices for most holdings, which
        is exactly what makes a half-failure dangerous: it looks fine until the one
        security that only lists on the missing exchange is valued at nothing.
        """
        return not self.failures

    @property
    def only_one_exchange(self) -> bool:
        return len(self.fetched) == 1 and bool(self.failures)


def merge(rows_by_exchange: dict[str, list[BhavcopyRow]]) -> list[MergedPrice]:
    """Merge on ISIN. NSE wins where both quote; a single-feed ISIN is used as-is.

    That last clause is the entire reason both exchanges are fetched. In this portfolio
    KALYANI-M (INE0N6U01018) appears only on BSE and KDL-SM (INE0O4R01018) only on NSE, so
    either single-exchange pipe loses a real holding -- a different one each way.
    """
    merged: dict[str, MergedPrice] = {}
    for exchange in SOURCE_PRIORITY:
        for row in rows_by_exchange.get(exchange, []):
            existing = merged.get(row.isin)
            if existing is None:
                merged[row.isin] = MergedPrice(
                    isin=row.isin,
                    price_date=row.price_date,
                    close=row.close,
                    source=SOURCE_LABEL.get(exchange, exchange),
                    symbol=row.symbol or None,
                )
            else:
                # Kept so "which feed did this number come from?" stays answerable.
                merged[row.isin] = MergedPrice(
                    isin=existing.isin,
                    price_date=existing.price_date,
                    close=existing.close,
                    source=existing.source,
                    also_quoted_by=(*existing.also_quoted_by, exchange),
                    symbol=existing.symbol,
                )
    return sorted(merged.values(), key=lambda p: p.isin)


def listings(rows_by_exchange: dict[str, list[BhavcopyRow]]) -> dict[str, tuple[str, str]]:
    """ISIN -> (exchange, symbol) a quote provider can be asked for, in SOURCE_PRIORITY."""
    out: dict[str, tuple[str, str]] = {}
    for exchange in SOURCE_PRIORITY:
        for row in rows_by_exchange.get(exchange, []):
            symbol = row.symbol.strip()
            if not symbol or row.isin in out:
                continue
            if exchange == "BSE" and not row.instrument_id:
                continue
            out[row.isin] = (exchange, symbol)
    return out


def fetch_day(
    on: date,
    sources: list[Any] | None = None,
    notifier: Notifier | None = None,
) -> PipeResult:
    """Fetch both exchanges for one date and merge.

    A missing file (weekend, holiday) is recorded and NOT alerted. A fetch that should have
    worked and did not IS alerted, on every failure path, without exception.
    """
    sources = sources if sources is not None else [nse_source(), bse_source()]
    notifier = notifier or LoggingNotifier()

    result = PipeResult(price_date=on)
    rows_by_exchange: dict[str, list[BhavcopyRow]] = {}

    for source in sources:
        try:
            rows = parse(source.fetch(on), source.exchange)
        except NotPublished:
            result.not_published.append(source.exchange)
            continue
        except (FetchFailed, BhavcopyFormatError) as exc:
            result.failures[source.exchange] = str(exc)
            notifier.notify(
                subject=f"{source.exchange} bhavcopy unavailable for {on}",
                detail=str(exc),
                context={"exchange": source.exchange, "date": on.isoformat()},
            )
            continue
        rows_by_exchange[source.exchange] = rows
        result.fetched[source.exchange] = len(rows)

    result.prices = merge(rows_by_exchange)
    result.nse_symbols = {
        row.isin: row.symbol.strip()
        for row in rows_by_exchange.get("NSE", [])
        if row.symbol and row.symbol.strip()
    }
    result.listings = listings(rows_by_exchange)

    if result.only_one_exchange:
        notifier.notify(
            subject=f"Only one exchange answered for {on}",
            detail=(
                "Prices were stored from a single feed. Any holding listed solely on the "
                "missing exchange has NO price today and will raise rather than be valued."
            ),
            context={"fetched": result.fetched, "failed": list(result.failures)},
        )
    return result


def trading_days(start: date, end: date, sessions: Iterable[date] = ()) -> list[date]:
    """Weekdays in range, plus any weekend ``sessions`` known to have traded. Exchange
    holidays still 404 and are handled as NotPublished.

    Not a holiday calendar: one would need maintaining every year, and getting it wrong
    means either missing a real trading day or alerting on a real holiday. Letting the
    exchange's own 404 be the answer is self-maintaining. Weekend special sessions (the
    1 February 2026 Budget Sunday) cannot be found that way -- nobody asks for a Sunday --
    so callers pass ``app.db.index_levels.weekend_sessions``: the dates an index close
    proves were traded. This function stays storage-free.
    """
    extra = {d for d in sessions if start <= d <= end}
    days, cursor = [], start
    while cursor <= end:
        if cursor.weekday() < 5 or cursor in extra:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def backfill_days(
    start: date,
    end: date,
    already_present: set[date] = frozenset(),
    sources: list[Any] | None = None,
    notifier: Notifier | None = None,
    sessions: Iterable[date] = (),
) -> list[PipeResult]:
    """``fetch_day`` for every trading day in ``[start, end]`` that isn't already stored.

    ``start`` is clamped to ``NSE_BHAVCOPY_EARLIEST``. That constant is this system's
    own verified floor, NOT the archives' true limit -- both exchanges serve older data
    than it admits, and lowering it is a follow-up that needs a verified fetch at the
    older date rather than an edit to the number. ``already_present`` (typically
    ``app.db.prices.price_dates_settled``) is what makes a multi-year backfill resumable;
    this function stays storage-free like ``fetch_day``, so the caller decides what
    "already stored" means and does the upserting itself.
    """
    clamped_start = max(start, NSE_BHAVCOPY_EARLIEST)
    days = [d for d in trading_days(clamped_start, end, sessions) if d not in already_present]
    return [fetch_day(d, sources=sources, notifier=notifier) for d in days]


def main(on: date | None = None) -> int:
    """The daily run, as docs/INGESTION.md documents it: ``python -m prices.pipe``.

    Stores ISINs in the ledger plus the ranking universe. ``price.isin`` is a foreign key
    to ``security``, so upserting a whole 5,000-row bhavcopy would fail on the first scrip
    with no security row -- and storing prices nothing reads is work with no reader. What
    to keep, and the symbol refresh that rides along with it, is ``app.prices.store``: one
    implementation, shared with the cron path, so the two cannot drift.

    Exit code is the cron contract: non-zero when an exchange that should have answered
    did not. A weekend or holiday is NotPublished, which is not a failure and must not
    page anyone -- alerting on holidays trains whoever reads the alerts to ignore them.
    """
    from app.config import get_settings
    from app.db.connection import connect
    from app.prices.alerts import get_notifier
    from app.prices.store import store_day

    result = fetch_day(on or date.today(), notifier=get_notifier(get_settings()))
    with connect() as conn:
        stored = store_day(conn, result)

    renamed = f"  renamed={len(stored.symbol_changes)}" if stored.symbol_changes else ""
    print(
        f"{result.price_date}  fetched={result.fetched or '-'}"
        f"  stored={stored.prices_stored}{renamed}"
        f"  not_published={result.not_published or '-'}  failed={list(result.failures) or '-'}"
    )
    return 0 if result.succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
