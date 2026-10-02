"""Raw source-adapter data contracts and feed-level errors."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.config import SourceConfig

ProviderFormat = Literal["rss", "atom"]


@dataclass(frozen=True, slots=True)
class RawProviderEntry:
    """An extracted entry whose fields retain their RSS/Atom names and values."""

    format: ProviderFormat
    fields: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class FetchedFeed:
    """Feed response context and raw entries passed to normalization."""

    source: SourceConfig
    retrieved_at: datetime
    entries: tuple[RawProviderEntry, ...]


class SourceFetchError(RuntimeError):
    """Raised when the feed cannot be retrieved or parsed as RSS/Atom."""

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status
