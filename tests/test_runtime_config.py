from pathlib import Path

from app.cli import main
from app.runtime_config import validate_send_configuration


def valid_send_environment(database_path: Path) -> dict[str, str]:
    return {
        "DATABASE_BACKEND": "sqlite",
        "DATABASE_PATH": str(database_path),
        "LLM_API_KEY": "test-llm-secret-not-real",
        "LLM_MODEL": "test/model",
        "LLM_BASE_URL": "https://llm.example.invalid/v1",
        "RESEND_API_KEY": "test-email-secret-not-real",
        "EMAIL_PROVIDER": "resend",
        "EMAIL_FROM": "briefing@example.com",
        "NEWSLETTER_RECIPIENT": "reader@example.com",
    }


def test_send_config_check_returns_only_safe_metadata(tmp_path):
    secret_llm = "test-llm-secret-not-real"
    secret_email = "test-email-secret-not-real"
    summary = validate_send_configuration(
        config_directory=Path(__file__).parents[1] / "config",
        environ=valid_send_environment(tmp_path / "runs.sqlite3"),
    )

    assert summary.database_backend == "sqlite"
    assert summary.enabled_source_count >= 1
    assert summary.timezone_name == "UTC"
    assert summary.llm_configured
    assert summary.email_configured
    assert summary.email_provider == "resend"
    assert summary.email_credential_configured
    assert secret_llm not in repr(summary)
    assert secret_email not in repr(summary)


def test_send_config_check_validates_smtp_configuration_and_os_credential(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        "app.pipeline.runner.smtp_credential_configured", lambda _username, **_kwargs: True
    )
    monkeypatch.setattr(
        "app.runtime_config.smtp_credential_configured", lambda _username, **_kwargs: True
    )
    environment = valid_send_environment(tmp_path / "runs.sqlite3")
    environment.pop("RESEND_API_KEY")
    environment.pop("EMAIL_FROM")
    environment.update(
        {
            "EMAIL_PROVIDER": "smtp",
            "SMTP_USERNAME": "sender@example.com",
            "NEWSLETTER_RECIPIENT": "reader@example.com",
        }
    )

    summary = validate_send_configuration(
        config_directory=Path(__file__).parents[1] / "config", environ=environment
    )

    assert summary.email_configured
    assert summary.email_status == "configured"
    assert summary.email_provider == "smtp"
    assert summary.email_credential_configured
    assert "smtp-test-secret" not in repr(summary)


def test_send_config_check_accepts_cloud_smtp_environment_secret(tmp_path):
    environment = {
        "DATABASE_BACKEND": "postgres",
        "DATABASE_URL": "postgresql://user:password@example.invalid/news?sslmode=require",
        "EMAIL_PROVIDER": "smtp",
        "SMTP_USERNAME": "sender@example.com",
        "SMTP_PASSWORD": "smtp-test-secret-not-real",
        "EMAIL_FROM": "sender@example.com",
        "NEWSLETTER_RECIPIENT": "reader@example.com",
    }

    summary = validate_send_configuration(
        config_directory=Path(__file__).parents[1] / "config", environ=environment
    )

    assert summary.database_backend == "postgres"
    assert summary.email_configured
    assert summary.email_credential_configured
    assert "smtp-test-secret-not-real" not in repr(summary)
    assert "password" not in repr(summary)


def test_send_config_check_treats_missing_llm_as_optional(tmp_path):
    environment = valid_send_environment(tmp_path / "runs.sqlite3")
    environment.pop("LLM_API_KEY")

    summary = validate_send_configuration(
        config_directory=Path(__file__).parents[1] / "config",
        environ=environment,
    )

    assert not summary.llm_configured
    assert summary.email_configured


def test_send_config_check_reports_email_as_optional_when_not_configured(tmp_path):
    environment = {
        "DATABASE_BACKEND": "sqlite",
        "DATABASE_PATH": str(tmp_path / "runs.sqlite3"),
    }

    summary = validate_send_configuration(
        config_directory=Path(__file__).parents[1] / "config",
        environ=environment,
    )

    assert not summary.email_configured
    assert not summary.email_credential_configured
    assert summary.email_provider == "smtp"
    assert summary.email_status == "not_configured_optional"
    assert not summary.llm_configured


def test_cli_config_check_never_displays_secret_values(monkeypatch, capsys, tmp_path):
    secrets = {
        "LLM_API_KEY": "llm-test-secret-value",
        "RESEND_API_KEY": "email-test-secret-value",
    }
    for name, value in {
        **secrets,
        "DATABASE_BACKEND": "sqlite",
        "DATABASE_PATH": str(tmp_path / "runs.sqlite3"),
        "LLM_MODEL": "test/model",
        "LLM_BASE_URL": "https://llm.example.invalid/v1",
        "EMAIL_FROM": "briefing@example.com",
        "EMAIL_PROVIDER": "resend",
        "NEWSLETTER_RECIPIENT": "reader@example.com",
    }.items():
        monkeypatch.setenv(name, value)

    assert main(["config-check"]) == 0
    output = capsys.readouterr().out

    assert '"status": "configuration_valid"' in output
    assert '"database_backend": "sqlite"' in output
    assert '"email_status": "configured"' in output
    assert all(secret not in output for secret in secrets.values())
