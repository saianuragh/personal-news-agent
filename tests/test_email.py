"""Offline tests for safe transactional delivery and local preview."""

import json
import os
import time
from datetime import date
from pathlib import Path

import httpx
import pytest
from app.email.base import (
    AmbiguousEmailError,
    EmailDeliveryService,
    EmailMessage,
    EmailSettings,
    PermanentEmailError,
    ProviderReceipt,
    TransientEmailError,
    deliver_or_preview,
    newsletter_idempotency_key,
)
from app.email.resend import ResendEmailProvider
from app.newsletter.renderer import NewsletterDocument

FAKE_SECRET = "unit-test-secret-never-a-real-credential"


class FakeProvider:
    provider_name = "fake"

    def __init__(self, results: list[object]) -> None:
        self.results = list(results)
        self.messages: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> ProviderReceipt:
        self.messages.append(message)
        outcome = self.results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def settings(**overrides: object) -> EmailSettings:
    values: dict[str, object] = {
        "api_key": FAKE_SECRET,
        "sender": "briefing@example.test",
        "recipient": "reader@example.test",
        "timeout_seconds": 2,
        "max_attempts": 2,
    }
    values.update(overrides)
    return EmailSettings(**values)  # type: ignore[arg-type]


def message(key: str = "briefing/2026-09-30/recipient-hash") -> EmailMessage:
    return EmailMessage(
        recipient="reader@example.test",
        subject="Your morning briefing",
        html_body="<h1>Briefing</h1>",
        plain_text_body="Briefing",
        idempotency_key=key,
    )


def document() -> NewsletterDocument:
    return NewsletterDocument(
        html="<html><body>Preview only</body></html>",
        plain_text="Preview only",
        included_story_ids=(),
        omitted_story_ids=(),
    )


def test_successful_delivery_returns_provider_message_id() -> None:
    provider = FakeProvider([ProviderReceipt("message-123")])
    service = EmailDeliveryService(provider, sleeper=lambda _: None)

    result = service.send(message())

    assert result.status == "accepted"
    assert result.success is True
    assert result.retryable is False
    assert result.provider_message_id == "message-123"
    assert len(provider.messages) == 1


def test_permanent_rejection_is_not_retried() -> None:
    provider = FakeProvider([PermanentEmailError("rejected"), ProviderReceipt("unexpected")])
    result = EmailDeliveryService(provider, max_attempts=3).send(message())

    assert result.status == "rejected"
    assert result.success is False
    assert result.retryable is False
    assert len(provider.messages) == 1


def test_transient_failure_retries_boundedly_with_same_key_and_message() -> None:
    provider = FakeProvider([TransientEmailError("busy"), ProviderReceipt("accepted-id")])
    delays: list[float] = []
    result = EmailDeliveryService(provider, max_attempts=2, sleeper=delays.append).send(message())

    assert result.status == "accepted"
    assert result.provider_message_id == "accepted-id"
    assert len(provider.messages) == 2
    assert provider.messages[0] == provider.messages[1]
    assert delays == [0.25]


def test_exhausted_transient_attempts_return_retryable_failure() -> None:
    provider = FakeProvider([TransientEmailError("busy")] * 5)
    result = EmailDeliveryService(provider, max_attempts=2, sleeper=lambda _: None).send(message())

    assert result.status == "failed"
    assert result.retryable is True
    assert len(provider.messages) == 2


def test_ambiguous_delivery_is_unknown_and_never_automatically_retried() -> None:
    provider = FakeProvider([AmbiguousEmailError("timeout"), ProviderReceipt("too-late")])
    result = EmailDeliveryService(provider, max_attempts=3).send(message())

    assert result.status == "unknown"
    assert result.retryable is False
    assert "reconcile" in result.error
    assert len(provider.messages) == 1


def test_missing_credentials_and_invalid_addresses_fail_before_delivery() -> None:
    with pytest.raises(ValueError, match="RESEND_API_KEY"):
        EmailSettings.from_env(
            {
                "EMAIL_FROM": "briefing@example.test",
                "NEWSLETTER_RECIPIENT": "reader@example.test",
            }
        )
    with pytest.raises(ValueError, match="NEWSLETTER_RECIPIENT"):
        EmailSettings.from_env(
            {
                "RESEND_API_KEY": FAKE_SECRET,
                "EMAIL_FROM": "briefing@example.test",
                "NEWSLETTER_RECIPIENT": "not-an-email",
            }
        )
    with pytest.raises(ValueError, match="EMAIL_FROM"):
        EmailSettings.from_env(
            {
                "RESEND_API_KEY": FAKE_SECRET,
                "EMAIL_FROM": "bad-sender",
                "NEWSLETTER_RECIPIENT": "reader@example.test",
            }
        )


