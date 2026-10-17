"""Phase 3, the half that talks to a model: one bounded selector call, inspected first.

docs/AI_ASSISTANT_PRIVACY_PLAN.md sections 5 and 10. The ONLY file in
``app.market_assistant`` allowed to reach a model provider; ``selection.py`` builds what is
sent and judges what comes back, without network access.

What is sent is fixed: the reviewed ``SYSTEM_PROMPT`` and ``selection.user_content`` built
from the catalogue and admitted official passages. ``check_outbound`` recomputes that
content and refuses the request if a single byte differs, so nothing else (a user's words,
an ID, a URL) can ride along. Provider routing asks for providers that do not collect
prompts and forbids fallbacks; if no free endpoint honours that, the provider refuses and
the assistant falls back to ranked excerpts. It never retries with a weaker policy.

Transport failures are never retried. The caller may request one bounded short rewrite
of an invalid answer, using the same public evidence and outbound checks.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import date
from typing import Any

from app.market_assistant.admission import MAX_HISTORY, Outcome, admit_text, clean_open_text
from app.market_assistant.evidence import is_safe_passage
from app.market_assistant.open_research import ANSWER_PROMPT, PLANNER_PROMPT, REWRITE_PROMPT
from app.market_assistant.open_research import REVIEWED_PROMPTS as OPEN_PROMPTS
from app.market_assistant.selection import SYSTEM_PROMPT

logger = logging.getLogger("market_assistant.model")

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
MAX_OUTPUT_TOKENS = 800
TIMEOUT_SECONDS = 25
MAX_RESPONSE_BYTES = 256 * 1024

#: Ask only providers that do not keep or train on prompts, and never fall back to one
#: that does, or to a paid model.
PROVIDER_POLICY: dict[str, Any] = {"data_collection": "deny", "allow_fallbacks": False}

#: The selector needs no chain of thought: it returns IDs and exact quotes. A reasoning
#: model left on spent the whole 800-token budget thinking and returned no content
#: (live check 11 Oct 2026, finish_reason "length"), so every answer fell back to ranked.
#: Turning reasoning off also keeps the model's private reasoning out of any response.
REASONING_POLICY: dict[str, Any] = {"enabled": False}

_ALLOWED_BODY_KEYS = frozenset(
    {
        "model",
        "messages",
        "response_format",
        "temperature",
        "max_tokens",
        "stream",
        "provider",
        "reasoning",
    }
)
_ALLOWED_HEADERS = frozenset({"authorization", "content-type"})


class ModelUnavailable(Exception):
    """No key, the provider refused (including "no endpoint meets this data policy"), or
    the reply was unusable. Public research reports a service error, not a substitute answer."""


class OutboundPolicyError(Exception):
    """The request about to be sent broke the outbound policy. Nothing was sent."""


def is_pinned_free_model(model: str) -> bool:
    """A single, explicit free model ID. No router (``openrouter/auto``), no list."""
    return (
        bool(model)
        and model.endswith(":free")
        and "/" in model
        and "," not in model
        and not model.startswith("openrouter/")
    )


#: Every system prompt this adapter may send, with its output ceiling. The selector's
#: (Phase 3) plus the open path's planner and answerer (owner amendment 11 Oct 2026).
PROMPT_LIMITS: dict[str, int] = {SYSTEM_PROMPT: MAX_OUTPUT_TOKENS, **OPEN_PROMPTS}


def request_body(
    model: str, user_content: str, system_prompt: str = SYSTEM_PROMPT
) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": PROMPT_LIMITS[system_prompt],
        "stream": False,
        "provider": PROVIDER_POLICY,
        "reasoning": REASONING_POLICY,
    }


def check_outbound(
    body: bytes,
    headers: dict[str, str],
    model: str,
    expected_user: str,
    expected_system: str = SYSTEM_PROMPT,
) -> None:
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OutboundPolicyError("body is not JSON") from exc
    if not isinstance(parsed, dict) or set(parsed) != _ALLOWED_BODY_KEYS:
        raise OutboundPolicyError("body fields are not exactly the reviewed set")
    if parsed["model"] != model or not is_pinned_free_model(model):
        raise OutboundPolicyError("model is not the pinned free model")
    if parsed["provider"] != PROVIDER_POLICY:
        raise OutboundPolicyError("provider policy changed")
    if parsed["reasoning"] != REASONING_POLICY:
        raise OutboundPolicyError("reasoning policy changed")
    if expected_system not in PROMPT_LIMITS or parsed["messages"] != [
        {"role": "system", "content": expected_system},
        {"role": "user", "content": expected_user},
    ]:
        raise OutboundPolicyError("messages are not the reviewed prompt and built content")
    if expected_system in (PLANNER_PROMPT, ANSWER_PROMPT, REWRITE_PROMPT):
        _check_public_content(expected_system, expected_user)
    if parsed["max_tokens"] != PROMPT_LIMITS[expected_system] or parsed["temperature"] != 0:
        raise OutboundPolicyError("output bounds changed")
    if parsed["stream"] is not False or parsed["response_format"] != {"type": "json_object"}:
        raise OutboundPolicyError("streaming or format changed")
    if {k.lower() for k in headers} != _ALLOWED_HEADERS:
        raise OutboundPolicyError("headers are not exactly the reviewed set")


def _admitted(text: object) -> bool:
    return (isinstance(text, str) and bool(text)
            and clean_open_text(text) == text
            and admit_text(text).outcome not in (Outcome.BLOCKED, Outcome.CLARIFY))


def _check_public_content(system: str, content: str) -> None:
    """Repeat privacy admission on the exact final prompt, not just at its call site."""
    try:
        payload = json.loads(content)
    except (ValueError, TypeError):
        raise OutboundPolicyError("public content is not JSON") from None
    keys = (
        {"question", "earlier", "today"}
        if system == PLANNER_PROMPT
        else {"question", "earlier", "advice", "passages"}
    )
    if not isinstance(payload, dict) or set(payload) != keys:
        raise OutboundPolicyError("public content fields changed")
    question = payload.get("question")
    if not _admitted(question):
        raise OutboundPolicyError("question failed final privacy admission")
    earlier = payload.get("earlier")
    if (not isinstance(earlier, list) or len(earlier) > MAX_HISTORY
            or not all(_admitted(text) for text in earlier)):
        raise OutboundPolicyError("earlier question failed final privacy admission")
    if system == PLANNER_PROMPT:
        try:
            date.fromisoformat(payload["today"])
        except (TypeError, ValueError):
            raise OutboundPolicyError("planner date changed") from None
    if system in (ANSWER_PROMPT, REWRITE_PROMPT):
        passages = payload["passages"]
        if (type(payload["advice"]) is not bool or not isinstance(passages, list)
                or len(passages) > 8):
            raise OutboundPolicyError("public passages changed")
        for n, passage in enumerate(passages, 1):
            if (not isinstance(passage, dict)
                    or set(passage) != {"n", "source", "kind", "date", "title", "text"}
                    or passage["n"] != n
                    or passage["kind"] not in ("official", "news", "issuer")
                    or not isinstance(passage["title"], str)
                    or len(passage["title"]) > 200
                    or not is_safe_passage(passage["title"])
                    or not isinstance(passage["text"], str)
                    or len(passage["text"]) > 8000
                    or not is_safe_passage(passage["text"])):
                raise OutboundPolicyError("passage failed final privacy admission")


def _content(payload: bytes) -> str:
    try:
        envelope = json.loads(payload)
        content = envelope["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError, IndexError, TypeError) as exc:
        raise ModelUnavailable("unreadable model response") from exc
    if not isinstance(content, str):
        raise ModelUnavailable("model response has no text content")
    return content


Opener = Callable[..., Any]


class SelectorClient:
    def __init__(self, api_key: str, model: str, opener: Opener | None = None):
        if not api_key:
            raise ModelUnavailable("no model key configured")
        if not is_pinned_free_model(model):
            raise ModelUnavailable("model is not a pinned free model")
        self._key = api_key
        self.model = model
        self._open = opener or urllib.request.urlopen

    def select(self, user_content: str) -> str:
        """Send the inspected selector request; return the raw text for ``validate``."""
        return self.complete(SYSTEM_PROMPT, user_content)

    def complete(self, system_prompt: str, user_content: str) -> str:
        """Send one inspected request with a reviewed system prompt; return raw text."""
        body = json.dumps(request_body(self.model, user_content, system_prompt)).encode()
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        check_outbound(body, headers, self.model, user_content, system_prompt)
        request = urllib.request.Request(ENDPOINT, data=body, headers=headers, method="POST")
        try:
            with self._open(request, timeout=TIMEOUT_SECONDS) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            # The body is not read: a provider error can quote request context.
            logger.warning("market_assistant model refused: HTTP %s", exc.code)
            raise ModelUnavailable(f"HTTP {exc.code}") from exc
        except Exception as exc:  # urllib raises assorted types; all mean "unavailable"
            logger.warning("market_assistant model failed: %s", type(exc).__name__)
            raise ModelUnavailable(type(exc).__name__) from exc
        if len(payload) > MAX_RESPONSE_BYTES:
            raise ModelUnavailable("model response too large")
        return _content(payload)
