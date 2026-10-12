"""Thin client for TypeSafe's Jev -- a fast, cheap classification call, not a chat model.

Plain ``urllib.request``, no SDK, same reason ``app/ai/llm.py`` gives: one POST, no
streaming, no auth flow beyond a bearer token.

Like its siblings this is a dumb HTTP client: it knows nothing about dividends, ISINs, or
NSE subject lines. It sends a state blob and one Choice question, and returns whatever
typed answer came back. The caller decides what the state and criteria mean, and whether
the returned confidence clears its own bar for acting -- see
``app/ingest/corporate_action_feed.py`` for the one caller that exists today.

The boundary this crosses, stated as plainly as ``llm.py`` states its own
-----------------------------------------------------------------------
What crosses this boundary for that one caller is a public NSE corporate-action subject
line plus the ISIN and symbol it was filed against -- a company-wide announcement, not an
investor's data. No PAN, no demat, no holding, no name. That is a materially different fact
from ``app/ai/llm.py``'s transmission of redacted broker-document text, which is why this
client is not gated the way ``LLM_PARSING_ENABLED`` gates that one -- see ``app/config.py``'s
``jev_api_key`` field. If a future caller ever wants to send this client anything about a
specific investor, that reasoning no longer holds and this module's gating should be
revisited before it does.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from typing import Any

_ENDPOINT = "https://api.typesafe.ai/v1/systemone"

#: One Choice call over a short subject line -- generous relative to that, not to a
#: document upload. Nothing here waits on a person the way llm_extract's 30s does.
_TIMEOUT_SECONDS = 15

_USER_AGENT = "trackr-plus/1.0 (+https://github.com/trackr-plus)"

#: The one question ID this client sends per call. Fixed rather than caller-supplied:
#: ``choose`` asks exactly one Choice question per request, so there is nothing for a
#: second name to disambiguate. Jev's API supports asking several questions at once, but
#: no caller here needs more than one yet -- widen this the day one does.
_QUESTION_ID = "choice"


@lru_cache(maxsize=1)
def _ssl_context() -> ssl.SSLContext:
    """Same fix, same reason, as app/ai/llm.py's ``_ssl_context``: Python does not use
    the OS trust store, so a fresh install or a minimal container fails
    CERTIFICATE_VERIFY_FAILED against a host curl reaches fine. Verification is never
    disabled -- only the CA bundle is made explicit, via certifi."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class JevUnavailable(Exception):
    """Transport: unreachable, timed out, rate-limited, or a non-200.

    NOT a data problem. The caller degrades to exactly the behaviour it had before this
    feature existed. Mirrors ``app.ai.llm.LlmUnavailable``.
    """


class JevResponseError(Exception):
    """HTTP 200, and the body is not the shape asked for -- missing the answer, a choice
    outside the criteria offered, or a confidence that is not a number. Mirrors
    ``app.ai.llm.LlmResponseError``, and is kept distinct from ``JevUnavailable`` for the
    same reason: "the network was down" and "the answer changed shape" need different
    responses.
    """


@dataclass(frozen=True, slots=True)
class ChoiceAnswer:
    """One Choice question's answer: what was picked, how sure, and the full
    distribution -- so a caller can apply its own confidence bar rather than trusting
    ``choice`` unconditionally.

    ``confidence`` and ``probabilities`` are ``Decimal``, not ``float``, for the same
    reason ``app.ai.llm``'s JSON parsing is: a value a caller compares against a
    threshold should not carry binary floating-point error, however small.
    """

    choice: str
    confidence: Decimal
    probabilities: dict[str, Decimal] = field(default_factory=dict)


class JevClient:
    """One model, one endpoint, one Choice question per round trip.

    ``opener`` is injectable for the same reason ``app.ai.llm.LlmClient`` makes it
    injectable: every test in this repo runs offline, and a client that could only be
    tested by reaching Jev would not be tested.
    """

    def __init__(self, api_key: str, model: str = "jev-latest", opener: Any = None) -> None:
        self._api_key = api_key
        self.model = model
        self._opener = opener or urllib.request.urlopen

    def choose(
        self, state: dict[str, Any], instructions: str, criteria: dict[str, str]
    ) -> ChoiceAnswer:
        """Ask one Choice question over ``state``. ``criteria`` maps option name ->
        description; the returned ``choice`` is always checked to be one of its keys,
        never merely assumed.

        Raises:
            JevUnavailable: the request did not complete.
            JevResponseError: it completed and the body is unusable.
        """
        body = {
            "state": state,
            "model": self.model,
            "questions": {
                _QUESTION_ID: {
                    "type": "choice",
                    "instructions": instructions,
                    "criteria": criteria,
                }
            },
        }
        request = urllib.request.Request(
            _ENDPOINT,
            data=json.dumps(body).encode("utf-8"),
            headers={
                # A header, not a query string -- same reasoning as app.ai.llm: a key in
                # a URL ends up in proxy logs and in any exception that quotes it.
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "User-Agent": _USER_AGENT,
            },
            method="POST",
        )

        try:
            with self._opener(
                request, timeout=_TIMEOUT_SECONDS, context=_ssl_context()
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # The response body is deliberately NOT read into this message -- same
            # reasoning as app.ai.llm.LlmClient: a 401 or quota-error body can echo
            # request context, and this exception is going to be logged.
            raise JevUnavailable(f"Jev returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise JevUnavailable(f"could not reach Jev: {type(exc).__name__}") from exc

        return _parse(raw, criteria)


def _as_decimal(value: Any) -> Decimal | None:
    """``parse_float=Decimal`` below only changes how a JSON *float* is read -- a whole
    number like ``1`` still arrives as a plain ``int``. Both are legitimate ways for a
    probability of exactly 0 or 1 to be spelled, so both are accepted here rather than
    only the one the model happens to have chosen this time."""
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    return None


def _parse(raw: bytes, criteria: dict[str, str]) -> ChoiceAnswer:
    try:
        payload = json.loads(raw, parse_float=Decimal)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise JevResponseError("Jev's response was not JSON") from exc

    try:
        answer = payload["answers"][_QUESTION_ID]
    except (KeyError, TypeError) as exc:
        raise JevResponseError("Jev returned no answer for the question asked") from exc

    if not isinstance(answer, dict):
        raise JevResponseError(f"Jev's answer was {type(answer).__name__}, not an object")

    choice = answer.get("choice")
    if not isinstance(choice, str) or choice not in criteria:
        raise JevResponseError(
            f"Jev's choice {choice!r} is not one of the options offered {sorted(criteria)}"
        )

    confidence = _as_decimal(answer.get("confidence"))
    if confidence is None:
        raise JevResponseError(f"Jev's confidence was not a number: {answer.get('confidence')!r}")

    probabilities: dict[str, Decimal] = {}
    probabilities_raw = answer.get("probabilities")
    if isinstance(probabilities_raw, dict):
        for key, value in probabilities_raw.items():
            decimal_value = _as_decimal(value)
            if decimal_value is not None:
                probabilities[key] = decimal_value

    return ChoiceAnswer(choice=choice, confidence=confidence, probabilities=probabilities)
