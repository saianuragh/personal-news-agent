"""Offline contract, fallback, and provider tests for LLM story summaries."""

import json
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import httpx
import pytest
from app.llm.base import (
    MAX_OUTPUT_TOKENS,
    LLMSettings,
    PermanentProviderError,
    Summarizer,
    TransientProviderError,
    build_prompt,
)
from app.llm.openai_compatible import OpenAICompatibleProvider
from app.models.article import Article
from app.processing.categorize import categorize_story
from app.processing.deduplicate import deduplicate_articles
from app.processing.rank import rank_stories

NOW = datetime(2026, 9, 30, 8, tzinfo=UTC)
VALID = {
    "summary": "The agency announced a new satellite mission.",
    "why_it_matters": "The mission could expand available Earth observation data.",
    "uncertainty": ["The source does not state a launch date."],
}


class FakeProvider:
    provider_name = "fake"
    model_name = "test-model"

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, int]] = []

    def complete(self, prompt: str, *, max_output_tokens: int) -> str:
        self.calls.append((prompt, max_output_tokens))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def ranked_story(*, description: str | None = "The agency announced a satellite mission."):
    url = "https://news.example/story/1"
    article = Article(
        article_id=uuid4(),
        source_id="science-source",
        publisher="Example News",
        title="Agency announces satellite mission",
        url=url,
        canonical_url=url,
        retrieved_at=NOW,
        published_at=NOW,
        description=description,
        content_hash=sha256(b"satellite story").hexdigest(),
        source_categories=("Science",),
    )
    story = deduplicate_articles([article]).stories[0]
    categorized = categorize_story(story)
    return rank_stories([categorized], as_of=NOW)[0]


def test_valid_structured_response_converts_to_enriched_story() -> None:
    selected = ranked_story()
    provider = FakeProvider([json.dumps(VALID)])

    result = Summarizer(provider).summarize(selected, generated_at=NOW)

    assert result.status == "generated"
    assert result.attempt_count == 1
    assert result.ranked_story is selected
    assert result.summary == VALID["summary"]
    assert result.response_format == "structured_json"
    assert result.why_it_matters == VALID["why_it_matters"]
    assert result.uncertainty == tuple(VALID["uncertainty"])
    assert result.provider == "fake"
    assert result.model == "test-model"
    assert result.generated_at == NOW
    assert provider.calls[0][1] == MAX_OUTPUT_TOKENS


@pytest.mark.parametrize(
    ("raw", "expected_summary", "expected_why"),
    [
        (
            "The agency announced a satellite mission that will expand Earth observation.",
            "The agency announced a satellite mission that will expand Earth observation.",
            None,
        ),
        (
            "Summary: A satellite mission was announced.\n"
            "Why it matters: It expands observation data.",
            "A satellite mission was announced.",
            "It expands observation data.",
        ),
    ],
)
def test_valid_plain_text_response_is_used_safely(
    raw: str, expected_summary: str, expected_why: str | None
) -> None:
    result = Summarizer(FakeProvider([raw])).summarize(ranked_story(), generated_at=NOW)

    assert result.status == "generated"
    assert result.response_format == "plain_text"
    assert result.summary == expected_summary
    assert result.why_it_matters == expected_why


def test_unusable_short_plain_text_uses_source_fallback() -> None:
    result = Summarizer(FakeProvider(["not json"])).summarize(ranked_story(), generated_at=NOW)

    assert result.status == "fallback"
    assert result.diagnostic is not None
    assert result.diagnostic.detail == "unusable_plain_text"


