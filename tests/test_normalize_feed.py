"""Tests for raw feed entry conversion into canonical Article records."""

from datetime import UTC, datetime
from pathlib import Path

import httpx
from app.config import load_sources
from app.models.article import Article
from app.processing.normalize import NormalizationIssue, normalize_feed
from app.sources.base import FetchedFeed, RawProviderEntry
from app.sources.rss_atom import RSSAtomSource

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def fetched_feed(fixture: str, retrieved_at: datetime):
    content = (FIXTURES / fixture).read_bytes()
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=content))
    )
    with client:
        return RSSAtomSource(
            load_sources(ROOT / "config" / "sources.yaml")[0],
            client=client,
            clock=lambda: retrieved_at,
        ).fetch()


def test_rss_entries_normalize_individually_to_articles() -> None:
    feed = fetched_feed("bbc_world_sample.rss", datetime(2026, 9, 29, 10, tzinfo=UTC))

    result = normalize_feed(feed)

    assert len(result.articles) == 3
    article, article_without_optional_data, invalid_date_article = result.articles
    assert article.title == "Sample world headline"
    assert article.source_id == "bbc_world"
    assert article.publisher == "BBC News"
    assert article.url == "https://EXAMPLE.com/world/story-1?utm_source=rss&id=1#top"
    assert article.canonical_url == "https://example.com/world/story-1?id=1"
    assert article.retrieved_at == datetime(2026, 9, 29, 10, tzinfo=UTC)
    assert article.published_at == datetime(2026, 9, 29, 8, 30, tzinfo=UTC)
    assert article.description == "A sanitized & short description."
    assert article.source_categories == ("World", "Policy")
    assert article.language == "en-GB"
    assert article_without_optional_data.published_at is None
    assert article_without_optional_data.description is None
    assert article_without_optional_data.source_categories == ()
    assert article_without_optional_data.language == "en-GB"
    assert invalid_date_article.published_at is None
    assert {issue.field for issue in result.issues} == {"title", "url", "published_at"}


def test_atom_date_converts_to_utc_and_tracking_fragment_are_removed() -> None:
    feed = fetched_feed("sample.atom", datetime(2026, 9, 29, 10, tzinfo=UTC))

    result = normalize_feed(feed)
    article = result.articles[0]

    assert article.published_at == datetime(2026, 9, 29, 8, 15, tzinfo=UTC)
    assert article.canonical_url == "https://example.com/atom/story-1?ref=42"
    assert article.source_categories == ("Science",)
    assert article.language == "en"
    assert result.articles[1].published_at is None


def test_second_configured_source_uses_generic_adapter_then_normalizes_to_article() -> None:
    guardian = load_sources(ROOT / "config" / "sources.yaml")[1]
    retrieved_at = datetime(2026, 9, 29, 10, tzinfo=UTC)
    xml = b"""<?xml version="1.0"?>
    <rss version="2.0"><channel><language>en</language><item>
      <title>World leaders meet for a summit</title>
      <link>https://www.theguardian.com/world/2026/sep/29/summit</link>
      <pubDate>Tue, 29 Sep 2026 08:30:00 GMT</pubDate>
      <description>Leaders discussed international affairs.</description>
      <category>World</category>
    </item></channel></rss>"""
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=xml))
    )
    with client:
        fetched = RSSAtomSource(guardian, client=client, clock=lambda: retrieved_at).fetch()

    result = normalize_feed(fetched)

    assert fetched.source is guardian
    assert len(fetched.entries) == 1
    assert len(result.articles) == 1
    article = result.articles[0]
    assert isinstance(article, Article)
    assert article.source_id == "guardian_world"
    assert article.publisher == "The Guardian"
    assert article.source_categories == ("World",)


def test_invalid_publication_date_is_optional_metadata_and_article_is_retained() -> None:
    source = load_sources(ROOT / "config" / "sources.yaml")[0]
    feed = FetchedFeed(
        source,
        datetime(2026, 9, 29, 10, tzinfo=UTC),
        (
            RawProviderEntry(
                "rss",
                {
                    "title": "Valid required fields despite invalid date",
                    "link": "https://example.com/story/invalid-date",
                    "pubDate": "not a valid date",
                },
            ),
        ),
    )

    result = normalize_feed(feed)

    assert len(result.articles) == 1
    assert result.articles[0].title == "Valid required fields despite invalid date"
    assert result.articles[0].published_at is None
    assert result.issues == (
        NormalizationIssue(
            0, "published_at", "Invalid publication timestamp; kept as unknown"
        ),
    )


