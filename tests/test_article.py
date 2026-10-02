"""Tests for the canonical Article contract."""

from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

import pytest
from app.models.article import Article


def valid_article_values() -> dict[str, object]:
    return {
        "article_id": uuid4(),
        "source_id": "reliable-news-feed",
        "publisher": "Example News",
        "title": "A validated article title",
        "url": "https://example.com/story?id=1&utm_source=feed",
        "canonical_url": "https://example.com/story?id=1",
        "retrieved_at": datetime(2026, 9, 29, 10, tzinfo=UTC),
        "content_hash": sha256(b"normalized article").hexdigest(),
        "published_at": datetime(2026, 9, 29, 9, tzinfo=UTC),
        "description": "A short plain-text description.",
        "source_categories": ["Technology", "AI"],
        "language": "en-IN",
    }


def test_creates_article_and_normalizes_utc_timestamps_and_categories() -> None:
    values = valid_article_values()
    values["article_id"] = str(values["article_id"])
    values["retrieved_at"] = datetime(
        2026,
        9,
        29,
        15,
        30,
        tzinfo=timezone(timedelta(hours=5, minutes=30)),
    )

    article = Article(**values)

    assert article.title == "A validated article title"
    assert article.retrieved_at == datetime(2026, 9, 29, 10, tzinfo=UTC)
    assert article.published_at == datetime(2026, 9, 29, 9, tzinfo=UTC)
    assert article.source_categories == ("Technology", "AI")


@pytest.mark.parametrize(
    "required_field",
    [
        "article_id",
        "source_id",
        "publisher",
        "title",
        "url",
        "canonical_url",
        "retrieved_at",
        "content_hash",
    ],
)
def test_missing_required_field_is_rejected(required_field: str) -> None:
    values = valid_article_values()
    del values[required_field]

    with pytest.raises(TypeError):
        Article(**values)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not-a-url",
        "ftp://example.com/story",
        "https:///missing-host",
        "https://user:secret@example.com/story",
    ],
)
def test_invalid_article_url_is_rejected(url: str) -> None:
    values = valid_article_values()
    values["url"] = url

    with pytest.raises(
        ValueError,
        match=(
            r"url must be a valid absolute HTTP\(S\) URL|url must not be empty|"
            r"url must not include embedded credentials"
        ),
    ):
        Article(**values)


def test_embedded_credentials_are_rejected_without_echoing_them() -> None:
    values = valid_article_values()
    values["canonical_url"] = "https://account:private-password@example.com/story"

    with pytest.raises(ValueError, match="embedded credentials") as raised:
        Article(**values)

    assert "private-password" not in str(raised.value)


def test_invalid_canonical_url_is_rejected() -> None:
    values = valid_article_values()
    values["canonical_url"] = "javascript:alert(1)"

    with pytest.raises(ValueError, match="canonical_url"):
        Article(**values)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("retrieved_at", datetime(2026, 9, 29, 10), ValueError),
        ("published_at", datetime(2026, 9, 29, 10), ValueError),
        ("published_at", "not-a-date", TypeError),
    ],
)
def test_invalid_timestamps_are_rejected(field: str, value: object, error: type[Exception]) -> None:
    values = valid_article_values()
    values[field] = value

    with pytest.raises(error, match="timezone-aware"):
        Article(**values)


def test_missing_publication_time_remains_unknown() -> None:
    values = valid_article_values()
    values["published_at"] = None

    article = Article(**values)

    assert article.published_at is None


@pytest.mark.parametrize("title", ["", "   ", "x" * 501])
def test_empty_or_oversized_title_is_rejected(title: str) -> None:
    values = valid_article_values()
    values["title"] = title

    with pytest.raises(ValueError, match="title"):
        Article(**values)


@pytest.mark.parametrize("content_hash", ["", "not-a-hash", "a" * 63, "A" * 64])
def test_malformed_content_hash_is_rejected(content_hash: str) -> None:
    values = valid_article_values()
    values["content_hash"] = content_hash

    with pytest.raises(ValueError, match="content_hash"):
        Article(**values)


def test_source_categories_are_bounded_and_immutable() -> None:
    values = valid_article_values()
    values["source_categories"] = ["category"] * 21

    with pytest.raises(ValueError, match="at most 20"):
        Article(**values)