@pytest.mark.parametrize(
    "raw",
    [
        '{"summary":',
        '{"summary":"only one field"}',
        '{"summary":"ok","why_it_matters":"ok","uncertainty":[],"extra":"no"}',
        '{"summary":"","why_it_matters":"valid","uncertainty":[]}',
        json.dumps({**VALID, "summary": "s" * 701}),
        json.dumps({**VALID, "uncertainty": ["c" * 301]}),
    ],
)
def test_malformed_missing_or_overlong_response_uses_labeled_fallback(raw: str) -> None:
    result = Summarizer(FakeProvider([raw])).summarize(ranked_story(), generated_at=NOW)

    assert result.status == "fallback"
    assert result.diagnostic is not None
    assert result.diagnostic.category in {"invalid_response", "validation_error"}
    assert result.summary == "The agency announced a satellite mission."
    assert result.why_it_matters is None
    assert result.failure_reason == "InvalidResponseError"


def test_uncertainty_caveats_are_preserved() -> None:
    result = Summarizer(FakeProvider([json.dumps(VALID)])).summarize(
        ranked_story(), generated_at=NOW
    )

    assert result.uncertainty == ("The source does not state a launch date.",)


def test_timeout_retries_once_then_succeeds() -> None:
    provider = FakeProvider([TransientProviderError("timeout"), json.dumps(VALID)])

    result = Summarizer(provider, max_attempts=2).summarize(ranked_story(), generated_at=NOW)

    assert result.status == "generated"
    assert len(provider.calls) == 2


def test_transient_provider_error_retry_is_bounded() -> None:
    provider = FakeProvider([TransientProviderError("busy")] * 5)

    result = Summarizer(provider, max_attempts=2).summarize(ranked_story(), generated_at=NOW)

    assert result.status == "fallback"
    assert result.failure_reason == "TransientProviderError"
    assert len(provider.calls) == 2
    assert result.attempt_count == 2
    assert result.diagnostic is not None
    assert result.diagnostic.category == "provider_error"


def test_permanent_provider_failure_is_not_retried_and_falls_back() -> None:
    provider = FakeProvider([PermanentProviderError("rejected")])

    result = Summarizer(provider, max_attempts=3).summarize(ranked_story(), generated_at=NOW)

    assert result.status == "fallback"
    assert result.failure_reason == "PermanentProviderError"
    assert len(provider.calls) == 1


def test_failure_without_source_description_is_explicitly_failed() -> None:
    provider = FakeProvider([PermanentProviderError("rejected")])

    result = Summarizer(provider).summarize(ranked_story(description=None), generated_at=NOW)

    assert result.status == "failed"
    assert result.summary is None
    assert result.why_it_matters is None
    assert result.ranked_story.story.members


def test_malformed_response_without_source_description_is_omitted() -> None:
    result = Summarizer(FakeProvider(['{"summary":'])).summarize(
        ranked_story(description=None), generated_at=NOW
    )

    assert result.status == "failed"
    assert result.summary is None
    assert result.failure_reason == "InvalidResponseError"


def test_prompt_construction_is_deterministic_and_keeps_original_source_url() -> None:
    selected = ranked_story()

    assert build_prompt(selected) == build_prompt(selected)
    assert "https://news.example/story/1" in build_prompt(selected)
    assert "Agency announces satellite mission" in build_prompt(selected)


def test_missing_api_key_or_model_configuration_is_rejected() -> None:
    with pytest.raises(ValueError, match="LLM_API_KEY"):
        LLMSettings.from_env({"LLM_MODEL": "configured", "LLM_BASE_URL": "https://llm.example/v1"})
    with pytest.raises(ValueError, match="LLM_MODEL"):
        LLMSettings.from_env({"LLM_API_KEY": "test-key", "LLM_BASE_URL": "https://llm.example/v1"})


def test_provider_builds_bounded_request_and_extracts_json_content() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(VALID)}}]},
        )

    settings = LLMSettings("test-key", "configured-model", "https://llm.example/v1")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(settings, client=client)

    result = Summarizer(provider).summarize(ranked_story(), generated_at=NOW)

    request_body = json.loads(requests[0].content)
    assert result.status == "generated"
    assert requests[0].url.path == "/v1/chat/completions"
    assert request_body["model"] == "configured-model"
    assert request_body["max_tokens"] == MAX_OUTPUT_TOKENS
    assert request_body["temperature"] == 0
    assert "Do not invent" in request_body["messages"][0]["content"]
    assert "response_format" not in request_body
    assert "test-key" not in str(request_body)
    client.close()


