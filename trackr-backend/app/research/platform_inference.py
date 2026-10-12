"""The restricted platform inference adapter for BT-P02 hosted jobs (RP 01).

THE ONLY MODULE THAT READS THE PLATFORM CREDENTIAL (``Settings.platform_inference_key``), and
only when called from the hosted worker (``app/jobs/hosted.py``). ``make lint`` refuses the
setting's name anywhere else. What that buys:

* not a general proxy: one provider (allowlisted), one operator-configured model, the SDK's
  default endpoint (no base-URL override, so no arbitrary egress), fixed token caps from the
  sealed job, a closed purpose enum, and a structured-output schema supplied by the caller;
* never a user key: the only key it ever uses comes from the worker's runtime environment. A
  personal BYOK key stays in the browser (BE 23) and has no path into this module;
* never in a record: the key is not returned, logged, placed in a packet, a checkpoint, a
  charge row, a queue payload or an exception message.

NO PROVIDER CALL IS MADE BY IMPORTING OR CONFIGURING THIS. And nothing here promises exact-once
billing: after a timeout or dropped connection the provider may have run and billed the
request, so such outcomes are AMBIGUOUS and the caller charges the full reservation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.config import Settings

ALLOWED_PROVIDERS = frozenset({"anthropic"})
PURPOSES = frozenset({"COMPANY_REPORT", "NOTEBOOK_REFLECTION"})
MILLION = Decimal(1_000_000)


class PlatformNotConfigured(RuntimeError):
    """A required operator value is missing. Admission refuses; nothing is invoked."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Pricing:
    provider: str
    model: str
    price_version: str
    currency: str
    input_per_mtok: Decimal
    output_per_mtok: Decimal

    def worst_case(self, max_input: int, max_output: int) -> Decimal:
        """The most one call with these caps can cost -- what is reserved before it."""
        return (Decimal(max_input) * self.input_per_mtok
                + Decimal(max_output) * self.output_per_mtok) / MILLION

    def actual(self, input_tokens: int, output_tokens: int) -> Decimal:
        return (Decimal(input_tokens) * self.input_per_mtok
                + Decimal(output_tokens) * self.output_per_mtok) / MILLION


def _decimal(value: str, name: str) -> Decimal:
    try:
        d = Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise PlatformNotConfigured(f"{name.upper()}_NOT_CONFIGURED") from exc
    if not d.is_finite() or d <= 0:
        raise PlatformNotConfigured(f"{name.upper()}_NOT_CONFIGURED")
    return d


def pricing(settings: Settings) -> Pricing:
    """The operator-declared model and prices, or PlatformNotConfigured naming what is missing.
    Never a default: an undeclared price would be an invented charge."""
    if not settings.hosted_jobs_enabled:
        raise PlatformNotConfigured("HOSTED_JOBS_DISABLED")
    if settings.platform_inference_provider not in ALLOWED_PROVIDERS:
        raise PlatformNotConfigured("PROVIDER_NOT_ALLOWLISTED")
    if not settings.platform_inference_model:
        raise PlatformNotConfigured("MODEL_NOT_CONFIGURED")
    if not settings.platform_inference_price_version:
        raise PlatformNotConfigured("PRICE_VERSION_NOT_CONFIGURED")
    if len(settings.platform_inference_currency) != 3 or \
            not settings.platform_inference_currency.isupper():
        raise PlatformNotConfigured("CURRENCY_NOT_CONFIGURED")
    if not settings.hosted_delegation_signing_key:
        raise PlatformNotConfigured("DELEGATION_KEY_NOT_CONFIGURED")
    return Pricing(
        settings.platform_inference_provider, settings.platform_inference_model,
        settings.platform_inference_price_version, settings.platform_inference_currency,
        _decimal(settings.platform_inference_input_price_per_mtok, "input_price"),
        _decimal(settings.platform_inference_output_price_per_mtok, "output_price"),
    )


def ready(settings: Settings) -> Pricing:
    """Admission-time check: pricing declared, credential present, SDK importable. Called by
    the API before enqueueing (without ever touching the key's value beyond presence)."""
    p = pricing(settings)
    if not settings.platform_inference_key:
        raise PlatformNotConfigured("PLATFORM_CREDENTIAL_NOT_CONFIGURED")
    try:
        import anthropic  # noqa: F401
    except ImportError as exc:
        raise PlatformNotConfigured("PROVIDER_SDK_NOT_INSTALLED") from exc
    return p


@dataclass(frozen=True)
class InferenceResult:
    #: OK: a response arrived. REFUSED: the provider declined (billed per usage).
    #: NOT_SENT: provably not processed (4xx other than timeouts) -- release the reservation.
    #: AMBIGUOUS: timeout / connection / 5xx -- may have been billed; charge the reservation.
    outcome: str
    payload: dict[str, Any] | None
    input_tokens: int | None
    output_tokens: int | None
    error_code: str | None = None


def invoke(settings: Settings, purpose: str, system: str, user_content: str,
           output_schema: dict[str, Any], max_output_tokens: int,
           timeout_seconds: float) -> InferenceResult:
    """One bounded, structured call. The caller has already reserved budget, rechecked the
    job's authority and sealed the packet; this does exactly one request with SDK retries OFF
    (a silent SDK retry would be an unaccounted second charge)."""
    if purpose not in PURPOSES:
        raise ValueError("purpose not authorised for platform inference")
    p = pricing(settings)
    key = settings.platform_inference_key
    if not key:
        raise PlatformNotConfigured("PLATFORM_CREDENTIAL_NOT_CONFIGURED")
    import anthropic

    client = anthropic.Anthropic(api_key=key, max_retries=0, timeout=timeout_seconds)
    try:
        response = client.messages.create(
            model=p.model,
            max_tokens=max_output_tokens,
            system=system,
            messages=[{"role": "user", "content": user_content}],
            output_config={"format": {"type": "json_schema", "schema": output_schema}},
        )
    except (anthropic.APITimeoutError, anthropic.APIConnectionError):
        return InferenceResult("AMBIGUOUS", None, None, None, "NETWORK_AMBIGUOUS")
    except anthropic.RateLimitError:
        return InferenceResult("NOT_SENT", None, None, None, "RATE_LIMITED")
    except anthropic.APIStatusError as exc:
        if exc.status_code >= 500:
            return InferenceResult("AMBIGUOUS", None, None, None, "PROVIDER_5XX")
        return InferenceResult("NOT_SENT", None, None, None, f"PROVIDER_{exc.status_code}")

    usage = response.usage
    in_tok, out_tok = int(usage.input_tokens), int(usage.output_tokens)
    if response.stop_reason == "refusal":
        return InferenceResult("REFUSED", None, in_tok, out_tok, "PROVIDER_REFUSED")
    if response.stop_reason == "max_tokens":
        return InferenceResult("OK", None, in_tok, out_tok, "OUTPUT_TRUNCATED")
    text = "".join(b.text for b in response.content if b.type == "text")
    try:
        payload = json.loads(text)
    except ValueError:
        return InferenceResult("OK", None, in_tok, out_tok, "OUTPUT_NOT_JSON")
    if not isinstance(payload, dict):
        return InferenceResult("OK", None, in_tok, out_tok, "OUTPUT_NOT_OBJECT")
    return InferenceResult("OK", payload, in_tok, out_tok)
