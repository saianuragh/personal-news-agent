"""Persistence adapters and database access."""

from app.database.base import PipelineRunRepository
from app.database.factory import create_run_repository
from app.database.postgres_runs import PostgresPipelineRunRepository
from app.database.sqlite_runs import SQLitePipelineRunRepository

__all__ = [
    "PipelineRunRepository",
    "PostgresPipelineRunRepository",
    "SQLitePipelineRunRepository",
    "create_run_repository",
]
