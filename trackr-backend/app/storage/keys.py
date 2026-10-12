"""Object key derivation.

Deliberately not "the filename". Statement filenames collide constantly: every CDSL
monthly statement is named to the same template, differing only by month, and this project
has already met five byte-identical copies of a single one. A store keyed on filenames
would have silently overwritten four of them, or kept five copies of one document,
depending on which way the collision fell.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date

#: Investor CODE, never name or PAN - the same rule DATA_MODEL sets for files and logs.
#: Object keys end up in bucket listings, access logs and support tickets.
SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9_-]")

DEFAULT_EXTENSION = "bin"


def content_hash(content: bytes) -> str:
    """SHA-256 of the bytes. The document's identity."""
    return hashlib.sha256(content).hexdigest()


def _safe(segment: str) -> str:
    return SAFE_SEGMENT.sub("_", segment.strip()) or "unknown"


def extension_of(filename: str) -> str:
    if "." not in filename:
        return DEFAULT_EXTENSION
    ext = filename.rsplit(".", 1)[-1].lower()
    return _safe(ext)[:8] or DEFAULT_EXTENSION


def object_key(
    investor_code: str,
    doctype: str,
    received: date,
    content: bytes,
    filename: str = "",
) -> str:
    """``{code}/{doctype}/{YYYY}/{MM}/{hash16}.{ext}``

    CONTENT-ADDRESSED. Storing the same bytes twice yields the same key, so a forwarded
    duplicate overwrites itself instead of accumulating - the same property `dedupe_key`
    gives the ledger, for the same reason: email will deliver the same statement twice.

    Prefixed by investor so their objects can be listed and deleted as a unit, which is
    what makes erasure tractable.
    """
    digest = content_hash(content)[:16]
    return (
        f"{_safe(investor_code)}/{_safe(doctype)}/"
        f"{received:%Y/%m}/{digest}.{extension_of(filename)}"
    )


def investor_prefix(investor_code: str) -> str:
    """Everything belonging to one investor. Used by erasure."""
    return f"{_safe(investor_code)}/"
