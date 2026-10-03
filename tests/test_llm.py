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
    ProviderDiagnostic,
    Summarizer,
    TransientProviderError,
    build_prompt,
)
from app.llm.openai_compatible import OpenAICompatibleProvider
from app.models.article import Article
from app.newsletter.renderer import render_newsletter
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


def ranked_story(
    *, description: str | None = "The agency announced a satellite mission.", story_number: int = 1
):
    url = f"https://news.example/story/{story_number}"
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


def test_balanced_json_inside_harmless_prose_is_recovered() -> None:
    response = 'Result follows:\n' + json.dumps(
        {**VALID, "summary": 'A report quotes "new capacity" and {adds context}.'}
    ) + "\nEnd of response."

    result = Summarizer(FakeProvider([response])).summarize(ranked_story(), generated_at=NOW)

    assert result.status == "generated"
    assert result.response_format == "structured_json"
    assert result.summary == 'A report quotes "new capacity" and {adds context}.'


def test_fenced_json_remains_supported() -> None:
    result = Summarizer(FakeProvider([f"```json\n{json.dumps(VALID)}\n```"])).summarize(
        ranked_story(), generated_at=NOW
    )

    assert result.status == "generated"
    assert result.response_format == "structured_json"


def test_missing_or_malformed_optional_fields_do_not_discard_valid_summary() -> None:
    raw = json.dumps(
        {
            "summary": VALID["summary"],
            "why_it_matters": "w" * 701,
            "uncertainty": ["u" * 301, None],
            "harmless_extra": "ignored",
        }
    )

    result = Summarizer(FakeProvider([raw])).summarize(ranked_story(), generated_at=NOW)

    assert result.status == "generated"
    assert result.summary == VALID["summary"]
    assert result.why_it_matters is None
    assert result.uncertainty == ()


def test_unusable_short_plain_text_uses_source_fallback() -> None:
    result = Summarizer(FakeProvider(["not json"])).summarize(ranked_story(), generated_at=NOW)

    assert result.status == "fallback"
    assert result.diagnostic is not None
    assert result.diagnostic.detail == "unusable_plain_text"


@pytest.mark.parametrize(
    "raw",
    [
        '{"summary":',
        '{"why_it_matters":"missing summary","uncertainty":[]}',
        '{"summary":"","why_it_matters":"valid","uncertainty":[]}',
        json.dumps({**VALID, "summary": "s" * 701}),
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
    assert request_body["response_format"] == {"type": "json_object"}
    assert "exactly one valid JSON object" in request_body["messages"][0]["content"]
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


def test_empty_content_preserves_safe_finish_reason_metadata() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": ""}, "finish_reason": "length"}
                    ]
                },
            )
        )
    )
    provider = OpenAICompatibleProvider(
        LLMSettings("test-key", "model", "https://llm.example/v1"), client=client
    )

    result = Summarizer(provider, max_attempts=1).summarize(ranked_story(), generated_at=NOW)

    assert result.status == "fallback"
    assert result.diagnostic is not None
    assert result.diagnostic.category == "empty_response"
    assert result.diagnostic.detail == "empty_content_finish_length"
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


def test_request_pacing_configuration_has_safe_default_and_bounds() -> None:
    settings = LLMSettings.from_env(
        {
            "LLM_API_KEY": "test-key",
            "LLM_MODEL": "openrouter/free",
            "LLM_BASE_URL": "https://openrouter.ai/api/v1",
        }
    )
    assert settings.request_interval_seconds == 3.2
    assert settings.retry_max_wait_seconds == 8
    configured = LLMSettings.from_env(
        {
            "LLM_API_KEY": "test-key",
            "LLM_MODEL": "openrouter/free",
            "LLM_BASE_URL": "https://openrouter.ai/api/v1",
            "LLM_REQUEST_INTERVAL_SECONDS": "4.5",
            "LLM_RETRY_MAX_WAIT_SECONDS": "5",
        }
    )
    assert configured.request_interval_seconds == 4.5
    assert configured.retry_max_wait_seconds == 5
    for name, value in (
        ("LLM_REQUEST_INTERVAL_SECONDS", "3.0"),
        ("LLM_REQUEST_INTERVAL_SECONDS", "61"),
        ("LLM_RETRY_MAX_WAIT_SECONDS", "31"),
    ):
        with pytest.raises(ValueError, match=name):
            LLMSettings.from_env(
                {
                    "LLM_API_KEY": "test-key",
                    "LLM_MODEL": "openrouter/free",
                    "LLM_BASE_URL": "https://openrouter.ai/api/v1",
                    name: value,
                }
            )


