"""PostgreSQL repository for sanitized pipeline run history."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.database.run_data import sanitize_diagnostic, warnings_for

if TYPE_CHECKING:
    from psycopg import Connection

    from app.pipeline.runner import PipelineRunResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id UUID PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('preview', 'send')),
    as_of TIMESTAMPTZ NOT NULL,
    sources_attempted INTEGER NOT NULL DEFAULT 0,
    sources_succeeded INTEGER NOT NULL DEFAULT 0,
    sources_failed INTEGER NOT NULL DEFAULT 0,
    raw_entries INTEGER NOT NULL DEFAULT 0,
    article_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    story_count INTEGER NOT NULL DEFAULT 0,
    categorized_count INTEGER NOT NULL DEFAULT 0,
    ranked_count INTEGER NOT NULL DEFAULT 0,
    selected_count INTEGER NOT NULL DEFAULT 0,
    summarized_count INTEGER NOT NULL DEFAULT 0,
    fallback_count INTEGER NOT NULL DEFAULT 0,
    summary_failure_count INTEGER NOT NULL DEFAULT 0,
    omitted_count INTEGER NOT NULL DEFAULT 0,
    delivery_outcome TEXT,
    warnings_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    error TEXT
)
"""

_DELIVERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS newsletter_deliveries (
    delivery_key TEXT PRIMARY KEY,
    run_id UUID NOT NULL,
    claimed_at TIMESTAMPTZ NOT NULL,
    outcome TEXT NOT NULL DEFAULT 'claimed',
    provider_message_id TEXT
)
"""


class PostgresRepositoryError(RuntimeError):
    """A safe database error that never includes the connection URL or credentials."""


class PostgresPipelineRunRepository:
    """Persist pipeline run metadata in PostgreSQL using Psycopg 3."""

    def __init__(self, database_url: str, *, connect_timeout: int = 5) -> None:
        if not database_url.strip():
            raise ValueError("DATABASE_URL must be non-empty for the postgres backend")
        if connect_timeout < 1 or connect_timeout > 60:
            raise ValueError("connect_timeout must be between 1 and 60 seconds")
        self._database_url = database_url
        self._connect_timeout = connect_timeout

    def initialize(self) -> None:
        """Create the run table if absent; this must be invoked explicitly."""
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(_SCHEMA)
                cursor.execute(_DELIVERY_SCHEMA)

    def create_run(
        self,
        run_id: UUID,
        *,
        started_at: datetime,
        as_of: datetime,
        mode: str,
    ) -> None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO pipeline_runs (run_id, started_at, status, mode, as_of)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (run_id, _aware(started_at), "running", mode, _aware(as_of)),
                )

    def complete_run(
        self,
        result: PipelineRunResult,
        *,
        started_at: datetime,
        completed_at: datetime,
    ) -> None:
        counts = result.counts
        delivery_outcome = result.delivery.status if result.delivery is not None else None
        parameters = (
            _aware(started_at),
            _aware(completed_at),
            result.status,
            _aware(result.as_of),
            counts.sources_attempted,
            counts.sources_succeeded,
            counts.sources_failed,
            counts.raw_entries,
            counts.normalized_articles,
            counts.duplicate_articles,
            counts.stories,
            counts.categorized_stories,
            counts.ranked_stories,
            counts.selected_stories,
            counts.summarized_stories,
            counts.fallback_stories,
            counts.summary_failures,
            counts.omitted_stories,
            delivery_outcome,
            Jsonb(warnings_for(result)),
            sanitize_diagnostic(result.error) if result.error else None,
            result.run_id,
        )
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE pipeline_runs SET
                        started_at = %s, completed_at = %s, status = %s, as_of = %s,
                        sources_attempted = %s, sources_succeeded = %s, sources_failed = %s,
                        raw_entries = %s, article_count = %s, duplicate_count = %s,
                        story_count = %s, categorized_count = %s, ranked_count = %s,
                        selected_count = %s, summarized_count = %s, fallback_count = %s,
                        summary_failure_count = %s, omitted_count = %s, delivery_outcome = %s,
                        warnings_json = %s, error = %s
                    WHERE run_id = %s
                    """,
                    parameters,
                )
                if cursor.rowcount != 1:
                    raise PostgresRepositoryError(
                        "PostgreSQL pipeline run record was not created."
                    )

    def claim_delivery(self, delivery_key: str, *, run_id: UUID, claimed_at: datetime) -> bool:
        """Atomically reserve a delivery key across concurrent job executions."""
        if not delivery_key or len(delivery_key) > 256:
            raise ValueError("delivery_key must contain 1-256 characters")
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO newsletter_deliveries
                        (delivery_key, run_id, claimed_at, outcome)
                    VALUES (%s, %s, %s, 'claimed')
                    ON CONFLICT (delivery_key) DO NOTHING
                    """,
                    (delivery_key, run_id, _aware(claimed_at)),
                )
                return cursor.rowcount == 1

    def complete_delivery_claim(
        self,
        delivery_key: str,
        *,
        outcome: str,
        provider_message_id: str | None = None,
    ) -> None:
        """Update a claim with a sanitized provider outcome."""
        if outcome not in {"accepted", "rejected", "failed", "unknown"}:
            raise ValueError("delivery outcome is invalid")
        safe_message_id = sanitize_diagnostic(provider_message_id) if provider_message_id else None
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE newsletter_deliveries
                    SET outcome = %s, provider_message_id = %s
                    WHERE delivery_key = %s
                    """,
                    (outcome, safe_message_id, delivery_key),
                )
                if cursor.rowcount != 1:
                    raise PostgresRepositoryError("PostgreSQL delivery claim was not created.")

    def delivery_claim_exists(self, delivery_key: str) -> bool:
        """Check for a prior daily delivery attempt without returning row data."""
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM newsletter_deliveries WHERE delivery_key = %s",
                    (delivery_key,),
                )
                return cursor.fetchone() is not None

    def reset_delivery_claim(self, delivery_key: str) -> bool:
        """Delete exactly one delivery claim for an explicit development retry."""
        if not delivery_key or len(delivery_key) > 256:
            raise ValueError("delivery_key must contain 1-256 characters")
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM newsletter_deliveries WHERE delivery_key = %s",
                    (delivery_key,),
                )
                return cursor.rowcount == 1

    def get_run(self, run_id: UUID | str) -> dict[str, Any] | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM pipeline_runs WHERE run_id = %s", (str(run_id),))
                row = cursor.fetchone()
                return dict(row) if row is not None else None

    def list_runs(self, limit: int = 10) -> tuple[dict[str, Any], ...]:
        """Return at most ``limit`` run rows ordered newest first."""
        _validate_run_limit(limit)
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM pipeline_runs ORDER BY started_at DESC, run_id DESC LIMIT %s",
                    (limit,),
                )
                return tuple(dict(row) for row in cursor.fetchall())

    @contextmanager
    def _connection(self) -> Iterator[Connection[Any]]:
        try:
            with psycopg.connect(
                self._database_url,
                connect_timeout=self._connect_timeout,
                row_factory=dict_row,
            ) as connection:
                yield connection
        except psycopg.OperationalError:
            raise PostgresRepositoryError(
                "Unable to connect to the configured PostgreSQL database."
            ) from None
        except psycopg.Error:
            raise PostgresRepositoryError("PostgreSQL persistence operation failed.") from None


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("database timestamps must be timezone-aware")
    return value


def _validate_run_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("run history limit must be an integer from 1 to 100")