def test_idempotency_key_is_stable_non_pii_and_changes_by_recipient_or_date() -> None:
    key = newsletter_idempotency_key("Reader@Example.test", date(2026, 9, 30))

    assert key == newsletter_idempotency_key("reader@example.test", date(2026, 9, 30))
    assert "reader@example.test" not in key
    assert key != newsletter_idempotency_key("other@example.test", date(2026, 9, 30))
    assert key != newsletter_idempotency_key("reader@example.test", date(2026, 10, 1))


def test_email_message_rejects_invalid_recipient_subject_and_delivery_key() -> None:
    with pytest.raises(ValueError, match="recipient"):
        EmailMessage("invalid", "subject", "<p>html</p>", "text", "key")
    with pytest.raises(ValueError, match="line breaks"):
        EmailMessage("reader@example.test", "bad\nsubject", "<p>html</p>", "text", "key")
    with pytest.raises(ValueError, match="idempotency_key"):
        message("bad\nkey")


def test_resend_adapter_sends_both_formats_and_same_idempotency_key() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "resend-msg-77"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = ResendEmailProvider(settings(), client=client)
    receipt = provider.send(message())

    request = requests[0]
    assert request.url == "https://api.resend.com/emails"
    assert request.headers["Idempotency-Key"] == message().idempotency_key
    assert request.headers["Authorization"] == f"Bearer {FAKE_SECRET}"
    body = json.loads(request.content)
    assert body["from"] == "briefing@example.test"
    assert body["to"] == ["reader@example.test"]
    assert body["html"] == "<h1>Briefing</h1>"
    assert body["text"] == "Briefing"
    assert receipt.message_id == "resend-msg-77"
    client.close()


def test_resend_retry_reuses_identical_body_and_key() -> None:
    requests: list[httpx.Request] = []
    statuses = iter((503, 200))

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        status = next(statuses)
        return httpx.Response(status, json={"id": "idempotent-id"} if status == 200 else {})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = ResendEmailProvider(settings(), client=client)
    result = EmailDeliveryService(provider, sleeper=lambda _: None).send(message())

    assert result.status == "accepted"
    assert len(requests) == 2
    assert requests[0].headers["Idempotency-Key"] == requests[1].headers["Idempotency-Key"]
    assert requests[0].content == requests[1].content
    client.close()


@pytest.mark.parametrize("status", [302, 401, 422])
def test_resend_permanent_rejection_and_ambiguous_timeout_are_sanitized(status: int) -> None:
    reject_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, text=f"bad key {FAKE_SECRET}")
        )
    )
    rejected_provider = ResendEmailProvider(settings(), client=reject_client)
    rejected = EmailDeliveryService(rejected_provider).send(message())

    assert rejected.status == "rejected"
    assert FAKE_SECRET not in str(rejected)
    reject_client.close()

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"timeout {FAKE_SECRET}", request=request)

    timeout_client = httpx.Client(transport=httpx.MockTransport(timeout_handler))
    timeout_provider = ResendEmailProvider(settings(), client=timeout_client)
    unknown = EmailDeliveryService(timeout_provider).send(message())

    assert unknown.status == "unknown"
    assert FAKE_SECRET not in str(unknown)
    timeout_client.close()


def test_settings_repr_does_not_expose_api_key() -> None:
    assert FAKE_SECRET not in repr(settings())


def test_preview_writes_local_html_and_text_without_provider_or_credentials(
    tmp_path: Path,
) -> None:
    provider = FakeProvider([])

    result = deliver_or_preview(
        document(),
        subject="Preview",
        idempotency_key="daily/2026-09-30",
        provider=provider,
        preview=True,
        preview_directory=tmp_path,
    )

    assert result.status == "preview"
    assert result.success is False
    assert result.preview_html_path is not None
    assert result.preview_text_path is not None
    assert result.preview_html_path.name == "newsletter-2026-09-30.html"
    assert result.preview_text_path.name == "newsletter-2026-09-30.txt"
    assert result.preview_html_path.read_text(encoding="utf-8") == document().html
    assert result.preview_text_path.read_text(encoding="utf-8") == document().plain_text
    assert provider.messages == []
    assert not list(tmp_path.glob("*unit-test-secret*"))


def test_preview_retention_removes_only_old_newsletter_artifacts(tmp_path: Path) -> None:
    old_html = tmp_path / "newsletter-2026-08-01.html"
    old_text = tmp_path / "newsletter-2026-08-01.txt"
    unrelated = tmp_path / "notes.txt"
    old_html.write_text("old", encoding="utf-8")
    old_text.write_text("old", encoding="utf-8")
    unrelated.write_text("keep", encoding="utf-8")
    old_time = time.time() - 31 * 24 * 60 * 60
    os.utime(old_html, (old_time, old_time))
    os.utime(old_text, (old_time, old_time))

    result = deliver_or_preview(
        document(),
        subject="Preview",
        idempotency_key="daily/2026-09-30",
        preview=True,
        preview_directory=tmp_path,
    )

    assert not old_html.exists()
    assert not old_text.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert result.preview_html_path is not None and result.preview_html_path.exists()
    assert result.preview_text_path is not None and result.preview_text_path.exists()
