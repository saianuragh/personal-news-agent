"""Offline integration tests for the pipeline and its injected external services."""

from __future__ import annotations

import json
import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import app.pipeline.runner as runner_module
import httpx
import yaml
from app.config import SourceConfig
from app.database.base import PipelineRunRepository
from app.database.sqlite_runs import SQLitePipelineRunRepository
from app.email.base import (
    AmbiguousEmailError,
    EmailMessage,
    EmailSettings,
    PermanentEmailError,
    ProviderReceipt,
    newsletter_idempotency_key,
)
from app.llm.base import AIEnrichedStory, LLMSettings, ProviderDiagnostic, Summarizer
from app.llm.openai_compatible import OpenAICompatibleProvider
from app.pipeline.runner import PipelineDependencies, PipelineRunner
from app.sources.base import FetchedFeed, RawProviderEntry, SourceFetchError

AS_OF = datetime(2026, 9, 30, 8, tzinfo=UTC)


class FakeSource:
    def __init__(self, feed: FetchedFeed | None = None, error: Exception | None = None) -> None:
        self.feed = feed
        self.error = error

    def fetch(self) -> FetchedFeed:
        if self.error:
            raise self.error
        assert self.feed is not None
        return self.feed


class FakeSummarizer:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []

    def summarize(self, story, *, generated_at: datetime) -> AIEnrichedStory:
        self.calls.append(str(story.story.story_id))
        if self.fail:
            from app.llm.base import PermanentProviderError

            raise PermanentProviderError("fake summarizer failed")
        return AIEnrichedStory(
            story,
            "A concise grounded summary.",
            "The development may matter to affected readers.",
            ("Long-term effects are not stated.",),
            "fake-provider",
            "fake-model",
            "fake-prompt-v1",
            "fake-schema-v1",
            generated_at,
            "generated",
        )


class DiagnosticSummarizer:
    def summarize(self, story, *, generated_at: datetime) -> AIEnrichedStory:
        return AIEnrichedStory(
            story,
            "Source description for fallback.",
            None,
            (),
            "openai_compatible",
            "test-model",
            "test-prompt",
            "test-schema",
            generated_at,
            "fallback",
            "TransientProviderError",
            ProviderDiagnostic(
                "rate_limit",
                "Rate limit exceeded.",
                http_status=429,
                provider_error_type="rate_limit_error",
                provider_error_code="rate_limit_exceeded",
            ),
        )


class FakeEmailProvider:
    provider_name = "fake-email"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> ProviderReceipt:
        self.messages.append(message)
        if self.fail:
            raise PermanentEmailError("fake provider rejection")
        return ProviderReceipt("fake-message-id")


def setup_config(
    tmp_path: Path,
    source_ids: tuple[str, ...] = ("source-a",),
    *,
    enabled: bool = True,
) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    repository_config = Path(__file__).resolve().parents[1] / "config"
    for filename in ("categories.yaml", "ranking.yaml", "selection.yaml"):
        shutil.copyfile(repository_config / filename, config_dir / filename)
    source_values = [
        {
            "source_id": source_id,
            "enabled": enabled,
            "name": f"Publisher {source_id}",
            "kind": "rss_atom",
            "endpoint": f"https://{source_id}.example/feed.xml",
            "categories": ["Technology"],
            "quality_weight": 0.8,
            "timeout_seconds": 2,
            "request_interval_seconds": 1,
        }
        for source_id in source_ids
    ]
    (config_dir / "sources.yaml").write_text(
        yaml.safe_dump({"sources": source_values}, sort_keys=False), encoding="utf-8"
    )
    return config_dir


def raw_entry(
    title: str = "Technology company releases a processor",
    *,
    url: str = "https://news.example/technology/story-1",
    published_at: datetime | None = AS_OF - timedelta(hours=2),
    description: str | None = "A company announced a new processor.",
    categories: tuple[str, ...] = (),
) -> RawProviderEntry:
    return RawProviderEntry(
        "rss",
        {
            "title": title,
            "link": url,
            "pubDate": published_at.strftime("%a, %d %b %Y %H:%M:%S GMT") if published_at else None,
            "description": description,
            "categories": categories,
            "language": "en",
        },
    )


def source_factory_for(
    outcomes: dict[str, tuple[RawProviderEntry, ...] | Exception],
    observed: list[tuple[str, datetime]] | None = None,
):
    def factory(source: SourceConfig, as_of: datetime) -> FakeSource:
        if observed is not None:
            observed.append((source.source_id, as_of))
        outcome = outcomes[source.source_id]
        if isinstance(outcome, Exception):
            return FakeSource(error=outcome)
        return FakeSource(FetchedFeed(source, as_of, outcome))

    return factory


def email_settings() -> EmailSettings:
    return EmailSettings(
        api_key="test-only-api-key",
        sender="briefing@example.test",
        recipient="reader@example.test",
        max_attempts=1,
    )


def run_preview(
    tmp_path: Path,
    source_ids: tuple[str, ...],
    outcomes: dict[str, tuple[RawProviderEntry, ...] | Exception],
    *,
    summarizer: FakeSummarizer | None = None,
    email_provider: FakeEmailProvider | None = None,
    as_of: datetime = AS_OF,
    ai_in_preview: bool = False,
    run_repository: PipelineRunRepository | None = None,
):
    config_dir = setup_config(tmp_path, source_ids)
    dependencies = PipelineDependencies(
        source_factory=source_factory_for(outcomes),
        summarizer=summarizer,
        email_provider=email_provider,
        run_repository=run_repository,
    )
    return PipelineRunner(dependencies).run(
        "preview",
        as_of=as_of,
        config_directory=config_dir,
        preview_directory=tmp_path / "preview",
        ai_in_preview=ai_in_preview,
        environ={},
    )


def test_pipeline_persists_successful_preview_run(tmp_path: Path) -> None:
    repository = SQLitePipelineRunRepository(tmp_path / "history.sqlite3")
    email_provider = FakeEmailProvider()
    result = run_preview(
        tmp_path,
        ("source-a",),
        {"source-a": (raw_entry(),)},
        email_provider=email_provider,
        run_repository=repository,
    )

    stored = repository.get_run(result.run_id)
    assert result.status == "preview"
    assert stored is not None
    assert stored["status"] == "preview"
    assert stored["raw_entries"] == result.counts.raw_entries == 1
    assert stored["selected_count"] == result.counts.selected_stories
    assert stored["completed_at"] is not None
    assert stored["delivery_outcome"] == "preview"
    assert not repository.delivery_claim_exists("preview/2026-09-30")
    assert email_provider.messages == []


