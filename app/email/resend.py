"""Resend transactional email API adapter."""

from __future__ import annotations

from typing import Any

import httpx

from app.email.base import (
    AmbiguousEmailError,
    EmailMessage,
    EmailSettings,
    PermanentEmailError,
    ProviderReceipt,
    TransientEmailError,
)


class ResendEmailProvider:
    """Send one message through Resend's idempotent Email API endpoint."""

    endpoint = "https://api.resend.com/emails"

    def __init__(self, settings: EmailSettings, *, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self._client = client or httpx.Client(timeout=settings.timeout_seconds)
        self._owns_client = client is None

    @property
    def provider_name(self) -> str:
        return "resend"

    def send(self, message: EmailMessage) -> ProviderReceipt:
        payload = {
            "from": self.settings.sender,
            "to": [message.recipient],
            "subject": message.subject,
            "html": message.html_body,
            "text": message.plain_text_body,
        }
        try:
            response = self._client.post(
                self.endpoint,
                headers={
                    "Authorization": f"Bearer {self.settings.api_key}",
                    "Idempotency-Key": message.idempotency_key,
                },
                json=payload,
                timeout=self.settings.timeout_seconds,
            )
        except httpx.TimeoutException:
            raise AmbiguousEmailError("Email provider timed out; acceptance is unknown.") from None
        except httpx.RequestError:
            raise AmbiguousEmailError(
                "Email provider network outcome is unknown; reconcile before retrying."
            ) from None

        if response.status_code in {408, 429} or response.status_code >= 500:
            raise TransientEmailError(
                f"Email provider returned transient HTTP {response.status_code}."
            ) from None
        if not 200 <= response.status_code < 300:
            raise PermanentEmailError(
                f"Email provider rejected the message with HTTP {response.status_code}."
            ) from None

        message_id = None
        try:
            body: Any = response.json()
            candidate = body.get("id") if isinstance(body, dict) else None
            if isinstance(candidate, str) and candidate.strip():
                message_id = candidate.strip()
        except (ValueError, TypeError):
            # The successful HTTP status establishes acceptance; the response ID is optional.
            pass
        return ProviderReceipt(message_id)

    def close(self) -> None:
        """Close only clients created internally by this adapter."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> ResendEmailProvider:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
