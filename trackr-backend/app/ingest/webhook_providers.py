"""Per-provider request verification and payload translation for ``POST /ingest/email``.

The three inbound-parse providers this endpoint accepts do not share one request shape
or one verification scheme:

- **Mailgun** posts ``multipart/form-data`` with numbered attachment fields
  (``attachment-1``, ``attachment-2``, ...) and signs the request: ``signature`` must
  equal ``hmac_sha256(signing_key, timestamp + token)``.
- **SendGrid** also posts ``multipart/form-data`` with numbered attachment fields
  (``attachment1``, ``attachment2``, ...), but signs nothing -- Inbound Parse has no
  custom-header support, so a shared secret travels as HTTP Basic Auth instead
  (typically by embedding it in the configured webhook URL).
- **Postmark** posts JSON, differently cased (``To``, ``From``,
  ``Attachments: [{Name, Content, ContentType}]``, ``Content`` already base64), and
  also signs nothing -- HTTP Basic Auth against a configured user/password pair.

Isolating that variance here, behind one ``verify_and_translate``, is what lets
``app.ingest.webhook.handle_inbound_email`` stay exactly what its own docstring says it
is: provider-ignorant. It only ever sees an ``InboundMessage``, whose ``attachments``
carry the one internal shape it already consumed before this module existed --
``{filename, content_type, content_b64}`` per attachment.

WHY THE MESSAGE CARRIES A SENDER AND A BODY. It used to be a bare ``(to, attachments)``
tuple, which was enough for every document this endpoint stages and not enough for the
one message that has to arrive before any of them can. Gmail refuses to forward to an
address until it has verified it, and it verifies by mailing a numeric code -- in the
BODY, with no attachment, from ``forwarding-noreply@google.com``. Against a tuple that
carried neither field, that message arrived, matched nothing, and was dropped, so no
client could ever finish Gmail's own setup (docs/ONBOARDING_FLOW.md, defect 2). Both
fields are optional in practice: a provider that omits them yields ``""``, and the
document path never reads them.

Verification always runs before translation, and translation never runs at all if
verification fails -- an unverified request must never reach ``stage_batch``.
"""

from __future__ import annotations

import base64
import binascii
import email
import email.header
import email.message
import email.utils
import hashlib
import hmac
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from fastapi import Request

if TYPE_CHECKING:
    from app.config import Settings


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """One inbound email, normalised across the three providers.

    ``to`` is the ENVELOPE recipient, never the ``To:`` header: when Gmail auto-forwards,
    ``To:`` still names the client's own mailbox and only the envelope names ours.
    """

    to: str
    attachments: list[dict[str, Any]] = field(default_factory=list)
    #: Envelope/header sender, lowercased by the translators so callers may compare it
    #: directly. ``""`` when the provider does not supply one.
    sender: str = ""
    #: The plain-text body. ``""`` when absent -- most staged documents have no body
    #: worth reading, and nothing on the document path looks at it.
    body_text: str = ""
    #: ``Subject`` of the outermost message. Kept because it is how a person recognises a
    #: delivery in the import list ("Forwarded: 100 contract notes"), not because
    #: anything routes on it -- routing is by address, always.
    subject: str = ""
    #: ``Message-ID`` of the outermost message, for provenance on the delivery itself.
    message_id: str = ""


#: How deep to follow nested ``message/rfc822`` parts. Gmail's bulk forward produces
#: exactly one level, so anything past a couple of levels is either a forward of a
#: forward or someone probing for a parser bomb. Bounded rather than trusted.
_MAX_NESTING_DEPTH = 20

#: Attachment container types that are a MESSAGE rather than a document. These are what
#: Gmail's "Forward as attachment" produces, and what nothing in this codebase could read
#: before: file_validation sniffs the container, finds neither PDF nor CSV nor XLSX, and
#: politely refuses all of them -- so a bulk forward delivered nothing at all.
_MESSAGE_TYPES = frozenset({"message/rfc822", "message/global", "text/rfc822-headers"})


