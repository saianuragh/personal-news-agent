"""Persistence adapters and database access."""

from app.database.base import PipelineRunRepository
from app.database.sqlite_runs import SQLitePipelineRunRepository

__all__ = [
    "PipelineRunRepository",
    "PostgresPipelineRunRepository",
    "SQLitePipelineRunRepository",
    "create_run_repository",
]


def __getattr__(name: str):
    """Load optional database implementations only when explicitly requested."""
    if name == "create_run_repository":
        from app.database.factory import create_run_repository

        return create_run_repository
    if name == "PostgresPipelineRunRepository":
        from app.database.postgres_runs import PostgresPipelineRunRepository

        return PostgresPipelineRunRepository
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
