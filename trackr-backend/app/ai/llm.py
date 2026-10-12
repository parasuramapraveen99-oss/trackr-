"""Thin client for OpenRouter's chat-completions API -- the one LLM transport this backend
uses. The model is env-driven (``OPENROUTER_MODEL``) so it can be changed without a code
change.

Used by the document structuring step (app/ingest/llm_extract.py), analytics narration,
and the rule-20 gap research step. Not by Ask about markets, which has its own pinned,
inspected adapter (app/market_assistant/model.py) and never imports this module.

Plain ``urllib.request``, no SDK: one POST, no streaming, no auth flow beyond a bearer
token. OpenRouter's endpoint is OpenAI-compatible, and a dependency tree would buy nothing
here against the Vercel bundle ceiling ``pyproject.toml`` already flags.

This is a dumb HTTP client: it does not know what a contract note is, what a Decimal is,
or that a ledger exists. It takes a system prompt and a user string and returns whatever
JSON came back. Every judgement about whether that JSON is *true* happens elsewhere,
deterministically, with no network and no model -- ``app/ingest/llm_verify.py`` for
documents, ``app/narrative/verify.py`` for analytics prose.

The boundary, stated plainly
----------------------------
Document extraction sends redacted document text out and gets numbers back. Analytics
narration and the assistant send an investor's derived positions out. That is survivable
for one reason: **nothing returned here is believed.** A value reaches the ledger only
after it has been found in the source document verbatim and reconciled arithmetically.
Analytics figures are rendered server-side from a computed fact pack, and every numeral
in a conversational reply must appear in the facts the model was given. If a future
change makes any value from this module trusted on its own, that reason is gone.

What is sent is only ever what ``app/ingest/redaction.py`` produced -- never a raw
document, never a PDF.

Retries
-------
A rate-limited (HTTP 429) or overloaded (5xx) response is retried after a short wait,
never immediately: the wait honours the ``Retry-After`` header when present, clamped to
[2s, 30s], and otherwise backs off 5s then 10s. After ``_MAX_ATTEMPTS`` tries the client
gives up and raises ``LlmUnavailable``, and the caller degrades as it did before the
feature existed. Client errors (400/401/403/404) are never retried.
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
from decimal import Decimal
from functools import lru_cache
from typing import Any

_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

#: A page of text in, a whole trade table out. Still bounded,
#: because a person is waiting on the upload request this sits inside.
_TIMEOUT_SECONDS = 30

#: Generous enough for a long note's table. Truncation is treated as corruption rather
#: than as a partial answer -- see ``_parse``.
_MAX_OUTPUT_TOKENS = 8192

#: Total tries per call, including the first. Three, then stop: a rate limit that outlasts
#: three short waits is a quota problem, and hammering it only burns the rest of the window.
_MAX_ATTEMPTS = 3

#: Waits between tries when the server gives no ``Retry-After``: 5s, then 10s.
_BACKOFF_SECONDS = (5, 10)

#: Clamp for a server-supplied ``Retry-After``, so a bad header can neither spin nor stall
#: a request for minutes.
_RETRY_AFTER_MIN, _RETRY_AFTER_MAX = 2, 30

#: Status codes worth another try after a wait. Everything else fails on the first answer.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

#: Identifies this client honestly. OpenRouter has no reason to object to a named API
#: client, and pretending to be a browser to an API we hold a key for would be a lie.
_USER_AGENT = "trackr-plus/1.0 (+https://github.com/trackr-plus)"


@lru_cache(maxsize=1)
def _ssl_context() -> ssl.SSLContext:
    """Python does not use the OS trust store, so a fresh install or a minimal container
    fails CERTIFICATE_VERIFY_FAILED against a host curl reaches fine. Verification is
    never disabled -- only the CA bundle is made explicit, via certifi."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class LlmUnavailable(Exception):
    """Transport: unreachable, timed out, still rate-limited after every try, or a non-200.

    NOT a data problem. The caller degrades to exactly the behaviour it had before this
    feature existed -- the document simply does not get an LLM pass.
    """


class LlmResponseError(Exception):
    """HTTP 200, and the body is not the shape we asked for.

    Missing ``choices``, content that is not JSON, or a response cut off at the token
    limit. The model or the API drifted, and a human needs to look. Kept distinct from
    ``LlmUnavailable``: "the network was down" and "the answer changed shape" need
    different responses, and collapsing them hides the second behind the first.
    """