def test_provider_timeout_is_transient_and_summarizer_falls_back() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        LLMSettings("test-key", "model", "https://llm.example/v1"), client=client
    )

    result = Summarizer(provider, max_attempts=2).summarize(ranked_story(), generated_at=NOW)

    assert result.status == "fallback"
    assert result.failure_reason == "TransientProviderError"
    assert result.diagnostic is not None
    assert result.diagnostic.category == "timeout"
    assert result.diagnostic.provider_error_type == "ReadTimeout"
    assert result.attempt_count == 2
    client.close()


def test_openai_compatible_provider_retries_a_real_timeout_once() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("simulated timeout", request=request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(VALID)}}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        LLMSettings("test-key", "model", "https://llm.example/v1"), client=client
    )
    result = Summarizer(provider, max_attempts=2).summarize(ranked_story(), generated_at=NOW)

    assert attempts == 2
    assert result.status == "generated"
    assert result.attempt_count == 2
    client.close()


@pytest.mark.parametrize(
    ("status", "category", "transient"),
    [
        (400, "http_error", False),
        (401, "http_error", False),
        (403, "http_error", False),
        (404, "http_error", False),
        (408, "timeout", True),
        (429, "rate_limit", True),
        (503, "provider_error", True),
    ],
)
def test_http_failures_include_safe_classified_diagnostics(
    status: int, category: str, transient: bool
) -> None:
    response = httpx.Response(
        status,
        json={
            "error": {
                "type": "invalid_request_error",
                "code": "model_not_found" if status == 404 else "invalid_request",
                "message": "Provider rejected request details.",
            }
        },
    )
    client = httpx.Client(transport=httpx.MockTransport(lambda request: response))
    provider = OpenAICompatibleProvider(
        LLMSettings("test-key", "model", "https://llm.example/v1"), client=client
    )

    expected_error = TransientProviderError if transient else PermanentProviderError
    with pytest.raises(expected_error) as raised:
        provider.complete("{}", max_output_tokens=MAX_OUTPUT_TOKENS)
    assert raised.value.diagnostic is not None
    assert raised.value.diagnostic.http_status == status
    assert raised.value.diagnostic.provider_error_type == "invalid_request_error"
    assert raised.value.diagnostic.provider_error_code == (
        "model_not_found" if status == 404 else "invalid_request"
    )
    assert raised.value.diagnostic.category == category
    assert raised.value.diagnostic.message == f"OpenAI-compatible provider returned HTTP {status}."
    client.close()


def test_http_error_diagnostic_redacts_credentials_and_limits_message() -> None:
    secret = "sk-proj-very-secret-value"
    message = (
        f"Invalid key {secret}; Authorization: Bearer {secret}; "
        f"api_key={secret}; https://user:password@api.example/?token={secret}"
    )
    response = httpx.Response(
        401,
        json={
            "error": {
                "type": "authentication_error",
                "code": "invalid_api_key",
                "message": message,
            }
        },
    )
    client = httpx.Client(transport=httpx.MockTransport(lambda request: response))
    provider = OpenAICompatibleProvider(
        LLMSettings(secret, "model", "https://llm.example/v1"), client=client
    )

    with pytest.raises(PermanentProviderError) as raised:
        provider.complete("{}", max_output_tokens=MAX_OUTPUT_TOKENS)
    diagnostic = raised.value.diagnostic
    assert diagnostic is not None
    assert diagnostic.category == "http_error"
    assert secret not in diagnostic.message
    assert secret not in repr(diagnostic)
    assert diagnostic.message == "OpenAI-compatible provider returned HTTP 401."
    assert "Provider rejected request details" not in diagnostic.message
    assert len(diagnostic.message) <= 500
    client.close()


