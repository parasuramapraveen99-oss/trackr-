"""Shared-secret gates for the ops endpoints (``/ops/prices/refresh``,
``/ops/snapshots/run``, and their ``/ops/cron/*`` counterparts) that mutate stored data
for every investor at once, not just the caller's own.

Deliberately NOT ``get_current_user``: these exist so a cron job or you, curling the
box directly, can trigger the same work ``app.prices.pipe.main()`` already does on a
schedule -- a real end-user login was never the intended caller, and per the
pre-deployment audit (2026-08-30), any signed-up alpha user could reach them with
nothing more than a valid session. A single header checked against one env var is the
same shape as ``app.ingest.webhook_providers``' Basic Auth check, applied here instead
of at the request body/form level since there is no per-request payload to authenticate,
just the call itself.

Errors are NOT collapsed into one, unlike ``app.auth.service``'s login/token errors:
there is no account-enumeration concern for a single shared operational secret, and a
distinct "not configured at all" case is worth its own loud 500 rather than reading like
a wrong key.
"""

from __future__ import annotations

import hmac

from fastapi import Depends, Header

from app.config import Settings, get_settings


class OpsKeyMissingError(Exception):
    """No ``X-Ops-Key`` header was presented at all."""


class OpsKeyInvalidError(Exception):
    """A key was presented but does not match ``OPS_API_KEY``."""


class OpsNotConfiguredError(Exception):
    """``OPS_API_KEY`` is unset. Fails closed -- these endpoints refuse every request
    rather than falling back to any other check, the same posture
    ``app.ingest.webhook_providers.WebhookNotConfiguredError`` takes."""


def require_ops_key(
    x_ops_key: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    if not settings.ops_api_key:
        raise OpsNotConfiguredError()
    if not x_ops_key:
        raise OpsKeyMissingError()
    if not hmac.compare_digest(x_ops_key, settings.ops_api_key):
        raise OpsKeyInvalidError()


class CronSecretMissingError(Exception):
    """No ``Authorization`` header was presented at all."""


class CronSecretInvalidError(Exception):
    """A bearer value was presented but does not match ``CRON_SECRET``."""


class CronSecretNotConfiguredError(Exception):
    """``CRON_SECRET`` is unset. Same fail-closed posture as ``OpsNotConfiguredError``."""


def require_cron_secret(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """Gate for ``GET /ops/cron/*`` -- the routes Vercel Cron itself calls.

    Vercel sends the project's ``CRON_SECRET`` env var back as this exact header shape
    (``Authorization: Bearer <value>``) on every cron-triggered request; there is no
    other verification mechanism it offers (no request signing, no fixed source IP to
    allowlist). Anyone who learns the URL could otherwise trigger it, same reasoning
    as ``require_ops_key`` above.
    """
    if not settings.cron_secret:
        raise CronSecretNotConfiguredError()
    if not authorization:
        raise CronSecretMissingError()
    expected = f"Bearer {settings.cron_secret}"
    if not hmac.compare_digest(authorization, expected):
        raise CronSecretInvalidError()
