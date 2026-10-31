"""Public exchange transport: database reads on hosted processes; explicit collection.

No setting in an HTTP request can enable network access. Only the local collector enters
``reference_context(..., collect=True)``. Scheduled cron/ops endpoints may enter
``archive_collection()``, which can fetch only ``ARCHIVE_LOCATIONS`` (owner amendment
2026-10-31, docs/INSTANT_IMPORT_PLAN.md). Existing parsers consume real captured bytes,
so IPO/rights/identity trust rules remain unchanged. Misses are durable work intent,
not fabricated responses. Task dependencies allow a later collection to wake only work
that asked for that evidence.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_scope: ContextVar[ReferenceScope | None] = ContextVar("exchange_reference_scope", default=None)
_task: ContextVar[tuple[str, str] | None] = ContextVar("exchange_reference_task", default=None)
_probing: ContextVar[bool] = ContextVar("exchange_reachability_probe", default=False)
PUBLIC_DOMAINS = ("nseindia.com", "bseindia.com", "niftyindices.com", "nsdl.com")
#: (host, path prefix) pairs scheduled hosted collection may fetch. Published files only:
#: bhavcopies, F&O bhavcopies, index constituent lists and niftyindices series. The NSE
#: API (``www.nseindia.com/api``) is deliberately absent; it stays with the laptop.
ARCHIVE_LOCATIONS = (
    ("nsearchives.nseindia.com", "/content/"),
    ("archives.nseindia.com", "/content/historical/"),
    ("www.bseindia.com", "/download/"),
    ("niftyindices.com", "/BackPage/"),
    ("www.niftyindices.com", "/BackPage/"),
    ("niftyindices.com", "/reports/"),
    ("www.niftyindices.com", "/reports/"),
)
#: Exact (host, path) pairs: the homepage request whose cookie NSE's legacy archive needs.
ARCHIVE_EXACT = frozenset({("www.nseindia.com", "/")})
SAFE_HEADERS = frozenset(
    {"accept", "content-type", "referer", "origin", "user-agent", "x-requested-with"}
)


def is_exchange_url(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in PUBLIC_DOMAINS)


def is_archive_url(url: str) -> bool:
    """True only for an HTTPS URL inside ``ARCHIVE_LOCATIONS`` / ``ARCHIVE_EXACT``."""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    path = parts.path or "/"
    if parts.scheme != "https" or parts.username or parts.password:
        return False
    if (host, path) in ARCHIVE_EXACT:
        return True
    return any(host == h and path.startswith(prefix) for h, prefix in ARCHIVE_LOCATIONS)


def _archive_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return any(host == h for h, _ in ARCHIVE_LOCATIONS) or any(
        host == h for h, _ in ARCHIVE_EXACT
    )


def _request(value: Any) -> urllib.request.Request:
    return value if isinstance(value, urllib.request.Request) else urllib.request.Request(value)


def request_key(value: Any) -> str:
    req = _request(value)
    return hashlib.sha256(
        req.get_method().encode() + b"\0" + req.full_url.encode() + b"\0" + (req.data or b"")
    ).hexdigest()


class MissingExchangeReference(Exception):
    reason_code = "REFERENCE_DATA_MISSING"

    def __init__(self, request: Any):
        self.request = _request(request)
        self.request_key = request_key(self.request)
        self.url = self.request.full_url
        super().__init__(f"{self.reason_code}: no collected response in database for {self.url}")


class InvalidExchangeReference(Exception):
    reason_code = "REFERENCE_DATA_INVALID"


class ExchangeNetworkDisabled(InvalidExchangeReference):
    reason_code = "EXCHANGE_NETWORK_DISABLED"


def _guard_exchange_network(event, args):
    """Also stop direct HTTP clients/future adapters before exchange DNS/connect."""
    if event == "socket.getaddrinfo":
        host = args[0]
    elif event == "http.client.connect":
        host = args[1]
    else:
        return
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="replace")
    if isinstance(host, str) and is_exchange_url(f"https://{host}") and not _may_connect(host):
        raise ExchangeNetworkDisabled(f"Exchange network access is disabled on hosted work: {host}")


sys.addaudithook(_guard_exchange_network)


@dataclass
class ReferenceScope:
    conn: Any
    collect: bool = False
    #: Collection limited to ``ARCHIVE_LOCATIONS``; every other exchange URL is a DB read.
    archive_only: bool = False
    pending: dict = field(default_factory=dict)

    def persist(self) -> None:
        """Persist misses after the caller has handled/rolled back its own work."""
        for req, dependencies, observed in self.pending.values():
            _register(self.conn, req)
            for kind, key in dependencies:
                self.conn.execute(
                    "INSERT INTO exchange_reference_dependency(kind,task_key,request_key) "
                    "SELECT %s,%s,%s WHERE EXISTS (SELECT 1 FROM reference_sync_task "
                    "WHERE kind=%s AND task_key=%s) ON CONFLICT DO NOTHING",
                    (kind, key, request_key(req), kind, key),
                )
                # Collection can finish between our read and dependency registration.
                # Compare actual content, so neither this race nor a later correction
                # leaves a completed task attached to obsolete evidence.
                self.conn.execute(
                    "UPDATE reference_sync_task SET state='pending',available_at=clock_timestamp(),"
                    "payload=payload-'reference_missing',last_error=NULL "
                    "WHERE kind=%s AND task_key=%s "
                    "AND state='done' AND EXISTS (SELECT 1 FROM exchange_reference_response "
                    "WHERE request_key=%s AND response_sha256 IS DISTINCT FROM %s)",
                    (kind, key, request_key(req), observed),
                )
        self.pending.clear()


@contextmanager
def reference_context(
    conn: Any,
    *,
    collect: bool | None = None,
    task: tuple[str, str] | None = None,
    archive_only: bool | None = None,
):
    previous = _scope.get()
    inherit = collect is None and previous is not None
    scope = ReferenceScope(
        conn,
        previous.collect if inherit else bool(collect),
        previous.archive_only if inherit and archive_only is None else bool(archive_only),
    )
    if previous is not None and previous.conn is conn:
        scope.pending = previous.pending
    token = _scope.set(scope)
    task_token = _task.set(task) if task is not None else None
    try:
        yield scope
    finally:
        if task_token is not None:
            _task.reset(task_token)
        _scope.reset(token)


@contextmanager
def reference_transaction(conn: Any):
    """Bind financial work to shared evidence without changing the thin DB factory."""
    with reference_context(conn) as references:
        try:
            yield conn
        except Exception:
            # Business work rolls back; public collection intent remains durable.
            conn.rollback()
            references.persist()
            conn.commit()
            raise
        references.persist()


@contextmanager
def reference_task(kind: str, key: str):
    token = _task.set((kind, key))
    try:
        yield
    finally:
        _task.reset(token)


@contextmanager
def archive_collection(conn: Any):
    """Scheduled hosted collection of published archive files only.

    For cron (``CRON_SECRET``) and ops (``X-Ops-Key``) endpoints. Never enter this from an
    import, sync or investor request: user-triggered work reads the database only. A URL
    outside ``ARCHIVE_LOCATIONS`` is answered from the database or raises
    ``MissingExchangeReference``, exactly as on any hosted read.
    """
    with reference_context(conn, collect=True, archive_only=True) as scope:
        yield scope


def collecting() -> bool:
    """Full collection (the local collector). False inside ``archive_collection``."""
    scope = _scope.get()
    return scope is not None and scope.collect and not scope.archive_only


def may_fetch(url: str) -> bool:
    """Whether the current scope may send this exchange request to the network."""
    scope = _scope.get()
    if scope is None or not scope.collect:
        return False
    return not scope.archive_only or is_archive_url(url)


@contextmanager
def reachability_probe():
    """Let ``/ops/exchange/reachability`` open exchange sockets to MEASURE access.

    Only that ops-key endpoint enters this. Its requests bypass ``urlopen``: nothing is
    stored, archived or treated as evidence, so a probe can never feed financial work.
    """
    token = _probing.set(True)
    try:
        yield
    finally:
        _probing.reset(token)


def _may_connect(host: str) -> bool:
    """Socket-level counterpart of ``may_fetch``. Paths are enforced in ``urlopen``."""
    if _probing.get():
        return True
    scope = _scope.get()
    if scope is None or not scope.collect:
        return False
    return not scope.archive_only or _archive_host(host)


def _immutable(req: urllib.request.Request) -> bool:
    """Published dated documents and completed historical windows are reusable evidence."""
    parsed = urllib.parse.urlsplit(req.full_url)
    path = parsed.path.lower()
    if "/content/circulars/" in path and path.endswith((".pdf", ".zip")):
        return True
    if any(part in path for part in ("/bhavcopy/", "/historical/")) and re.search(r"\d{6}", path):
        return True
    query = urllib.parse.parse_qs(parsed.query)
    ends = [
        value
        for key, values in query.items()
        if key.lower() in {"to_date", "todate", "end", "enddate"}
        for value in values
    ]
    ends += re.findall(
        r"endDate['\"]\s*:\s*['\"]([^'\"]+)", (req.data or b"").decode(errors="replace")
    )
    for value in ends:
        for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d-%b-%Y"):
            try:
                end = datetime.strptime(value, fmt).date()
            except ValueError:
                continue
            return end < datetime.now(ZoneInfo("Asia/Kolkata")).date()
    return False


def needs_warmup(opener: Any) -> bool:
    """SQL reads need no HTTP cookie handshake; custom injected sources keep their contract."""
    owner = getattr(opener, "__self__", opener)
    scope = _scope.get()
    return (scope is not None and scope.collect) or not isinstance(owner, ReferenceOpener)


def _register(conn: Any, req: urllib.request.Request) -> None:
    parsed = urllib.parse.urlsplit(req.full_url)
    if (
        not is_exchange_url(req.full_url)
        or parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise InvalidExchangeReference("Only HTTPS public exchange URLs may be collected")
    if req.get_method() not in ("GET", "POST") or len(req.data or b"") > 512 * 1024:
        raise InvalidExchangeReference("Unsupported public reference request")
    headers = {k: v for k, v in req.header_items() if k.lower() in SAFE_HEADERS}
    conn.execute(
        "INSERT INTO exchange_reference_request(request_key,url,method,request_body,"
        "request_headers,immutable) "
        "VALUES (%s,%s,%s,%s,%s::jsonb,%s) ON CONFLICT(request_key) DO UPDATE "
        "SET immutable=EXCLUDED.immutable WHERE exchange_reference_request.immutable "
        "IS DISTINCT FROM EXCLUDED.immutable",
        (
            request_key(req),
            req.full_url,
            req.get_method(),
            req.data or b"",
            json.dumps(headers),
            _immutable(req),
        ),
    )


class StoredResponse(io.BytesIO):
    def __init__(self, body: bytes, status: int, headers: dict, url: str):
        super().__init__(body)
        self.status = self.code = status
        self.headers = headers
        self.url = url

    def geturl(self):
        return self.url

    def getcode(self):
        return self.status


def _read(conn: Any, req: urllib.request.Request):
    row = conn.execute(
        "SELECT status,response_body,response_sha256,response_headers,fetched_at "
        "FROM exchange_reference_response WHERE request_key=%s",
        (request_key(req),),
    ).fetchone()
    if row is None:
        return None
    status, body, digest, headers, fetched = row
    body = bytes(body)
    if hashlib.sha256(body).hexdigest() != digest:
        raise InvalidExchangeReference(f"Stored reference checksum mismatch: {request_key(req)}")
    scope = _scope.get()
    if scope is not None and _task.get() is not None:
        entry = scope.pending.setdefault(request_key(req), (req, set(), digest))
        entry[1].add(_task.get())
    return status, body, headers, fetched


def _response(req, row):
    status, body, headers, _fetched = row
    response = StoredResponse(body, status, headers, req.full_url)
    if status >= 400:
        raise urllib.error.HTTPError(
            req.full_url, status, "Recorded source response", headers, response
        )
    return response


def _store(conn, req, status, body, headers):
    _register(conn, req)
    digest = hashlib.sha256(body).hexdigest()
    conn.execute(
        "INSERT INTO exchange_reference_revision(request_key,status,response_sha256,"
        "response_body,response_headers) VALUES (%s,%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING",
        (request_key(req), status, digest, body, json.dumps(headers)),
    )
    changed = conn.execute(
        "INSERT INTO exchange_reference_response(request_key,status,response_body,"
        "response_sha256,response_headers) "
        "VALUES (%s,%s,%s,%s,%s::jsonb) ON CONFLICT(request_key) DO UPDATE "
        "SET status=EXCLUDED.status,response_body=EXCLUDED.response_body,"
        "response_sha256=EXCLUDED.response_sha256,response_headers=EXCLUDED.response_headers,"
        "fetched_at=clock_timestamp(),changed_at=clock_timestamp() "
        "WHERE (exchange_reference_response.status,exchange_reference_response.response_sha256) "
        "IS DISTINCT FROM (EXCLUDED.status,EXCLUDED.response_sha256) RETURNING request_key",
        (request_key(req), status, body, digest, json.dumps(headers)),
    ).fetchone()
    if changed is None:
        conn.execute(
            "UPDATE exchange_reference_response SET fetched_at=clock_timestamp() "
            "WHERE request_key=%s",
            (request_key(req),),
        )
    else:
        conn.execute(
            "UPDATE reference_sync_task t SET state='pending',available_at=clock_timestamp(),"
            "last_error=NULL,payload=t.payload-'reference_missing' WHERE state='done' AND EXISTS "
            "(SELECT 1 FROM exchange_reference_dependency d "
            "WHERE d.kind=t.kind AND d.task_key=t.task_key "
            "AND d.request_key=%s)",
            (request_key(req),),
        )
    conn.execute(
        "UPDATE exchange_reference_request SET state='collected',last_attempt_at=clock_timestamp(),"
        "last_error=NULL,attempts=attempts+1 WHERE request_key=%s",
        (request_key(req),),
    )


def urlopen(value: Any, *, network=None, refresh: bool = False, **kwargs):
    """A real recorded response or an explicit miss. Never a network fallback on hosted work."""
    req = _request(value)
    network = network or urllib.request.urlopen
    if not is_exchange_url(req.full_url):
        return network(req, **kwargs)
    scope = _scope.get()
    fetch = may_fetch(req.full_url)
    row = _read(scope.conn, req) if scope is not None else None
    fresh = (
        row is not None
        and row[3].astimezone(ZoneInfo("Asia/Kolkata")).date()
        == datetime.now(ZoneInfo("Asia/Kolkata")).date()
    )
    if row is not None and (not fetch or (not refresh and (_immutable(req) or fresh))):
        return _response(req, row)
    if not fetch:
        if scope is not None:
            key = request_key(req)
            entry = scope.pending.setdefault(key, (req, set(), None))
            if _task.get() is not None:
                entry[1].add(_task.get())
        logger.warning(
            "exchange_reference_missing",
            extra={"request_key": request_key(req), "source_url": req.full_url},
        )
        raise MissingExchangeReference(req)
    _register(scope.conn, req)
    try:
        response = network(req, **kwargs)
    except urllib.error.HTTPError as exc:
        # A real 404 can establish archive absence. Authentication, throttling and
        # source outages never replace previously collected evidence.
        if exc.code not in (404, 410):
            raise
        response = exc
    with response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
        status = getattr(response, "status", None)
        if status is None and hasattr(response, "getcode"):
            status = response.getcode()
        if not isinstance(status, int) or not 200 <= status <= 599:
            raise InvalidExchangeReference("Source response has no valid HTTP status")
        headers = {
            k: v
            for k, v in getattr(response, "headers", {}).items()
            if k.lower() in {"content-type", "last-modified"}
        }
    if len(body) > MAX_RESPONSE_BYTES:
        raise InvalidExchangeReference("Public reference response exceeds archive size limit")
    _store(scope.conn, req, status, body, headers)
    return _response(req, (status, body, headers, datetime.now(UTC)))


class ReferenceOpener:
    """Cookie sessions are used only by the explicit collector; hosted reads use SQL."""

    def __init__(self, opener):
        self.opener = opener

    def open(self, request, **kwargs):
        return urlopen(request, network=self.opener.open, **kwargs)


def collect_requests(
    conn: Any, *, limit: int = 200, network=None, budget_seconds: float = 300
) -> dict:
    """One attempt per selected request. Failures do not stop other public evidence."""
    rows = conn.execute(
        "SELECT request_key,url,method,request_body,request_headers "
        "FROM exchange_reference_request "
        "WHERE state='pending' OR (state='failed' AND last_attempt_at < date_trunc('day',now())) "
        "OR (NOT immutable AND state='collected' AND "
        "last_attempt_at < date_trunc('day',now())) "
        "ORDER BY CASE state WHEN 'pending' THEN 0 WHEN 'failed' THEN 1 ELSE 2 END,requested_at "
        "LIMIT %s",
        (limit,),
    ).fetchall()
    out = {"collected": 0, "failed": [], "attempted": 0}
    began = time.monotonic()
    # One cookie jar for this collection pass; nothing is installed process-globally.
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    warmed = False
    with reference_context(conn, collect=True):
        for key, url, method, body, headers in rows:
            if time.monotonic() - began >= budget_seconds:
                break
            out["attempted"] += 1
            req = urllib.request.Request(
                url, data=bytes(body) if body else None, headers=headers, method=method
            )
            try:
                if _immutable(req) and _read(conn, req) is not None:
                    conn.execute(
                        "UPDATE exchange_reference_request SET immutable=true WHERE request_key=%s",
                        (key,),
                    )
                    conn.commit()
                    continue
                if (
                    network is None
                    and "/api/" in url
                    and (urllib.parse.urlsplit(url).hostname or "").endswith("nseindia.com")
                    and not warmed
                ):
                    with opener.open(
                        urllib.request.Request("https://www.nseindia.com/", headers=headers),
                        timeout=30,
                    ):
                        warmed = True
                with urlopen(req, network=network or opener.open, refresh=True, timeout=30):
                    out["collected"] += 1
                conn.commit()
            except urllib.error.HTTPError as exc:
                if exc.code in (404, 410):
                    # Real published absence is archived evidence, not a fabricated
                    # empty response and not an upstream transport outage.
                    conn.commit()
                    out["collected"] += 1
                    continue
                conn.rollback()
                _collection_failed(conn, key, url, exc, out)
            except Exception as exc:
                conn.rollback()
                _collection_failed(conn, key, url, exc, out)
    return out


def _collection_failed(conn, key, url, exc, out):
    conn.execute(
        "UPDATE exchange_reference_request SET state='failed',last_attempt_at=clock_timestamp(),"
        "attempts=attempts+1,last_error=%s WHERE request_key=%s",
        (f"{type(exc).__name__}: {exc}"[:1000], key),
    )
    conn.commit()
    out["failed"].append({"request_key": key, "url": url, "error": str(exc)[:500]})
    logger.warning("exchange_collection_failed", extra={"request_key": key}, exc_info=True)
