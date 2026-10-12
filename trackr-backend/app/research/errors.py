"""The one error type every Workspace research route raises.

BLUEPRINT_BACKEND.md section 7.1 fixes the envelope for new routes:

    {"detail": {"code": ..., "message": ..., "fields": {}, "request_id": ...}}

``app/main.py`` renders ``WorkspaceError`` into exactly that. Clients branch on ``code``,
never on the sentence, so the wording can improve without breaking a frontend.

Framework-free on purpose: the repositories raise this too, and they must not import
FastAPI. The status is carried as a plain int.
"""

from __future__ import annotations

from typing import Any

# --- stable codes -------------------------------------------------------------------------
NOT_FOUND = "NOT_FOUND"
RITUAL_REQUIRED = "RITUAL_REQUIRED"
IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
STALE_PREDECESSOR = "STALE_PREDECESSOR"
VERSION_CONFLICT = "VERSION_CONFLICT"
QUESTION_IMMUTABLE = "QUESTION_IMMUTABLE"
KIND_MISMATCH = "KIND_MISMATCH"
BLOCKING_QUESTION_OPEN = "BLOCKING_QUESTION_OPEN"
INVALID_INPUT = "INVALID_INPUT"
INVALID_RULE = "INVALID_RULE"
INVALID_CURSOR = "INVALID_CURSOR"
UNKNOWN_SECURITY = "UNKNOWN_SECURITY"
COMPANY_NOT_LINKED = "COMPANY_NOT_LINKED"
SOURCE_REQUIRED = "SOURCE_REQUIRED"
SOURCE_NOT_FOUND = "SOURCE_NOT_FOUND"
SOURCE_CONTENT_UNAVAILABLE = "SOURCE_CONTENT_UNAVAILABLE"
CONTEXT_DRIFT = "CONTEXT_DRIFT"
PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
UNSUPPORTED_MEDIA = "UNSUPPORTED_MEDIA"
EXTRACTION_UNAVAILABLE = "EXTRACTION_UNAVAILABLE"
PAGE_REQUIRED = "PAGE_REQUIRED"
INVALID_PAGE = "INVALID_PAGE"
SOURCE_USE_DENIED = "SOURCE_USE_DENIED"
METRIC_NOT_APPROVED = "METRIC_NOT_APPROVED"
FACT_VALUE_INVALID = "FACT_VALUE_INVALID"
COMMITMENT_CONFLICT = "COMMITMENT_CONFLICT"
NO_COMMITMENT_IN_FORCE = "NO_COMMITMENT_IN_FORCE"
EVALUATION_NOT_TRIGGERED = "EVALUATION_NOT_TRIGGERED"
EVALUATION_STALE = "EVALUATION_STALE"
WORKSPACE_DISABLED = "WORKSPACE_DISABLED"
WORKSPACE_WRITES_NOT_READY = "WORKSPACE_WRITES_NOT_READY"
WORKSPACE_UNAVAILABLE = "WORKSPACE_UNAVAILABLE"
# --- Butler (BT 01-06) ---
BUTLER_UNAVAILABLE = "BUTLER_UNAVAILABLE"
INVALID_TRANSITION = "INVALID_TRANSITION"
TIMEZONE_REQUIRED = "TIMEZONE_REQUIRED"
ISIN_REQUIRED = "ISIN_REQUIRED"
REVIEW_CLOSED = "REVIEW_CLOSED"
REVIEW_STALE = "REVIEW_STALE"
UNRESOLVED_NOT_ACKNOWLEDGED = "UNRESOLVED_NOT_ACKNOWLEDGED"
EVIDENCE_NOT_AVAILABLE = "EVIDENCE_NOT_AVAILABLE"
PROPOSAL_REJECTED = "PROPOSAL_REJECTED"


class WorkspaceError(Exception):
    """A refused research operation, with an HTTP status and a stable machine code.

    ``fields`` maps an input field to a short reason code, for 422s a form can attach to
    the right control. It never carries request prose back to the caller or the log.
    """

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        fields: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.fields = fields or {}


def not_found(what: str) -> WorkspaceError:
    """A scoped miss. Identical for "does not exist" and "belongs to another investor"."""
    return WorkspaceError(404, NOT_FOUND, f"no such {what}")


def ritual_required() -> WorkspaceError:
    return WorkspaceError(
        409,
        RITUAL_REQUIRED,
        "record your own questions for this company before opening its research",
    )


def invalid(message: str, fields: dict[str, Any] | None = None, code: str = INVALID_INPUT
            ) -> WorkspaceError:
    return WorkspaceError(422, code, message, fields)


def conflict(code: str, message: str) -> WorkspaceError:
    return WorkspaceError(409, code, message)
