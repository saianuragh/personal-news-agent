"""SQLite repository for sanitized pipeline run history."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from app.database.run_data import sanitize_diagnostic, warnings_for

if TYPE_CHECKING:
    from app.pipeline.runner import PipelineRunResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('preview', 'send')),
    as_of TEXT NOT NULL,
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
    warnings_json TEXT NOT NULL DEFAULT '[]',
    error TEXT
)
"""

_DELIVERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS newsletter_deliveries (
    delivery_key TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT 'claimed',
    provider_message_id TEXT
)
"""


class SQLitePipelineRunRepository:
    """Persist only run metadata and sanitized warnings/errors in local SQLite."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    def initialize(self) -> None:
        """Create parent directory and schema; safe to call on every operation."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        try:
            with connection:
                connection.execute("PRAGMA busy_timeout = 5000")
                connection.execute(_SCHEMA)
                connection.execute(_DELIVERY_SCHEMA)
        finally:
            connection.close()

    def create_run(
        self,
        run_id: UUID,
        *,
        started_at: datetime,
        as_of: datetime,
        mode: str,
    ) -> None:
        self.initialize()
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO pipeline_runs (run_id, started_at, status, mode, as_of)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (str(run_id), _timestamp(started_at), "running", mode, _timestamp(as_of)),
                )
        finally:
            connection.close()

    def complete_run(
        self,
        result: PipelineRunResult,
        *,
        started_at: datetime,
        completed_at: datetime,
    ) -> None:
        self.initialize()
        counts = result.counts
        warnings = warnings_for(result)
        delivery_outcome = result.delivery.status if result.delivery is not None else None
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        try:
            with connection:
                cursor = connection.execute(
                    """
                    UPDATE pipeline_runs SET
                        started_at = ?, completed_at = ?, status = ?, as_of = ?,
                        sources_attempted = ?, sources_succeeded = ?, sources_failed = ?,
                        raw_entries = ?, article_count = ?, duplicate_count = ?,
                        story_count = ?, categorized_count = ?, ranked_count = ?,
                        selected_count = ?, summarized_count = ?, fallback_count = ?,
                        summary_failure_count = ?, omitted_count = ?, delivery_outcome = ?,
                        warnings_json = ?, error = ?
                    WHERE run_id = ?
                    """,
                    (
                        _timestamp(started_at),
                        _timestamp(completed_at),
                        result.status,
                        _timestamp(result.as_of),
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
                        json.dumps(warnings, ensure_ascii=False, sort_keys=True),
                        sanitize_diagnostic(result.error) if result.error else None,
                        str(result.run_id),
                    ),
                )
                if cursor.rowcount != 1:
                    raise sqlite3.IntegrityError("pipeline run record was not created")
        finally:
            connection.close()

    def claim_delivery(self, delivery_key: str, *, run_id: UUID, claimed_at: datetime) -> bool:
        """Atomically reserve one local-date delivery key across processes."""
        if not delivery_key or len(delivery_key) > 256:
            raise ValueError("delivery_key must contain 1-256 characters")
        self.initialize()
        connection = sqlite3.connect(self.database_path, timeout=5.0, isolation_level="IMMEDIATE")
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
            with connection:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO newsletter_deliveries
                        (delivery_key, run_id, claimed_at, outcome)
                    VALUES (?, ?, ?, 'claimed')
                    """,
                    (delivery_key, str(run_id), _timestamp(claimed_at)),
                )
                return cursor.rowcount == 1
        finally:
            connection.close()

    def complete_delivery_claim(
        self,
        delivery_key: str,
        *,
        outcome: str,
        provider_message_id: str | None = None,
    ) -> None:
        """Record a sanitized delivery outcome without storing address or message body."""
        allowed_outcomes = {"accepted", "rejected", "failed", "unknown"}
        if outcome not in allowed_outcomes:
            raise ValueError("delivery outcome is invalid")
        safe_message_id = sanitize_diagnostic(provider_message_id) if provider_message_id else None
        self.initialize()
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        try:
            with connection:
                cursor = connection.execute(
                    """
                    UPDATE newsletter_deliveries
                    SET outcome = ?, provider_message_id = ?
                    WHERE delivery_key = ?
                    """,
                    (outcome, safe_message_id, delivery_key),
                )
                if cursor.rowcount != 1:
                    raise sqlite3.IntegrityError("delivery claim was not created")
        finally:
            connection.close()

    def get_delivery_claim(self, delivery_key: str) -> dict[str, Any] | None:
        """Return a claim for diagnostics/tests; it contains no recipient or credentials."""
        self.initialize()
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT * FROM newsletter_deliveries WHERE delivery_key = ?", (delivery_key,)
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            connection.close()

    def delivery_claim_exists(self, delivery_key: str) -> bool:
        """Check for an existing daily claim without exposing its stored fields."""
        self.initialize()
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        try:
            row = connection.execute(
                "SELECT 1 FROM newsletter_deliveries WHERE delivery_key = ?", (delivery_key,)
            ).fetchone()
            return row is not None
        finally:
            connection.close()

    def reset_delivery_claim(self, delivery_key: str) -> bool:
        """Delete exactly one delivery claim; intended for explicit development retries."""
        if not delivery_key or len(delivery_key) > 256:
            raise ValueError("delivery_key must contain 1-256 characters")
        self.initialize()
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        try:
            with connection:
                cursor = connection.execute(
                    "DELETE FROM newsletter_deliveries WHERE delivery_key = ?",
                    (delivery_key,),
                )
                return cursor.rowcount == 1
        finally:
            connection.close()

    def get_run(self, run_id: UUID | str) -> dict[str, Any] | None:
        """Return one stored row as a plain mapping, primarily for diagnostics/tests."""
        self.initialize()
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT * FROM pipeline_runs WHERE run_id = ?", (str(run_id),)
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            connection.close()

    def list_runs(self, limit: int = 10) -> tuple[dict[str, Any], ...]:
        """Return at most ``limit`` run rows ordered newest first."""
        _validate_run_limit(limit)
        self.initialize()
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT * FROM pipeline_runs ORDER BY started_at DESC, run_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return tuple(dict(row) for row in rows)
        finally:
            connection.close()


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("database timestamps must be timezone-aware")
    return value.isoformat()


def _validate_run_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("run history limit must be an integer from 1 to 100")
