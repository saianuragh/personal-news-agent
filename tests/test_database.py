"""Offline tests for SQLite pipeline run persistence."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from app.database.sqlite_runs import SQLitePipelineRunRepository
from app.pipeline.runner import PipelineCounts, PipelineRunResult, StageFailure

NOW = datetime(2026, 10, 1, 5, tzinfo=UTC)


def make_result(
    run_id=None,
    *,
    status: str = "preview",
    stage_failures: tuple[StageFailure, ...] = (),
    error: str | None = None,
) -> PipelineRunResult:
    return PipelineRunResult(
        run_id or uuid4(),
        "preview",
        NOW,
        status,  # type: ignore[arg-type]
        PipelineCounts(
            sources_attempted=2,
            sources_succeeded=1,
            sources_failed=1,
            raw_entries=10,
            normalized_articles=8,
            duplicate_articles=1,
            stories=7,
            categorized_stories=6,
            ranked_stories=6,
            selected_stories=3,
            summarized_stories=2,
            fallback_stories=1,
            summary_failures=1,
            omitted_stories=1,
        ),
        source_failures=(StageFailure("source_fetch", "Timeout", "Feed unavailable."),),
        stage_failures=stage_failures,
        error=error,
    )


def test_database_initialization_is_repeatable_and_creates_schema(tmp_path) -> None:
    path = tmp_path / "nested" / "runs.sqlite3"
    repository = SQLitePipelineRunRepository(path)

    repository.initialize()
    repository.initialize()

    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(pipeline_runs)")}
    assert {
        "run_id",
        "started_at",
        "completed_at",
        "status",
        "as_of",
        "article_count",
        "delivery_outcome",
        "warnings_json",
    } <= columns
    with sqlite3.connect(path) as connection:
        delivery_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(newsletter_deliveries)")
        }
    assert {"delivery_key", "run_id", "claimed_at", "outcome"} <= delivery_columns


def test_daily_delivery_claim_is_unique_and_records_only_safe_outcome(tmp_path) -> None:
    repository = SQLitePipelineRunRepository(tmp_path / "runs.sqlite3")
    first_run = uuid4()
    second_run = uuid4()
    delivery_key = "personal-newsletter/2026-10-01/recipient-hash"

    assert repository.claim_delivery(delivery_key, run_id=first_run, claimed_at=NOW)
    assert repository.delivery_claim_exists(delivery_key)
    assert not repository.claim_delivery(delivery_key, run_id=second_run, claimed_at=NOW)
    repository.complete_delivery_claim(
        delivery_key, outcome="accepted", provider_message_id="message-id"
    )

    claim = repository.get_delivery_claim(delivery_key)
    assert claim["run_id"] == str(first_run)
    assert claim["outcome"] == "accepted"
    assert claim["provider_message_id"] == "message-id"


def test_concurrent_delivery_claim_race_allows_only_one_winner(tmp_path) -> None:
    repository = SQLitePipelineRunRepository(tmp_path / "runs.sqlite3")
    repository.initialize()
    barrier = Barrier(2)
    delivery_key = "personal-newsletter/2026-10-02/concurrent-recipient"

    def claim(run_id):
        barrier.wait(timeout=5)
        return repository.claim_delivery(delivery_key, run_id=run_id, claimed_at=NOW)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(claim, uuid4()) for _ in range(2)]
        outcomes = [future.result(timeout=10) for future in futures]

    assert sorted(outcomes) == [False, True]
    assert repository.delivery_claim_exists(delivery_key)


def test_distinct_delivery_keys_can_each_be_claimed(tmp_path) -> None:
    repository = SQLitePipelineRunRepository(tmp_path / "runs.sqlite3")

    first_key = "personal-newsletter/2026-10-02/recipient"
    next_day_key = "personal-newsletter/2026-10-03/recipient"
    assert repository.claim_delivery(first_key, run_id=uuid4(), claimed_at=NOW)
    assert repository.claim_delivery(
        next_day_key, run_id=uuid4(), claimed_at=NOW + timedelta(days=1)
    )


def test_reset_delivery_claim_removes_only_the_exact_key(tmp_path) -> None:
    repository = SQLitePipelineRunRepository(tmp_path / "runs.sqlite3")
    run_id = uuid4()
    repository.create_run(run_id, started_at=NOW, as_of=NOW, mode="send")
    first = "personal-newsletter/2026-10-02/recipient-a"
    other_recipient = "personal-newsletter/2026-10-02/recipient-b"
    other_date = "personal-newsletter/2026-10-03/recipient-a"
    for key in (first, other_recipient, other_date):
        assert repository.claim_delivery(key, run_id=uuid4(), claimed_at=NOW)

    assert repository.reset_delivery_claim(first)
    assert not repository.reset_delivery_claim(first)
    assert not repository.delivery_claim_exists(first)
    assert repository.delivery_claim_exists(other_recipient)
    assert repository.delivery_claim_exists(other_date)
    assert repository.get_run(run_id) is not None


def test_reset_delivery_claim_surfaces_sqlite_persistence_failure(monkeypatch, tmp_path) -> None:
    repository = SQLitePipelineRunRepository(tmp_path / "runs.sqlite3")
    repository.initialize()

    def fail_connect(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("app.database.sqlite_runs.sqlite3.connect", fail_connect)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        repository.reset_delivery_claim("delivery-key")


def test_daily_delivery_claim_rejects_invalid_outcome(tmp_path) -> None:
    repository = SQLitePipelineRunRepository(tmp_path / "runs.sqlite3")
    key = "delivery-key"
    repository.claim_delivery(key, run_id=uuid4(), claimed_at=NOW)
    with pytest.raises(ValueError, match="outcome"):
        repository.complete_delivery_claim(key, outcome="retry-anyway")


def test_create_and_complete_run_persists_counts_status_and_delivery(tmp_path) -> None:
    repository = SQLitePipelineRunRepository(tmp_path / "runs.sqlite3")
    run_id = uuid4()
    started = NOW - timedelta(minutes=1)
    repository.create_run(run_id, started_at=started, as_of=NOW, mode="preview")
    assert repository.get_run(run_id)["status"] == "running"

    result = make_result(run_id)
    repository.complete_run(result, started_at=started, completed_at=NOW)
    row = repository.get_run(run_id)

    assert row["status"] == "preview"
    assert row["started_at"] == started.isoformat()
    assert row["completed_at"] == NOW.isoformat()
    assert row["as_of"] == NOW.isoformat()
    assert row["sources_attempted"] == 2
    assert row["sources_succeeded"] == 1
    assert row["sources_failed"] == 1
    assert row["raw_entries"] == 10
    assert row["article_count"] == 8
    assert row["duplicate_count"] == 1
    assert row["story_count"] == 7
    assert row["categorized_count"] == 6
    assert row["ranked_count"] == 6
    assert row["selected_count"] == 3
    assert row["summarized_count"] == 2
    assert row["fallback_count"] == 1
    assert row["summary_failure_count"] == 1
    assert row["omitted_count"] == 1


def test_failed_run_and_sanitized_warnings_are_persisted_without_secrets(tmp_path) -> None:
    repository = SQLitePipelineRunRepository(tmp_path / "runs.sqlite3")
    run_id = uuid4()
    started = NOW - timedelta(seconds=5)
    repository.create_run(run_id, started_at=started, as_of=NOW, mode="preview")
    secret = "sk-proj-test-secret-abcdef012345"
    result = make_result(
        run_id,
        status="pipeline_failed",
        stage_failures=(
            StageFailure(
                "llm",
                "ProviderError",
                f"API key={secret}; Authorization: Bearer {secret}; provider said 'no'.",
            ),
        ),
        error=f"Request rejected with token={secret}",
    )

    repository.complete_run(result, started_at=started, completed_at=NOW)
    row = repository.get_run(run_id)
    stored_text = row["warnings_json"] + row["error"]

    assert row["status"] == "pipeline_failed"
    assert secret not in stored_text
    assert "Authorization=[REDACTED]" in stored_text
    assert secret not in stored_text
    assert "API key=[REDACTED]" in stored_text
    assert "token=[REDACTED]" in stored_text
    assert "provider said 'no'" in stored_text
    assert "authorization" not in row["warnings_json"].lower() or "[REDACTED]" in stored_text
    assert "api_key" not in row["warnings_json"]


def test_multiple_runs_are_retained(tmp_path) -> None:
    repository = SQLitePipelineRunRepository(tmp_path / "runs.sqlite3")
    run_ids = (uuid4(), uuid4())
    for offset, run_id in enumerate(run_ids):
        started = NOW + timedelta(minutes=offset)
        repository.create_run(run_id, started_at=started, as_of=NOW, mode="preview")
        repository.complete_run(
            make_result(run_id), started_at=started, completed_at=started + timedelta(seconds=1)
        )

    rows = repository.list_runs()
    assert len(rows) == 2
    assert {row["run_id"] for row in rows} == {str(run_id) for run_id in run_ids}


def test_run_history_is_newest_first_and_has_a_bounded_limit(tmp_path) -> None:
    repository = SQLitePipelineRunRepository(tmp_path / "runs.sqlite3")
    run_ids = (uuid4(), uuid4(), uuid4())
    for offset, run_id in enumerate(run_ids):
        started = NOW + timedelta(minutes=offset)
        repository.create_run(run_id, started_at=started, as_of=NOW, mode="preview")
        repository.complete_run(
            make_result(run_id), started_at=started, completed_at=started + timedelta(seconds=1)
        )

    recent = repository.list_runs(limit=2)

    assert [row["run_id"] for row in recent] == [str(run_ids[2]), str(run_ids[1])]
    with pytest.raises(ValueError, match="limit"):
        repository.list_runs(limit=0)
    with pytest.raises(ValueError, match="limit"):
        repository.list_runs(limit=True)


def test_parameterized_update_handles_sql_like_diagnostic_text(tmp_path) -> None:
    repository = SQLitePipelineRunRepository(tmp_path / "runs.sqlite3")
    run_id = uuid4()
    repository.create_run(run_id, started_at=NOW, as_of=NOW, mode="preview")
    message = "quote'; DROP TABLE pipeline_runs; --"

    repository.complete_run(
        make_result(run_id, error=message), started_at=NOW, completed_at=NOW
    )

    assert repository.get_run(run_id)["error"] == message
    assert len(repository.list_runs()) == 1


def test_update_requires_an_existing_started_run(tmp_path) -> None:
    repository = SQLitePipelineRunRepository(tmp_path / "runs.sqlite3")

    with pytest.raises(sqlite3.IntegrityError, match="was not created"):
        repository.complete_run(make_result(), started_at=NOW, completed_at=NOW)
