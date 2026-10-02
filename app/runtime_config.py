"""Read-only validation of configuration needed for a production send run."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from app.email.base import EmailSettings, SMTPSettings
from app.email.credentials import smtp_credential_configured
from app.pipeline.runner import PipelineDependencies, _load_configuration


@dataclass(frozen=True, slots=True)
class RuntimeConfigurationSummary:
    """Safe configuration facts; never contains credential values."""

    enabled_source_count: int
    timezone_name: str
    llm_configured: bool
    email_configured: bool
    email_provider: str
    email_credential_configured: bool
    email_status: str


def validate_send_configuration(
    *,
    config_directory: str | Path,
    environ: Mapping[str, str] | None = None,
) -> RuntimeConfigurationSummary:
    """Validate send configuration without connecting to external services."""
    values = os.environ if environ is None else environ
    configuration = _load_configuration(
        Path(config_directory),
        mode="send",
        ai_in_preview=False,
        dependencies=PipelineDependencies(),
        environ=values,
    )
    enabled_count = sum(source.enabled for source in configuration.sources)
    if enabled_count == 0:
        raise ValueError("At least one enabled source is required for a send run")
    email_settings = configuration.email_settings
    configured_provider = values.get("EMAIL_PROVIDER", "smtp").strip().casefold() or "smtp"
    if isinstance(email_settings, SMTPSettings):
        email_provider = "smtp"
        credential_configured = smtp_credential_configured(email_settings.username, environ=values)
    elif isinstance(email_settings, EmailSettings):
        email_provider = "resend"
        credential_configured = True
    else:
        email_provider = configured_provider
        credential_configured = False
    email_configured = email_settings is not None and credential_configured
    return RuntimeConfigurationSummary(
        enabled_source_count=enabled_count,
        timezone_name=configuration.timezone_name,
        llm_configured=configuration.llm_settings is not None,
        email_configured=email_configured,
        email_provider=email_provider,
        email_credential_configured=credential_configured,
        email_status="configured" if email_configured else "not_configured_optional",
    )
