"""The search adapter: one fixed public search, inspected before it is sent.

docs/AI_ASSISTANT_PRIVACY_PLAN.md section 5. The request is built ONLY from a
``PublicQuestion`` and the catalogue: the query text, the domains and the date window all
come from ``catalogue.SearchSpec``, so there is no path by which the user's words reach
the search service. ``check_outbound`` then inspects the exact bytes about to be sent and
refuses anything that is not one of the catalogue's own queries, any field outside the
reviewed set (including ``userIdentifier``, which the service keeps even under zero data
retention), and any header beyond the key and content type.

Failures raise ``SearchUnavailable``. There is no retry: a 429 or an exhausted credit
balance must not turn into a burst of repeat calls.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from app.market_assistant.admission import PublicQuestion, QuestionType, is_clean_query
from app.market_assistant.catalogue import CONCEPTS, OFFICIAL_DOMAINS, SOURCE_DOMAINS
from app.market_assistant.evidence import RawResult

logger = logging.getLogger("market_assistant.search")

SEARCH_URL = "https://api.exa.ai/search"
#: docs/AI_ASSISTANT_PRIVACY_PLAN.md section 10: one search, up to 5 results.
MAX_RESULTS = 5
#: Text returned per document. Bounded so one long report cannot crowd out the rest.
MAX_TEXT_CHARS = 8000
#: Bytes read from the response before giving up on it.
MAX_RESPONSE_BYTES = 512 * 1024
TIMEOUT_SECONDS = 15

#: Every field the request may carry. Nothing about the user, ever.
_ALLOWED_BODY_KEYS = frozenset(
    {"query", "type", "numResults", "includeDomains", "startPublishedDate", "contents"}
)
_ALLOWED_HEADERS = frozenset({"x-api-key", "content-type", "accept"})
_CANONICAL_QUERIES = frozenset(c.search.query for c in CONCEPTS.values() if c.search)
#: Queries whose source is dated by the index, so the request MUST carry a date filter.
_DATE_FILTERED_QUERIES = frozenset(
    c.search.query for c in CONCEPTS.values() if c.search and not c.search.dates_from_text
)


class SearchUnavailable(Exception):
    """No key, the service failed, or the response was unusable. The caller falls back to
    the catalogue text and says it could not check the official source."""


class OutboundPolicyError(Exception):
    """The request about to be sent broke the outbound policy. Nothing was sent."""


@dataclass(frozen=True)
class PublicSearchRequest:
    query: str
    domains: tuple[str, ...]
    #: None for a ``dates_from_text`` source: no date filter is sent, dates are checked
    #: in the document text instead (evidence.admit_documents).
    start_published: date | None


def build_request(question: PublicQuestion, today: date) -> PublicSearchRequest:
    """The only constructor callers use. Everything comes from the catalogue."""
    if question.question_type is not QuestionType.LATEST:
        raise ValueError("only a LATEST question is searched")
    spec = CONCEPTS[question.topic_id].search
    if spec is None:
        raise ValueError("topic has no search spec")
    return PublicSearchRequest(
        query=spec.query,
        domains=spec.domains,
        start_published=None
        if spec.dates_from_text
        else today - timedelta(days=spec.max_age_days),
    )


def build_open_request(
    query: str, *, discover: bool = False, start_published: date | None = None
) -> PublicSearchRequest:
    """An open question's search (owner amendment 11 Oct 2026): a planner query that has
    passed ``admission.is_clean_query``, over every approved source, with no date filter
    (exchange and news pages are dated in their text, and dates are shown, not assumed)."""
    if not is_clean_query(query):
        raise ValueError("query failed the privacy checks")
    return PublicSearchRequest(
        query=query, domains=() if discover else tuple(sorted(SOURCE_DOMAINS)),
        start_published=start_published,
    )


def request_body(req: PublicSearchRequest) -> dict[str, Any]:
    body: dict[str, Any] = {
        "query": req.query,
        "type": "auto",
        "numResults": MAX_RESULTS,
        "contents": {"text": {"maxCharacters": MAX_TEXT_CHARS}},
    }
    if req.domains:
        body["includeDomains"] = list(req.domains)
    if req.start_published is not None:
        body["startPublishedDate"] = f"{req.start_published.isoformat()}T00:00:00.000Z"
    return body


def check_outbound(body: bytes, headers: dict[str, str]) -> None:
    """Inspect the serialised request. Raises ``OutboundPolicyError``; never edits."""
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OutboundPolicyError("body is not JSON") from exc
    if not isinstance(parsed, dict) or set(parsed) - _ALLOWED_BODY_KEYS:
        raise OutboundPolicyError("body carries a field outside the reviewed set")
    query = parsed.get("query")
    domains = parsed.get("includeDomains")
    if query in _CANONICAL_QUERIES:
        if not isinstance(domains, list) or not domains:
            raise OutboundPolicyError("domains missing")
        if query in _DATE_FILTERED_QUERIES and "startPublishedDate" not in parsed:
            raise OutboundPolicyError("date filter missing for an index-dated source")
        if not set(domains) <= OFFICIAL_DOMAINS:
            raise OutboundPolicyError("domains are not a subset of the official list")
    else:
        # An open question's planner query: re-checked here, at the last moment, with the
        # same privacy rules as typed text.
        if not isinstance(query, str) or not is_clean_query(query):
            raise OutboundPolicyError("query is neither a catalogue query nor a clean query")
        if domains is not None and (
            not isinstance(domains, list) or not domains
            or not all(isinstance(d, str) for d in domains)
            or not set(domains) <= SOURCE_DOMAINS
        ):
            raise OutboundPolicyError("domains are not a subset of the approved sources")
    if "startPublishedDate" in parsed:
        try:
            datetime_value = parsed["startPublishedDate"]
            if not isinstance(datetime_value, str):
                raise ValueError
            date.fromisoformat(datetime_value.removesuffix("T00:00:00.000Z"))
        except ValueError:
            raise OutboundPolicyError("invalid date filter") from None
    if parsed.get("numResults") != MAX_RESULTS or parsed.get("type") != "auto":
        raise OutboundPolicyError("result count or search type changed")
    if parsed.get("contents") != {"text": {"maxCharacters": MAX_TEXT_CHARS}}:
        raise OutboundPolicyError("contents request changed")
    if {k.lower() for k in headers} - _ALLOWED_HEADERS:
        raise OutboundPolicyError("request carries a header outside the reviewed set")


def parse_response(payload: bytes) -> list[RawResult]:
    try:
        results = json.loads(payload).get("results")
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError) as exc:
        raise SearchUnavailable("unreadable search response") from exc
    if not isinstance(results, list):
        raise SearchUnavailable("search response has no result list")
    out: list[RawResult] = []
    for r in results[:MAX_RESULTS]:
        if not isinstance(r, dict):
            continue
        published = r.get("publishedDate")
        out.append(
            RawResult(
                url=str(r.get("url") or ""),
                title=str(r.get("title") or "")[:300],
                published=published if isinstance(published, str) else None,
                text=str(r.get("text") or "")[:MAX_TEXT_CHARS],
            )
        )
    return out


Opener = Callable[..., Any]


class SearchClient:
    """Sends one inspected request. ``opener`` is injectable so tests capture the exact
    bytes and headers without touching the network."""

    def __init__(self, api_key: str, opener: Opener | None = None):
        if not api_key:
            raise SearchUnavailable("no search key configured")
        self._key = api_key
        self._open = opener or urllib.request.urlopen

    def search(self, req: PublicSearchRequest) -> list[RawResult]:
        body = json.dumps(request_body(req), separators=(",", ":")).encode()
        headers = {"x-api-key": self._key, "Content-Type": "application/json"}
        check_outbound(body, headers)
        request = urllib.request.Request(SEARCH_URL, data=body, headers=headers, method="POST")
        try:
            with self._open(request, timeout=TIMEOUT_SECONDS) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except Exception as exc:  # urllib raises assorted types; all mean "unavailable"
            # The query is public catalogue text, so naming the error type is safe; the
            # message is cut because a provider error body is not ours to log in full.
            logger.warning("market_assistant search failed: %s", type(exc).__name__)
            raise SearchUnavailable(type(exc).__name__) from exc
        if len(payload) > MAX_RESPONSE_BYTES:
            raise SearchUnavailable("search response too large")
        return parse_response(payload)
