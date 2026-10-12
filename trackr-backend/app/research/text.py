"""Size limits and a narrow secret-shape check for human research writing.

BLUEPRINT_BACKEND.md section 7.1: notes, answers and briefs are bounded at 64 KiB of UTF-8.
Limits are counted in ENCODED BYTES, not characters -- a 64 Ki-character note in Devanagari
is three times the storage and the transport of one in ASCII, and the contract is about
bytes.

THE SECRET CHECK IS A TRIPWIRE, NOT A GUARANTEE. It refuses writing that contains the
recognisable SHAPE of a credential -- a provider API key, a cloud access key, a PEM private
key block -- because a research note is widely re-read, exported and (later) placed into
model context, and a pasted key there is a leak. It cannot prove arbitrary prose contains no
secret, and nothing in this codebase may describe it as doing so (migration 044's header
says the same about the database).
"""

from __future__ import annotations

import re

from app.research.errors import WorkspaceError

NOTE_BODY_MAX_BYTES = 64 * 1024
ANSWER_MAX_BYTES = 64 * 1024
TITLE_MAX_BYTES = 2 * 1024
QUESTION_MAX_BYTES = 4 * 1024
REASON_MAX_BYTES = 8 * 1024
THESIS_FIELD_MAX_BYTES = 64 * 1024
SHORT_FIELD_MAX_BYTES = 512
SEARCH_QUERY_MAX_CHARS = 200

SECRET_LIKE_CONTENT = "SECRET_LIKE_CONTENT"
TOO_LARGE = "TOO_LARGE"
BLANK = "BLANK"

_SECRET_SHAPES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{24,}"),  # Anthropic / OpenAI style
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),  # Google API key
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),  # GitHub token
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),  # Slack token
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),  # PEM private key
)


def check_text(
    value: str,
    field: str,
    max_bytes: int,
    *,
    required: bool = True,
) -> str:
    """Return ``value`` unchanged, or raise 422 naming ``field``.

    The writing is never trimmed, normalised or rewritten: a person's words are stored
    verbatim. Blank-ness is judged on a whitespace-stripped copy only.
    """
    if required and not value.strip():
        raise WorkspaceError(422, "INVALID_INPUT", f"{field} cannot be blank", {field: BLANK})
    if len(value.encode("utf-8")) > max_bytes:
        raise WorkspaceError(
            413,
            "PAYLOAD_TOO_LARGE",
            f"{field} exceeds {max_bytes} bytes of UTF-8",
            {field: TOO_LARGE},
        )
    if "\x00" in value:
        raise WorkspaceError(422, "INVALID_INPUT", f"{field} contains a NUL byte", {field: "NUL"})
    for pattern in _SECRET_SHAPES:
        if pattern.search(value):
            raise WorkspaceError(
                422,
                SECRET_LIKE_CONTENT,
                f"{field} looks like it contains a credential; remove it before saving",
                {field: SECRET_LIKE_CONTENT},
            )
    return value