def test_provider_paces_requests_at_configured_interval() -> None:
    now = 0.0
    sleeps: list[float] = []

    def sleep_for(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"choices": [{"message": {"content": json.dumps(VALID)}}]}
            )
        )
    )
    settings = LLMSettings(
        "test-key", "model", "https://llm.example/v1", request_interval_seconds=3.2
    )
    provider = OpenAICompatibleProvider(
        settings, client=client, sleep=sleep_for, monotonic=lambda: now
    )
    provider.complete("{}", max_output_tokens=MAX_OUTPUT_TOKENS)
    provider.complete("{}", max_output_tokens=MAX_OUTPUT_TOKENS)
    assert sleeps == [pytest.approx(3.2)]
    client.close()


def test_429_retry_after_is_parsed_and_respected(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO")
    attempts = 0
    retry_waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"error": {}})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(VALID)}}]}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        LLMSettings("test-key", "model", "https://llm.example/v1", request_interval_seconds=3.2),
        client=client,
        sleep=lambda _: None,
        monotonic=lambda: 0.0,
    )
    summarizer = Summarizer(
        provider,
        max_attempts=2,
        sleep=retry_waits.append,
        jitter=lambda low, high: high,
    )
    result = summarizer.summarize(ranked_story(), generated_at=NOW)
    assert result.status == "generated"
    assert attempts == 2
    assert retry_waits == [2]
    assert "strategy=retry_after_used" in caplog.text
    client.close()


def test_429_without_retry_after_uses_bounded_jittered_backoff() -> None:
    sleeps: list[float] = []
    provider = FakeProvider(
        [
            TransientProviderError(
                "rate limited",
                diagnostic=ProviderDiagnostic("rate_limit", "rate limited", http_status=429),
            ),
            TransientProviderError(
                "rate limited",
                diagnostic=ProviderDiagnostic("rate_limit", "rate limited", http_status=429),
            ),
        ]
    )
    result = Summarizer(
        provider,
        max_attempts=2,
        retry_max_wait_seconds=1.2,
        sleep=sleeps.append,
        jitter=lambda low, high: high,
    ).summarize(ranked_story(), generated_at=NOW)
    assert result.status == "fallback"
    assert len(provider.calls) == 2
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(1.2)
    assert result.diagnostic is not None
    assert result.diagnostic.detail == "retry_backoff_exhausted"


def test_unreasonable_retry_after_uses_capped_backoff_instead() -> None:
    sleeps: list[float] = []
    provider = FakeProvider(
        [
            TransientProviderError(
                "rate limited",
                diagnostic=ProviderDiagnostic(
                    "rate_limit", "rate limited", http_status=429, retry_after_seconds=900
                ),
            ),
            json.dumps(VALID),
        ]
    )
    result = Summarizer(
        provider,
        max_attempts=2,
        retry_max_wait_seconds=1.2,
        sleep=sleeps.append,
        jitter=lambda low, high: high,
    ).summarize(ranked_story(), generated_at=NOW)
    assert result.status == "generated"
    assert sleeps[0] == pytest.approx(1.2)
    assert sleeps[0] <= 1.2


def test_repeated_rate_limits_open_circuit_and_all_stories_render_fallbacks() -> None:
    provider = FakeProvider(
        [
            TransientProviderError(
                "rate limited",
                diagnostic=ProviderDiagnostic("rate_limit", "rate limited", http_status=429),
            ),
            TransientProviderError(
                "rate limited",
                diagnostic=ProviderDiagnostic("rate_limit", "rate limited", http_status=429),
            ),
        ]
    )
    summarizer = Summarizer(
        provider, max_attempts=3, sleep=lambda _: None, jitter=lambda low, high: 0
    )
    ranked = [ranked_story(story_number=index) for index in range(1, 4)]
    enriched = [summarizer.summarize(item, generated_at=NOW) for item in ranked]
    assert len(provider.calls) == 2
    assert all(item.status == "fallback" for item in enriched)
    assert enriched[0].attempt_count == 2
    assert enriched[1].attempt_count == 0
    assert enriched[1].diagnostic is not None
    assert enriched[1].diagnostic.detail == "rate_limit_circuit_open"
    document = render_newsletter(enriched, generated_at=NOW, timezone_name="UTC")
    assert len(document.included_story_ids) == 3
    for item in ranked:
        original_url = item.story.retained_article.url
        assert original_url in document.html
        assert original_url in document.plain_text


def test_valid_json_mode_response_still_enriches_after_retry_handling() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"choices": [{"message": {"content": json.dumps(VALID)}}]}
            )
        )
    )
    provider = OpenAICompatibleProvider(
        LLMSettings("test-key", "model", "https://llm.example/v1", request_interval_seconds=3.2),
        client=client,
        sleep=lambda _: None,
        monotonic=lambda: 0.0,
    )
    result = Summarizer(provider).summarize(ranked_story(), generated_at=NOW)
    assert result.status == "generated"
    assert result.response_format == "structured_json"
    assert result.summary == VALID["summary"]
    client.close()
