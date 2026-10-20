"""Range-scoped snapshot inputs: SQL per batch, valuation per day.

All caches belong to one synchronous rebuild and are discarded before another
revision is processed. Broker rows, date-effective identity evidence and the existing
financial engine remain authoritative. Nothing here fetches from an external source.
"""

from __future__ import annotations

from bisect import bisect_right
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, timedelta

from app.db import repository
from app.db.corporate_actions import list_corporate_actions
from app.db.fund_reference import non_exchange_isins
from app.db.prices import price_dates_settled
from app.db.security_alias import list_aliases, resolve_alias_map
from app.engine.corporate_actions import Verification
from app.engine.holdings import MAX_PRICE_STALENESS_DAYS
from app.engine.snapshot import prepare_snapshot, value_prepared_snapshot

_active: ContextVar[SnapshotBatch | None] = ContextVar("snapshot_batch", default=None)


class SnapshotBatch:
    def __init__(self, conn, start, end, queued=False):
        self.conn, self.start, self.end = conn, start, end
        self.queued = queued
        self.clear()

    def clear(self):
        self._settled = None
        self._scopes = None
        self._existing = {}
        self._inputs = {}
        self._references = None
        self.ledger_loads = self.state_replays = 0

    @property
    def settled(self):
        if self._settled is None:
            self._settled = price_dates_settled(self.conn, self.start, self.end)
        return self._settled

    def missing_scopes(self, on):
        if self._scopes is None:
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT a.investor_id::text,a.is_advised,min(t.txn_date) "
                    "FROM transaction t JOIN account a ON a.id=t.account_id GROUP BY 1,2"
                )
                self._scopes = {}
                for investor, advised, first in cur.fetchall():
                    key = (investor, False)
                    self._scopes[key] = min(first, self._scopes.get(key, first))
                    if advised:
                        self._scopes[(investor, True)] = first
                cur.execute(
                    "SELECT investor_id::text,advised_only,as_of FROM portfolio_snapshot "
                    "WHERE as_of BETWEEN %s AND %s",
                    (self.start, self.end),
                )
                for investor, advised, saved in cur.fetchall():
                    self._existing.setdefault((investor, advised), set()).add(saved)
        return [
            key
            for key, first in self._scopes.items()
            if first <= on and on not in self._existing.get(key, set())
        ]

    def saved(self, investor, advised, on):
        self._existing.setdefault((investor, advised), set()).add(on)

    def _load(self, investor, advised):
        key = (investor, advised)
        self.prefetch({key})
        return self._inputs[key]

    def prefetch(self, scopes):
        """Fetch a bounded claim's inputs together, independent of portfolio count."""
        missing = set(scopes) - self._inputs.keys()
        if not missing:
            return
        if self._references is None:
            self._references = (
                list_aliases(self.conn, Verification.VERIFIED),
                list_corporate_actions(self.conn),
            )
        ledgers = repository.load_snapshot_ledgers(self.conn, missing)
        ranges = {}
        if self.queued:
            rows = self.conn.execute(
                "SELECT payload->>'investor_id',payload->>'advised_only',payload->>'on' "
                "FROM reference_sync_task WHERE kind='SNAPSHOT' AND state<>'done' "
                "AND payload->>'investor_id'=ANY(%s)",
                (sorted({investor for investor, _ in missing}),),
            ).fetchall()
            for investor, advised, value in rows:
                key = (investor, advised == "true")
                if key not in missing:
                    continue
                try:
                    on = date.fromisoformat(value)
                except (ValueError, TypeError):
                    continue
                lo, hi = ranges.get(key, (on, on))
                ranges[key] = min(lo, on), max(hi, on)
        new = {}
        for key, raw in ledgers.items():
            data = self._prepare_inputs(raw)
            data["start"], data["end"] = ranges.get(key, (self.start, self.end))
            new[key] = data
        self._refresh_prices(new.values())
        self._inputs.update(new)
        self.ledger_loads += len(new)

    def _prepare_inputs(self, raw):
        aliases, actions = self._references
        # A ledger is loaded once, including immutable anchors whose action revision
        # moved its effect. Filtering raw txn_date here would lose those corrections.
        isins = {t.isin for t in raw}
        expanded = set(isins)
        for _ in aliases:
            for alias in aliases:
                if alias.old_isin in expanded:
                    expanded.add(alias.current_isin)
        # Include revised derived-effect identifiers, even when the immutable anchor
        # names an older action's identifier.
        tagged = {(t.raw_payload or {}).get("auto_corporate_action_id") for t in raw}
        relevant_actions = actions if any(tagged) else None
        if relevant_actions is not None:
            for action in actions:
                if action.id in tagged:
                    expanded.add(action.isin)
                    if action.resulting_isin:
                        expanded.add(action.resulting_isin)
        for _ in aliases:
            for alias in aliases:
                if alias.old_isin in expanded:
                    expanded.add(alias.current_isin)
        boundaries = sorted(
            {t.txn_date for t in raw}
            | {a.effective_date for a in aliases}
            | {a.ex_date for a in actions if a.id in tagged}
        )
        return {
            "raw": raw,
            "aliases": aliases,
            "actions": relevant_actions,
            "isins": expanded,
            "boundaries": boundaries,
            "epoch": None,
            "state": None,
            "start": self.start,
            "end": self.end,
        }

    def _refresh_prices(self, inputs):
        inputs = list(inputs)
        if not inputs:
            return
        floor = min(data["start"] for data in inputs) - timedelta(days=MAX_PRICE_STALENESS_DAYS)
        end = max(data["end"] for data in inputs)
        isins = sorted(set().union(*(data["isins"] for data in inputs)))
        off_exchange = set(non_exchange_isins(self.conn, isins))
        # Independent reads travel together; fetching each result inside this block
        # would restore the network round trip per statement.
        with self.conn.pipeline():
            prices = self.conn.execute(
                "SELECT isin,price_date,close FROM price "
                "WHERE isin=ANY(%s) AND price_date BETWEEN %s AND %s ORDER BY isin,price_date",
                (isins, floor, end),
            )
            absent = self.conn.execute(
                "SELECT isin,min(price_date) FROM price_absent "
                "WHERE isin=ANY(%s) AND price_date<=%s GROUP BY isin",
                (isins, end),
            )
            # Identical evidence/ambiguity gates to latest_prices, prefetched once.
            fallback = self.conn.execute(
                """SELECT e.current_isin,e.effective_date,b.price_date,b.close
                   FROM security_alias_evidence e JOIN security_alias a
                     ON a.old_isin=e.old_isin AND a.current_isin=e.current_isin
                     AND a.verification='VERIFIED' AND a.effective_date=e.effective_date
                   JOIN reference_bhavcopy_security b
                     ON b.exchange='NSE' AND b.isin=e.evidence->>'ex_price_isin'
                     AND b.price_date=(e.evidence->'after'->>'date')::date
                     AND b.series=e.evidence->>'series'
                   WHERE e.current_isin=ANY(%s) AND b.price_date BETWEEN %s AND %s""",
                (isins, floor, end),
            )
        series = {}
        for isin, on, close in prices.fetchall():
            series.setdefault(isin, []).append((on, close))
        shared = {
            "prices": series,
            "price_dates": {isin: [on for on, _ in points] for isin, points in series.items()},
            "absent": dict(absent.fetchall()),
            "fallback": fallback.fetchall(),
            "off_exchange": off_exchange,
        }
        for data in inputs:
            data.update(shared)

    def prices_changed(self):
        self._refresh_prices(self._inputs.values())

    def build(self, investor, advised, on):
        data = self._load(str(investor), advised)
        epoch = bisect_right(data["boundaries"], on)
        if data["epoch"] != epoch:
            aliases = resolve_alias_map(data["aliases"], on)
            rows = repository.normalise_transactions(
                data["raw"],
                aliases,
                data["actions"],
                on,
            )
            data["state"] = prepare_snapshot(rows, on)
            data["epoch"] = epoch
            self.state_replays += 1
        state = data["state"]
        prices = {}
        floor = on - timedelta(days=MAX_PRICE_STALENESS_DAYS)
        held = {h.isin for h in state.holdings}
        for isin in held:
            pos = bisect_right(data["price_dates"].get(isin, []), on) - 1
            if pos >= 0:
                observed, close = data["prices"][isin][pos]
                if observed >= floor:
                    prices[isin] = close
        observations = {}
        for isin, effective, observed, close in data["fallback"]:
            if isin in held and isin not in prices and effective <= on and floor <= observed <= on:
                observations.setdefault(isin, set()).add(close)
        prices.update(
            {isin: next(iter(values)) for isin, values in observations.items() if len(values) == 1}
        )
        excluded = held & data["off_exchange"]
        excluded |= {
            isin
            for isin in held - prices.keys() - excluded
            if data["absent"].get(isin, date.max) <= on
        }
        return value_prepared_snapshot(state, prices, on, excluded)


@contextmanager
def snapshot_batch(conn, start, end, queued=False):
    current = _active.get()
    if (
        current is not None
        and current.conn is conn
        and current.start <= start
        and current.end >= end
    ):
        yield current
        return
    batch = SnapshotBatch(conn, start, end, queued=queued)
    token = _active.set(batch)
    try:
        yield batch
    finally:
        _active.reset(token)


def invalidate_batch():
    batch = _active.get()
    if batch is not None:
        batch.clear()
