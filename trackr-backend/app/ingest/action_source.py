"""The operator corporate-action file contract and the corporate-action source errors.

There is no HTTP provider client: no production provider has been chosen (plan OD-8).
An operator imports an authorized file through ``POST /ops/corporate-actions/import``,
which validates it as an ``ActionBatch`` and hands it to ``app.db.action_ingestion``.
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

PARSER_VERSION = "corporate-actions-v1"


class ActionSourceError(RuntimeError):
    """An unavailable, incomplete, unauthorized or invalid source is not an empty feed."""


class ActionIngestionError(ActionSourceError):
    """Malformed or invalid operator input."""


class ActionConflictError(ActionSourceError):
    """A notice contradicts what is already stored for the same event. Never merged by
    guesswork: the observation is recorded CONFLICT and the stored row goes back to DRAFT."""

    def __init__(self, message: str, reason_code: str):
        super().__init__(message)
        self.reason_code = reason_code


class ActionBatchFailed(ActionSourceError):
    """A batch finished with rejected or conflicting rows. Good rows were committed; no
    coverage was written. ``rejected`` and ``conflicts`` are observation IDs."""

    def __init__(self, rejected: list[str], conflicts: list[str]):
        super().__init__(
            f"{len(rejected)} corporate action observation(s) rejected and "
            f"{len(conflicts)} in conflict; inspect operator observations"
        )
        self.rejected = rejected
        self.conflicts = conflicts


class ActionNotice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=256)
    component_id: str = Field(min_length=1, max_length=128)
    revision: StrictInt = Field(ge=1)
    isin: str = Field(pattern=r"^IN[A-Z0-9]{10}$")
    exchange: Literal["NSE", "BSE"]
    action_type: Literal[
        "BONUS",
        "SPLIT",
        "DIVIDEND",
        "RIGHTS",
        "BUYBACK",
        "MERGER",
        "DEMERGER",
        "CAPITAL_REDUCTION",
        "CONSOLIDATION",
        "IDENTITY",
        "UNKNOWN_ADJUSTMENT",
        "UNKNOWN",
    ]
    status: Literal["ANNOUNCED", "CONFIRMED", "CANCELLED"]
    source_url: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    ex_date: date | None = None
    record_date: date | None = None
    announcement_date: date | None = None
    allotment_date: date | None = None
    payment_date: date | None = None
    bonus_new: StrictInt | None = Field(default=None, gt=0)
    bonus_for: StrictInt | None = Field(default=None, gt=0)
    face_value_before: str | None = None
    face_value_after: str | None = None
    amount_per_share: str | None = None
    adjustment_factor: str | None = None
    currency: Literal["INR"] | None = None
    dividend_kind: Literal["INTERIM", "FINAL", "SPECIAL", "UNSPECIFIED"] | None = None
    resulting_isin: str | None = Field(default=None, pattern=r"^IN[A-Z0-9]{10}$")

    @model_validator(mode="after")
    def validate_terms(self):
        parsed = urllib.parse.urlsplit(self.source_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("source_url must be an HTTPS evidence URL without credentials")
        for field in (
            "face_value_before",
            "face_value_after",
            "amount_per_share",
            "adjustment_factor",
        ):
            value = getattr(self, field)
            if value is not None:
                try:
                    number = Decimal(value)
                except InvalidOperation as exc:
                    raise ValueError(f"{field} must be exact decimal text") from exc
                if not number.is_finite() or number <= 0:
                    raise ValueError(f"{field} must be finite and positive")
                if field != "adjustment_factor" and (
                    number.as_tuple().exponent < -6 or number >= Decimal("100000000000000")
                ):
                    raise ValueError(f"{field} exceeds exact NUMERIC(20,6) storage")
        if self.ex_date and self.record_date and self.record_date < self.ex_date:
            raise ValueError("record_date precedes ex_date")
        if self.ex_date and self.allotment_date and self.allotment_date < self.ex_date:
            raise ValueError("allotment_date precedes ex_date")
        return self


class ActionBatch(BaseModel):
    """One operator file. ``events`` stay raw objects so a bad row is retained as a
    REJECTED observation instead of discarding the rows around it."""

    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1, max_length=128)
    start: date
    end: date
    exchanges: list[Literal["NSE", "BSE"]] = Field(min_length=1)
    complete: StrictBool
    events: list[dict[str, Any]]

    @model_validator(mode="after")
    def check_range(self):
        if self.start > self.end:
            raise ValueError("start must not follow end")
        if not self.complete:
            raise ValueError("incomplete/paginated data must be completed by the provider adapter")
        return self


def canonical_json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
