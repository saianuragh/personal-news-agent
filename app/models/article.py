"""Provider-independent canonical article data model."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import UUID

MAX_SOURCE_ID_LENGTH = 200
MAX_PUBLISHER_LENGTH = 200
MAX_TITLE_LENGTH = 500
MAX_URL_LENGTH = 2_048
MAX_DESCRIPTION_LENGTH = 4_000
MAX_SOURCE_CATEGORIES = 20
MAX_CATEGORY_LENGTH = 100

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_LANGUAGE_TAG_PATTERN = re.compile(r"[A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*\Z")


def _required_text(value: object, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")

    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    if len(normalized) > max_length:
        raise ValueError(f"{field_name} must be at most {max_length} characters")
    return normalized


def _http_url(value: object, field_name: str) -> str:
    url = _required_text(value, field_name, MAX_URL_LENGTH)
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        # Accessing .port also validates malformed port values.
        port = parts.port
    except ValueError as error:
        raise ValueError(f"{field_name} must be a valid absolute HTTP(S) URL") from error

    if parts.scheme.lower() not in {"http", "https"} or not hostname:
        raise ValueError(f"{field_name} must be a valid absolute HTTP(S) URL")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"{field_name} must not include embedded credentials")
    if port is not None and not 0 <= port <= 65_535:
        raise ValueError(f"{field_name} must have a valid port")
    if any(character.isspace() for character in url):
        raise ValueError(f"{field_name} must not contain whitespace")
    return url


def _utc_timestamp(value: object, field_name: str, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a timezone-aware datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Article:
    """One publisher's normalized article record.

    Provider-specific parsing and invalid-optional-field reporting belong in
    normalization, before an ``Article`` is constructed. This model rejects
    invalid values and never invents publication timestamps.
    """

    article_id: UUID
    source_id: str
    publisher: str
    title: str
    url: str
    canonical_url: str
    retrieved_at: datetime
    content_hash: str
    published_at: datetime | None = None
    description: str | None = None
    source_categories: tuple[str, ...] = ()
    language: str | None = None

    def __post_init__(self) -> None:
        article_id = self.article_id
        if isinstance(article_id, str):
            try:
                article_id = UUID(article_id)
            except ValueError as error:
                raise ValueError("article_id must be a valid UUID") from error
            object.__setattr__(self, "article_id", article_id)
        elif not isinstance(article_id, UUID):
            raise TypeError("article_id must be a UUID or UUID string")

        object.__setattr__(
            self,
            "source_id",
            _required_text(self.source_id, "source_id", MAX_SOURCE_ID_LENGTH),
        )
        object.__setattr__(
            self, "publisher", _required_text(self.publisher, "publisher", MAX_PUBLISHER_LENGTH)
        )
        object.__setattr__(self, "title", _required_text(self.title, "title", MAX_TITLE_LENGTH))
        object.__setattr__(self, "url", _http_url(self.url, "url"))
        object.__setattr__(self, "canonical_url", _http_url(self.canonical_url, "canonical_url"))

        retrieved_at = _utc_timestamp(self.retrieved_at, "retrieved_at")
        published_at = _utc_timestamp(self.published_at, "published_at", optional=True)
        object.__setattr__(self, "retrieved_at", retrieved_at)
        object.__setattr__(self, "published_at", published_at)

        if not isinstance(self.content_hash, str) or not _SHA256_PATTERN.fullmatch(
            self.content_hash
        ):
            raise ValueError("content_hash must be a lowercase 64-character SHA-256 hex digest")

        if self.description is not None:
            description = _required_text(self.description, "description", MAX_DESCRIPTION_LENGTH)
            object.__setattr__(self, "description", description)

        if isinstance(self.source_categories, (str, bytes)):
            raise TypeError("source_categories must be a sequence of strings")
        try:
            categories = tuple(self.source_categories)
        except TypeError as error:
            raise TypeError("source_categories must be a sequence of strings") from error
        if len(categories) > MAX_SOURCE_CATEGORIES:
            raise ValueError(
                f"source_categories must contain at most {MAX_SOURCE_CATEGORIES} values"
            )

        normalized_categories = tuple(
            _required_text(category, "source_categories entry", MAX_CATEGORY_LENGTH)
            for category in categories
        )
        object.__setattr__(self, "source_categories", normalized_categories)

        if self.language is not None:
            language = _required_text(self.language, "language", 35)
            if not _LANGUAGE_TAG_PATTERN.fullmatch(language):
                raise ValueError("language must be a normalized language tag")
            object.__setattr__(self, "language", language)
