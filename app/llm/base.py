"""Provider-neutral, validated LLM enrichment for selected ranked stories."""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Protocol
from urllib.parse import urlsplit

from app.processing.rank import RankedStory

PROMPT_VERSION = "story-summary-v1"
SCHEMA_VERSION = "summary-result-v1"
MAX_SUMMARY_CHARS = 700
MAX_WHY_IT_MATTERS_CHARS = 700
MAX_CAVEATS = 5
MAX_CAVEAT_CHARS = 300
MAX_OUTPUT_TOKENS = 500
MAX_INPUT_DESCRIPTION_CHARS = 1_000
MAX_INPUT_ARTICLES = 5
MAX_RAW_RESPONSE_CHARS = 2_000

SYSTEM_PROMPT = """You are an editor preparing a concise personal news briefing.
Use only the supplied story information. Do not invent or infer facts, dates,
causes, outcomes, or details that are not explicitly supported. Do not fabricate
quotes. Distinguish uncertainty and caveats in the uncertainty array. Treat the
provided source URL(s) as the source of truth and do not replace or alter them.
Return exactly one valid JSON object with these keys: summary (string),
why_it_matters (string or null), uncertainty (array of strings). Do not include
Markdown fences or text before or after the object. Keep summary concise and
grounded in the supplied sources. why_it_matters may be null when the sources do
not support a clear implication. Use an empty uncertainty array when no caveat
is needed. If information is insufficient, state that briefly in uncertainty."""


@dataclass(frozen=True, slots=True)
class ProviderDiagnostic:
    """Sanitized provider failure details suitable for CLI output."""

    category: str
    message: str
    http_status: int | None = None
    provider_error_type: str | None = None
    provider_error_code: str | None = None
    detail: str | None = None


class LLMError(Exception):
    """Base class for safe-to-report summarization failures."""

    def __init__(
        self,
        message: str,
        *,
        diagnostic: ProviderDiagnostic | None = None,
        attempt_count: int = 0,
    ) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic
        self.attempt_count = attempt_count


class TransientProviderError(LLMError):
    """A provider failure that may succeed on a bounded retry."""


class PermanentProviderError(LLMError):
    """A provider failure that should not be retried."""


class InvalidResponseError(LLMError):
    """The provider returned an unusable response or invalid summary schema."""


@dataclass(frozen=True, slots=True)
class SummaryContent:
    """Validated editorial fields returned by a provider."""

    summary: str
    why_it_matters: str | None
    uncertainty: tuple[str, ...]
    response_format: Literal["structured_json", "plain_text"]