def test_pipeline_passes_yaml_freshness_window_into_ranking(
    tmp_path: Path, monkeypatch
) -> None:
    config_dir = setup_config(tmp_path, ("source-a",))
    ranking_path = config_dir / "ranking.yaml"
    ranking_document = yaml.safe_load(ranking_path.read_text(encoding="utf-8"))
    ranking_document["freshness_window_hours"] = 24
    ranking_path.write_text(yaml.safe_dump(ranking_document), encoding="utf-8")

    ranked_outputs = []
    original_rank_stories = runner_module.rank_stories

    def capture_ranked_stories(stories, source_configs, *, as_of, config=None):
        ranked = original_rank_stories(stories, source_configs, as_of=as_of, config=config)
        ranked_outputs.extend(ranked)
        return ranked

    monkeypatch.setattr(runner_module, "rank_stories", capture_ranked_stories)
    dependencies = PipelineDependencies(
        source_factory=source_factory_for(
            {
                "source-a": (
                    raw_entry(published_at=AS_OF - timedelta(hours=12)),
                )
            }
        )
    )

    result = PipelineRunner(dependencies).run(
        "preview",
        as_of=AS_OF,
        config_directory=config_dir,
        preview_directory=tmp_path / "preview",
        environ={},
    )

    assert result.status == "preview"
    assert len(ranked_outputs) == 1
    freshness = next(
        signal for signal in ranked_outputs[0].score_breakdown if signal.name == "freshness"
    )
    assert freshness.value == 50.0
    assert "freshness_window_hours=24" in freshness.evidence


def test_newsletter_render_failure_is_persisted_without_preview_success(
    tmp_path: Path, monkeypatch
) -> None:
    repository = SQLitePipelineRunRepository(tmp_path / "history.sqlite3")

    def fail_render(*args, **kwargs):
        raise RuntimeError("renderer failed")

    monkeypatch.setattr("app.pipeline.runner.render_newsletter", fail_render)
    result = run_preview(
        tmp_path,
        ("source-a",),
        {"source-a": (raw_entry(),)},
        run_repository=repository,
    )

    stored = repository.get_run(result.run_id)
    assert result.status == "pipeline_failed"
    assert result.error == "Newsletter rendering failed."
    assert result.stage_failures[-1].stage == "render"
    assert stored is not None
    assert stored["status"] == "pipeline_failed"
    assert stored["delivery_outcome"] is None
    assert stored["completed_at"] is not None


def test_pipeline_emits_structured_secret_safe_lifecycle_logs(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="personal_news_agent.pipeline")
    repository = SQLitePipelineRunRepository(tmp_path / "history.sqlite3")
    secret_values = ("API_SECRET_SENTINEL", "PASSWORD_SENTINEL", "TOKEN_SENTINEL")
    result = run_preview(
        tmp_path,
        ("source-a", "source-b"),
        {
            "source-a": (raw_entry(),),
            "source-b": RuntimeError(
                "request failed api_key=API_SECRET_SENTINEL "
                "password=PASSWORD_SENTINEL token=TOKEN_SENTINEL"
            ),
        },
        run_repository=repository,
    )

    events = [json.loads(record.message) for record in caplog.records]
    started = next(event for event in events if event["event"] == "pipeline.run.started")
    finished = next(event for event in events if event["event"] == "pipeline.run.finished")
    assert started["run_id"] == str(result.run_id)
    assert finished["status"] == "partial"
    assert finished["counts"]["raw_entries"] == 1
    assert finished["counts"]["selected_stories"] == 1
    assert finished["delivery_attempted"] is False
    logs = " ".join(record.message for record in caplog.records)
    for secret in secret_values:
        assert secret not in logs
        assert secret not in str(result.source_failures)
    stored = repository.get_run(result.run_id)
    assert stored is not None
    for secret in secret_values:
        assert secret not in stored["warnings_json"]
        assert secret not in (stored["error"] or "")
    assert finished["source_failures"] == [
        {"error_type": "RuntimeError", "source_id": "source-b", "stage": "source_fetch"}
    ]


def test_database_start_failure_stops_pipeline_before_source_or_email(
    tmp_path: Path, caplog
) -> None:
    secret = "postgresql://agent:database-secret@example.invalid/news"

    class FailingRepository:
        def create_run(self, *args, **kwargs) -> None:
            raise OSError(f"connection failed: {secret}")

        def complete_run(self, *args, **kwargs) -> None:
            raise AssertionError("completion must not run after start failure")

    email_provider = FakeEmailProvider()
    caplog.set_level(logging.INFO, logger="personal_news_agent.pipeline")
    result = run_preview(
        tmp_path,
        ("source-a",),
        {"source-a": (raw_entry(),)},
        email_provider=email_provider,
        run_repository=FailingRepository(),  # type: ignore[arg-type]
    )

    assert result.status == "persistence_failed"
    assert result.counts.sources_attempted == 0
    assert result.stage_failures[0].stage == "database_start"
    assert not email_provider.messages
    assert secret not in str(result)
    assert secret not in caplog.text


def test_corrupted_sqlite_file_fails_safely_before_pipeline_work(tmp_path: Path) -> None:
    database_path = tmp_path / "corrupted.sqlite3"
    database_path.write_bytes(b"this is not a SQLite database")
    repository = SQLitePipelineRunRepository(database_path)
    email_provider = FakeEmailProvider()
    result = run_preview(
        tmp_path,
        ("source-a",),
        {"source-a": (raw_entry(),)},
        email_provider=email_provider,
        run_repository=repository,
    )

    assert result.status == "persistence_failed"
    assert result.exit_code != 0
    assert result.counts.sources_attempted == 0
    assert result.newsletter is None
    assert result.delivery is None
    assert not email_provider.messages


def test_final_run_persistence_failure_does_not_report_success(
    tmp_path: Path, caplog
) -> None:
    secret = "postgresql://agent:database-secret@example.invalid/news"
    stored_repository = SQLitePipelineRunRepository(tmp_path / "history.sqlite3")

    class FailingCompletionRepository:
        def create_run(self, *args, **kwargs) -> None:
            stored_repository.create_run(*args, **kwargs)

        def complete_run(self, *args, **kwargs) -> None:
            raise OSError(f"update failed for {secret}")

        def get_run(self, run_id):
            return stored_repository.get_run(run_id)

    caplog.set_level(logging.INFO, logger="personal_news_agent.pipeline")
    result = run_preview(
        tmp_path,
        ("source-a",),
        {"source-a": (raw_entry(),)},
        run_repository=FailingCompletionRepository(),  # type: ignore[arg-type]
    )

    stored = stored_repository.get_run(result.run_id)
    assert result.status == "persistence_failed"
    assert result.exit_code != 0
    assert result.counts.sources_succeeded == 1
    assert result.counts.normalized_articles == 1
    assert result.newsletter is not None and result.newsletter.included_story_ids
    assert result.delivery is not None and result.delivery.status == "preview"
    assert result.stage_failures[-1].stage == "database_completion"
    assert "could not be completed" in (result.error or "")
    assert stored is not None
    assert stored["status"] == "running"
    assert stored["completed_at"] is None
    assert secret not in str(result)
    assert secret not in caplog.text
    finished = next(
        json.loads(record.message)
        for record in caplog.records
        if json.loads(record.message).get("event") == "pipeline.run.finished"
    )
    assert finished["status"] == "persistence_failed"
    assert secret not in json.dumps(finished)


