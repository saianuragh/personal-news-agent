"""Interfaces for local pipeline persistence."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

if TYPE_CHECKING:
    from app.pipeline.runner import PipelineRunResult, RunMode


class PipelineRunRepository(Protocol):
    """Minimal lifecycle persistence contract for one pipeline run."""

    def create_run(
        self,
        run_id: UUID,
        *,
        started_at: datetime,
        as_of: datetime,
        mode: RunMode,
    ) -> None: ...

    def complete_run(
        self,
        result: PipelineRunResult,
        *,
        started_at: datetime,
        completed_at: datetime,
    ) -> None: ...

    def claim_delivery(self, delivery_key: str, *, run_id: UUID, claimed_at: datetime) -> bool: ...

    def delivery_claim_exists(self, delivery_key: str) -> bool: ...

    def reset_delivery_claim(self, delivery_key: str) -> bool: ...

    def complete_delivery_claim(
        self,
        delivery_key: str,
        *,
        outcome: str,
        provider_message_id: str | None = None,
    ) -> None: ...

    def initialize(self) -> None: ...

    def list_runs(self, limit: int = 10) -> tuple[dict[str, object], ...]: ...
