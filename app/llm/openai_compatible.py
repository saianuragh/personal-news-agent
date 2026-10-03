"""Small OpenAI-compatible chat-completions transport isolated from the LLM contract."""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.llm.base import (
    SYSTEM_PROMPT,
    LLMSettings,
    PermanentProviderError,
    ProviderDiagnostic,
    TransientProviderError,
)


class OpenAICompatibleProvider:
    """HTTP adapter for providers exposing the standard chat-completions shape."""

    def __init__(
        self,
        settings: LLMSettings,
        *,
        client: httpx.Client | None = None,
        sleep=time.sleep,
        monotonic=time.monotonic,
    ) -> None:
        self.settings = settings
        self._client = client or httpx.Client(timeout=settings.timeout_seconds)
        self._owns_client = client is None
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request_at: float | None = None

    @property
    def provider_name(self) -> str:
        return "openai_compatible"

    @property
    def model_name(self) -> str:
        return self.settings.model

    def complete(self, prompt: str, *, max_output_tokens: int) -> str:
        """Send bounded prompt and return only the provider's content string."""
        self._pace_request()
        try:
            response = self._client.post(
                f"{self.settings.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.settings.api_key}"},
                json={
                    "model": self.settings.model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0,
                    "max_tokens": max_output_tokens,
                    "response_format": {"type": "json_object"},
                },
                timeout=self.settings.timeout_seconds,
            )
        except httpx.TimeoutException as error:
            diagnostic = ProviderDiagnostic(
                category="timeout",
                message=f"Provider request timed out ({type(error).__name__}).",
                provider_error_type=type(error).__name__,
            )
            raise TransientProviderError(diagnostic.message, diagnostic=diagnostic) from error
        except httpx.NetworkError as error:
            diagnostic = ProviderDiagnostic(
                category="provider_error",
                message=f"Provider network request failed ({type(error).__name__}).",
                provider_error_type=type(error).__name__,
            )
            raise TransientProviderError(diagnostic.message, diagnostic=diagnostic) from error

        if response.is_error:
            diagnostic = _http_error_diagnostic(response, self.settings.api_key)
        if (
            response.status_code == 408
            or response.status_code == 429
            or response.status_code >= 500
        ):
            raise TransientProviderError(diagnostic.message, diagnostic=diagnostic)
        if response.is_error:
            raise PermanentProviderError(diagnostic.message, diagnostic=diagnostic)
        try:
            body: Any = response.json()
        except ValueError as error:
            diagnostic = ProviderDiagnostic(
                "invalid_response",
                "LLM provider response body was not valid JSON.",
                detail="malformed_provider_json",
            )
            raise PermanentProviderError(diagnostic.message, diagnostic=diagnostic) from error
        choices = body.get("choices") if isinstance(body, dict) else None
        if not isinstance(choices, list) or not choices:
            diagnostic = ProviderDiagnostic(
                "invalid_response",
                "LLM provider response did not contain choices.",
                detail="missing_choices",
            )
            raise PermanentProviderError(diagnostic.message, diagnostic=diagnostic)
        choice = choices[0]
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict) or "content" not in message:
            diagnostic = ProviderDiagnostic(
                "invalid_response",
                "LLM provider response did not contain message content.",
                detail="missing_content",
            )
            raise PermanentProviderError(diagnostic.message, diagnostic=diagnostic)
        content = message["content"]
        if content is None or content == "":
            finish_reason = choice.get("finish_reason")
            safe_finish_reason = (
                finish_reason
                if isinstance(finish_reason, str)
                and finish_reason in {"stop", "length", "content_filter", "tool_calls"}
                else None
            )
            detail = (
                f"empty_content_finish_{safe_finish_reason}"
                if safe_finish_reason
                else "empty_content"
            )
            diagnostic = ProviderDiagnostic(
                "empty_response", "LLM provider returned empty content.", detail=detail
            )
            raise TransientProviderError(diagnostic.message, diagnostic=diagnostic)
        if not isinstance(content, str):
            diagnostic = ProviderDiagnostic(
                "invalid_response",
                "LLM provider response content was not text.",
                detail="non_text_content",
            )
            raise PermanentProviderError(diagnostic.message, diagnostic=diagnostic)
        return content

    def close(self) -> None:
        """Close the internally created HTTP client; injected clients remain caller-owned."""
        if self._owns_client:
            self._client.close()

    def _pace_request(self) -> None:
        """Keep this provider instance below the configured request rate."""
        now = self._monotonic()
        if self._last_request_at is not None:
            remaining = self.settings.request_interval_seconds - (now - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_at = self._monotonic()

    def __enter__(self) -> OpenAICompatibleProvider:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _http_error_diagnostic(response: httpx.Response, api_key: str) -> ProviderDiagnostic:
    """Extract allowlisted error metadata; never retain provider response prose."""
    provider_type: str | None = None
    provider_code: str | None = None
    provider_identifier: str | None = None
    request_id: str | None = None
    try:
        body: Any = response.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        provider_type = _safe_metadata(error.get("type"), api_key)
        provider_code = _safe_metadata(error.get("code"), api_key)
        metadata = error.get("metadata")
        if isinstance(metadata, dict):
            provider_identifier = _safe_provider_identifier(
                metadata.get("provider_name")
                or metadata.get("provider")
                or metadata.get("provider_id"),
                api_key,
            )
            request_id = _safe_request_id(metadata.get("request_id"), api_key)
    if response.status_code == 429:
        request_id = request_id or _safe_request_id(
            response.headers.get("x-openrouter-request-id")
            or response.headers.get("x-request-id"),
            api_key,
        )
    category = _classify_http_error(response.status_code)
    message = f"OpenAI-compatible provider returned HTTP {response.status_code}."
    return ProviderDiagnostic(
        category=category,
        message=message,
        http_status=response.status_code,
        provider_error_type=provider_type,
        provider_error_code=provider_code,
        retry_after_seconds=(
            _parse_retry_after(response.headers.get("Retry-After"))
            if response.status_code == 429
            else None
        ),
        provider_identifier=provider_identifier,
        request_id=request_id,
        rate_limit_limit=(
            _safe_integer_header(response, "RateLimit-Limit", "X-RateLimit-Limit")
            if response.status_code == 429
            else None
        ),
        rate_limit_remaining=(
            _safe_integer_header(response, "RateLimit-Remaining", "X-RateLimit-Remaining")
            if response.status_code == 429
            else None
        ),
        rate_limit_reset_seconds=(
            _safe_float_header(response, "RateLimit-Reset", "X-RateLimit-Reset")
            if response.status_code == 429
            else None
        ),
    )


def _safe_integer_header(response: httpx.Response, *names: str) -> int | None:
    for name in names:
        value = response.headers.get(name)
        if value is not None and re.fullmatch(r"\d{1,9}", value.strip()):
            return int(value)
    return None


def _safe_float_header(response: httpx.Response, *names: str) -> float | None:
    for name in names:
        value = response.headers.get(name)
        if value is None:
            continue
        try:
            parsed = float(value.strip())
        except ValueError:
            continue
        if 0 <= parsed <= 31_536_000:
            return parsed
    return None


def _safe_provider_identifier(value: object, api_key: str) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if api_key and api_key in normalized:
        return None
    return normalized if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", normalized) else None


def _safe_request_id(value: object, api_key: str) -> str | None:
    """Accept only opaque request/generation IDs, never arbitrary header text."""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if api_key and api_key in normalized:
        return None
    known_id = re.fullmatch(
        r"(?:req|gen|chatcmpl)[-_][A-Za-z0-9_-]{8,100}"
        r"|[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}",
        normalized,
    )
    return normalized if known_id else None


def _parse_retry_after(value: str | None) -> float | None:
    """Parse delta-seconds or an HTTP date without retaining header text."""
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            seconds = (retry_at - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    if seconds < 0:
        return None
    return seconds if seconds < float("inf") else None


def _classify_http_error(
    status: int,
) -> str:
    if status == 429:
        return "rate_limit"
    if status == 408:
        return "timeout"
    if status >= 500:
        return "provider_error"
    if 400 <= status < 500:
        return "http_error"
    return "unknown"


def _safe_metadata(value: object, api_key: str) -> str | None:
    if not isinstance(value, str | int | float):
        return None
    sanitized = _sanitize_message(str(value), api_key)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", sanitized):
        return None
    return sanitized


def _sanitize_message(message: str, api_key: str) -> str:
    sanitized = message.replace(api_key, "[REDACTED]") if api_key else message
    sanitized = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [REDACTED]", sanitized)
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
    return " ".join(sanitized.split())[:500]
