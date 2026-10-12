"""Filesystem-backed document store for development or explicitly configured self-hosting.

Signs its URLs with an HMAC over key and expiry so it behaves like the S3 backend in the
ways that matter -- a URL that expires, and a URL that cannot be edited to point somewhere
else. Without that, development would exercise a permission model production does not have.

Self-hosting requires an operator-managed persistent volume, backups, retention and
disk encryption. This store provides none of those infrastructure properties itself.
"""

from __future__ import annotations

import hmac
import os
import shutil
import time
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlencode

from app.storage.base import DEFAULT_URL_TTL_SECONDS, ObjectNotFoundError, SignatureError

SIGNING_SECRET_ENV = "PORTFOLIO_STORAGE_SIGNING_SECRET"


class LocalDocumentStore:
    def __init__(self, root: Path, base_url: str = "http://localhost:8000/documents"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.base_url = base_url.rstrip("/")

    def _path(self, key: str) -> Path:
        # Refuse traversal outright rather than sanitising: a key that escapes the root is
        # a bug or an attack, and quietly rewriting it hides both.
        candidate = (self.root / key).resolve()
        if not candidate.is_relative_to(self.root.resolve()):
            raise ValueError(f"key escapes storage root: {key}")
        return candidate

    def _secret(self) -> bytes:
        secret = os.environ.get(SIGNING_SECRET_ENV)
        if not secret:
            raise SignatureError(
                f"{SIGNING_SECRET_ENV} is not set. Refusing to issue unsigned URLs: an "
                "unsigned URL to a client statement is a public one."
            )
        return secret.encode()

    def put(self, key: str, content: bytes, content_type: str = "application/pdf") -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return key

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise ObjectNotFoundError(key)
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> bool:
        path = self._path(key)
        if not path.exists():
            return False
        path.unlink()
        return True

    def delete_prefix(self, prefix: str) -> int:
        """Remove everything under a prefix. Used by erasure."""
        base = self._path(prefix)
        if not base.exists():
            return 0
        count = sum(1 for p in base.rglob("*") if p.is_file())
        shutil.rmtree(base, ignore_errors=True)
        return count

    def _signature(self, key: str, expires: int) -> str:
        return hmac.new(self._secret(), f"{key}:{expires}".encode(), sha256).hexdigest()

    def signed_url(self, key: str, ttl_seconds: int = DEFAULT_URL_TTL_SECONDS) -> str:
        expires = int(time.time()) + ttl_seconds
        query = urlencode({"expires": expires, "signature": self._signature(key, expires)})
        return f"{self.base_url}/{key}?{query}"

    def verify(self, key: str, expires: int, signature: str) -> None:
        """Validate a signed URL. The serving layer calls this before returning bytes.

        Raises:
            SignatureError: expired, tampered, or signed with a different secret.
        """
        if expires < int(time.time()):
            raise SignatureError("signed URL has expired")
        # Constant-time: a timing oracle here would let a signature be guessed byte by byte.
        if not hmac.compare_digest(self._signature(key, expires), signature):
            raise SignatureError("signature does not match")
