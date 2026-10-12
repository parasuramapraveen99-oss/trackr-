"""Private object storage for client documents.

Statements are never public. Access is by short-lived signed URL only, and issuing one is
an audited access event.

May import ``engine``; nothing in ``engine`` or ``db`` may import this. Documents are a
layer-1 concern: the engine computes from transactions and has no idea a PDF exists.
"""

from app.storage.base import (
    DEFAULT_URL_TTL_SECONDS,
    DocumentStore,
    ObjectNotFoundError,
    SignatureError,
)
from app.storage.keys import content_hash, investor_prefix, object_key
from app.storage.local import LocalDocumentStore
from app.storage.s3 import S3DocumentStore

__all__ = [
    "DEFAULT_URL_TTL_SECONDS",
    "DocumentStore",
    "LocalDocumentStore",
    "ObjectNotFoundError",
    "S3DocumentStore",
    "SignatureError",
    "content_hash",
    "investor_prefix",
    "object_key",
]
