"""The document store contract.

One protocol, two backends. Both are exercised by the same test suite so they cannot
drift: a bug that only shows up in production because development used the other backend
is exactly what a shared suite is for.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

#: Signed URLs are short-lived by default. Long enough for a browser to follow a redirect
#: and start a download, short enough that a URL leaking into a log or a chat history is
#: not a standing grant of access to someone's financial statement.
DEFAULT_URL_TTL_SECONDS = 300


class ObjectNotFoundError(KeyError):
    """No object at that key."""


class SignatureError(PermissionError):
    """A signed URL was expired, tampered with, or signed by another secret."""


@runtime_checkable
class DocumentStore(Protocol):
    """Private object storage. Nothing here is publicly readable by design."""

    def put(self, key: str, content: bytes, content_type: str = "application/pdf") -> str:
        """Store bytes at ``key`` and return the key.

        Idempotent when the key is content-addressed, which is how ``storage.keys``
        derives it: re-storing an identical document overwrites it with itself.
        """
        ...

    def get(self, key: str) -> bytes:
        """Raises ObjectNotFoundError if absent."""
        ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> bool:
        """Returns whether anything was removed. Deleting an absent object is not an
        error -- erasure must be safely re-runnable after a partial failure."""
        ...

    def signed_url(self, key: str, ttl_seconds: int = DEFAULT_URL_TTL_SECONDS) -> str:
        """A time-limited URL granting read access to one object.

        Issuing one IS the grant of access, and should be audited at that moment rather
        than when the URL is later followed -- by then the system may never hear about it.
        """
        ...