def test_final_persistence_failure_after_email_acceptance_is_not_reported_as_sent(
    tmp_path: Path,
) -> None:
    stored_repository = SQLitePipelineRunRepository(tmp_path / "history.sqlite3")

    class FailingCompletionRepository:
        def create_run(self, *args, **kwargs) -> None:
            stored_repository.create_run(*args, **kwargs)

        def complete_run(self, *_args, **_kwargs) -> None:
            raise OSError("database update failed")

        def claim_delivery(self, *args, **kwargs) -> bool:
            return stored_repository.claim_delivery(*args, **kwargs)

        def delivery_claim_exists(self, *args, **kwargs) -> bool:
            return stored_repository.delivery_claim_exists(*args, **kwargs)

        def complete_delivery_claim(self, *args, **kwargs) -> None:
            stored_repository.complete_delivery_claim(*args, **kwargs)

    config_dir = setup_config(tmp_path)
    email_provider = FakeEmailProvider()
    dependencies = PipelineDependencies(
        source_factory=source_factory_for({"source-a": (raw_entry(),)}),
        summarizer=FakeSummarizer(),
        email_provider=email_provider,
        email_settings=email_settings(),
        run_repository=FailingCompletionRepository(),  # type: ignore[arg-type]
    )

    result = PipelineRunner(dependencies).run(
        "send", as_of=AS_OF, config_directory=config_dir, environ={}
    )

    assert len(email_provider.messages) == 1
    assert result.delivery is not None and result.delivery.status == "accepted"
    assert result.status == "persistence_failed"
    assert result.status != "sent"
    assert result.exit_code != 0
    assert result.stage_failures[-1].stage == "database_completion"
    stored_run = stored_repository.get_run(result.run_id)
    assert stored_run is not None and stored_run["status"] == "running"
    delivery_key = email_provider.messages[0].idempotency_key
    claim = stored_repository.get_delivery_claim(delivery_key)
    assert claim is not None and claim["outcome"] == "accepted"

    retry_provider = FakeEmailProvider()
    retry_dependencies = PipelineDependencies(
        source_factory=source_factory_for({"source-a": (raw_entry(),)}),
        summarizer=FakeSummarizer(),
        email_provider=retry_provider,
        email_settings=email_settings(),
        run_repository=stored_repository,
    )
    retry = PipelineRunner(retry_dependencies).run(
        "send", as_of=AS_OF, config_directory=config_dir, environ={}
    )
    assert retry.status == "duplicate_send_skipped"
    assert retry_provider.messages == []


def test_complete_send_pipeline_with_multiple_sources_and_duplicate_story(
    tmp_path: Path,
) -> None:
    entries = (raw_entry(),)
    config_dir = setup_config(tmp_path, ("source-a", "source-b"))
    email_provider = FakeEmailProvider()
    summarizer = FakeSummarizer()
    dependencies = PipelineDependencies(
        source_factory=source_factory_for({"source-a": entries, "source-b": entries}),
        summarizer=summarizer,
        email_provider=email_provider,
        email_settings=email_settings(),
    )

    result = PipelineRunner(dependencies).run(
        "send", as_of=AS_OF, config_directory=config_dir, environ={}
    )

    assert result.status == "sent"
    assert result.counts.sources_attempted == 2
    assert result.counts.sources_succeeded == 2
    assert result.counts.raw_entries == 2
    assert result.counts.normalized_articles == 2
    assert result.counts.duplicate_articles == 1
    assert result.counts.stories == 1
    assert result.counts.categorized_stories == 1
    assert result.counts.ranked_stories == 1
    assert result.counts.selected_stories == 1
    assert result.counts.summarized_stories == 1
    assert len(summarizer.calls) == 1
    assert email_provider.messages[0].idempotency_key
    assert result.delivery is not None
    assert result.delivery.provider_message_id == "fake-message-id"
    assert result.newsletter is not None


def test_distinct_entries_from_both_sources_reach_the_newsletter(tmp_path: Path) -> None:
    result = run_preview(
        tmp_path,
        ("source-a", "source-b"),
        {
            "source-a": (
                raw_entry(
                    "Technology company unveils a new processor",
                    url="https://publisher-a.example/processor",
                ),
            ),
            "source-b": (
                raw_entry(
                    "India launches a new space mission",
                    url="https://publisher-b.example/space-mission",
                ),
            ),
        },
    )

    assert result.counts.sources_attempted == 2
    assert result.counts.sources_succeeded == 2
    assert result.counts.raw_entries == 2
    assert result.counts.normalized_articles == 2
    assert result.counts.stories == 2
    assert result.newsletter is not None
    assert "Technology company unveils a new processor" in result.newsletter.plain_text
    assert "India launches a new space mission" in result.newsletter.plain_text


def test_partial_source_failure_continues_and_records_sanitized_error(
    tmp_path: Path,
) -> None:
    repository = SQLitePipelineRunRepository(tmp_path / "history.sqlite3")
    result = run_preview(
        tmp_path,
        ("source-a", "source-b"),
        {
            "source-a": (raw_entry(),),
            "source-b": RuntimeError("sensitive details must not be copied"),
        },
        run_repository=repository,
    )

    assert result.status == "partial"
    assert result.exit_code == 0
    assert result.counts.sources_succeeded == 1
    assert result.counts.sources_failed == 1
    assert result.counts.normalized_articles == 1
    assert result.counts.duplicate_articles == 0
    assert result.counts.stories == 1
    assert result.counts.categorized_stories == 1
    assert result.counts.ranked_stories == 1
    assert result.counts.selected_stories == 1
    assert result.newsletter is not None
    assert result.delivery is not None and result.delivery.status == "preview"
    assert result.source_failures[0].source_id == "source-b"
    assert result.source_failures[0].error_type == "RuntimeError"
    assert "sensitive details" not in str(result.source_failures)
    stored = repository.get_run(result.run_id)
    assert stored is not None
    assert stored["status"] == "partial"
    assert stored["sources_succeeded"] == 1
    assert stored["sources_failed"] == 1
    assert stored["article_count"] == 1
    assert stored["selected_count"] == 1
    assert stored["error"] is None
    warnings = json.loads(stored["warnings_json"])
    assert warnings[0] == {
        "stage": "source_fetch",
        "error_type": "RuntimeError",
        "message": "Source fetch failed (RuntimeError).",
        "source_id": "source-b",
        "category": None,
        "http_status": None,
        "provider_error_type": None,
        "provider_error_code": None,
    }
    assert warnings[1]["stage"] == "delivery"
    assert warnings[1]["message"] == "Preview files written locally; no email was sent."


