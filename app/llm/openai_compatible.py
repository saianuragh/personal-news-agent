"""Small OpenAI-compatible chat-completions transport isolated from the LLM contract."""

from __future__ import annotations

import re
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

    def __init__(self, settings: LLMSettings, *, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self._client = client or httpx.Client(timeout=settings.timeout_seconds)
        self._owns_client = client is None

    @property
    def provider_name(self) -> str:
        return "openai_compatible"

    @property
    def model_name(self) -> str:
        return self.settings.model

    def complete(self, prompt: str, *, max_output_tokens: int) -> str:
        """Send bounded prompt and return only the provider's content string."""
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
                category="network",
                message=f"Provider request timed out ({type(error).__name__}).",
                provider_error_type=type(error).__name__,
            )
            raise TransientProviderError(diagnostic.message, diagnostic=diagnostic) from error
        except httpx.NetworkError as error:
            diagnostic = ProviderDiagnostic(
                category="network",
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
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise PermanentProviderError("LLM provider response had an unexpected shape") from error
        if not isinstance(content, str):
            raise PermanentProviderError("LLM provider response content was not text")
        return content

    def close(self) -> None:
        """Close the internally created HTTP client; injected clients remain caller-owned."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> OpenAICompatibleProvider:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _http_error_diagnostic(response: httpx.Response, api_key: str) -> ProviderDiagnostic:
    """Extract only provider error metadata and redact credential-shaped values."""
    provider_type: str | None = None
    provider_code: str | None = None
    provider_message: str | None = None
    try:
        body: Any = response.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        provider_type = _safe_metadata(error.get("type"), api_key)
        provider_code = _safe_metadata(error.get("code"), api_key)
        provider_message = _safe_metadata(error.get("message"), api_key)
    elif isinstance(error, str):
        provider_message = _sanitize_message(error, api_key)

    category = _classify_http_error(
        response.status_code,
        provider_type=provider_type,
        provider_code=provider_code,
        provider_message=provider_message,
    )
    message = provider_message or (
        f"OpenAI-compatible provider returned HTTP {response.status_code}."
    )
    return ProviderDiagnostic(
        category=category,
        message=message,
        http_status=response.status_code,
        provider_error_type=provider_type,
        provider_error_code=provider_code,
    )


def _classify_http_error(
    status: int,
    *,
    provider_type: str | None,
    provider_code: str | None,
    provider_message: str | None,
) -> str:
    if status == 401:
        return "authentication"
    if status == 403:
        return "authorization"
    if status == 429:
        return "rate_limiting"
    detail = " ".join(value or "" for value in (provider_type, provider_code, provider_message))
    if status == 404 or re.search(r"\bmodel\b|model_not_found", detail, re.IGNORECASE):
        return "model_configuration"
    if 400 <= status < 500 and status != 408:
        return "invalid_request"
    if status == 408:
        return "network"
    if status >= 500:
        return "provider"
    return "other"


def _safe_metadata(value: object, api_key: str) -> str | None:
    if not isinstance(value, str | int | float):
        return None
    sanitized = _sanitize_message(str(value), api_key)
    return sanitized[:100] or None


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
