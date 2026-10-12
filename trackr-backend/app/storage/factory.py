"""Pick a ``DocumentStore`` backend from ``app/config.py``. NEW -- neither backend needed
this before, because nothing outside tests constructed one; the routers now do.
"""

from __future__ import annotations

from app.config import Settings
from app.storage.base import DocumentStore
from app.storage.local import LocalDocumentStore
from app.storage.s3 import S3DocumentStore


def get_store(settings: Settings) -> DocumentStore:
    """``local`` for development, ``s3`` for R2/S3-compatible production storage.

    Raises:
        ValueError: an unrecognised backend name. Failing loudly here beats silently
            falling back to local storage in an environment that meant to use S3.
    """
    if settings.storage_backend == "local":
        return LocalDocumentStore(settings.local_storage_root, settings.local_storage_base_url)
    if settings.storage_backend == "s3":
        return S3DocumentStore(
            bucket=settings.portfolio_storage_bucket,
            endpoint_url=settings.portfolio_storage_endpoint,
        )
    raise ValueError(f"unknown STORAGE_BACKEND: {settings.storage_backend!r}")