def test_http_503_source_failure_is_recorded_and_successful_source_continues(
    tmp_path: Path,
) -> None:
    repository = SQLitePipelineRunRepository(tmp_path / "history.sqlite3")
    result = run_preview(
        tmp_path,
        ("source-a", "source-b"),
        {
            "source-a": (raw_entry(),),
            "source-b": SourceFetchError(
                "HTTP 503 fetching source source-b", http_status=503
            ),
        },
        run_repository=repository,
    )

    assert result.status == "partial"
    assert result.counts.sources_succeeded == 1
    assert result.counts.sources_failed == 1
    assert result.counts.normalized_articles == 1
    assert result.source_failures[0].source_id == "source-b"
    assert result.source_failures[0].http_status == 503
    stored = repository.get_run(result.run_id)
    assert stored is not None
    assert stored["status"] == "partial"
    assert '"http_status": 503' in stored["warnings_json"]


def test_partial_failure_with_three_sources_processes_both_successes_and_persists_counts(
    tmp_path: Path,
) -> None:
    repository = SQLitePipelineRunRepository(tmp_path / "history.sqlite3")
    source_ids = ("source-a", "source-b", "source-c")
    observed: list[tuple[str, datetime]] = []
    source_a_entries = tuple(
        raw_entry(
            f"Technology report A {index}",
            url=f"https://news-a.example/technology/{index}",
            published_at=AS_OF - timedelta(minutes=index + 1),
        )
        for index in range(20)
    )
    source_c_entries = tuple(
        raw_entry(
            f"Technology report C {index}",
            url=f"https://news-c.example/technology/{index}",
            published_at=AS_OF - timedelta(hours=index + 1),
        )
        for index in range(15)
    )
    # The C feed contains one duplicate of A, so 35 fetched entries become 34 Articles
    # and 34 distinct Stories after deterministic URL-based deduplication.
    source_c_entries = (*source_c_entries[:-1], raw_entry(
        "A duplicate report from another feed",
        url="https://news-a.example/technology/0",
        published_at=AS_OF - timedelta(minutes=1),
    ))
    config_dir = setup_config(tmp_path, source_ids)
    email_provider = FakeEmailProvider()
    dependencies = PipelineDependencies(
        source_factory=source_factory_for(
            {
                "source-a": source_a_entries,
                "source-b": SourceFetchError("HTTP 503", http_status=503),
                "source-c": source_c_entries,
            },
            observed,
        ),
        email_provider=email_provider,
        run_repository=repository,
    )

    result = PipelineRunner(dependencies).run(
        "preview",
        as_of=AS_OF,
        config_directory=config_dir,
        preview_directory=tmp_path / "preview",
        environ={},
    )

    assert [source_id for source_id, _ in observed] == list(source_ids)
    assert result.status == "partial"
    assert result.counts.sources_attempted == 3
    assert result.counts.sources_succeeded == 2
    assert result.counts.sources_failed == 1
    assert result.counts.raw_entries == 35
    assert result.counts.normalized_articles == 35
    assert result.counts.duplicate_articles == 1
    assert result.counts.stories == 34
    assert result.newsletter is not None
    assert result.delivery is not None and result.delivery.status == "preview"
    assert email_provider.messages == []
    assert result.source_failures[0].source_id == "source-b"
    assert result.source_failures[0].http_status == 503
    stored = repository.get_run(result.run_id)
    assert stored is not None
    assert stored["status"] == "partial"
    assert stored["sources_attempted"] == 3
    assert stored["sources_succeeded"] == 2
    assert stored["sources_failed"] == 1
    assert stored["raw_entries"] == 35
    assert stored["article_count"] == 35
    assert stored["duplicate_count"] == 1
    assert stored["story_count"] == 34


def test_independent_partial_runs_store_only_their_own_source_failures(tmp_path: Path) -> None:
    repository = SQLitePipelineRunRepository(tmp_path / "history.sqlite3")
    config_dir = setup_config(tmp_path, ("source-a", "source-b"))

    first = PipelineRunner(
        PipelineDependencies(
            source_factory=source_factory_for(
                {
                    "source-a": (raw_entry(),),
                    "source-b": SourceFetchError("HTTP 503", http_status=503),
                }
            ),
            run_repository=repository,
        )
    ).run("preview", as_of=AS_OF, config_directory=config_dir, environ={})
    second = PipelineRunner(
        PipelineDependencies(
            source_factory=source_factory_for(
                {
                    "source-a": SourceFetchError("HTTP 502", http_status=502),
                    "source-b": (raw_entry(url="https://news.example/technology/second"),),
                }
            ),
            run_repository=repository,
        )
    ).run("preview", as_of=AS_OF, config_directory=config_dir, environ={})

    assert first.run_id != second.run_id
    assert [failure.source_id for failure in first.source_failures] == ["source-b"]
    assert first.source_failures[0].http_status == 503
    assert [failure.source_id for failure in second.source_failures] == ["source-a"]
    assert second.source_failures[0].http_status == 502
    first_record = repository.get_run(first.run_id)
    second_record = repository.get_run(second.run_id)
    assert first_record is not None and '"source_id": "source-b"' in first_record["warnings_json"]
    assert second_record is not None and '"source_id": "source-a"' in second_record["warnings_json"]


