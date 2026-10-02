"""Deterministic, offline tests for conservative article deduplication."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from app.config import SourceConfig
from app.models.article import Article
from app.processing.deduplicate import deduplicate_articles
from app.processing.normalize import normalize_feed
from app.sources.base import FetchedFeed, RawProviderEntry

FETCHED_AT = datetime(2026, 9, 30, 8, tzinfo=UTC)


def make_article(
    *,
    title: str = "A distinct news story",
    url: str | None = None,
    canonical_url: str | None = None,
    content_hash: str | None = None,
    publisher: str = "Example News",
    source_id: str = "example-news",
    published_at: datetime | None = datetime(2026, 9, 30, 7, tzinfo=UTC),
) -> Article:
    article_id = uuid4()
    article_url = url or f"https://example.com/story/{article_id}"
    article_canonical_url = canonical_url or article_url
    return Article(
        article_id=article_id,
        source_id=source_id,
        publisher=publisher,
        title=title,
        url=article_url,
        canonical_url=article_canonical_url,
        retrieved_at=FETCHED_AT,
        content_hash=content_hash or sha256(str(article_id).encode()).hexdigest(),
        published_at=published_at,
    )


def only_story(articles: list[Article]):
    result = deduplicate_articles(articles)
    assert len(result.stories) == 1
    return result.stories[0]


def test_exact_canonical_url_duplicates_keep_all_members_and_reason() -> None:
    first = make_article(
        title="First title",
        url="https://publisher-a.example/story",
        canonical_url="https://publisher-a.example/story",
        source_id="publisher-a",
        publisher="Publisher A",
    )
    second = make_article(
        title="Updated headline",
        url="https://publisher-a.example/story?utm_source=newsletter",
        canonical_url="https://publisher-a.example/story",
        source_id="publisher-b",
        publisher="Publisher B",
    )

    story = only_story([second, first])

    assert {article.article_id for article in story.members} == {
        first.article_id,
        second.article_id,
    }
    assert story.duplicate_method == "exact_url"
    assert len(story.duplicate_matches) == 1
    match = story.duplicate_matches[0]
    assert set(match.article_ids) == {first.article_id, second.article_id}
    assert match.reasons == ("exact_canonical_url",)


def test_tracking_and_url_case_equivalence_is_reported_as_normalized_url() -> None:
    first_url = "https://EXAMPLE.com/news?id=5&utm_source=feed#top"
    second_url = "https://example.COM/news?id=5&utm_medium=email#story"
    first = make_article(url=first_url, canonical_url=first_url)
    second = make_article(url=second_url, canonical_url=second_url)

    story = only_story([first, second])

    assert story.duplicate_method == "normalized_url"
    assert story.duplicate_matches[0].reasons == ("normalized_url",)


def test_source_tracking_duplicates_keep_provenance_and_cross_source_story_separate() -> None:
    def normalized_article(
        *, source_id: str, publisher: str, url: str, article_id: int
    ) -> Article:
        source = SourceConfig(
            source_id=source_id,
            enabled=True,
            name=publisher,
            kind="rss_atom",
            endpoint=f"https://{source_id}.example/feed.xml",
            categories=(),
            quality_weight=0.8,
            timeout_seconds=5,
            request_interval_seconds=1,
        )
        feed = FetchedFeed(
            source,
            FETCHED_AT,
            (
                RawProviderEntry(
                    "rss",
                    {
                        "title": "Major AI breakthrough announced",
                        "link": url,
                        "pubDate": "Tue, 29 Sep 2026 07:00:00 GMT",
                        "description": "Researchers announced a major AI breakthrough.",
                    },
                ),
            ),
        )
        result = normalize_feed(feed)
        assert len(result.articles) == 1
        return replace(result.articles[0], article_id=UUID(int=article_id))

    source_a_first = normalized_article(
        source_id="source_a",
        publisher="Publisher A",
        url="https://example.com/article?id=123&utm_source=rss",
        article_id=1,
    )
    source_b = normalized_article(
        source_id="source_b",
        publisher="Publisher B",
        url="https://example.org/article?id=456",
        article_id=2,
    )
    source_b_same_url = normalized_article(
        source_id="source_b",
        publisher="Publisher B",
        url="https://example.com/article?id=123&utm_source=rss",
        article_id=4,
    )
    source_a_second = normalized_article(
        source_id="source_a",
        publisher="Publisher A",
        url="https://example.com/article?id=123&utm_medium=email#section",
        article_id=3,
    )

    assert source_a_first.canonical_url == source_a_second.canonical_url
    assert source_a_first.canonical_url == "https://example.com/article?id=123"
    assert source_a_first.content_hash == source_a_second.content_hash
    assert source_a_first.content_hash != source_b.content_hash
    assert source_a_first.content_hash != source_b_same_url.content_hash

    cross_source_result = deduplicate_articles([source_a_first, source_b_same_url])
    assert len(cross_source_result.stories) == 1
    cross_source_story = cross_source_result.stories[0]
    assert {article.source_id for article in cross_source_story.members} == {
        "source_a",
        "source_b",
    }
    assert cross_source_story.duplicate_method == "exact_url"
    assert cross_source_story.duplicate_matches[0].reasons == ("exact_canonical_url",)

    result = deduplicate_articles([source_a_first, source_b, source_a_second])
    source_a_story = next(story for story in result.stories if len(story.members) == 2)
    source_b_story = next(story for story in result.stories if len(story.members) == 1)

    assert len(result.stories) == 2
    assert {article.article_id for article in source_a_story.members} == {
        source_a_first.article_id,
        source_a_second.article_id,
    }
    assert {article.url for article in source_a_story.members} == {
        "https://example.com/article?id=123&utm_source=rss",
        "https://example.com/article?id=123&utm_medium=email#section",
    }
    assert source_a_story.representative_article_id == source_a_first.article_id
    assert source_a_story.retained_article == source_a_first
    assert source_a_story.story_id == uuid5(
        NAMESPACE_URL,
        "personal-news-story:https://example.com/article?id=123",
    )
    assert source_a_story.duplicate_matches[0].article_ids == (
        source_a_first.article_id,
        source_a_second.article_id,
    )
    assert source_a_story.duplicate_matches[0].reasons == (
        "exact_canonical_url",
        "content_hash",
    )
    assert source_a_story.duplicate_method == "mixed"
    assert source_b_story.members == (source_b,)
    assert source_b_story.duplicate_method == "none"
    assert source_b_story.duplicate_matches == ()

    reversed_result = deduplicate_articles([source_a_second, source_b, source_a_first])
    reversed_source_a = next(story for story in reversed_result.stories if len(story.members) == 2)
    assert reversed_source_a.story_id == source_a_story.story_id
    assert reversed_source_a.representative_article_id == source_a_story.representative_article_id


def test_identical_content_hash_is_a_deterministic_duplicate_signal() -> None:
    shared_hash = sha256(b"same normalized source content").hexdigest()
    first = make_article(content_hash=shared_hash, url="https://example.com/a")
    second = make_article(content_hash=shared_hash, url="https://example.com/b")

    story = only_story([first, second])

    assert story.duplicate_method == "content_hash"
    assert story.duplicate_matches[0].reasons == ("content_hash",)


def test_similar_titles_with_distinct_urls_and_hashes_remain_separate() -> None:
    first = make_article(
        title="Central bank holds interest rates steady",
        url="https://example.com/rate-decision",
    )
    second = make_article(
        title="Central bank holds interest rates steady",
        url="https://example.com/another-rate-decision",
    )

    result = deduplicate_articles([first, second])

    assert len(result.stories) == 2
    assert all(story.duplicate_method == "none" for story in result.stories)


def test_different_publishers_reporting_similar_stories_are_not_merged() -> None:
    first = make_article(
        title="A new telescope discovers a distant planet",
        url="https://publisher-a.example/planet",
        publisher="Publisher A",
        source_id="publisher-a",
    )
    second = make_article(
        title="New telescope finds distant planet",
        url="https://publisher-b.example/planet",
        publisher="Publisher B",
        source_id="publisher-b",
    )

    result = deduplicate_articles([first, second])

    assert len(result.stories) == 2


def test_cross_publisher_entries_merge_only_on_existing_exact_url_signal() -> None:
    first = make_article(
        title="International leaders announce a climate agreement",
        url="https://wire.example/story/climate-agreement",
        canonical_url="https://wire.example/story/climate-agreement",
        publisher="Wire One",
        source_id="wire-one",
    )
    second = make_article(
        title="Leaders reach climate deal, publisher reports",
        url="https://wire.example/story/climate-agreement?utm_source=feed",
        canonical_url="https://wire.example/story/climate-agreement",
        publisher="Wire Two",
        source_id="wire-two",
    )
    assert first.content_hash != second.content_hash

    result = deduplicate_articles([first, second])

    assert len(result.stories) == 1
    story = result.stories[0]
    assert {article.source_id for article in story.members} == {"wire-one", "wire-two"}
    assert story.duplicate_method == "exact_url"
    assert story.duplicate_matches[0].reasons == ("exact_canonical_url",)


def test_multiple_duplicates_form_one_story_with_pairwise_provenance() -> None:
    canonical_url = "https://example.com/multi-source-story"
    articles = [
        make_article(
            url=f"https://example.com/multi-source-story?utm_source={source_id}",
            canonical_url=canonical_url,
            source_id=source_id,
            publisher=source_id.title(),
            published_at=FETCHED_AT - timedelta(minutes=minutes_ago),
        )
        for source_id, minutes_ago in (("wire-a", 20), ("wire-b", 10), ("wire-c", 5))
    ]

    story = only_story(list(reversed(articles)))

    assert len(story.members) == 3
    assert len(story.duplicate_matches) == 3
    assert all(match.reasons == ("exact_canonical_url",) for match in story.duplicate_matches)
    assert story.first_seen_at == FETCHED_AT
    assert story.latest_published_at == FETCHED_AT - timedelta(minutes=5)


def test_earliest_known_publication_is_retained_independent_of_input_order() -> None:
    earliest = make_article(
        url="https://example.com/shared",
        canonical_url="https://example.com/shared",
        source_id="z-publisher",
        published_at=FETCHED_AT - timedelta(hours=2),
    )
    later = make_article(
        url="https://example.com/shared?utm_source=other",
        canonical_url="https://example.com/shared",
        source_id="a-publisher",
        published_at=FETCHED_AT - timedelta(hours=1),
    )
    unknown = make_article(
        url="https://example.com/shared?utm_source=unknown",
        canonical_url="https://example.com/shared",
        source_id="0-unknown",
        published_at=None,
    )

    story_forward = only_story([later, unknown, earliest])
    story_reverse = only_story([earliest, unknown, later])

    assert story_forward.retained_article == earliest
    assert story_reverse.retained_article == earliest
    assert story_forward.story_id == story_reverse.story_id


def test_ties_use_source_id_then_canonical_url_then_article_uuid() -> None:
    later_source = make_article(
        url="https://example.com/shared",
        canonical_url="https://example.com/shared",
        source_id="z-source",
    )
    first_source = make_article(
        url="https://example.com/shared?utm_source=first",
        canonical_url="https://example.com/shared",
        source_id="a-source",
    )

    story = only_story([later_source, first_source])

    assert story.retained_article == first_source


def test_empty_and_single_article_inputs_are_preserved() -> None:
    assert deduplicate_articles([]).stories == ()

    article = make_article()
    result = deduplicate_articles([article])

    assert len(result.stories) == 1
    assert result.stories[0].members == (article,)
    assert result.stories[0].retained_article == article
    assert result.stories[0].duplicate_method == "none"
    assert result.stories[0].duplicate_matches == ()


def test_known_content_hash_and_url_match_report_all_supporting_reasons() -> None:
    shared_hash = sha256(b"same content and same canonical URL").hexdigest()
    canonical_url = "https://example.com/same"
    first = make_article(
        url=canonical_url,
        canonical_url=canonical_url,
        content_hash=shared_hash,
    )
    second = make_article(
        url=f"{canonical_url}?utm_source=feed",
        canonical_url=canonical_url,
        content_hash=shared_hash,
    )

    story = only_story([first, second])

    assert story.duplicate_matches[0].reasons == ("exact_canonical_url", "content_hash")
    assert story.duplicate_method == "mixed"


@pytest.mark.parametrize("article_count", [0, 1])
def test_story_output_count_for_small_input(article_count: int) -> None:
    articles = [make_article() for _ in range(article_count)]

    result = deduplicate_articles(articles)

    assert len(result.stories) == article_count