def _header(msg: email.message.Message, name: str) -> str:
    """One header, RFC 2047-decoded, whitespace-collapsed.

    Subjects arrive as ``=?UTF-8?B?...?=`` often enough that reading them raw would put
    mojibake in front of a person. Never raises: a malformed header yields "" rather than
    failing a whole delivery, because a subject is provenance and not data anyone acts on.
    """
    raw = msg.get(name)
    if not raw:
        return ""
    try:
        parts = email.header.decode_header(str(raw))
        out = "".join(
            fragment.decode(charset or "utf-8", errors="replace")
            if isinstance(fragment, bytes)
            else fragment
            for fragment, charset in parts
        )
    except Exception:  # noqa: BLE001 - provenance must never fail a delivery
        out = str(raw)
    return " ".join(out.split())


def _email_meta(msg: email.message.Message) -> dict[str, str]:
    """The four fields the document archive indexes on.

    ``date`` is normalised to ISO-8601 where it parses, and left as the raw header where
    it does not -- a date we could not read is still better provenance than no date, and
    silently substituting "now" would be inventing history.

    NOTE what this is not: the email's date is NOT evidence of a trade date. It selects
    which stored documents are worth opening; only the parsed document states what
    happened. See docs/BACKFILL_PIPELINE.md and migration 038's column comment.
    """
    raw_date = _header(msg, "Date")
    iso = raw_date
    try:
        parsed = email.utils.parsedate_to_datetime(raw_date) if raw_date else None
        if parsed is not None:
            iso = parsed.isoformat()
    except (TypeError, ValueError):
        pass
    return {
        "subject": _header(msg, "Subject"),
        "from": _header(msg, "From"),
        "date": iso,
        "message_id": _header(msg, "Message-ID"),
    }


def _walk_parts(
    msg: email.message.Message,
    meta: dict[str, str],
    depth: int,
    out: list[dict[str, Any]],
) -> None:
    """Collect every document-shaped leaf under ``msg``, tagged with the innermost
    message's headers.

    ``meta`` is the provenance of the message this part came from, so a PDF two levels
    down inside a forward-of-a-forward is attributed to the original Zerodha mail rather
    than to the wrapper the client sent us. That attribution is the entire value of
    unwrapping: the wrapper's date is the day they got round to forwarding, and the
    original's date is the day the note was issued.
    """
    if depth > _MAX_NESTING_DEPTH:
        out.append({"filename": msg.get_filename() or "nested-message.eml",
                    "content_type": msg.get_content_type(), "content_b64": "",
                    "email_meta": meta,
                    "extraction_error": "MIME nesting limit exceeded; original delivery retained"})
        return

    content_type = (msg.get_content_type() or "").lower()

    if content_type in _MESSAGE_TYPES:
        # A nested message. Its own headers supersede the parent's for everything inside.
        for inner in msg.get_payload():
            if isinstance(inner, email.message.Message):
                _walk_parts(inner, _email_meta(inner), depth + 1, out)
        return

    if msg.is_multipart():
        for part in msg.get_payload():
            if isinstance(part, email.message.Message):
                _walk_parts(part, meta, depth + 1, out)
        return

    # A leaf. Keep it if it looks like a file rather than the message body: either it is
    # explicitly an attachment, or it has a filename. Deliberately NOT filtered by
    # content type here -- file_validation decides what a document is, from the bytes,
    # and duplicating that judgement in two places is how the two drift apart.
    filename = msg.get_filename() or ""
    disposition = (msg.get_content_disposition() or "").lower()
    if (disposition != "attachment" and not filename
            and not content_type.startswith("application/")
            and content_type not in ("text/csv", "text/tab-separated-values")):
        return
    try:
        payload = msg.get_payload(decode=True)
    except Exception:  # noqa: BLE001 - one unreadable part must not lose the others
        out.append({"filename": filename, "content_type": content_type,
                    "content_b64": "", "email_meta": meta,
                    "extraction_error": "Attachment could not be decoded; original retained"})
        return
    out.append(
        {
            "filename": _header(msg, "Content-Description") if not filename else filename,
            "content_type": content_type or "application/octet-stream",
            "content_b64": base64.b64encode(payload or b"").decode("ascii"),
            "email_meta": meta,
        }
    )