def test_mixed_source_llm_and_database_chaos_run_isolated_safe_and_persisted(
    tmp_path: Path, caplog
) -> None:
    secret = "chaos-fixture-secret-must-not-leak"
    titles = (
        '<script>alert("x")</script> Technology report A 0',
        "Technology report A 1",
        "Technology report A 2",
        "Technology report A 3",
    )
    attempts_by_title: dict[str, int] = {}
    valid_payload = {
        "summary": "A concise summary grounded in the supplied article.",
        "why_it_matters": "It may affect readers following technology news.",
        "uncertainty": [],
    }

    def llm_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompt_payload = json.loads(body["messages"][1]["content"])
        headline = prompt_payload["headline"]
        attempts_by_title[headline] = attempts_by_title.get(headline, 0) + 1
        attempt = attempts_by_title[headline]
        if headline == titles[1] and attempt == 1:
            raise httpx.ReadTimeout(f"timeout {secret}", request=request)
        if headline == titles[2]:
            raise httpx.ReadTimeout(f"timeout {secret}", request=request)
        content = (
            "malformed structured response"
            if headline == titles[3]
            else json.dumps(valid_payload)
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    llm_client = httpx.Client(transport=httpx.MockTransport(llm_handler))
    summarizer = Summarizer(
        OpenAICompatibleProvider(
            LLMSettings(secret, "offline-test-model", "https://llm.example.test/v1"),
            client=llm_client,
        ),
        max_attempts=2,
    )

    source_a_entries = [
        raw_entry(
            title,
            url=(
                'https://news-a.example/technology/0?next=%22%20onmouseover%3D%22alert(1)&x=1'
                if index == 0
                else f"https://news-a.example/technology/{index}"
            ),
            published_at=AS_OF - timedelta(hours=index + 1),
            description=(
                '<script>alert("fallback")</script> Original reporting detail.'
                if index == 3
                else f"Source description for A {index}."
            ),
        )
        for index, title in enumerate(
            (*titles, *(f"Technology report A {index}" for index in range(4, 20)))
        )
    ]

    source_c_entries = [
        raw_entry(
            f"Technology report C {index}",
            url=f"https://news-c.example/technology/{index}",
            published_at=AS_OF - timedelta(hours=24 + index),
            description="" if index == 1 else f"Source description for C {index}.",
        )
        for index in range(12)
    ]
    malformed_category = dict(source_c_entries[0].fields)
    malformed_category["categories"] = "not-a-list"
    source_c_entries[0] = RawProviderEntry("rss", malformed_category)
    bad_title_fields = dict(raw_entry("", url="https://news-c.example/missing-title").fields)
    bad_title_fields["title"] = None
    invalid_date_fields = dict(
        raw_entry(
            "Technology report C invalid date",
            url="https://news-c.example/invalid-date",
        ).fields
    )
    invalid_date_fields["pubDate"] = "not a valid publication date"
    source_c_entries.extend(
        (
            RawProviderEntry("rss", bad_title_fields),
            RawProviderEntry("rss", invalid_date_fields),
            raw_entry(
                "Duplicate of the hostile headline from source A",
                url=source_a_entries[0].fields["link"],
                published_at=AS_OF - timedelta(hours=1),
            ),
        )
    )
    assert len(source_c_entries) == 15

    config_dir = setup_config(tmp_path, ("source-a", "source-b", "source-c"))
    selection_path = config_dir / "selection.yaml"
    selection_path.write_text("max_total_stories: 4\nmax_per_category: 4\n", encoding="utf-8")
    repository = SQLitePipelineRunRepository(tmp_path / "chaos-history.sqlite3")
    email_provider = FakeEmailProvider()
    dependencies = PipelineDependencies(
        source_factory=source_factory_for(
            {
                "source-a": tuple(source_a_entries),
                "source-b": SourceFetchError("HTTP 503", http_status=503),
                "source-c": tuple(source_c_entries),
            }
        ),
        summarizer=summarizer,
        email_provider=email_provider,
        run_repository=repository,
    )

    caplog.set_level(logging.INFO, logger="personal_news_agent.pipeline")
    try:
        result = PipelineRunner(dependencies).run(
            "preview",
            as_of=AS_OF,
            config_directory=config_dir,
            preview_directory=tmp_path / "preview",
            ai_in_preview=True,
            environ={},
        )
    finally:
        llm_client.close()

    assert result.status == "partial"
    assert result.counts.sources_attempted == 3
    assert result.counts.sources_succeeded == 2
    assert result.counts.sources_failed == 1
    assert result.counts.raw_entries == 35
    assert result.counts.normalized_articles == 34
    assert result.counts.rejected_entries == 1
    assert result.counts.normalization_issues == 3
    assert result.counts.duplicate_articles == 1
    assert result.counts.stories == 33
    assert result.counts.categorized_stories == 33
    assert result.counts.ranked_stories == 33
    assert result.counts.selected_stories == 4
    assert result.counts.summarized_stories == 4
    assert result.counts.fallback_stories == 2
    assert result.counts.summary_failures == 2
    assert result.counts.omitted_stories == 0
    assert attempts_by_title == {
        titles[0]: 1,
        titles[1]: 2,
        titles[2]: 2,
        titles[3]: 1,
    }
    assert result.source_failures[0].http_status == 503
    assert result.newsletter is not None
    # Each selected Technology story appears in Home and its Technology view.
    assert result.newsletter.html.count("AI-generated summary") == 4
    assert result.newsletter.html.count("Source description (fallback)") == 4
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in result.newsletter.html
    assert "<script>alert(\"x\")</script>" not in result.newsletter.html
    assert (
        'href="https://news-a.example/technology/0?next=%22%20onmouseover%3D%22alert(1)&amp;x=1"'
        in result.newsletter.html
    )
    assert "%22%20onmouseover%3D%22alert(1)&amp;x=1" in result.newsletter.html
    assert result.delivery is not None and result.delivery.status == "preview"
    assert result.delivery.preview_html_path is not None
    assert result.delivery.preview_html_path.is_file()
    assert result.delivery.preview_text_path is not None
    assert result.delivery.preview_text_path.is_file()
    assert email_provider.messages == []
    stored = repository.get_run(result.run_id)
    assert stored is not None
    assert stored["status"] == "partial"
    assert stored["raw_entries"] == 35
    assert stored["article_count"] == 34
    assert stored["duplicate_count"] == 1
    assert stored["story_count"] == 33
    assert stored["selected_count"] == 4
    assert stored["fallback_count"] == 2
    assert stored["delivery_outcome"] == "preview"
    assert secret not in str(result.source_failures)
    assert secret not in str(result.stage_failures)
    assert secret not in " ".join(record.getMessage() for record in caplog.records)
    assert secret not in result.newsletter.html
    assert secret not in result.newsletter.plain_text


def test_all_http_503_source_failures_do_not_render_or_attempt_delivery(
    tmp_path: Path,
) -> None:
    config_dir = setup_config(tmp_path, ("source-a", "source-b"))
    email_provider = FakeEmailProvider()
    repository = SQLitePipelineRunRepository(tmp_path / "history.sqlite3")
    dependencies = PipelineDependencies(
        source_factory=source_factory_for(
            {
                "source-a": SourceFetchError("HTTP 503 fetching source source-a", http_status=503),
                "source-b": SourceFetchError("HTTP 503 fetching source source-b", http_status=503),
            }
        ),
        email_provider=email_provider,
        email_settings=email_settings(),
        run_repository=repository,
    )

    result = PipelineRunner(dependencies).run(
        "send", as_of=AS_OF, config_directory=config_dir, environ={}
    )

    assert result.status == "all_sources_failed"
    assert result.exit_code == 1
    assert result.error == "All enabled sources failed; no newsletter was rendered or sent."
    assert result.newsletter is None
    assert result.delivery is None
    assert result.counts.sources_attempted == 2
    assert result.counts.sources_succeeded == 0
    assert result.counts.sources_failed == 2
    assert {failure.source_id for failure in result.source_failures} == {"source-a", "source-b"}
    assert all(failure.http_status == 503 for failure in result.source_failures)
    assert email_provider.messages == []
    stored = repository.get_run(result.run_id)
    assert stored is not None
    assert stored["status"] == "all_sources_failed"
    assert stored["sources_attempted"] == 2
    assert stored["sources_succeeded"] == 0
    assert stored["sources_failed"] == 2
    assert stored["error"] == result.error
    warnings = json.loads(stored["warnings_json"])
    assert {warning["source_id"] for warning in warnings} == {"source-a", "source-b"}
    assert all(warning["http_status"] == 503 for warning in warnings)
    assert stored["delivery_outcome"] is None


def test_all_sources_fail_stops_before_render_or_delivery(tmp_path: Path) -> None:
    email_provider = FakeEmailProvider()
    result = run_preview(
        tmp_path,
        ("source-a", "source-b"),
        {"source-a": RuntimeError("offline"), "source-b": TimeoutError("offline")},
        email_provider=email_provider,
    )

    assert result.status == "all_sources_failed"
    assert result.exit_code == 1
    assert result.newsletter is None
    assert result.delivery is None
    assert len(result.source_failures) == 2
    assert not email_provider.messages


def test_malformed_entry_is_rejected_while_pipeline_renders_empty_preview(
    tmp_path: Path,
) -> None:
    result = run_preview(
        tmp_path,
        ("source-a",),
        {"source-a": (raw_entry(title="Valid-looking but no URL", url=""),)},
    )

    assert result.status == "preview"
    assert result.counts.raw_entries == 1
    assert result.counts.normalized_articles == 0
    assert result.counts.rejected_entries == 1
    assert result.newsletter is not None
    assert "No eligible stories" in result.newsletter.plain_text
    assert result.delivery is not None and result.delivery.status == "preview"


def test_invalid_pubdate_among_36_entries_does_not_interrupt_pipeline(
    tmp_path: Path,
) -> None:
    entries = []
    for index in range(36):
        entry = raw_entry(
            f"Technology processor update {index}",
            url=f"https://news.example/technology/story-{index}",
            published_at=AS_OF - timedelta(hours=index + 1),
        )
        if index == 0:
            fields = dict(entry.fields)
            fields["pubDate"] = "not a valid date"
            entry = RawProviderEntry(entry.format, fields)
        entries.append(entry)

    result = run_preview(tmp_path, ("source-a",), {"source-a": tuple(entries)})

    assert result.status == "preview"
    assert result.counts.raw_entries == 36
    # All 36 entries form valid Articles; one Article has unknown optional publication time.
    assert result.counts.normalized_articles == 36
    assert result.counts.rejected_entries == 0
    assert result.counts.normalization_issues == 1
    assert result.counts.duplicate_articles == 0
    assert result.counts.stories == 36
    assert result.counts.categorized_stories == 36
    assert result.counts.ranked_stories == 36
    assert result.counts.selected_stories > 0
    assert result.newsletter is not None
    assert result.newsletter.included_story_ids
    assert result.delivery is not None and result.delivery.status == "preview"


def test_unclassified_stories_are_ranked_but_not_selected_or_sent(tmp_path: Path) -> None:
    email_provider = FakeEmailProvider()
    config_dir = setup_config(tmp_path)
    dependencies = PipelineDependencies(
        source_factory=source_factory_for(
            {"source-a": (raw_entry("Local road closure update", description=None),)}
        ),
        summarizer=FakeSummarizer(),
        email_provider=email_provider,
        email_settings=email_settings(),
    )

    result = PipelineRunner(dependencies).run(
        "send", as_of=AS_OF, config_directory=config_dir, environ={}
    )

    assert result.status == "no_eligible_stories"
    assert result.counts.ranked_stories == 1
    assert result.counts.categorized_stories == 0
    assert result.counts.selected_stories == 0
    assert not email_provider.messages
    assert result.newsletter is not None
    assert "No eligible stories" in result.newsletter.html


def test_preview_uses_source_fallback_and_never_calls_llm_or_email(
    tmp_path: Path,
) -> None:
    summarizer = FakeSummarizer()
    email_provider = FakeEmailProvider()
    result = run_preview(
        tmp_path,
        ("source-a",),
        {"source-a": (raw_entry(),)},
        summarizer=summarizer,
        email_provider=email_provider,
        ai_in_preview=False,
    )

    assert result.status == "preview"
    assert result.counts.fallback_stories == 1
    assert summarizer.calls == []
    assert email_provider.messages == []
    assert result.delivery is not None
    assert result.delivery.preview_html_path is not None
    assert result.delivery.preview_html_path.exists()
    assert result.delivery.preview_text_path is not None
    assert result.delivery.preview_text_path.exists()


def test_preview_can_explicitly_use_injected_llm_but_never_email(tmp_path: Path) -> None:
    summarizer = FakeSummarizer()
    email_provider = FakeEmailProvider()
    result = run_preview(
        tmp_path,
        ("source-a",),
        {"source-a": (raw_entry(),)},
        summarizer=summarizer,
        email_provider=email_provider,
        ai_in_preview=True,
    )

    assert result.status == "preview"
    assert len(summarizer.calls) == 1
    assert not email_provider.messages
    assert result.counts.fallback_stories == 0


def test_summarizer_failure_uses_source_fallback_and_is_counted(tmp_path: Path) -> None:
    summarizer = FakeSummarizer(fail=True)
    result = run_preview(
        tmp_path,
        ("source-a",),
        {"source-a": (raw_entry(),)},
        summarizer=summarizer,
        ai_in_preview=True,
    )

    assert result.counts.summary_failures == 1
    assert result.counts.fallback_stories == 1
    assert result.newsletter is not None
    assert "Source description (fallback)" in result.newsletter.html


def test_mixed_llm_outcomes_fallback_per_story_and_continue_pipeline(
    tmp_path: Path, caplog
) -> None:
    secret = "test-secret-must-not-leak"
    attempts_by_headline: dict[str, int] = {}
    valid_response = {
        "summary": "A concise summary grounded in the supplied source.",
        "why_it_matters": "It may affect developers choosing new tools.",
        "uncertainty": [],
    }
    story_two = "Technology update two reports a model release"
    story_three = "Technology update three reports a model release"
    story_four = "Technology update four reports a model release"
    story_five = "Technology update five reports a model release"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompt_data = json.loads(body["messages"][1]["content"])
        headline = prompt_data["headline"]
        attempts_by_headline[headline] = attempts_by_headline.get(headline, 0) + 1
        attempt = attempts_by_headline[headline]
        if headline == story_two and attempt == 1:
            raise httpx.ReadTimeout(f"timeout {secret}", request=request)
        if headline == story_three:
            raise httpx.ReadTimeout(f"timeout {secret}", request=request)
        content = (
            "not valid structured JSON"
            if headline == story_four
            else json.dumps(valid_response)
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        LLMSettings(secret, "mocked-openrouter-model", "https://openrouter.example/api/v1"),
        client=client,
    )
    summarizer = Summarizer(provider, max_attempts=2)
    titles = (
        "Technology update one reports a model release",
        story_two,
        story_three,
        story_four,
        story_five,
    )
    entries = tuple(
        raw_entry(
            title,
            url=f"https://news.example/technology/story-{index}",
            description=(
                None if title == story_three else f"Source description for story {index}."
            ),
        )
        for index, title in enumerate(titles, 1)
    )
    caplog.set_level(logging.INFO, logger="personal_news_agent.pipeline")
    try:
        result = run_preview(
            tmp_path,
            ("source-a",),
            {"source-a": entries},
            summarizer=summarizer,
            ai_in_preview=True,
        )
    finally:
        client.close()

    assert result.status == "preview"
    assert result.counts.selected_stories == 5
    assert result.counts.summarized_stories == 4
    assert result.counts.fallback_stories == 1
    assert result.counts.summary_failures == 2
    assert result.counts.omitted_stories == 1
    assert attempts_by_headline == {
        titles[0]: 1,
        story_two: 2,
        story_three: 2,
        story_four: 1,
        story_five: 1,
    }
    assert result.newsletter is not None
    # The HTML includes Home and the matching category view; text stays one copy.
    assert result.newsletter.html.count("AI-generated summary") == 6
    assert result.newsletter.plain_text.count("AI-generated summary") == 3
    assert result.newsletter.html.count("Source description (fallback)") == 2
    assert result.newsletter.plain_text.count("Source description (fallback)") == 1
    assert story_three not in result.newsletter.html
    assert story_five in result.newsletter.html
    assert "Source description for story 4." in result.newsletter.plain_text
    assert secret not in str(result.stage_failures)
    assert secret not in " ".join(record.getMessage() for record in caplog.records)
    assert secret not in result.newsletter.html
    assert secret not in result.newsletter.plain_text
    assert result.stage_failures
    events = [json.loads(record.message) for record in caplog.records]
    enrichment = next(event for event in events if event["event"] == "llm.enrichment.completed")
    assert enrichment["model"] == "mocked-openrouter-model"
    assert enrichment["selected_stories"] == 5
    assert enrichment["attempted_stories"] == 5
    assert enrichment["request_attempts"] == 7
    assert enrichment["retry_count"] == 2
    assert enrichment["generated_stories"] == 3
    assert enrichment["fallback_stories"] == 1
    assert enrichment["failed_stories"] == 1
    assert enrichment["failure_categories"] == {"invalid_response": 1, "timeout": 1}
    captured_logs = " ".join(record.getMessage() for record in caplog.records)
    assert secret not in captured_logs
    assert story_two not in captured_logs
    assert "not valid structured JSON" not in captured_logs
    assert valid_response["summary"] not in captured_logs


def test_sanitized_provider_diagnostic_is_preserved_in_pipeline_result(tmp_path: Path) -> None:
    result = run_preview(
        tmp_path,
        ("source-a",),
        {"source-a": (raw_entry(),)},
        summarizer=DiagnosticSummarizer(),
        ai_in_preview=True,
    )

    assert result.counts.fallback_stories == 1
    assert len(result.stage_failures) == 1
    failure = result.stage_failures[0]
    assert failure.error_type == "TransientProviderError"
    assert failure.category == "rate_limit"
    assert failure.http_status == 429
    assert failure.provider_error_type == "rate_limit_error"
    assert failure.provider_error_code == "rate_limit_exceeded"
    assert failure.message == "LLM provider rate limit was reached."


def test_llm_rate_limit_falls_back_and_email_still_succeeds(tmp_path: Path) -> None:
    config_dir = setup_config(tmp_path)
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                429,
                json={
                    "error": {
                        "type": "rate_limit_error",
                        "code": "rate_limit_exceeded",
                        "message": "Private provider response text is not logged.",
                    }
                },
            )
        )
    )
    summarizer = Summarizer(
        OpenAICompatibleProvider(
            LLMSettings("test-api-key", "test-model", "https://llm.example.test/v1"),
            client=client,
        ),
        max_attempts=1,
    )
    email_provider = FakeEmailProvider()
    dependencies = PipelineDependencies(
        source_factory=source_factory_for({"source-a": (raw_entry(),)}),
        summarizer=summarizer,
        email_provider=email_provider,
        email_settings=email_settings(),
    )
    try:
        result = PipelineRunner(dependencies).run(
            "send",
            as_of=AS_OF,
            config_directory=config_dir,
            environ={},
        )
    finally:
        client.close()

    assert result.status == "partial"
    assert result.counts.fallback_stories == 1
    assert result.newsletter is not None
    assert "Source description (fallback)" in result.newsletter.html
    assert "Source description (fallback)" in result.newsletter.plain_text
    assert len(email_provider.messages) == 1
    assert result.delivery is not None and result.delivery.status == "accepted"
    failure = next(item for item in result.stage_failures if item.stage == "summarization")
    assert failure.category == "rate_limit"
    assert failure.http_status == 429
    assert failure.attempt_count == 1
    assert max(0, failure.attempt_count - 1) == 0
    assert "Private provider response text" not in failure.message