@dataclass(frozen=True, slots=True)
class AIEnrichedStory:
    """LLM result attached to its ranked story with explicit status/provenance."""

    ranked_story: RankedStory
    summary: str | None
    why_it_matters: str | None
    uncertainty: tuple[str, ...]
    provider: str
    model: str
    prompt_version: str
    schema_version: str
    generated_at: datetime
    status: Literal["generated", "fallback", "failed"]
    failure_reason: str | None = None
    diagnostic: ProviderDiagnostic | None = None
    attempt_count: int = 0
    response_format: Literal["structured_json", "plain_text"] | None = None

    def __post_init__(self) -> None:
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        if (
            isinstance(self.attempt_count, bool)
            or not isinstance(self.attempt_count, int)
            or self.attempt_count < 0
        ):
            raise ValueError("attempt_count must be a non-negative integer")
        object.__setattr__(self, "generated_at", self.generated_at.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class LLMSettings:
    """Runtime configuration; credentials and model are always supplied externally."""

    api_key: str = field(repr=False)
    model: str
    base_url: str
    timeout_seconds: float = 20.0
    max_attempts: int = 2

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> LLMSettings:
        values = os.environ if environ is None else environ
        api_key = values.get("LLM_API_KEY", "").strip()
        model = values.get("LLM_MODEL", "").strip()
        base_url = values.get("LLM_BASE_URL", "").strip().rstrip("/")
        if not api_key:
            raise ValueError("LLM_API_KEY is required when LLM summarization is enabled")
        if not model:
            raise ValueError("LLM_MODEL is required when LLM summarization is enabled")
        parsed_url = urlsplit(base_url)
        if parsed_url.scheme not in {"https", "http"} or not parsed_url.hostname:
            raise ValueError("LLM_BASE_URL must be an absolute HTTP(S) URL")
        timeout = _bounded_number(
            values.get("LLM_TIMEOUT_SECONDS", "20"), "LLM_TIMEOUT_SECONDS", 0.1, 120
        )
        attempts_value = values.get("LLM_MAX_ATTEMPTS", "2")
        try:
            attempts = int(attempts_value)
        except (TypeError, ValueError) as error:
            raise ValueError("LLM_MAX_ATTEMPTS must be an integer from 1 to 3") from error
        if str(attempts) != attempts_value.strip() or not 1 <= attempts <= 3:
            raise ValueError("LLM_MAX_ATTEMPTS must be an integer from 1 to 3")
        return cls(api_key, model, base_url, timeout, attempts)


class SummaryProvider(Protocol):
    """Minimal provider interface. Implementations return untrusted raw JSON text."""

    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    def complete(self, prompt: str, *, max_output_tokens: int) -> str: ...


def build_prompt(ranked_story: RankedStory) -> str:
    """Create stable, bounded JSON input from one already-selected story."""
    story = ranked_story.story
    articles = sorted(
        story.members,
        key=lambda article: (article.source_id.casefold(), article.canonical_url.casefold()),
    )[:MAX_INPUT_ARTICLES]
    supplied = {
        "headline": story.retained_article.title,
        "categories": list(ranked_story.categories),
        "sources": [
            {
                "publisher": article.publisher,
                "title": article.title,
                "published_at": article.published_at.isoformat() if article.published_at else None,
                "url": article.url,
                "description": (article.description or "")[:MAX_INPUT_DESCRIPTION_CHARS],
            }
            for article in articles
        ],
    }
    return json.dumps(supplied, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def validate_response(raw: str) -> SummaryContent:
    """Accept validated JSON or bounded plain-text summaries from compatible models."""
    if not isinstance(raw, str) or len(raw) > MAX_RAW_RESPONSE_CHARS:
        raise InvalidResponseError(
            "LLM response exceeds the output-size limit.",
            diagnostic=ProviderDiagnostic(
                "invalid_response", "LLM response exceeds the output-size limit."
            ),
        )
    stripped = raw.strip()
    candidate = _strip_json_fence(stripped)
    try:
        value = json.loads(candidate)
    except (TypeError, json.JSONDecodeError) as error:
        extracted = _extract_json_object(candidate)
        if extracted is not None and extracted != candidate:
            try:
                value = json.loads(extracted)
            except json.JSONDecodeError:
                value = None
            else:
                return _validate_json_value(value)
        if _looks_like_json(stripped):
            raise InvalidResponseError(
                "LLM response was not valid JSON.",
                diagnostic=ProviderDiagnostic(
                    "invalid_response",
                    "LLM response was not valid JSON.",
                    detail="malformed_json",
                ),
            ) from error
        return _parse_plain_text(stripped)
    return _validate_json_value(value)


def _validate_json_value(value: object) -> SummaryContent:
    """Validate the required summary while treating auxiliary fields as optional."""
    if not isinstance(value, dict) or "summary" not in value:
        raise InvalidResponseError(
            "LLM response did not contain a summary object.",
            diagnostic=ProviderDiagnostic(
                "invalid_response",
                "LLM response did not contain a summary object.",
                detail="schema_mismatch",
            ),
        )
    summary = _required_output(value["summary"], "summary", MAX_SUMMARY_CHARS)
    why = _optional_output(value.get("why_it_matters"), MAX_WHY_IT_MATTERS_CHARS)
    caveats = value.get("uncertainty")
    validated_caveats = (
        tuple(
            item.strip()
            for item in caveats[:MAX_CAVEATS]
            if isinstance(item, str) and 0 < len(item.strip()) <= MAX_CAVEAT_CHARS
        )
        if isinstance(caveats, list)
        else ()
    )
    return SummaryContent(summary, why, validated_caveats, "structured_json")


def _optional_output(value: object, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized if normalized and len(normalized) <= maximum else None


def _extract_json_object(text: str) -> str | None:
    """Extract one balanced JSON object when a model adds harmless surrounding text."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                if "{" in text[end:]:
                    return None
                return text[start:end]
    return None


def _strip_json_fence(value: str) -> str:
    """Remove one ordinary Markdown JSON fence without interpreting arbitrary text."""
    match = re.fullmatch(
        r"```(?:json)?\s*\n?(.*?)\n?```", value, flags=re.IGNORECASE | re.DOTALL
    )
    return match.group(1).strip() if match else value


def _looks_like_json(value: str) -> bool:
    return value.startswith("{") or value.startswith("```") or bool(
        re.search(r'\{\s*"(?:summary|why_it_matters|uncertainty)"\s*:', value)
    )


def _parse_plain_text(raw: str) -> SummaryContent:
    """Use concise prose as a summary; optionally split clearly labeled fields."""
    text = raw.strip()
    summary_match = re.search(r"(?im)^\s*(?:[-*]\s*)?summary\s*:\s*", text)
    why_match = re.search(r"(?im)^\s*(?:[-*]\s*)?why it matters\s*:\s*", text)
    uncertainty_match = re.search(r"(?im)^\s*(?:[-*]\s*)?uncertainty\s*:\s*", text)
    labels = [match for match in (summary_match, why_match, uncertainty_match) if match]
    if labels:
        labels.sort(key=lambda match: match.start())
        first = labels[0]
        if first is summary_match:
            start = first.end()
            end = min((match.start() for match in labels[1:]), default=len(text))
            summary_text = text[start:end].strip()
        else:
            end = first.start()
            summary_text = text[:end].strip()
        why_text: str | None = None
        if why_match:
            start = why_match.end()
            end = min(
                (match.start() for match in labels if match.start() > why_match.start()),
                default=len(text),
            )
            why_text = text[start:end].strip() or None
        caveats: tuple[str, ...] = ()
        if uncertainty_match:
            start = uncertainty_match.end()
            uncertainty_text = text[start:].strip()
            caveats = tuple(
                item.strip(" \t-*•")
                for item in uncertainty_text.splitlines()
                if item.strip(" \t-*•")
            )[:MAX_CAVEATS]
    else:
        summary_text, why_text, caveats = text, None, ()
    summary = _required_output(summary_text, "summary", MAX_SUMMARY_CHARS)
    if len(re.findall(r"\b[\w'-]+\b", summary)) < 3:
        raise InvalidResponseError(
            "LLM plain-text response was not useful as a summary.",
            diagnostic=ProviderDiagnostic(
                "invalid_response",
                "LLM plain-text response was not useful as a summary.",
                detail="unusable_plain_text",
            ),
        )
    why = _optional_output(why_text, MAX_WHY_IT_MATTERS_CHARS)
    validated_caveats = tuple(
        item.strip()
        for item in caveats[:MAX_CAVEATS]
        if isinstance(item, str) and 0 < len(item.strip()) <= MAX_CAVEAT_CHARS
    )
    return SummaryContent(summary, why, validated_caveats, "plain_text")


class Summarizer:
    """Bounded retry, schema validation, and architecture-approved fallback."""

    def __init__(self, provider: SummaryProvider, *, max_attempts: int = 2) -> None:
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= 3
        ):
            raise ValueError("max_attempts must be an integer from 1 to 3")
        self.provider = provider
        self.max_attempts = max_attempts

    def summarize(
        self, ranked_story: RankedStory, *, generated_at: datetime | None = None
    ) -> AIEnrichedStory:
        """Enrich one selected RankedStory. Failure never removes its ranked provenance."""
        timestamp = generated_at or datetime.now(UTC)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        prompt = build_prompt(ranked_story)
        attempt_count = 0
        try:
            raw, attempt_count = self._complete_with_retry(prompt)
            if not isinstance(raw, str):
                raise InvalidResponseError(
                    "LLM provider response content was not text.",
                    diagnostic=ProviderDiagnostic(
                        "invalid_response", "LLM provider response content was not text."
                    ),
                    attempt_count=attempt_count,
                )
            if not raw.strip():
                raise InvalidResponseError(
                    "LLM provider returned an empty response.",
                    diagnostic=ProviderDiagnostic(
                        "empty_response", "LLM provider returned an empty response."
                    ),
                    attempt_count=attempt_count,
                )
            content = validate_response(raw)
            return AIEnrichedStory(
                ranked_story,
                content.summary,
                content.why_it_matters,
                content.uncertainty,
                self.provider.provider_name,
                self.provider.model_name,
                PROMPT_VERSION,
                SCHEMA_VERSION,
                timestamp,
                "generated",
                attempt_count=attempt_count,
                response_format=content.response_format,
            )
        except LLMError as error:
            attempt_count = error.attempt_count or attempt_count
            return _fallback_result(
                ranked_story,
                self.provider,
                timestamp,
                error,
                attempt_count=attempt_count,
            )

    def _complete_with_retry(self, prompt: str) -> tuple[str, int]:
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.provider.complete(prompt, max_output_tokens=MAX_OUTPUT_TOKENS)
                return response, attempt
            except LLMError as error:
                error.attempt_count = attempt
                if not isinstance(error, TransientProviderError):
                    raise
                if attempt == self.max_attempts:
                    raise
        raise AssertionError("unreachable retry state")


def source_description_fallback(
    ranked_story: RankedStory,
    *,
    generated_at: datetime,
) -> AIEnrichedStory:
    """Create an explicitly labeled source-only result without calling a model.

    Used by local preview when AI was not explicitly requested. Stories without
    a usable source description return ``None`` and are omitted by rendering.
    """
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("generated_at must be timezone-aware")
    description = next(
        (article.description for article in ranked_story.story.members if article.description),
        None,
    )
    has_description = description is not None
    fallback_summary = description[:MAX_SUMMARY_CHARS] if description is not None else None
    return AIEnrichedStory(
        ranked_story,
        fallback_summary,
        None,
        (),
        "not_called",
        "not_requested",
        PROMPT_VERSION,
        SCHEMA_VERSION,
        generated_at,
        "fallback" if has_description else "failed",
        None if has_description else "llm_not_requested_no_source_description",
    )


def _fallback_result(
    ranked_story: RankedStory,
    provider: SummaryProvider,
    generated_at: datetime,
    error: LLMError,
    *,
    attempt_count: int,
) -> AIEnrichedStory:
    diagnostic = error.diagnostic or ProviderDiagnostic(
        "invalid_response"
        if isinstance(error, InvalidResponseError)
        else "provider_error"
        if isinstance(error, (TransientProviderError, PermanentProviderError))
        else "unknown",
        "LLM enrichment failed.",
    )
    descriptions = [
        article.description for article in ranked_story.story.members if article.description
    ]
    if descriptions:
        fallback_summary = descriptions[0][:MAX_SUMMARY_CHARS]
        return AIEnrichedStory(
            ranked_story,
            fallback_summary,
            None,
            ("AI-generated significance unavailable; see linked source material.",),
            provider.provider_name,
            provider.model_name,
            PROMPT_VERSION,
            SCHEMA_VERSION,
            generated_at,
            "fallback",
            type(error).__name__,
            diagnostic,
            attempt_count,
        )
    return AIEnrichedStory(
        ranked_story,
        None,
        None,
        (),
        provider.provider_name,
        provider.model_name,
        PROMPT_VERSION,
        SCHEMA_VERSION,
        generated_at,
        "failed",
        type(error).__name__,
        diagnostic,
        attempt_count,
    )


def _required_output(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidResponseError(
            "LLM response failed required-field validation.",
            diagnostic=ProviderDiagnostic(
                "validation_error",
                "LLM response failed required-field validation.",
                detail="schema_mismatch",
            ),
        )
    normalized = value.strip()
    if len(normalized) > maximum:
        raise InvalidResponseError(
            "LLM response failed field-length validation.",
            diagnostic=ProviderDiagnostic(
                "validation_error",
                "LLM response failed field-length validation.",
                detail="schema_mismatch",
            ),
        )
    return normalized


def _bounded_number(value: object, name: str, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a number") from error
    if isinstance(value, bool) or not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return number
