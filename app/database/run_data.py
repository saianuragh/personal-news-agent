"""Shared serialization and sanitization for persisted pipeline runs."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.pipeline.runner import PipelineRunResult


def sanitize_diagnostic(value: str) -> str:
    """Redact common credential patterns and bound persisted provider text."""
    sanitized = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [REDACTED]", value)
    sanitized = re.sub(r"(?i)sk-[A-Za-z0-9_-]{8,}", "[REDACTED]", sanitized)
    sanitized = re.sub(
        r"(?i)(api[_ -]?key|authorization|password|secret)\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        sanitized,
    )
    sanitized = re.sub(
        r"(?i)(https?://)[^/@\s]+:[^/@\s]+@",
        r"\1[REDACTED]@",
        sanitized,
    )
    sanitized = re.sub(
        r"(?i)([?&](?:api[_-]?key|token|access_token|secret|password)=)[^&\s]+",
        r"\1[REDACTED]",
        sanitized,
    )
    return " ".join(sanitized.split())[:1_000]


def warnings_for(result: PipelineRunResult) -> list[dict[str, str | int | None]]:
    """Convert run diagnostics to bounded, sanitized JSON-compatible values."""
    failures = (*result.source_failures, *result.stage_failures)
    warnings = [
        {
            "stage": sanitize_diagnostic(failure.stage),
            "error_type": sanitize_diagnostic(failure.error_type),
            "message": sanitize_diagnostic(failure.message),
            "source_id": sanitize_diagnostic(failure.source_id)
            if failure.source_id
            else None,
            "category": sanitize_diagnostic(failure.category) if failure.category else None,
            "http_status": failure.http_status,
            "provider_error_type": sanitize_diagnostic(failure.provider_error_type)
            if failure.provider_error_type
            else None,
            "provider_error_code": sanitize_diagnostic(failure.provider_error_code)
            if failure.provider_error_code
            else None,
        }
        for failure in failures
    ]
    if result.delivery is not None and result.delivery.error:
        warnings.append(
            {
                "stage": "delivery",
                "error_type": None,
                "message": sanitize_diagnostic(result.delivery.error),
                "source_id": None,
                "category": None,
                "http_status": None,
                "provider_error_type": None,
                "provider_error_code": None,
            }
        )
    return warnings