def test_send_mode_calls_email_provider_and_reports_delivery_failure(
    tmp_path: Path,
) -> None:
    config_dir = setup_config(tmp_path)
    repository = SQLitePipelineRunRepository(tmp_path / "history.sqlite3")
    email_provider = FakeEmailProvider(fail=True)
    dependencies = PipelineDependencies(
        source_factory=source_factory_for({"source-a": (raw_entry(),)}),
        summarizer=FakeSummarizer(),
        email_provider=email_provider,
        email_settings=email_settings(),
        run_repository=repository,
    )

    result = PipelineRunner(dependencies).run(
        "send", as_of=AS_OF, config_directory=config_dir, environ={}
    )

    assert result.status == "delivery_failed"
    assert len(email_provider.messages) == 1
    assert result.delivery is not None
    assert result.delivery.status == "rejected"
    assert result.delivery.retryable is False
    claim = repository.get_delivery_claim(email_provider.messages[0].idempotency_key)
    assert claim is not None and claim["outcome"] == "rejected"

    retry = PipelineRunner(dependencies).run(
        "send", as_of=AS_OF, config_directory=config_dir, environ={}
    )
    assert retry.status == "duplicate_send_skipped"
    assert retry.delivery is not None and retry.delivery.status == "duplicate_skipped"
    assert len(email_provider.messages) == 1


