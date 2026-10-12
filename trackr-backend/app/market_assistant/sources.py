"""Which URLs count as an official source. docs/AI_ASSISTANT_PRIVACY_PLAN.md section 6.

The domain list is sent to the search service AND re-checked here on every result, so a
result from anywhere else is dropped whatever the service returns. The check is strict on
purpose: HTTPS only, the exact host or a true subdomain of an allowed domain (a dot
boundary, so ``rbi.org.in.evil.com`` and ``notrbi.org.in`` fail), no user name or password
in the URL, no non-standard port, no IP address, and no query string that looks like a
signed or tokenised link.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import unquote, urlsplit

from app.market_assistant.catalogue import (
    NEWS_PUBLISHERS,
    OFFICIAL_DOMAINS,
    PUBLISHERS,
    SOURCE_DOMAINS,
)

_SIGNED_QUERY = re.compile(
    r"(?:^|[&;])(?:[a-z_]*token|sig|signature|x-amz-[a-z-]+|key|apikey|api_key|auth|session|"
    r"sid|expires)=",
    re.I,
)
MAX_URL_CHARS = 2048


def admit_public_url(url: str) -> bool:
    """Discovery does not imply trust. Reject private, credentialed and malformed URLs.

    Only Exa is contacted; this function never fetches a discovered URL itself.
    """
    if not url or len(url) > MAX_URL_CHARS or re.search(r"[\s\\\x00-\x1f]", url):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or parts.username or parts.password or port not in (None, 443):
        return False
    host = (parts.hostname or "").lower()
    if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}", host):
        return False
    if host.endswith((".localhost", ".local", ".internal", ".test", ".invalid", ".onion")):
        return False
    if any(label.startswith("xn--") for label in host.split(".")):
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    query = parts.query
    for _ in range(3):
        query = unquote(query)
        if _SIGNED_QUERY.search(query):
            return False
    return True


def _host_allowed(host: str, domains: frozenset[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def admit_url(url: str, domains: frozenset[str] | tuple[str, ...] = OFFICIAL_DOMAINS) -> bool:
    """True only for a public HTTPS URL on one of ``domains``. ``domains`` must itself be
    a subset of the official list, so a caller cannot widen the policy."""
    allowed = frozenset(domains)
    if not allowed <= SOURCE_DOMAINS:
        raise ValueError("a source policy may only narrow the approved source domains")
    if not admit_public_url(url):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or parts.username or parts.password:
        return False
    if port not in (None, 443):
        return False
    host = (parts.hostname or "").rstrip(".").lower()
    if not host or not host.isascii():
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    if _SIGNED_QUERY.search(parts.query):
        return False
    return _host_allowed(host, allowed)


def publisher_domain(url: str) -> str:
    """The official domain a URL belongs to, for display. Call only on an admitted URL."""
    host = (urlsplit(url).hostname or "").lower()
    for domain in sorted(SOURCE_DOMAINS, key=len, reverse=True):
        if host == domain or host.endswith("." + domain):
            return domain
    raise ValueError("URL is not on an approved source domain")


def public_domain(url: str) -> str:
    """Host for an issuer already verified from an official website reference."""
    if not admit_public_url(url):
        raise ValueError("unsafe public URL")
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def publisher_name(domain: str) -> str:
    return PUBLISHERS[domain]


def is_news(domain: str) -> bool:
    return domain in NEWS_PUBLISHERS
