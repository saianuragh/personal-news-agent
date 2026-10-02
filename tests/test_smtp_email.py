from __future__ import annotations

import smtplib

import pytest
from app.email.base import (
    EmailDeliveryService,
    EmailMessage,
    ProviderReceipt,
    SMTPSettings,
)
from app.email.credentials import (
    load_smtp_credential,
    prompt_and_store_smtp_credential,
    smtp_credential_configured,
)
from app.email.smtp import SMTPEmailProvider

_TEST_PASSWORD = "smtp-test-secret-not-real"


def settings(**overrides: object) -> SMTPSettings:
    values: dict[str, object] = {
        "host": "smtp.example.test",
        "port": 587,
        "username": "sender@example.test",
        "sender": "sender@example.test",
        "recipient": "reader@example.test",
        "max_attempts": 2,
    }
    values.update(overrides)
    return SMTPSettings(**values)  # type: ignore[arg-type]


def message() -> EmailMessage:
    return EmailMessage(
        "reader@example.test",
        "Daily briefing",
        "<p>HTML</p>",
        "Plain text",
        "daily/2026-10-01/hash",
    )


class FakeSMTP:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.messages = []
        self.logins: list[tuple[str, str]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def ehlo(self):
        return 250, b"ok"

    def starttls(self, *, context):
        assert context is not None
        return 220, b"ready"

    def login(self, username: str, password: str):
        self.logins.append((username, password))
        return 235, b"authenticated"

    def send_message(self, mime, **_kwargs):
        if self.error:
            raise self.error
        self.messages.append(mime)
        return {}


def test_smtp_success_sends_multipart_message_over_starttls_without_message_id() -> None:
    fake = FakeSMTP()
    provider = SMTPEmailProvider(
        settings(), credential_loader=lambda _username: _TEST_PASSWORD,
        smtp_factory=lambda *_args, **_kwargs: fake,
    )

    receipt = provider.send(message())

    assert receipt == ProviderReceipt(None)
    assert fake.logins == [("sender@example.test", _TEST_PASSWORD)]
    assert fake.messages[0]["X-Personal-News-Delivery-Key"] == message().idempotency_key
    assert [part.get_content_type() for part in fake.messages[0].get_payload()] == [
        "text/plain",
        "text/html",
    ]


def test_missing_keyring_credential_fails_without_opening_smtp() -> None:
    opened = False

    def factory(*_args, **_kwargs):
        nonlocal opened
        opened = True
        return FakeSMTP()

    result = EmailDeliveryService(
        SMTPEmailProvider(
            settings(), credential_loader=lambda _username: None, smtp_factory=factory
        )
    ).send(message())

    assert result.status == "rejected"
    assert opened is False


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (smtplib.SMTPAuthenticationError(535, b"secret auth detail"), "rejected"),
        (
            smtplib.SMTPRecipientsRefused(
                {"reader@example.test": (550, b"secret recipient detail")}
            ),
            "rejected",
        ),
        (smtplib.SMTPDataError(421, b"temporary server response"), "accepted"),
    ],
)
def test_smtp_rejections_are_classified_safely_and_transient_response_is_retried(
    error: Exception, expected: str
) -> None:
    calls = 0

    def factory(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return FakeSMTP(error if calls == 1 else None)

    provider = SMTPEmailProvider(
        settings(), credential_loader=lambda _username: _TEST_PASSWORD, smtp_factory=factory
    )
    result = EmailDeliveryService(provider, sleeper=lambda _: None).send(message())

    assert result.status == expected
    assert _TEST_PASSWORD not in repr(result)
    assert "secret auth detail" not in repr(result)
    if isinstance(error, smtplib.SMTPDataError):
        assert calls == 2
    else:
        assert calls == 1


def test_smtp_network_failure_is_ambiguous_and_never_retried() -> None:
    calls = 0

    def factory(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return FakeSMTP(TimeoutError(f"connection timeout {_TEST_PASSWORD}"))

    result = EmailDeliveryService(
        SMTPEmailProvider(
            settings(), credential_loader=lambda _username: _TEST_PASSWORD, smtp_factory=factory
        )
    ).send(message())

    assert result.status == "unknown"
    assert calls == 1
    assert _TEST_PASSWORD not in repr(result)


class MemoryKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))


def test_hidden_credential_prompt_stores_secret_in_keyring_only() -> None:
    backend = MemoryKeyring()
    prompts: list[str] = []

    def prompt(label: str) -> str:
        prompts.append(label)
        return _TEST_PASSWORD

    prompt_and_store_smtp_credential("sender@example.test", prompt=prompt, backend=backend)

    assert prompts == ["SMTP app password (input hidden): "]
    assert len(backend.values) == 1
    assert _TEST_PASSWORD not in repr(settings())


def test_cloud_smtp_password_environment_secret_takes_precedence_over_keyring() -> None:
    backend = MemoryKeyring()
    backend.set_password("personal-news-agent/smtp", "sender@example.test", "local-secret")
    environment = {"SMTP_PASSWORD": _TEST_PASSWORD}

    assert load_smtp_credential("sender@example.test", backend=backend, environ=environment) == (
        _TEST_PASSWORD
    )
    assert smtp_credential_configured("sender@example.test", environ=environment)
    assert _TEST_PASSWORD not in repr(environment.keys())


def test_blank_cloud_smtp_password_uses_local_keyring() -> None:
    backend = MemoryKeyring()
    backend.set_password("personal-news-agent/smtp", "sender@example.test", _TEST_PASSWORD)

    assert load_smtp_credential(
        "sender@example.test", backend=backend, environ={"SMTP_PASSWORD": " "}
    ) == _TEST_PASSWORD


def test_smtp_provider_uses_environment_credential_and_does_not_log_it(monkeypatch) -> None:
    monkeypatch.setenv("SMTP_PASSWORD", _TEST_PASSWORD)
    fake = FakeSMTP()

    result = EmailDeliveryService(
        SMTPEmailProvider(settings(), smtp_factory=lambda *_args, **_kwargs: fake)
    ).send(message())

    assert result.status == "accepted"
    assert fake.logins == [("sender@example.test", _TEST_PASSWORD)]
    assert _TEST_PASSWORD not in repr(result)


def test_smtp_environment_validation_uses_gmail_defaults_and_rejects_invalid_port() -> None:
    valid = SMTPSettings.from_env(
        {"SMTP_USERNAME": "sender@example.test", "NEWSLETTER_RECIPIENT": "reader@example.test"}
    )
    assert valid.host == "smtp.gmail.com"
    assert valid.port == 587

    with pytest.raises(ValueError, match="SMTP_PORT"):
        SMTPSettings.from_env(
            {
                "SMTP_USERNAME": "sender@example.test",
                "NEWSLETTER_RECIPIENT": "reader@example.test",
                "SMTP_PORT": "not-a-port",
            }
        )
    with pytest.raises(ValueError, match="EMAIL_FROM must match SMTP_USERNAME"):
        SMTPSettings.from_env(
            {
                "SMTP_USERNAME": "account@example.test",
                "EMAIL_FROM": "different@example.test",
                "NEWSLETTER_RECIPIENT": "reader@example.test",
            }
        )
    with pytest.raises(ValueError, match="EMAIL_FROM must match SMTP_USERNAME"):
        SMTPSettings.from_env(
            {
                "SMTP_USERNAME": "account@example.test",
                "EMAIL_FROM": "different@example.test",
                "NEWSLETTER_RECIPIENT": "reader@example.test",
            }
        )