def unwrap_attachments(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace every ``message/rfc822`` attachment with the documents inside it.

    WHY THIS IS NOT OPTIONAL. A Gmail filter's "Forward it to" action applies only to NEW
    mail -- checking "also apply to matching conversations" does not forward anything
    retroactively. So the only way a client's existing three years of contract notes can
    reach this system by email is selecting them and choosing "Forward as attachment",
    which nests each original as a ``message/rfc822`` part. Without this function that
    delivery arrives, matches no known document type, and is refused in full.

    Non-message attachments pass through untouched, so a plain single-document forward --
    the ordinary case, and every existing test -- behaves exactly as before. Each
    extracted attachment carries an ``email_meta`` dict describing the message it came
    out of; a pass-through attachment has none, and its provenance is the delivery itself.
    """
    unwrapped: list[dict[str, Any]] = []
    for attachment in attachments:
        if attachment.get("extraction_error"):
            unwrapped.append(attachment)
            continue
        content_type = str(attachment.get("content_type", "")).lower().split(";")[0].strip()
        filename = str(attachment.get("filename", ""))
        is_message = content_type in _MESSAGE_TYPES or filename.lower().endswith(".eml")
        if not is_message:
            unwrapped.append(attachment)
            continue
        try:
            raw = base64.b64decode(attachment.get("content_b64", ""), validate=False)
            inner = email.message_from_bytes(raw)
        except Exception:  # noqa: BLE001 - keep the container rather than lose it
            # An .eml we cannot parse is handed on as-is: file_validation will refuse it
            # with a sentence naming the file, which is a better outcome than dropping it
            # silently and reporting a delivery of nothing.
            unwrapped.append(attachment)
            continue
        found: list[dict[str, Any]] = []
        _walk_parts(inner, _email_meta(inner), 0, found)
        if found:
            unwrapped.extend(found)
        else:
            # A message that parsed but carried nothing document-shaped. Handing the
            # container on keeps the delivery honest: the caller reports one refused
            # attachment naming the file, instead of "0 documents" with no explanation.
            unwrapped.append(attachment)
    return unwrapped


class WebhookAuthError(Exception):
    """Base for a failed webhook credential check."""


class MissingWebhookCredentialError(WebhookAuthError):
    """No credential was presented at all, or it was malformed -- 401."""


class InvalidWebhookCredentialError(WebhookAuthError):
    """A credential was presented but does not check out -- 403."""


class WebhookNotConfiguredError(Exception):
    """``INGEST_WEBHOOK_PROVIDER`` is unset -- there is no verifier to run.

    Never a 200 that stages an unverified file: an unconfigured deploy must refuse
    every request here, the same as a wrongly-configured one does.
    """


# --- shared HTTP Basic Auth helpers (Postmark, SendGrid) --------------------------


def _parse_basic_auth(request: Request) -> tuple[str, str]:
    header = request.headers.get("authorization", "")
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        raise MissingWebhookCredentialError("missing Basic Authorization header")
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise MissingWebhookCredentialError("malformed Basic Authorization header") from exc
    username, sep, password = decoded.partition(":")
    if not sep:
        raise MissingWebhookCredentialError("malformed Basic Authorization header")
    return username, password


async def _numbered_attachments(
    request: Request, prefix: str, count_field: str
) -> list[dict[str, Any]]:
    """``attachment-count``/``attachment-1..N`` (Mailgun) or ``attachments``/
    ``attachment1..N`` (SendGrid) -- real file uploads, not base64, so each is read and
    re-encoded into the one shape ``handle_inbound_email`` already consumes."""
    form = await request.form()
    try:
        count = int(form.get(count_field, 0))
    except (TypeError, ValueError):
        count = 0

    attachments = []
    for i in range(1, count + 1):
        upload = form.get(f"{prefix}{i}")
        if upload is None or not hasattr(upload, "read"):
            continue
        content = await upload.read()
        attachments.append(
            {
                "filename": upload.filename or "",
                "content_type": upload.content_type or "application/octet-stream",
                "content_b64": base64.b64encode(content).decode("ascii"),
            }
        )
    return attachments


# --- Mailgun ------------------------------------------------------------------------


async def _verify_mailgun(request: Request, settings: Settings) -> None:
    form = await request.form()
    timestamp = form.get("timestamp")
    token = form.get("token")
    signature = form.get("signature")
    if not timestamp or not token or not signature:
        raise MissingWebhookCredentialError("mailgun signature fields missing")

    # settings.ingest_mailgun_signing_key is guaranteed set: app.config.Settings
    # validates this combination at startup, not per-request.
    expected = hmac.new(
        settings.ingest_mailgun_signing_key.encode(),
        f"{timestamp}{token}".encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, str(signature)):
        raise InvalidWebhookCredentialError("mailgun signature does not match")


async def _translate_mailgun(request: Request) -> InboundMessage:
    """Mailgun, in either of its two inbound modes.

    A route whose destination URL ends in ``mime``/``raw-mime`` posts the ORIGINAL message
    whole, in ``body-mime``, and omits the parsed body and numbered attachment fields.
    Raw MIME is the better mode for this product and is preferred whenever present, for
    two reasons: nested ``message/rfc822`` parts are walked here deterministically instead
    of depending on whether a provider chose to recurse, and the ORIGINAL message's Date,
    From, Subject and Message-ID survive rather than the forwarding wrapper's. Those four
    fields are the archive index.

    The parsed mode is still fully supported -- it is what a plain single-document forward
    arrives as, and switching modes is a change to a route's URL in someone else's
    dashboard, which this code must not assume has happened.
    """
    form = await request.form()
    to = str(form.get("recipient", ""))

    raw_mime = form.get("body-mime")
    if raw_mime:
        raw = raw_mime.encode("utf-8", errors="replace") if isinstance(raw_mime, str) else raw_mime
        return _from_raw_mime(raw, to=to)

    sender = str(form.get("sender", "")).strip().lower()
    body_text = str(form.get("body-plain", ""))
    attachments = await _numbered_attachments(
        request, prefix="attachment-", count_field="attachment-count"
    )
    return InboundMessage(
        to=to,
        attachments=attachments,
        sender=sender,
        body_text=body_text,
        subject=str(form.get("subject", "")),
        message_id=str(form.get("Message-Id", "") or form.get("message-id", "")),
    )


def _from_raw_mime(raw: bytes, *, to: str) -> InboundMessage:
    """One whole RFC-822 message -> ``InboundMessage``. Shared by any provider that can
    hand over raw MIME, so the walking logic exists once."""
    msg = email.message_from_bytes(raw)
    meta = _email_meta(msg)
    attachments: list[dict[str, Any]] = []
    _walk_parts(msg, meta, 0, attachments)

    body_text = ""
    for part in msg.walk():
        if part.get_content_type() == "text/plain" and not part.get_filename():
            payload = part.get_payload(decode=True)
            if payload:
                body_text = payload.decode(part.get_content_charset() or "utf-8", "replace")
                break

    sender = ""
    addresses = email.utils.getaddresses([meta.get("from", "")])
    if addresses:
        sender = (addresses[0][1] or "").strip().lower()

    return InboundMessage(
        to=to,
        attachments=attachments,
        sender=sender,
        body_text=body_text,
        subject=meta.get("subject", ""),
        message_id=meta.get("message_id", ""),
    )


# --- SendGrid -----------------------------------------------------------------------


async def _verify_sendgrid(request: Request, settings: Settings) -> None:
    _username, password = _parse_basic_auth(request)
    # settings.ingest_sendgrid_shared_secret is guaranteed set -- see _verify_mailgun.
    if not hmac.compare_digest(password, settings.ingest_sendgrid_shared_secret):
        raise InvalidWebhookCredentialError("sendgrid shared secret does not match")


async def _translate_sendgrid(request: Request) -> InboundMessage:
    form = await request.form()
    to = str(form.get("to", ""))
    sender = str(form.get("from", "")).strip().lower()
    body_text = str(form.get("text", ""))
    attachments = await _numbered_attachments(
        request, prefix="attachment", count_field="attachments"
    )
    return InboundMessage(to=to, attachments=attachments, sender=sender, body_text=body_text)


# --- Postmark -----------------------------------------------------------------------


async def _verify_postmark(request: Request, settings: Settings) -> None:
    username, password = _parse_basic_auth(request)
    # settings.ingest_postmark_basic_auth_user/_password are guaranteed set -- see
    # _verify_mailgun.
    user_ok = hmac.compare_digest(username, settings.ingest_postmark_basic_auth_user)
    password_ok = hmac.compare_digest(password, settings.ingest_postmark_basic_auth_password)
    if not (user_ok and password_ok):
        raise InvalidWebhookCredentialError("postmark basic auth credentials do not match")


async def _translate_postmark(request: Request) -> InboundMessage:
    body = await request.json()
    to = str(body.get("To", ""))
    sender = str(body.get("From", "")).strip().lower()
    attachments = [
        {
            "filename": a.get("Name", ""),
            "content_type": a.get("ContentType", "application/octet-stream"),
            # Postmark already base64-encodes attachment content in the JSON payload --
            # no re-encoding needed, unlike the multipart providers above.
            "content_b64": a.get("Content", ""),
        }
        for a in body.get("Attachments", [])
    ]
    return InboundMessage(
        to=to, attachments=attachments, sender=sender, body_text=str(body.get("TextBody", ""))
    )


# --- registry, selected by INGEST_WEBHOOK_PROVIDER --------------------------------

_VERIFIERS: dict[str, Callable[[Request, Settings], Awaitable[None]]] = {
    "mailgun": _verify_mailgun,
    "sendgrid": _verify_sendgrid,
    "postmark": _verify_postmark,
}

_TRANSLATORS: dict[str, Callable[[Request], Awaitable[InboundMessage]]] = {
    "mailgun": _translate_mailgun,
    "sendgrid": _translate_sendgrid,
    "postmark": _translate_postmark,
}


async def verify_and_translate(request: Request, settings: Settings) -> InboundMessage:
    """Verify the inbound request's provider-specific credential, then translate its
    provider-specific shape into an ``InboundMessage`` -- the internal shape
    ``app.ingest.webhook.handle_inbound_email`` already consumes.

    Raises:
        MissingWebhookCredentialError: no credential was presented, or it was
            malformed -- the router answers 401.
        InvalidWebhookCredentialError: a credential was presented but does not check
            out -- the router answers 403.

    ``settings.ingest_webhook_provider`` is validated to be one of this module's three
    registered names at Settings construction (see ``app.config``), so a configured
    deployment always resolves here; an unconfigured one (``None``) is handled by the
    router before this is called.
    """
    verify = _VERIFIERS[settings.ingest_webhook_provider]
    translate = _TRANSLATORS[settings.ingest_webhook_provider]
    await verify(request, settings)
    message = await translate(request)
    # Applied for EVERY provider, after translation, because a client can bulk-forward
    # through any of them -- the nesting is Gmail's doing, not the provider's. A delivery
    # with no nested message is returned unchanged.
    return replace(message, attachments=unwrap_attachments(message.attachments))
