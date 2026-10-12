"""External research evidence: one narrow adapter interface, bounded retrieval (BE 29, RP 02).

THIS IS A RESEARCH PATH. It never touches financial ingestion: nothing here imports
``app/ingest``, creates a transaction, parses a contract note or relabels a financial document.
Retained bytes use the private document store with the research-only parse status
(``PARSE_RESEARCH_ONLY``), so a hash-deduplicated financial document is reused without being
relabelled (BE-V04).

NO SOURCE IS APPROVED (BE-Q04 OPEN). ``LIVE_ADAPTERS`` is empty. A source needs a dated verdict
for EACH use -- fetch, store, extract, quote/read, model disclosure, export -- before it can be
registered, and permission for one use is not permission for another. Until then the only
adapter is ``SyntheticAdapter``, which replays RECORDED SYNTHETIC responses from a fixture file
and can only be supplied by a test through the ``get_source_adapters`` dependency. It is not a
provider, and its output is labelled synthetic wherever it lands.

ONE ADAPTER, ONE INTERFACE. There is deliberately no plugin framework, no registry of
hypothetical providers and no configuration surface for adapters that do not exist. When the
first verdict arrives, its concrete module implements ``Adapter`` and is added to
``LIVE_ADAPTERS`` alongside a dated entry in docs/RESEARCH_SOURCES.md; the second verdict is
when a shared abstraction is earned.

WHAT AN ADAPTER RETURNS IS UNVERIFIED. Parsed values become UNVERIFIED facts in session 13's
series; parsed events become UNVERIFIED research_event rows. Only a person's recheck produces
VERIFIED (migrations 049/055). Confidence is not verification.

UNKNOWN PROVENANCE STAYS UNKNOWN. A missing publication date is not today, not the retrieval
time and not an inferred filing deadline: the item is reported as UNKNOWN and not stored as a
source (research_source requires a publication date by design). An event whose date is not
exact carries no date; a claimed (rumoured) date never populates a date field.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from app.research.errors import WorkspaceError

SOURCE_NOT_APPROVED = "SOURCE_NOT_APPROVED"
ADAPTER_UNAVAILABLE = "ADAPTER_UNAVAILABLE"
REFRESH_TOO_LARGE = "REFRESH_TOO_LARGE"

#: Per-request bounds. An unbounded historical sweep is a separate, explicit operation.
MAX_PERIODS = 10
MAX_ITEMS = 60
MAX_ITEM_BYTES = 25 * 1024 * 1024
MAX_FACTS_PER_ITEM = 400
MAX_EVENTS_PER_ITEM = 50
#: Transient failures: at most two retries per item fetch, with backoff (BUTLER_REPORTS §3).
MAX_RETRIES = 2

EVENT_TYPES = frozenset({
    "BOARD_MEETING", "FINANCIAL_RESULTS", "AGM", "EGM", "DIVIDEND", "BONUS", "SPLIT", "BUYBACK",
    "RIGHTS_ISSUE", "MERGER_OR_DEMERGER", "AUDITOR_CHANGE", "CREDIT_RATING", "REGULATORY_ORDER",
    "MANAGEMENT_CHANGE",
    # 057: an earnings call is its own event, not the results filing it discusses (BE 37).
    "EARNINGS_CALL",
})
EVENT_STATUSES = frozenset({"SCHEDULED", "OBSERVED", "CLAIMED", "CANCELLED"})
#: Source types whose events can only ever be CLAIMS -- they report what somebody else said.
CLAIM_ONLY_SOURCE_TYPES = frozenset({"NEWS"})


class TransientAdapterError(Exception):
    """A retryable failure (timeout, 5xx, connection reset)."""


class PermanentAdapterError(Exception):
    """Not retryable (404, malformed response, refused by policy)."""


# =========================================================================================
# what an adapter returns
# =========================================================================================


@dataclass(frozen=True)
class ExtractedFact:
    metric: str
    basis: str
    unit: str
    cadence: str
    period_start: date | None
    period_end: date | None
    #: Decimal STRING exactly as extracted. Never a float, never rescaled here.
    value: str
    locator: str


@dataclass(frozen=True)
class ExtractedEvent:
    #: Stable id of the event at its origin, so a repeat refresh is a no-op.
    origin_event_id: str
    event_type: str | None
    status: str
    event_date: date | None
    date_precision: str
    period_start: date | None
    period_end: date | None
    description: str
    locator: str
    #: origin_event_id of an earlier SCHEDULED event this one moves or cancels.
    reschedules_origin_event_id: str | None = None


@dataclass(frozen=True)
class RetrievedItem:
    origin_item_id: str
    origin_url: str
    source_type: str
    title: str
    #: None = UNKNOWN. Never defaulted to the retrieval date.
    publication_date: date | None
    period_label: str | None
    retrieved_at: datetime
    content: bytes | None
    media_type: str | None
    facts: tuple[ExtractedFact, ...] = ()
    events: tuple[ExtractedEvent, ...] = ()

    @property
    def content_sha256(self) -> str | None:
        return hashlib.sha256(self.content).hexdigest() if self.content is not None else None


class Adapter(Protocol):
    adapter_id: str
    #: The research_source types this adapter can produce.
    source_types: frozenset[str]
    #: True for a fixture replay. Synthetic output is labelled and never live evidence.
    synthetic: bool
    #: Minimum seconds between two fetches (the provider's published rate limit).
    min_interval_seconds: float

    def fetch(self, isin: str, periods: Sequence[str]) -> list[RetrievedItem]: ...


#: NO ADAPTER HAS A VERDICT. See docs/RESEARCH_SOURCES.md before adding one.
LIVE_ADAPTERS: dict[str, Adapter] = {}


# =========================================================================================
# the one adapter that exists: recorded synthetic responses
# =========================================================================================


def _d(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


class SyntheticAdapter:
    """Replays a recorded SYNTHETIC response file. Test-only; never in ``LIVE_ADAPTERS``.

    File shape: ``{"adapter_id": "...", "responses": {"<ISIN>": [item, ...]}}`` where each item
    mirrors ``RetrievedItem`` (content as UTF-8 text). ``fail_first`` makes the first N fetches
    raise ``TransientAdapterError`` so retry/backoff can be exercised deterministically.
    """

    synthetic = True
    min_interval_seconds = 0.0

    def __init__(self, path: Path | str, adapter_id: str = "synthetic.fixture",
                 fail_first: int = 0, variant: str = "responses") -> None:
        self.path = Path(path)
        self.adapter_id = adapter_id
        self._fail = fail_first
        self.calls = 0
        body = json.loads(self.path.read_text(encoding="utf-8"))
        #: ``variant`` picks a recorded response set, e.g. "revised" replays the same origin
        #: items with changed bytes, to exercise corrections.
        self._responses: dict[str, list[dict[str, Any]]] = body.get(variant, {})
        self.source_types = frozenset(body.get("declared_source_types", [])) or frozenset(
            i["source_type"] for items in self._responses.values() for i in items)

    def fetch(self, isin: str, periods: Sequence[str]) -> list[RetrievedItem]:
        self.calls += 1
        if self._fail > 0:
            self._fail -= 1
            raise TransientAdapterError("synthetic transient failure")
        items = []
        for raw in self._responses.get(isin, []):
            if periods and raw.get("period_label") not in periods \
                    and raw.get("period_label") is not None:
                continue
            content = raw.get("content")
            items.append(RetrievedItem(
                origin_item_id=raw["origin_item_id"],
                origin_url=raw["origin_url"],
                source_type=raw["source_type"],
                title=raw["title"],
                publication_date=_d(raw.get("publication_date")),
                period_label=raw.get("period_label"),
                retrieved_at=datetime.fromisoformat(raw["retrieved_at"]),
                content=content.encode("utf-8") if content is not None else None,
                media_type=raw.get("media_type", "text/plain"),
                facts=tuple(ExtractedFact(
                    metric=f["metric"], basis=f["basis"], unit=f["unit"], cadence=f["cadence"],
                    period_start=_d(f.get("period_start")), period_end=_d(f.get("period_end")),
                    value=f["value"], locator=f["locator"]) for f in raw.get("facts", [])),
                events=tuple(ExtractedEvent(
                    origin_event_id=e["origin_event_id"], event_type=e.get("event_type"),
                    status=e["status"], event_date=_d(e.get("event_date")),
                    date_precision=e.get("date_precision", "EXACT" if e.get("event_date")
                                         else "UNKNOWN"),
                    period_start=_d(e.get("period_start")), period_end=_d(e.get("period_end")),
                    description=e["description"], locator=e["locator"],
                    reschedules_origin_event_id=e.get("reschedules_origin_event_id"))
                    for e in raw.get("events", [])),
            ))
        return items


# =========================================================================================
# bounded retrieval
# =========================================================================================

_last_fetch: dict[str, float] = {}
_fetch_lock = threading.Lock()


def _respect_rate(adapter: Adapter, sleep: Callable[[float], None]) -> None:
    with _fetch_lock:
        now = time.monotonic()
        wait = _last_fetch.get(adapter.adapter_id, 0.0) + adapter.min_interval_seconds - now
        if wait > 0:
            sleep(wait)
        _last_fetch[adapter.adapter_id] = time.monotonic()


def retrieve(adapter: Adapter, isin: str, periods: Sequence[str],
             sleep: Callable[[float], None] = time.sleep) -> list[RetrievedItem]:
    """One bounded fetch with at most ``MAX_RETRIES`` retries on a transient failure,
    exponential backoff, and the adapter's rate limit. Size bounds are enforced on the result;
    an oversized response is refused, never truncated into apparent completeness."""
    if len(periods) > MAX_PERIODS:
        raise WorkspaceError(422, REFRESH_TOO_LARGE,
                             f"at most {MAX_PERIODS} periods per refresh",
                             {"periods": "TOO_MANY"})
    attempt = 0
    while True:
        _respect_rate(adapter, sleep)
        try:
            items = adapter.fetch(isin, list(periods))
            break
        except TransientAdapterError as exc:
            if attempt >= MAX_RETRIES:
                raise WorkspaceError(503, ADAPTER_UNAVAILABLE,
                                     "the source did not respond; nothing was stored") from exc
            sleep(0.5 * (2 ** attempt))
            attempt += 1
        except PermanentAdapterError as exc:
            raise WorkspaceError(502, ADAPTER_UNAVAILABLE,
                                 "the source refused or returned malformed data; nothing was "
                                 "stored") from exc
    if len(items) > MAX_ITEMS:
        raise WorkspaceError(422, REFRESH_TOO_LARGE,
                             f"the source returned more than {MAX_ITEMS} items; narrow the "
                             "periods", {"periods": "TOO_MANY_ITEMS"})
    for it in items:
        if it.content is not None and len(it.content) > MAX_ITEM_BYTES:
            raise WorkspaceError(422, REFRESH_TOO_LARGE, "an item exceeds the size bound",
                                 {"item": it.origin_item_id})
        if len(it.facts) > MAX_FACTS_PER_ITEM or len(it.events) > MAX_EVENTS_PER_ITEM:
            raise WorkspaceError(422, REFRESH_TOO_LARGE, "an item extracted too many values",
                                 {"item": it.origin_item_id})
        if it.retrieved_at.tzinfo is None:
            raise WorkspaceError(502, ADAPTER_UNAVAILABLE, "adapter returned a naive timestamp")
    return items


# =========================================================================================
# fetch-destination validation (for any future live adapter; RP 02)
# =========================================================================================

#: Media types a research fetch may accept. Nothing executable, no archives of unknown shape.
ALLOWED_MEDIA = frozenset({"application/pdf", "text/html", "text/plain", "application/xml",
                           "text/xml", "text/csv", "application/json"})
MAX_REDIRECTS = 3


class UnsafeDestination(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def check_destination(url: str, allowed_hosts: frozenset[str],
                      resolve: Callable[[str], list[str]] | None = None) -> str:
    """Refuse anything but https to an allowlisted host whose every resolved address is public.

    SSRF defence for adapter fetches and every redirect hop: no credentials in the URL, no IP
    literal, no non-default port, no private / loopback / link-local / multicast / reserved
    address after DNS resolution. ``resolve`` is injectable for tests; the default uses the
    system resolver. Returns the canonical URL or raises ``UnsafeDestination``."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise UnsafeDestination("SCHEME_NOT_HTTPS")
    if parts.username or parts.password:
        raise UnsafeDestination("CREDENTIALS_IN_URL")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsafeDestination("NO_HOST")
    if parts.port not in (None, 443):
        raise UnsafeDestination("PORT_NOT_ALLOWED")
    try:
        ipaddress.ip_address(host)
        raise UnsafeDestination("IP_LITERAL")
    except ValueError:
        pass
    if host not in allowed_hosts:
        raise UnsafeDestination("HOST_NOT_ALLOWLISTED")
    resolver = resolve or (lambda h: [a[4][0] for a in socket.getaddrinfo(h, 443)])
    addresses = resolver(host)
    if not addresses:
        raise UnsafeDestination("UNRESOLVED")
    for a in addresses:
        ip = ipaddress.ip_address(a.split("%", 1)[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified):
            raise UnsafeDestination("NON_PUBLIC_ADDRESS")
    return url


def check_redirect_chain(urls: Sequence[str], allowed_hosts: frozenset[str],
                         resolve: Callable[[str], list[str]] | None = None) -> None:
    """Every hop is validated; more than ``MAX_REDIRECTS`` hops is refused."""
    if len(urls) > MAX_REDIRECTS + 1:
        raise UnsafeDestination("TOO_MANY_REDIRECTS")
    for u in urls:
        check_destination(u, allowed_hosts, resolve)


def check_media(media_type: str | None) -> None:
    base = (media_type or "").split(";", 1)[0].strip().lower()
    if base not in ALLOWED_MEDIA:
        raise UnsafeDestination("MEDIA_NOT_ALLOWED")


# =========================================================================================
# preparing items for storage (pure)
# =========================================================================================


@dataclass
class Prepared:
    """What the repository will store for one item, and every honest UNKNOWN on the way."""

    item: RetrievedItem
    store_bytes: bool
    unknowns: list[dict[str, Any]] = field(default_factory=list)


def prepare(items: Sequence[RetrievedItem], policy: Any, adapter: Adapter
            ) -> tuple[list[Prepared], list[dict[str, Any]]]:
    """Apply per-source, per-use verdicts and provenance rules before anything is written.

    METADATA is required to record a source at all; STORE decides whether bytes are retained;
    EXTRACT decides whether parsed facts/events may be recorded from them. An item without a
    publication date is reported UNKNOWN and not stored."""
    from app.research.source_policy import USE_EXTRACT, USE_METADATA, USE_STORE

    prepared: list[Prepared] = []
    unknowns: list[dict[str, Any]] = []
    for it in items:
        base = {"origin_item_id": it.origin_item_id, "source_type": it.source_type}
        if it.source_type not in adapter.source_types:
            unknowns.append({**base, "code": "SOURCE_TYPE_NOT_DECLARED"})
            continue
        if not policy.allows(it.source_type, USE_METADATA):
            unknowns.append({**base, "code": SOURCE_NOT_APPROVED, "use": USE_METADATA})
            continue
        if it.publication_date is None:
            unknowns.append({**base, "code": "PUBLICATION_DATE_UNKNOWN"})
            continue
        if it.publication_date > it.retrieved_at.astimezone(UTC).date() and \
                it.publication_date > datetime.now(UTC).date():
            unknowns.append({**base, "code": "PUBLICATION_DATE_IN_FUTURE"})
            continue
        p = Prepared(item=it, store_bytes=it.content is not None
                     and policy.allows(it.source_type, USE_STORE))
        if (it.facts or it.events) and not policy.allows(it.source_type, USE_EXTRACT):
            p.unknowns.append({**base, "code": SOURCE_NOT_APPROVED, "use": USE_EXTRACT,
                               "dropped_facts": str(len(it.facts)),
                               "dropped_events": str(len(it.events))})
            p.item = replace(it, facts=(), events=())
        prepared.append(p)
    return prepared, unknowns
