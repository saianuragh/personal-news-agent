"""Provider-neutral transactional email contract and safe delivery orchestration."""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal, Protocol

from app.newsletter.renderer import NewsletterDocument

_EMAIL_PATTERN = re.compile(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+\Z")
_DELIVERY_KEY_PATTERN = re.compile(r"[\x21-\x7e]{1,256}\Z")
_NEWSLETTER_FILENAME_PATTERN = re.compile(
    r"newsletter-(?:\d{4}-\d{2}-\d{2}|[0-9a-f]{16})\.(?:html|txt)\Z"
)
_PREVIEW_RETENTION_SECONDS = 30 * 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class EmailSettings:
    """Validated send configuration. The credential is excluded from repr output."""

    api_key: str = field(repr=False)
    sender: str
    recipient: str
    timeout_seconds: float = 15.0
    max_attempts: int = 2

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise ValueError("email API key is required")
        _validate_address(self.sender, "EMAIL_FROM")
        _validate_address(self.recipient, "NEWSLETTER_RECIPIENT")
        _bounded_number(self.timeout_seconds, 0.1, 120.0)
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise ValueError("max_attempts must be an integer from 1 to 3")
        if not 1 <= self.max_attempts <= 3:
            raise ValueError("max_attempts must be an integer from 1 to 3")

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> EmailSettings:
        values = os.environ if environ is None else environ
        api_key = values.get("RESEND_API_KEY", "").strip()
        sender = values.get("EMAIL_FROM", "").strip()
        recipient = values.get("NEWSLETTER_RECIPIENT", "").strip()
        if not api_key:
            raise ValueError("RESEND_API_KEY is required when email delivery is enabled")
        _validate_address(sender, "EMAIL_FROM")
        _validate_address(recipient, "NEWSLETTER_RECIPIENT")
        timeout = _bounded_number(values.get("EMAIL_TIMEOUT_SECONDS", "15"), 0.1, 120.0)
        attempts_text = values.get("EMAIL_MAX_ATTEMPTS", "2").strip()
        try:
            attempts = int(attempts_text)
        except ValueError as error:
            raise ValueError("EMAIL_MAX_ATTEMPTS must be an integer from 1 to 3") from error
        if str(attempts) != attempts_text or not 1 <= attempts <= 3:
            raise ValueError("EMAIL_MAX_ATTEMPTS must be an integer from 1 to 3")
        return cls(api_key, sender, recipient, timeout, attempts)


@dataclass(frozen=True, slots=True)
class SMTPSettings:
    """Non-secret settings for authenticated SMTP delivery."""

    host: str
    port: int
    username: str
    sender: str
    recipient: str
    timeout_seconds: float = 20.0
    max_attempts: int = 2

    def __post_init__(self) -> None:
        if not self.host.strip() or any(character.isspace() for character in self.host):
            raise ValueError("SMTP_HOST must be a non-empty hostname")
        if (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 1 <= self.port <= 65535
        ):
            raise ValueError("SMTP_PORT must be an integer from 1 to 65535")
        _validate_address(self.username, "SMTP_USERNAME")
        _validate_address(self.sender, "EMAIL_FROM")
        _validate_address(self.recipient, "NEWSLETTER_RECIPIENT")
        if (
            self.host.casefold() == "smtp.gmail.com"
            and self.sender.casefold() != self.username.casefold()
        ):
            raise ValueError("EMAIL_FROM must match SMTP_USERNAME for Gmail SMTP")
        _bounded_number(self.timeout_seconds, 0.1, 120.0)
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise ValueError("EMAIL_MAX_ATTEMPTS must be an integer from 1 to 3")
        if not 1 <= self.max_attempts <= 3:
            raise ValueError("EMAIL_MAX_ATTEMPTS must be an integer from 1 to 3")

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> SMTPSettings:
        values = os.environ if environ is None else environ
        username = values.get("SMTP_USERNAME", "").strip()
        sender = values.get("EMAIL_FROM", "").strip() or username
        recipient = values.get("NEWSLETTER_RECIPIENT", "").strip()
        _validate_address(username, "SMTP_USERNAME")
        _validate_address(sender, "EMAIL_FROM")
        _validate_address(recipient, "NEWSLETTER_RECIPIENT")
        host = values.get("SMTP_HOST", "smtp.gmail.com").strip()
        port_text = values.get("SMTP_PORT", "587").strip()
        try:
            port = int(port_text)
        except ValueError as error:
            raise ValueError("SMTP_PORT must be an integer from 1 to 65535") from error
        timeout = _bounded_number(values.get("EMAIL_TIMEOUT_SECONDS", "20"), 0.1, 120.0)
        attempts_text = values.get("EMAIL_MAX_ATTEMPTS", "2").strip()
        try:
            attempts = int(attempts_text)
        except ValueError as error:
            raise ValueError("EMAIL_MAX_ATTEMPTS must be an integer from 1 to 3") from error
        if str(attempts) != attempts_text or not 1 <= attempts <= 3:
            raise ValueError("EMAIL_MAX_ATTEMPTS must be an integer from 1 to 3")
        return cls(host, port, username, sender, recipient, timeout, attempts)


EmailConfiguration = EmailSettings | SMTPSettings


def email_settings_from_env(environ: dict[str, str] | None = None) -> EmailConfiguration:
    """Select a validated provider configuration without connecting externally."""
    values = os.environ if environ is None else environ
    provider = values.get("EMAIL_PROVIDER", "smtp").strip().casefold() or "smtp"
    if provider == "smtp":
        return SMTPSettings.from_env(values)
    if provider == "resend":
        return EmailSettings.from_env(values)
    raise ValueError("EMAIL_PROVIDER must be either 'smtp' or 'resend'")


@dataclass(frozen=True, slots=True)
class EmailMessage:
    """One immutable message and its stable provider idempotency key."""

    recipient: str
    subject: str
    html_body: str
    plain_text_body: str
    idempotency_key: str

    def __post_init__(self) -> None:
        _validate_address(self.recipient, "recipient")
        if not isinstance(self.subject, str) or not self.subject.strip():
            raise ValueError("subject must be non-empty")
        if "\r" in self.subject or "\n" in self.subject:
            raise ValueError("subject must not contain line breaks")
        if not isinstance(self.html_body, str) or not self.html_body:
            raise ValueError("html_body must be non-empty")
        if not isinstance(self.plain_text_body, str) or not self.plain_text_body:
            raise ValueError("plain_text_body must be non-empty")
        if not isinstance(self.idempotency_key, str) or not _DELIVERY_KEY_PATTERN.fullmatch(
            self.idempotency_key
        ):
            raise ValueError("idempotency_key must be 1-256 printable ASCII characters")


@dataclass(frozen=True, slots=True)
class ProviderReceipt:
    """Provider acceptance data, independent of any specific API response shape."""

    message_id: str | None


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """Delivery outcome with safe diagnostics and retry guidance."""

    status: Literal[
        "accepted", "rejected", "failed", "unknown", "preview", "duplicate_skipped"
    ]
    success: bool
    retryable: bool
    provider: str
    provider_message_id: str | None = None
    error: str | None = None
    preview_html_path: Path | None = None
    preview_text_path: Path | None = None


class EmailDeliveryError(Exception):
    """Base class for sanitized provider failures."""


class TransientEmailError(EmailDeliveryError):
    """A known transient rejection that can be retried with the same key/body."""


class PermanentEmailError(EmailDeliveryError):
    """A definitive rejection that must not be retried."""


class AmbiguousEmailError(EmailDeliveryError):
    """The request may have been accepted; reconcile before another attempt."""


class EmailProvider(Protocol):
    """Narrow provider-neutral send interface."""

    @property
    def provider_name(self) -> str: ...

    def send(self, message: EmailMessage) -> ProviderReceipt: ...


class EmailDeliveryService:
    """Retry only known transient failures, retaining one exact key and payload."""

    def __init__(
        self,
        provider: EmailProvider,
        *,
        max_attempts: int = 2,
        sleeper=time.sleep,
    ) -> None:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise ValueError("max_attempts must be an integer from 1 to 3")
        if not 1 <= max_attempts <= 3:
            raise ValueError("max_attempts must be an integer from 1 to 3")
        self.provider = provider
        self.max_attempts = max_attempts
        self.sleeper = sleeper

    def send(self, message: EmailMessage) -> DeliveryResult:
        for attempt in range(1, self.max_attempts + 1):
            try:
                receipt = self.provider.send(message)
                return DeliveryResult(
                    "accepted", True, False, self.provider.provider_name, receipt.message_id
                )
            except TransientEmailError:
                if attempt == self.max_attempts:
                    return DeliveryResult(
                        "failed",
                        False,
                        True,
                        self.provider.provider_name,
                        error="Provider returned a transient error after bounded retries.",
                    )
                self.sleeper(min(0.25 * (2 ** (attempt - 1)), 1.0))
            except PermanentEmailError:
                return DeliveryResult(
                    "rejected",
                    False,
                    False,
                    self.provider.provider_name,
                    error="Provider definitively rejected the message; it was not retried.",
                )
            except AmbiguousEmailError:
                return DeliveryResult(
                    "unknown",
                    False,
                    False,
                    self.provider.provider_name,
                    error=(
                        "Provider acceptance is unknown; reconcile using the delivery key "
                        "before retrying."
                    ),
                )
        raise AssertionError("unreachable delivery retry state")


def newsletter_idempotency_key(recipient: str, briefing_date: date) -> str:
    """Derive a stable non-PII key from recipient and the newsletter's local date."""
    _validate_address(recipient, "recipient")
    recipient_digest = hashlib.sha256(recipient.casefold().encode("utf-8")).hexdigest()
    return f"personal-newsletter/{briefing_date.isoformat()}/{recipient_digest}"


def deliver_or_preview(
    document: NewsletterDocument,
    *,
    subject: str,
    idempotency_key: str,
    settings: EmailConfiguration | None = None,
    provider: EmailProvider | None = None,
    preview: bool = False,
    preview_directory: str | Path = "data/previews",
    sleeper=time.sleep,
) -> DeliveryResult:
    """Write a local preview or send rendered alternatives through one provider.

    Preview mode requires no credentials or provider and never attempts network
    delivery. Its filenames contain only a digest of the idempotency key.
    """
    if preview:
        return _write_preview(document, idempotency_key, preview_directory)
    active_settings = settings if settings is not None else email_settings_from_env()
    active_provider = provider
    created_provider = active_provider is None
    if active_provider is None:
        if isinstance(active_settings, SMTPSettings):
            from app.email.smtp import SMTPEmailProvider

            active_provider = SMTPEmailProvider(active_settings)
        else:
            from app.email.resend import ResendEmailProvider

            active_provider = ResendEmailProvider(active_settings)
    message = EmailMessage(
        recipient=active_settings.recipient,
        subject=subject,
        html_body=document.html,
        plain_text_body=document.plain_text,
        idempotency_key=idempotency_key,
    )
    service = EmailDeliveryService(
        active_provider,
        max_attempts=active_settings.max_attempts,
        sleeper=sleeper,
    )
    try:
        return service.send(message)
    finally:
        if created_provider:
            close = getattr(active_provider, "close", None)
            if close is not None:
                close()


def _write_preview(
    document: NewsletterDocument,
    idempotency_key: str,
    preview_directory: str | Path,
) -> DeliveryResult:
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise ValueError("idempotency_key is required for preview naming")
    directory = Path(preview_directory)
    directory.mkdir(parents=True, exist_ok=True)
    date_suffix = idempotency_key.rpartition("/")[-1]
    try:
        filename_date = date.fromisoformat(date_suffix).isoformat()
    except ValueError:
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:16]
        filename_date = digest
    html_path = directory / f"newsletter-{filename_date}.html"
    text_path = directory / f"newsletter-{filename_date}.txt"
    html_path.write_text(document.html, encoding="utf-8")
    text_path.write_text(document.plain_text, encoding="utf-8")
    _prune_old_previews(directory, keep_paths=(html_path, text_path))
    return DeliveryResult(
        "preview",
        False,
        False,
        "preview",
        error="Preview files written locally; no email was sent.",
        preview_html_path=html_path,
        preview_text_path=text_path,
    )


def _prune_old_previews(directory: Path, *, keep_paths: tuple[Path, ...]) -> None:
    """Remove only old generated newsletter HTML/TXT artifacts after 30 days."""
    cutoff = time.time() - _PREVIEW_RETENTION_SECONDS
    keep = {path.resolve() for path in keep_paths}
    try:
        candidates = tuple(directory.iterdir())
    except OSError:
        return
    for candidate in candidates:
        if candidate.resolve() in keep or not _NEWSLETTER_FILENAME_PATTERN.fullmatch(
            candidate.name
        ):
            continue
        try:
            if candidate.is_file() and candidate.stat().st_mtime < cutoff:
                candidate.unlink()
        except OSError:
            # Retention is best-effort and cannot invalidate a newly written issue.
            continue


def _validate_address(value: str, name: str) -> None:
    if not isinstance(value, str) or not _EMAIL_PATTERN.fullmatch(value.strip()):
        raise ValueError(f"{name} must be a valid email address")


def _bounded_number(value: object, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("EMAIL_TIMEOUT_SECONDS must be a number") from error
    if isinstance(value, bool) or not 0.1 <= number <= 120.0:
        raise ValueError(f"EMAIL_TIMEOUT_SECONDS must be between {minimum} and {maximum}")
    return number
