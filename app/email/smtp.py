"""Authenticated SMTP adapter using TLS and secure runtime credentials."""

from __future__ import annotations

import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage as MIMEMessage

from app.email.base import (
    AmbiguousEmailError,
    EmailMessage,
    EmailProvider,
    PermanentEmailError,
    ProviderReceipt,
    SMTPSettings,
    TransientEmailError,
)
from app.email.credentials import load_smtp_credential


class SMTPEmailProvider(EmailProvider):
    """Send MIME alternatives via authenticated STARTTLS SMTP.

    No credential is retained on the provider instance. Network failures after
    sending begins are ambiguous and are never automatically retried.
    """

    def __init__(
        self,
        settings: SMTPSettings,
        *,
        credential_loader: Callable[[str], str | None] = load_smtp_credential,
        smtp_factory=smtplib.SMTP,
    ) -> None:
        self.settings = settings
        self._credential_loader = credential_loader
        self._smtp_factory = smtp_factory

    @property
    def provider_name(self) -> str:
        return "smtp"

    def send(self, message: EmailMessage) -> ProviderReceipt:
        try:
            password = self._credential_loader(self.settings.username)
        except Exception:
            raise PermanentEmailError(
                "SMTP credential is unavailable from the configured secure credential source."
            ) from None
        if not password:
            raise PermanentEmailError(
                "SMTP credential is not configured in the secure credential source."
            )

        mime_message = MIMEMessage()
        mime_message["From"] = self.settings.sender
        mime_message["To"] = message.recipient
        mime_message["Subject"] = message.subject
        mime_message.set_content(message.plain_text_body)
        mime_message.add_alternative(message.html_body, subtype="html")
        mime_message["X-Personal-News-Delivery-Key"] = message.idempotency_key

        try:
            with self._smtp_factory(
                self.settings.host,
                self.settings.port,
                timeout=self.settings.timeout_seconds,
            ) as client:
                client.ehlo()
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
                client.login(self.settings.username, password)
                refused = client.send_message(
                    mime_message,
                    from_addr=self.settings.sender,
                    to_addrs=[message.recipient],
                )
                if refused:
                    raise PermanentEmailError("SMTP server rejected the recipient.")
        except PermanentEmailError:
            raise
        except smtplib.SMTPAuthenticationError:
            raise PermanentEmailError("SMTP authentication was rejected.") from None
        except smtplib.SMTPRecipientsRefused:
            raise PermanentEmailError("SMTP server rejected the recipient.") from None
        except smtplib.SMTPSenderRefused:
            raise PermanentEmailError("SMTP server rejected the configured sender.") from None
        except smtplib.SMTPDataError as error:
            _raise_smtp_response(
                error.smtp_code,
                transient="SMTP server temporarily rejected the message.",
            )
        except smtplib.SMTPResponseException as error:
            _raise_smtp_response(
                error.smtp_code,
                transient="SMTP server temporarily rejected the request.",
            )
        except (TimeoutError, OSError, smtplib.SMTPServerDisconnected, smtplib.SMTPException):
            raise AmbiguousEmailError(
                "SMTP connection outcome is uncertain; the message was not retried automatically."
            ) from None
        return ProviderReceipt(message_id=None)


def _raise_smtp_response(code: int, *, transient: str) -> None:
    if 400 <= code <= 499:
        raise TransientEmailError(transient) from None
    raise PermanentEmailError("SMTP server rejected the request.") from None