def test_missing_smtp_credential_disables_email_and_generates_newsletter(
    monkeypatch, tmp_path: Path
) -> None:
    config_dir = setup_config(tmp_path)
    observed: list[tuple[str, datetime]] = []
    dependencies = PipelineDependencies(
        source_factory=source_factory_for({"source-a": (raw_entry(),)}, observed),
        run_repository=SQLitePipelineRunRepository(tmp_path / "runs.sqlite3"),
    )
    monkeypatch.setattr(
        "app.pipeline.runner.smtp_credential_configured", lambda _username, **_kwargs: False
    )
    environment = {
        "EMAIL_PROVIDER": "smtp",
        "SMTP_USERNAME": "sender@example.test",
        "EMAIL_FROM": "sender@example.test",
        "NEWSLETTER_RECIPIENT": "reader@example.test",
        "LLM_MODEL": "test/model",
        "LLM_BASE_URL": "https://llm.example.test/v1",
    }

    result = PipelineRunner(dependencies).run(
        "send", as_of=AS_OF, config_directory=config_dir, environ=environment
    )

    assert result.status == "preview"
    assert result.delivery is not None
    assert result.delivery.provider == "preview"
    assert result.delivery.status == "preview"
    assert result.newsletter is not None
    assert result.delivery.preview_html_path.is_file()
    assert result.delivery.preview_text_path.is_file()
    assert result.counts.fallback_stories == 1
    assert any(
        failure.stage == "llm_configuration"
        and failure.category == "missing_configuration"
        for failure in result.stage_failures
    )
    assert len(observed) == 1
    stored = dependencies.run_repository.get_run(result.run_id)
    assert stored is not None
    assert stored["status"] == "preview"
    assert stored["delivery_outcome"] == "preview"
    assert result.exit_code == 0