def test_empty_provider_content_is_classified_and_falls_back() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    client = httpx.Client(
        transport=httpx.MockTransport(handler)
    )
    provider = OpenAICompatibleProvider(
        LLMSettings("test-key", "model", "https://llm.example/v1"), client=client
    )

    result = Summarizer(provider).summarize(ranked_story(), generated_at=NOW)

    assert result.status == "fallback"
    assert result.diagnostic is not None
    assert result.diagnostic.category == "empty_response"
    assert result.diagnostic.detail == "empty_content"
    assert result.attempt_count == 2
    assert attempts == 2
    client.close()


def test_empty_provider_content_retries_once_and_uses_recovery() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        content = "" if attempts == 1 else json.dumps(VALID)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        LLMSettings("test-key", "model", "https://llm.example/v1"), client=client
    )
    result = Summarizer(provider, max_attempts=2).summarize(ranked_story(), generated_at=NOW)

    assert attempts == 2
    assert result.status == "generated"
    assert result.attempt_count == 2
    client.close()


def test_unexpected_provider_response_shape_is_classified_without_body() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"result": ""}))
    )
    provider = OpenAICompatibleProvider(
        LLMSettings("test-key", "model", "https://llm.example/v1"), client=client
    )

    result = Summarizer(provider).summarize(ranked_story(), generated_at=NOW)

    assert result.status == "fallback"
    assert result.diagnostic is not None
    assert result.diagnostic.category == "invalid_response"
    assert result.diagnostic.detail == "missing_choices"
    client.close()


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"choices": [{"message": {}}]}, "missing_content"),
        ({"choices": []}, "missing_choices"),
    ],
)
def test_missing_choices_or_content_has_specific_safe_diagnostic(body, detail: str) -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    )
    provider = OpenAICompatibleProvider(
        LLMSettings("test-key", "model", "https://llm.example/v1"), client=client
    )

    with pytest.raises(PermanentProviderError) as raised:
        provider.complete("private prompt", max_output_tokens=MAX_OUTPUT_TOKENS)

    assert raised.value.diagnostic is not None
    assert raised.value.diagnostic.detail == detail
    assert "private prompt" not in str(raised.value.diagnostic)
    client.close()


def test_empty_raw_model_response_is_distinguished_from_invalid_json() -> None:
    result = Summarizer(FakeProvider([""])).summarize(ranked_story(), generated_at=NOW)

    assert result.status == "fallback"
    assert result.diagnostic is not None
    assert result.diagnostic.category == "empty_response"


def test_non_text_model_response_is_classified_and_falls_back() -> None:
    result = Summarizer(FakeProvider([None])).summarize(ranked_story(), generated_at=NOW)

    assert result.status == "fallback"
    assert result.diagnostic is not None
    assert result.diagnostic.category == "invalid_response"
    assert result.attempt_count == 1


def test_schema_validation_failure_has_validation_category() -> None:
    malformed = json.dumps({**VALID, "summary": ""})
    result = Summarizer(FakeProvider([malformed])).summarize(ranked_story(), generated_at=NOW)

    assert result.status == "fallback"
    assert result.diagnostic is not None
    assert result.diagnostic.category == "validation_error"
    assert result.diagnostic.detail == "schema_mismatch"
    assert result.attempt_count == 1


def test_malformed_json_has_invalid_response_category() -> None:
    result = Summarizer(FakeProvider(['{"summary":'])).summarize(ranked_story(), generated_at=NOW)

    assert result.status == "fallback"
    assert result.diagnostic is not None
    assert result.diagnostic.category == "invalid_response"
    assert result.diagnostic.detail == "malformed_json"


def test_api_key_is_excluded_from_settings_repr() -> None:
    settings = LLMSettings("test-key-must-not-appear", "model", "https://llm.example/v1")

    assert "test-key-must-not-appear" not in repr(settings)
