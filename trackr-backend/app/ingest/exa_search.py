"""Exa search, restricted to the exchanges -- the document source for rule-20 research.

WHY EXA. BSE's own API answers Akamai 403 to every non-browser client (re-checked
2026-10-08), and plan OD-8 forbids evading that. Exa's index carries the exchanges'
public pages and filings, so it is the permitted transport for an exchange document
this system cannot fetch directly. Owner decision 2026-10-08.

ONLY nseindia.com AND bseindia.com. The domain list is sent to Exa AND re-checked on every
result here, so a result from anywhere else can never reach the model, whatever Exa
returns. Each document is hashed on receipt; the verifier
(``app.engine.gap_resolution.check_event_against_document``) checks the model's quote
against exactly these bytes.

HTTP API, not MCP: MCP is for interactive assistants; a server pipeline calls the same
Exa service directly, which keeps the query reproducible and the model out of the loop
of choosing where to look.
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urlsplit

EXA_SEARCH_URL = "https://api.exa.ai/search"

#: The only sources rule 20a accepts through this path. Subdomains included
#: (archives.nseindia.com, nsearchives.nseindia.com, www.bseindia.com).
ALLOWED_DOMAINS = ("nseindia.com", "bseindia.com")

#: Per-document text cap sent back by Exa. Corporate-action notices are short; this keeps a
#: long annual report from crowding out the announcement in the model's context.
MAX_CHARACTERS = 12000


class ExaUnavailable(Exception):
    """No key configured, or the service could not be reached. The gap stays open and the
    task retries -- an outage is never a refusal."""


@dataclass(frozen=True)
class ExchangeDocument:
    url: str
    title: str
    published: str | None
    text: str
    sha256: str


def allowed(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)


def parse_results(payload: bytes) -> list[ExchangeDocument]:
    """Exa's response -> allowed documents with text. Anything off-domain is dropped."""
    try:
        results = json.loads(payload).get("results", [])
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError) as exc:
        raise ExaUnavailable(f"unreadable Exa response: {exc}") from exc
    docs: list[ExchangeDocument] = []
    for r in results:
        url, text = str(r.get("url") or ""), str(r.get("text") or "")
        if not url or not text.strip() or not allowed(url):
            continue
        docs.append(
            ExchangeDocument(
                url=url,
                title=str(r.get("title") or ""),
                published=r.get("publishedDate"),
                text=text,
                sha256=hashlib.sha256(text.encode()).hexdigest(),
            )
        )
    return docs


class ExaSource:
    def __init__(self, api_key: str | None, opener: Any = None, timeout: int = 30):
        if not api_key:
            raise ExaUnavailable("EXA_API_KEY is not configured")
        self._key = api_key
        self._open = opener or urllib.request.urlopen
        self._timeout = timeout

    def search(
        self, query: str, start: date | None = None, end: date | None = None, limit: int = 8
    ) -> list[ExchangeDocument]:
        body: dict[str, Any] = {
            "query": query,
            "numResults": limit,
            "includeDomains": list(ALLOWED_DOMAINS),
            "contents": {"text": {"maxCharacters": MAX_CHARACTERS}},
        }
        if start:
            body["startPublishedDate"] = f"{start.isoformat()}T00:00:00.000Z"
        if end:
            body["endPublishedDate"] = f"{end.isoformat()}T23:59:59.999Z"
        request = urllib.request.Request(
            EXA_SEARCH_URL,
            data=json.dumps(body).encode(),
            headers={"x-api-key": self._key, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._open(request, timeout=self._timeout) as response:
                return parse_results(response.read())
        except ExaUnavailable:
            raise
        except Exception as exc:  # urllib raises assorted types; all mean "try again later"
            raise ExaUnavailable(f"{type(exc).__name__}: {str(exc)[:200]}") from exc