def _retry_wait(attempt: int, exc: urllib.error.HTTPError) -> float:
    """Seconds to wait before try ``attempt + 1``, given the failed try ``attempt``."""
    header = exc.headers.get("Retry-After") if exc.headers is not None else None
    if header is not None:
        try:
            seconds = float(header)
        except ValueError:
            seconds = None
        if seconds is not None:
            return min(max(seconds, _RETRY_AFTER_MIN), _RETRY_AFTER_MAX)
    return _BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)]


class LlmClient:
    """One model, one endpoint, one call (with bounded retries on rate limits).

    ``opener`` and ``sleep`` are injectable: every test in this repo runs offline and
    without waiting, and a client that could only be tested by reaching OpenRouter would
    not be tested.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        opener: Any = None,
        sleep: Any = None,
        max_attempts: int = _MAX_ATTEMPTS,
    ) -> None:
        self._api_key = api_key
        self.model = model
        #: Background research passes 2: a free model's 429 rarely clears within seconds,
        #: and every extra try spends the account's shared daily quota.
        self._max_attempts = max(1, max_attempts)
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep or time.sleep

    def complete_json(self, system_prompt: str, user_content: str) -> dict[str, Any]:
        """Send one prompt, return the parsed JSON object the model produced.

        Raises:
            LlmUnavailable: the request did not complete, or was still rate-limited after
                every try.
            LlmResponseError: it completed and the body is unusable.
        """
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
            # Transcription, not writing. Any sampling at all is a chance to invent a
            # digit, and there is no upside here to a second-best token ever winning.
            "temperature": 0,
            "max_tokens": _MAX_OUTPUT_TOKENS,
            "stream": False,
        }
        data = json.dumps(body).encode("utf-8")

        for attempt in range(self._max_attempts):
            request = urllib.request.Request(
                _ENDPOINT,
                data=data,
                headers={
                    # A header, not a query string. A key in a URL ends up in proxy logs
                    # and in any exception that quotes the request line.
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
                return _parse(raw)
            except urllib.error.HTTPError as exc:
                # The response body is deliberately NOT read into this message. A 401 or a
                # quota error can echo request context, and this exception is logged.
                if exc.code not in _RETRYABLE_STATUS:
                    raise LlmUnavailable(f"OpenRouter returned HTTP {exc.code}") from exc
                if attempt + 1 == self._max_attempts:
                    raise LlmUnavailable(
                        f"OpenRouter returned HTTP {exc.code} on all {self._max_attempts} tries"
                    ) from exc
                self._sleep(_retry_wait(attempt, exc))
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise LlmUnavailable(
                    f"could not reach OpenRouter: {type(exc).__name__}"
                ) from exc
        raise AssertionError("unreachable: the loop returns or raises")


def _parse(raw: bytes) -> dict[str, Any]:
    """The response envelope -> the model's JSON object.

    ``parse_float=Decimal`` on BOTH loads is the single most important detail in this
    module. If the model emits ``"quantity": 4000.5`` as a bare JSON number instead of the
    string it was asked for, stock ``json.loads`` produces a **binary float, and the paise
    are gone before any of this project's code runs**. With this kwarg a float cannot
    exist in the process at all.

    (``llm_verify`` still REJECTS non-string numbers rather than accepting these. Both are
    needed: this one makes the damage impossible, that one makes the drift visible.)
    """
    try:
        payload = json.loads(raw, parse_float=Decimal)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise LlmResponseError("OpenRouter's response envelope was not JSON") from exc

    try:
        choice = payload["choices"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmResponseError("OpenRouter returned no choices") from exc

    # Truncation is the silent-corruption case, and the reason it gets its own check: a
    # JSON array cut off mid-way either fails to parse -- fine, caught below -- or parses
    # as a SHORTER TRADE LIST THAT LOOKS COMPLETE. A note whose last four trades vanished
    # would stage cleanly and reconcile against nothing.
    if choice.get("finish_reason") == "length":
        raise LlmResponseError(
            "OpenRouter's response was truncated at the token limit; a partial table is "
            "not a partial answer"
        )

    content = (choice.get("message") or {}).get("content")
    if not content:
        raise LlmResponseError("OpenRouter returned an empty message")

    try:
        parsed = json.loads(content, parse_float=Decimal)
    except (json.JSONDecodeError, TypeError) as exc:
        raise LlmResponseError("OpenRouter's message content was not JSON") from exc

    if not isinstance(parsed, dict):
        raise LlmResponseError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed
