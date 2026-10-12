"""Failure notification for the price pipe.

BUILD_PLAN says "failure alerts by email". ``EmailNotifier`` below is that: plain
``smtplib`` from the standard library, no provider SDK, because nothing else in this
repo needs a heavier dependency for one outbound email per failure. ``get_notifier``
picks it when SMTP is configured (``app/config.py``'s ``Settings``) and falls back to
``LoggingNotifier`` otherwise -- the same "required only when selected" convention
``app/storage/factory.py`` uses for the S3 backend.

A notifier whose own delivery attempt can raise would turn "alert on this failure" into
"crash while alerting on this failure", which is worse than either logging or emailing
alone: it could take down the rest of a price-pipe run that otherwise had prices worth
storing. ``EmailNotifier.notify`` never propagates -- see its docstring.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from app.config import Settings

logger = logging.getLogger("prices")


@runtime_checkable
class Notifier(Protocol):
    def notify(self, subject: str, detail: str, context: dict[str, Any]) -> None: ...


class LoggingNotifier:
    """The fallback. Writes at ERROR and nothing else.

    Deliberately noisy in the logs and deliberately NOT silent-by-default: a pipe whose
    failure path does nothing at all is indistinguishable from one that never fails.
    """

    def notify(self, subject: str, detail: str, context: dict[str, Any]) -> None:
        logger.error("PRICE PIPE ALERT: %s -- %s | %s", subject, detail, context)


class CollectingNotifier:
    """Records alerts instead of sending them. For tests, and for a dry run."""

    def __init__(self) -> None:
        self.alerts: list[dict[str, Any]] = []

    def notify(self, subject: str, detail: str, context: dict[str, Any]) -> None:
        self.alerts.append({"subject": subject, "detail": detail, "context": context})


def _header_safe(value: str) -> str:
    """Strip CR/LF so a fetch-error message can never smuggle extra headers into the
    email -- ``notify``'s callers are trusted today, but the Protocol is not scoped to
    stay that way."""
    return value.replace("\r", " ").replace("\n", " ")


class EmailNotifier:
    """Sends one plain-text email per alert over SMTP.

    ``host``/``from_address``/``recipient`` have no safe default (same reasoning as
    every other secret-adjacent field in ``app/config.py``'s ``Settings``): construct
    this only once real values are available, which ``get_notifier`` below enforces.
    """

    def __init__(
        self,
        host: str,
        port: int,
        use_tls: bool,
        from_address: str,
        recipient: str,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        if not host:
            raise ValueError("SMTP host is not set")
        if not from_address:
            raise ValueError("SMTP from-address is not set")
        if not recipient:
            raise ValueError("SMTP alert recipient is not set")
        self.host = host
        self.port = port
        self.use_tls = use_tls
        self.from_address = from_address
        self.recipient = recipient
        self.username = username
        self.password = password

    def notify(self, subject: str, detail: str, context: dict[str, Any]) -> None:
        """Best-effort. Logs and returns rather than raising when SMTP itself fails --
        a broken mail server must not take down the rest of a price-pipe run that still
        has prices worth storing from the exchange that DID answer."""
        message = EmailMessage()
        message["Subject"] = _header_safe(f"[Trackr] {subject}")
        message["From"] = self.from_address
        message["To"] = self.recipient
        body = "\n".join([detail, "", *(f"{key}: {value}" for key, value in context.items())])
        message.set_content(body)

        try:
            with smtplib.SMTP(self.host, self.port, timeout=10) as client:
                if self.use_tls:
                    client.starttls()
                if self.username and self.password:
                    client.login(self.username, self.password)
                client.send_message(message)
        except (smtplib.SMTPException, OSError):
            logger.error(
                "PRICE PIPE ALERT EMAIL FAILED TO SEND (%s -- %s | %s)",
                subject,
                detail,
                context,
                exc_info=True,
            )


def get_notifier(settings: Settings) -> Notifier:
    """``EmailNotifier`` when SMTP is configured, ``LoggingNotifier`` otherwise.

    Deliberately does not raise when SMTP is unset: a price pipe that refuses to run
    because alert delivery isn't configured yet is a worse failure mode than one that
    runs and only logs. The gap is still made loud, just via a WARNING at call time
    rather than a crash.
    """
    if settings.smtp_host and settings.smtp_from_address and settings.smtp_alert_recipient:
        return EmailNotifier(
            host=settings.smtp_host,
            port=settings.smtp_port,
            use_tls=settings.smtp_use_tls,
            from_address=settings.smtp_from_address,
            recipient=settings.smtp_alert_recipient,
            username=settings.smtp_username,
            password=settings.smtp_password,
        )
    logger.warning(
        "SMTP not configured (SMTP_HOST/SMTP_FROM_ADDRESS/SMTP_ALERT_RECIPIENT) -- "
        "price-pipe failures will only be logged, not emailed."
    )
    return LoggingNotifier()
