"""DTOs for /ops/security-alias/* -- the automated ISIN-change feed and DRAFT->VERIFIED
promotion, mirroring app/schemas/corporate_actions.py's RefreshCorporateActionFeedOut."""

from __future__ import annotations

from pydantic import BaseModel


class IsinChangeFeedOut(BaseModel):
    """Result of one poll -- see ``app.db.isin_change_circulars.refresh_isin_change_feed``."""

    fetched: int
    aliases_created: int
    skipped: int
    breaker_open: bool
    error: str | None = None


class PromotionOut(BaseModel):
    """Result of one promotion sweep -- see ``app.db.security_alias.promote_draft_aliases``."""

    verified: int
    quarantined: int
    inconclusive: int
    details: tuple[str, ...] = ()


class SecurityAliasRefreshOut(BaseModel):
    """Both halves of the weekly job, one response -- see docs/DEPLOYMENT.md's
    "why one job, not two" note on running these back to back rather than on
    independent schedules: a promoted alias should follow its own ingestion by
    minutes, not wait for the next independent cron tick."""

    feed: IsinChangeFeedOut
    promotion: PromotionOut