def test_missing_required_field_rejects_only_its_entry() -> None:
    source = load_sources(ROOT / "config" / "sources.yaml")[0]
    feed = FetchedFeed(
        source,
        datetime(2026, 9, 29, 10, tzinfo=UTC),
        (
            RawProviderEntry(
                "rss",
                {"link": "https://example.com/story/missing-title", "pubDate": "invalid"},
            ),
            RawProviderEntry(
                "rss",
                {
                    "title": "Valid sibling article",
                    "link": "https://example.com/story/valid-sibling",
                    "pubDate": "Tue, 29 Sep 2026 08:30:00 GMT",
                },
            ),
        ),
    )

    result = normalize_feed(feed)

    assert [article.title for article in result.articles] == ["Valid sibling article"]
    assert result.articles[0].published_at == datetime(2026, 9, 29, 8, 30, tzinfo=UTC)
    assert result.issues == (
        NormalizationIssue(0, "title", "Missing required title; entry rejected"),
    )


def test_bad_optional_categories_and_empty_description_do_not_reject_valid_entry() -> None:
    source = load_sources(ROOT / "config" / "sources.yaml")[0]
    feed = FetchedFeed(
        source,
        datetime(2026, 9, 29, 10, tzinfo=UTC),
        (
            RawProviderEntry(
                "rss",
                {
                    "title": "Valid article with malformed optional metadata",
                    "link": "https://example.com/story/optional-metadata",
                    "pubDate": "Tue, 29 Sep 2026 08:30:00 GMT",
                    "description": "   ",
                    "categories": "not-a-category-list",
                    "language": "en",
                },
            ),
            RawProviderEntry(
                "rss",
                {
                    "title": "Valid sibling remains available",
                    "link": "https://example.com/story/sibling",
                    "description": "Useful source description.",
                    "categories": ("Technology",),
                },
            ),
        ),
    )

    result = normalize_feed(feed)

    assert len(result.articles) == 2
    first, sibling = result.articles
    assert first.description is None
    assert first.source_categories == ()
    assert sibling.title == "Valid sibling remains available"
    assert sibling.description == "Useful source description."
    assert sibling.source_categories == ("Technology",)
    assert result.issues == (
        NormalizationIssue(0, "source_categories", "Invalid categories; omitted"),
    )


def test_embedded_credentials_in_one_article_url_reject_only_that_entry() -> None:
    source = load_sources(ROOT / "config" / "sources.yaml")[0]
    feed = FetchedFeed(
        source,
        datetime(2026, 9, 29, 10, tzinfo=UTC),
        (
            RawProviderEntry(
                "rss",
                {
                    "title": "Credential-bearing URL must not be retained",
                    "link": "https://feed-user:private-password@example.com/story",
                },
            ),
            RawProviderEntry(
                "rss",
                {
                    "title": "Valid sibling article",
                    "link": "https://example.com/story/safe",
                },
            ),
        ),
    )

    result = normalize_feed(feed)

    assert [article.title for article in result.articles] == ["Valid sibling article"]
    assert result.issues[0].field == "url"
    assert "embedded credentials" in result.issues[0].message
    assert "private-password" not in str(result.issues)


def test_content_hash_is_deterministic_and_excludes_generated_id_and_fetch_time() -> None:
    first = normalize_feed(
        fetched_feed("bbc_world_sample.rss", datetime(2026, 9, 29, 10, tzinfo=UTC))
    ).articles[0]
    second = normalize_feed(
        fetched_feed("bbc_world_sample.rss", datetime(2026, 9, 30, 10, tzinfo=UTC))
    ).articles[0]

    assert first.article_id != second.article_id
    assert first.retrieved_at != second.retrieved_at
    assert first.content_hash == second.content_hash
    assert len(first.content_hash) == 64


def test_canonicalization_removes_only_known_tracking_parameters_and_fragment() -> None:
    feed = fetched_feed("bbc_world_sample.rss", datetime(2026, 9, 29, 10, tzinfo=UTC))
    article = normalize_feed(feed).articles[0]

    assert article.url.endswith("utm_source=rss&id=1#top")
    assert article.canonical_url.endswith("?id=1")
