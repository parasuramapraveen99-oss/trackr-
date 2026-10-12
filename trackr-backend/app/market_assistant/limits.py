"""The daily search ceiling and the public-evidence cache. Section 10 of the plan.

``DailyBudget`` counts in memory by default. Given a ``UsageStore`` it counts there
instead: the router passes ``app.db.market_assistant_usage.PostgresUsageStore`` (Phase 4),
so the ceiling holds across restarts and processes. This package never imports the
database; it only sees the two-method protocol below.

The cache and the per-user rate limit stay process-local. The cache holding public
evidence per process costs at most one extra search per process per TTL, which the shared
ceiling still bounds; the rate limit is abuse control, not spend control.

The cache holds public evidence only, keyed by catalogue topic and policy version. It
never holds a typed question, a user, or anything derived from either.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Generic, Protocol, TypeVar

T = TypeVar("T")


class UsageStore(Protocol):
    """Shared storage for a daily counter. ``reserve`` must be atomic and must return False
    on any failure (fail closed)."""

    def reserve(self, day: date, limit: int) -> bool: ...

    def used(self, day: date) -> int: ...


class DailyBudget:
    """At most ``limit`` searches per UTC day. ``reserve`` is atomic; a refused reserve
    means the search is not made, not that it is queued."""

    def __init__(
        self,
        limit: int,
        today: Callable[[], date] | None = None,
        store: UsageStore | None = None,
    ):
        if limit < 0:
            raise ValueError("limit must be zero or more")
        self.limit = limit
        self._today = today or (lambda: datetime.now(UTC).date())
        self._store = store
        self._lock = threading.Lock()
        self._day: date | None = None
        self._used = 0

    @property
    def shared(self) -> bool:
        return self._store is not None

    def reserve(self) -> bool:
        if self._store is not None:
            return self.limit > 0 and self._store.reserve(self._today(), self.limit)
        with self._lock:
            day = self._today()
            if day != self._day:
                self._day, self._used = day, 0
            if self._used >= self.limit:
                return False
            self._used += 1
            return True

    @property
    def used(self) -> int:
        if self._store is not None:
            return self._store.used(self._today())
        with self._lock:
            return self._used if self._day == self._today() else 0


class TTLCache(Generic[T]):
    def __init__(
        self, ttl_seconds: float, max_entries: int = 64, clock: Callable[[], float] = time.monotonic
    ):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._items: OrderedDict[str, tuple[float, T]] = OrderedDict()

    def get(self, key: str) -> T | None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            stored_at, value = item
            if self._clock() - stored_at > self._ttl:
                del self._items[key]
                return None
            return value

    def put(self, key: str, value: T) -> None:
        with self._lock:
            self._items[key] = (self._clock(), value)
            self._items.move_to_end(key)
            while len(self._items) > self._max:
                self._items.popitem(last=False)


class PerKeyRateLimit:
    """At most ``max_events`` per ``window_seconds`` for each key (a user ID). Sliding
    window, process-local, same caveat as ``DailyBudget``."""

    def __init__(
        self,
        max_events: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ):
        self._max = max_events
        self._window = window_seconds
        self._clock = clock
        self._max_keys = max_keys
        self._lock = threading.Lock()
        self._events: OrderedDict[str, list[float]] = OrderedDict()

    def allow(self, key: str) -> bool:
        with self._lock:
            now = self._clock()
            recent = [t for t in self._events.get(key, []) if now - t < self._window]
            if len(recent) >= self._max:
                self._events[key] = recent
                return False
            recent.append(now)
            self._events[key] = recent
            self._events.move_to_end(key)
            while len(self._events) > self._max_keys:
                self._events.popitem(last=False)
            return True
