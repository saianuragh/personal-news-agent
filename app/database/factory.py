"""Select a pipeline run repository from validated environment settings."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from app.database.base import PipelineRunRepository
from app.database.postgres_runs import PostgresPipelineRunRepository
from app.database.sqlite_runs import SQLitePipelineRunRepository


def create_run_repository(
    environ: Mapping[str, str] | None = None,
) -> PipelineRunRepository:
    """Create the selected repository; never substitute one backend for another."""
    values = os.environ if environ is None else environ
    backend = values.get("DATABASE_BACKEND", "sqlite").strip().casefold() or "sqlite"
    if backend == "sqlite":
        database_path = values.get("DATABASE_PATH", "").strip()
        if not database_path:
            database_path = str(
                Path(__file__).resolve().parents[2] / "data" / "pipeline_runs.sqlite3"
            )
        return SQLitePipelineRunRepository(database_path)
    if backend == "postgres":
        database_url = values.get("DATABASE_URL", "").strip()
        if not _valid_postgres_url(database_url):
            raise ValueError(
                "DATABASE_URL must be a valid postgresql:// or postgres:// URL "
                "when DATABASE_BACKEND=postgres"
            )
        return PostgresPipelineRunRepository(database_url)
    raise ValueError("DATABASE_BACKEND must be either 'sqlite' or 'postgres'")


def repository_backend(repository: PipelineRunRepository) -> str:
    """Return a non-secret backend label for status output."""
    return "postgres" if isinstance(repository, PostgresPipelineRunRepository) else "sqlite"


def _valid_postgres_url(database_url: str) -> bool:
    if not database_url:
        return False
    try:
        parsed = urlsplit(database_url)
        unix_socket_hosts = parse_qs(parsed.query).get("host", [])
        has_socket_host = any(host.startswith("/") for host in unix_socket_hosts)
        return (
            parsed.scheme.casefold() in {"postgres", "postgresql"}
            and (bool(parsed.hostname) or has_socket_host)
            and parsed.port != 0
            and bool(parsed.path.strip("/"))
            and not any(character.isspace() for character in database_url)
        )
    except ValueError:
        return False
