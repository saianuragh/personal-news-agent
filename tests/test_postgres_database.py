"""Mock-only tests for PostgreSQL persistence and configuration selection."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from app.cli import main
from app.database.factory import create_run_repository
from app.database.postgres_runs import (
    PostgresPipelineRunRepository,
    PostgresRepositoryError,
)
from app.database.sqlite_runs import SQLitePipelineRunRepository
from app.pipeline.runner import PipelineCounts, PipelineRunResult, StageFailure

AS_OF = datetime(2026, 10, 1, 5, tzinfo=UTC)
SECRET_URL = "postgresql://agent:db-password@example.invalid/news"


class FakeCursor:
    def __init__(self, *, rowcount: int = 1) -> None:
        self.rowcount = rowcount
        self.queries: list[tuple[str, tuple[object, ...] | None]] = []
        self.closed = False
        self.one = None
        self.many: list[dict[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        self.closed = True

    def execute(self, query: str, params=None) -> None:
        self.queries.append((query, params))

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self.fake_cursor = cursor
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        self.closed = True

    def cursor(self):
        return self.fake_cursor


def make_result(run_id=None, *, status="preview", error=None) -> PipelineRunResult:
    return PipelineRunResult(
        run_id or uuid4(),
        "preview",
        AS_OF,
        status,
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
        stage_failures=(
            StageFailure("llm", "ProviderError", f"API key={SECRET_URL}"),
        ),
        error=error,
    )


def patch_connect(monkeypatch, *, cursor: FakeCursor | None = None):
    fake_cursor = cursor or FakeCursor()
    connection = FakeConnection(fake_cursor)
    calls = []

    def connect(database_url, **kwargs):
        calls.append((database_url, kwargs))
        return connection

    monkeypatch.setattr("app.database.postgres_runs.psycopg.connect", connect)
    return connection, fake_cursor, calls


def test_repository_selection_defaults_to_sqlite_and_uses_explicit_postgres():
    assert isinstance(create_run_repository({}), SQLitePipelineRunRepository)
    assert isinstance(
        create_run_repository({"DATABASE_BACKEND": "sqlite", "DATABASE_PATH": "local.db"}),
        SQLitePipelineRunRepository,
    )
    assert isinstance(
        create_run_repository(
            {"DATABASE_BACKEND": "postgres", "DATABASE_URL": SECRET_URL}
        ),
        PostgresPipelineRunRepository,
    )
    assert isinstance(
        create_run_repository(
            {
                "DATABASE_BACKEND": "postgres",
                "DATABASE_URL": (
                    "postgresql://agent:db-password@/news"
                    "?host=/cloudsql/project:region:instance"
                ),
            }
        ),
        PostgresPipelineRunRepository,
    )


@pytest.mark.parametrize(
    ("configuration", "message"),
    [
        ({"DATABASE_BACKEND": "mysql"}, "DATABASE_BACKEND"),
        ({"DATABASE_BACKEND": "postgres"}, "DATABASE_URL"),
        (
            {"DATABASE_BACKEND": "postgres", "DATABASE_URL": "postgresql://bad"},
            "DATABASE_URL",
        ),
    ],
)
def test_invalid_database_configuration_fails_without_echoing_url(configuration, message):
    with pytest.raises(ValueError, match=message) as error:
        create_run_repository(configuration)

    assert SECRET_URL not in str(error.value)


def test_explicit_initialization_is_repeatable_and_nondestructive(monkeypatch):
    connection, cursor, calls = patch_connect(monkeypatch)
    repository = PostgresPipelineRunRepository(SECRET_URL)

    repository.initialize()
    repository.initialize()

    assert len(calls) == 2
    assert len(cursor.queries) == 4
    assert (
        sum("CREATE TABLE IF NOT EXISTS pipeline_runs" in query for query, _ in cursor.queries)
        == 2
    )
    assert sum(
        "CREATE TABLE IF NOT EXISTS newsletter_deliveries" in query
        for query, _ in cursor.queries
    ) == 2
    assert all("DROP TABLE" not in query for query, _ in cursor.queries)
    assert connection.closed
    assert cursor.closed


def test_successful_insert_uses_parameterized_sql_and_closes_resources(monkeypatch):
    connection, cursor, calls = patch_connect(monkeypatch)
    repository = PostgresPipelineRunRepository(SECRET_URL)
    run_id = uuid4()

    repository.create_run(run_id, started_at=AS_OF, as_of=AS_OF, mode="send")

    query, params = cursor.queries[0]
    assert "INSERT INTO pipeline_runs" in query
    assert "%s" in query
    assert str(run_id) not in query
    assert params == (run_id, AS_OF, "running", "send", AS_OF)
    assert calls[0][0] == SECRET_URL
    assert connection.closed
    assert cursor.closed


def test_successful_update_persists_all_counts_and_sanitized_diagnostics(monkeypatch):
    connection, cursor, _calls = patch_connect(monkeypatch)
    repository = PostgresPipelineRunRepository(SECRET_URL)
    run_id = uuid4()
    provider_error = f"authorization: Bearer {SECRET_URL}; do not leak"
    result = make_result(run_id, status="pipeline_failed", error=provider_error)

    repository.complete_run(result, started_at=AS_OF, completed_at=AS_OF)

    query, params = cursor.queries[0]
    warning_payload = params[19].obj
    assert "UPDATE pipeline_runs SET" in query
    assert query.count("%s") == len(params)
    assert SECRET_URL not in query
    assert SECRET_URL not in str(warning_payload)
    assert SECRET_URL not in params[20]
    assert params[4:18] == (2, 1, 1, 10, 8, 1, 7, 6, 6, 3, 2, 1, 1, 1)
    assert params[21] == run_id
    assert connection.closed
    assert cursor.closed


def test_connection_failure_is_safe_and_never_falls_back(monkeypatch):
    def fail_connect(database_url, **_kwargs):
        raise psycopg.OperationalError(f"connect failed: {database_url}")

    monkeypatch.setattr("app.database.postgres_runs.psycopg.connect", fail_connect)
    repository = PostgresPipelineRunRepository(SECRET_URL)

    with pytest.raises(PostgresRepositoryError, match="Unable to connect") as error:
        repository.create_run(uuid4(), started_at=AS_OF, as_of=AS_OF, mode="preview")

    assert SECRET_URL not in str(error.value)


def test_postgres_does_not_initialize_schema_implicitly(monkeypatch):
    _connection, cursor, _calls = patch_connect(monkeypatch)
    repository = PostgresPipelineRunRepository(SECRET_URL)

    repository.create_run(uuid4(), started_at=AS_OF, as_of=AS_OF, mode="preview")

    assert len(cursor.queries) == 1
    assert "CREATE TABLE" not in cursor.queries[0][0]


def test_postgres_run_history_uses_bounded_parameterized_limit(monkeypatch):
    connection, cursor, _calls = patch_connect(monkeypatch)
    cursor.many = [{"run_id": "recent-run", "status": "preview"}]
    repository = PostgresPipelineRunRepository(SECRET_URL)

    records = repository.list_runs(limit=7)

    query, params = cursor.queries[0]
    assert records == ({"run_id": "recent-run", "status": "preview"},)
    assert "ORDER BY started_at DESC, run_id DESC LIMIT %s" in query
    assert params == (7,)
    assert "7" not in query
    assert connection.closed
    assert cursor.closed
    with pytest.raises(ValueError, match="limit"):
        repository.list_runs(limit=101)


def test_parameterized_update_does_not_interpolate_sql_text(monkeypatch):
    _connection, cursor, _calls = patch_connect(monkeypatch)
    repository = PostgresPipelineRunRepository(SECRET_URL)
    sql_like_text = "x'; DROP TABLE pipeline_runs; --"

    repository.complete_run(
        make_result(error=sql_like_text), started_at=AS_OF, completed_at=AS_OF
    )

    query, params = cursor.queries[0]
    assert sql_like_text not in query
    assert params[20] == sql_like_text


def test_postgres_delivery_claim_uses_unique_parameterized_queries(monkeypatch):
    connection, cursor, _calls = patch_connect(monkeypatch)
    repository = PostgresPipelineRunRepository(SECRET_URL)
    run_id = uuid4()
    key = "personal-newsletter/2026-10-01/recipient-hash"

    assert repository.claim_delivery(key, run_id=run_id, claimed_at=AS_OF)
    repository.complete_delivery_claim(key, outcome="accepted", provider_message_id="smtp-id")
    cursor.one = {"?column?": 1}
    assert repository.delivery_claim_exists(key)

    claim_query, claim_params = cursor.queries[0]
    update_query, update_params = cursor.queries[1]
    exists_query, exists_params = cursor.queries[2]
    assert "ON CONFLICT (delivery_key) DO NOTHING" in claim_query
    assert claim_params == (key, run_id, AS_OF)
    assert "WHERE delivery_key = %s" in update_query
    assert update_params == ("accepted", "smtp-id", key)
    assert "SELECT 1 FROM newsletter_deliveries" in exists_query
    assert exists_params == (key,)
    assert SECRET_URL not in repr(cursor.queries)
    assert connection.closed


def test_postgres_reset_delivery_claim_is_exact_and_parameterized(monkeypatch):
    connection, cursor, _calls = patch_connect(monkeypatch, cursor=FakeCursor(rowcount=1))
    repository = PostgresPipelineRunRepository(SECRET_URL)
    key = "personal-newsletter/2026-10-02/recipient-hash"

    assert repository.reset_delivery_claim(key)

    query, params = cursor.queries[0]
    assert "DELETE FROM newsletter_deliveries WHERE delivery_key = %s" in query
    assert params == (key,)
    assert key not in query
    assert connection.closed
    assert cursor.closed


def test_connection_url_is_not_logged(monkeypatch, caplog):
    def fail_connect(database_url, **_kwargs):
        raise psycopg.OperationalError(f"bad dsn {database_url}")

    monkeypatch.setattr("app.database.postgres_runs.psycopg.connect", fail_connect)
    repository = PostgresPipelineRunRepository(SECRET_URL)

    with caplog.at_level("DEBUG"):
        with pytest.raises(PostgresRepositoryError):
            repository.initialize()

    assert SECRET_URL not in caplog.text


def test_cli_rejects_invalid_backend_before_pipeline_run(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_BACKEND", "unsupported")

    exit_code = main(["preview"])

    assert exit_code == 2
    assert '"status": "configuration_failed"' in capsys.readouterr().out


def test_database_init_cli_explicitly_initializes_selected_repository(monkeypatch, capsys):
    initialized = []

    class FakeRepository:
        def initialize(self):
            initialized.append(True)

        def create_run(self, *_args, **_kwargs):
            raise AssertionError("database-init must not start a pipeline")

        def complete_run(self, *_args, **_kwargs):
            raise AssertionError("database-init must not run a pipeline")

    monkeypatch.setattr("app.cli.create_run_repository", lambda: FakeRepository())

    assert main(["database-init"]) == 0
    assert initialized == [True]
    assert '"status": "database_initialized"' in capsys.readouterr().out