def test_send_mode_skips_duplicate_daily_delivery_from_sqlite_ledger(tmp_path: Path) -> None:
    config_dir = setup_config(tmp_path)
    repository = SQLitePipelineRunRepository(tmp_path / "history.sqlite3")
    provider = FakeEmailProvider()
    summarizer = FakeSummarizer()
    dependencies = PipelineDependencies(
        source_factory=source_factory_for({"source-a": (raw_entry(),)}),
        summarizer=summarizer,
        email_provider=provider,
        email_settings=email_settings(),
        run_repository=repository,
    )
    runner = PipelineRunner(dependencies)

    first = runner.run("send", as_of=AS_OF, config_directory=config_dir, environ={})
    second = runner.run("send", as_of=AS_OF, config_directory=config_dir, environ={})
    delivery_key = newsletter_idempotency_key(email_settings().recipient, AS_OF.date())
    assert repository.reset_delivery_claim(delivery_key)
    retried = runner.run("send", as_of=AS_OF, config_directory=config_dir, environ={})
    next_day = runner.run(
        "send",
        as_of=AS_OF + timedelta(days=1),
        config_directory=config_dir,
        environ={},
    )

    assert first.status == "sent"
    first_claim = repository.get_delivery_claim(provider.messages[0].idempotency_key)
    assert first_claim is not None and first_claim["outcome"] == "accepted"
    assert second.status == "duplicate_send_skipped"
    assert second.delivery is not None
    assert second.delivery.status == "duplicate_skipped"
    assert retried.status == "sent"
    assert len(provider.messages) == 3
    assert second.counts.sources_attempted == 0
    assert next_day.status == "sent"
    assert len(provider.messages) == 3
    assert provider.messages[0].idempotency_key != provider.messages[2].idempotency_key
    assert len(summarizer.calls) == 3


def test_ambiguous_delivery_is_persisted_and_blocks_same_day_retry(tmp_path: Path) -> None:
    config_dir = setup_config(tmp_path)
    repository = SQLitePipelineRunRepository(tmp_path / "history.sqlite3")

    class AmbiguousProvider:
        provider_name = "ambiguous-test-provider"

        def __init__(self) -> None:
            self.calls = 0

        def send(self, _message: EmailMessage) -> ProviderReceipt:
            self.calls += 1
            raise AmbiguousEmailError("provider may have accepted before network timeout")

    provider = AmbiguousProvider()
    dependencies = PipelineDependencies(
        source_factory=source_factory_for({"source-a": (raw_entry(),)}),
        summarizer=FakeSummarizer(),
        email_provider=provider,
        email_settings=email_settings(),
        run_repository=repository,
    )
    runner = PipelineRunner(dependencies)

    first = runner.run("send", as_of=AS_OF, config_directory=config_dir, environ={})
    second = runner.run("send", as_of=AS_OF, config_directory=config_dir, environ={})

    assert first.status == "delivery_unknown"
    assert first.delivery is not None and first.delivery.status == "unknown"
    claim = repository.get_delivery_claim(
        newsletter_key := newsletter_idempotency_key(email_settings().recipient, AS_OF.date())
    )
    assert claim is not None and claim["outcome"] == "unknown"
    assert second.status == "duplicate_send_skipped"
    assert second.delivery is not None and second.delivery.status == "duplicate_skipped"
    assert provider.calls == 1
    assert repository.delivery_claim_exists(newsletter_key)


def test_deterministic_as_of_is_used_for_sources_scoring_and_rendering(
    tmp_path: Path,
) -> None:
    config_dir = setup_config(tmp_path)
    observed: list[tuple[str, datetime]] = []
    dependencies = PipelineDependencies(
        source_factory=source_factory_for({"source-a": (raw_entry(),)}, observed),
        summarizer=FakeSummarizer(),
    )

    result = PipelineRunner(dependencies).run(
        "preview",
        as_of=AS_OF,
        config_directory=config_dir,
        preview_directory=tmp_path / "preview",
        environ={},
    )

    assert observed == [("source-a", AS_OF)]
    assert result.as_of == AS_OF
    assert result.newsletter is not None
    assert "30 Sep 2026" in result.newsletter.html


def test_configuration_failure_returns_structured_result(tmp_path: Path) -> None:
    result = PipelineRunner().run(
        "preview",
        as_of=AS_OF,
        config_directory=tmp_path / "missing-config",
        preview_directory=tmp_path / "preview",
        environ={},
    )

    assert result.status == "configuration_failed"
    assert result.counts.sources_attempted == 0
    assert result.stage_failures[0].stage == "configuration"
    assert result.exit_code == 1


def test_empty_enabled_source_list_is_reported_without_fetching(tmp_path: Path) -> None:
    config_dir = setup_config(tmp_path, ("source-a",), enabled=False)
    result = PipelineRunner().run(
        "preview",
        as_of=AS_OF,
        config_directory=config_dir,
        preview_directory=tmp_path / "preview",
        environ={},
    )

    assert result.status == "no_sources"
    assert result.counts.sources_attempted == 0
