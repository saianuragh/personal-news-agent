"""Conservative, deterministic grouping of canonical articles into stories."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from app.models.article import Article
from app.processing.normalize import canonicalize_url

DuplicateReason = Literal["exact_canonical_url", "normalized_url", "content_hash"]
DuplicateMethod = Literal["none", "exact_url", "normalized_url", "content_hash", "mixed"]

_REASON_ORDER: dict[DuplicateReason, int] = {
    "exact_canonical_url": 0,
    "normalized_url": 1,
    "content_hash": 2,
}


@dataclass(frozen=True, slots=True)
class DuplicateMatch:
    """A directly matched pair and every deterministic signal supporting it."""

    article_ids: tuple[UUID, UUID]
    reasons: tuple[DuplicateReason, ...]


@dataclass(frozen=True, slots=True)
class Story:
    """A group of source Articles with explicit retained-article provenance."""

    story_id: UUID
    members: tuple[Article, ...]
    representative_article_id: UUID
    first_seen_at: datetime
    latest_published_at: datetime | None
    duplicate_method: DuplicateMethod
    duplicate_matches: tuple[DuplicateMatch, ...]

    @property
    def retained_article(self) -> Article:
        """Return the selected representative while keeping every member."""
        return next(
            article
            for article in self.members
            if article.article_id == self.representative_article_id
        )


@dataclass(frozen=True, slots=True)
class DeduplicationResult:
    """All stories, including singletons, produced by one deduplication pass."""

    stories: tuple[Story, ...]


def deduplicate_articles(articles: list[Article] | tuple[Article, ...]) -> DeduplicationResult:
    """Group only articles connected by exact, normalized URL, or hash equality.

    The representative is selected by earliest known publication timestamp.
    Articles without a publication timestamp sort after those with a known
    time. Ties are broken by case-folded source ID, canonical URL, then UUID.
    No article is discarded: each story retains its full member list and
    pairwise match reasons.
    """
    article_list = tuple(articles)
    if not article_list:
        return DeduplicationResult(())

    pair_reasons = _find_duplicate_pairs(article_list)
    parent = list(range(len(article_list)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left = find(left)
        root_right = find(right)
        if root_left != root_right:
            parent[max(root_left, root_right)] = min(root_left, root_right)

    for left, right in pair_reasons:
        union(left, right)

    grouped_indices: dict[int, list[int]] = defaultdict(list)
    for index in range(len(article_list)):
        grouped_indices[find(index)].append(index)

    stories = [
        _make_story(article_list, indices, pair_reasons)
        for indices in grouped_indices.values()
    ]
    stories.sort(key=lambda story: _article_order_key(story.retained_article))
    return DeduplicationResult(tuple(stories))


def _find_duplicate_pairs(
    articles: tuple[Article, ...],
) -> dict[tuple[int, int], tuple[DuplicateReason, ...]]:
    exact_url_buckets: dict[str, list[int]] = defaultdict(list)
    normalized_url_buckets: dict[str, list[int]] = defaultdict(list)
    content_hash_buckets: dict[str, list[int]] = defaultdict(list)

    for index, article in enumerate(articles):
        exact_url_buckets[article.canonical_url].append(index)
        normalized_url_buckets[canonicalize_url(article.url)].append(index)
        normalized_url_buckets[canonicalize_url(article.canonical_url)].append(index)
        content_hash_buckets[article.content_hash].append(index)

    pair_reasons: dict[tuple[int, int], set[DuplicateReason]] = defaultdict(set)
    _add_bucket_pairs(pair_reasons, exact_url_buckets, "exact_canonical_url")
    _add_bucket_pairs(pair_reasons, normalized_url_buckets, "normalized_url")
    _add_bucket_pairs(pair_reasons, content_hash_buckets, "content_hash")

    result: dict[tuple[int, int], tuple[DuplicateReason, ...]] = {}
    for pair, reasons in pair_reasons.items():
        # Exact canonical equality already explains normalized equivalence.
        if "exact_canonical_url" in reasons:
            reasons.discard("normalized_url")
        result[pair] = tuple(sorted(reasons, key=_REASON_ORDER.__getitem__))
    return result


def _add_bucket_pairs(
    pair_reasons: dict[tuple[int, int], set[DuplicateReason]],
    buckets: dict[str, list[int]],
    reason: DuplicateReason,
) -> None:
    for indices in buckets.values():
        unique_indices = sorted(set(indices))
        for offset, left in enumerate(unique_indices):
            for right in unique_indices[offset + 1 :]:
                pair_reasons[(left, right)].add(reason)


def _make_story(
    articles: tuple[Article, ...],
    indices: list[int],
    pair_reasons: dict[tuple[int, int], tuple[DuplicateReason, ...]],
) -> Story:
    members = tuple(sorted((articles[index] for index in indices), key=_article_order_key))
    retained = members[0]
    member_indices = set(indices)
    matches = tuple(
        DuplicateMatch(
            article_ids=tuple(
                sorted(
                    (articles[left].article_id, articles[right].article_id),
                    key=str,
                )
            ),
            reasons=reasons,
        )
        for (left, right), reasons in sorted(pair_reasons.items())
        if left in member_indices and right in member_indices
    )

    methods = {reason for match in matches for reason in match.reasons}
    if not methods:
        duplicate_method: DuplicateMethod = "none"
    elif methods == {"exact_canonical_url"}:
        duplicate_method = "exact_url"
    elif methods == {"normalized_url"}:
        duplicate_method = "normalized_url"
    elif methods == {"content_hash"}:
        duplicate_method = "content_hash"
    else:
        duplicate_method = "mixed"

    publication_times = [article.published_at for article in members if article.published_at]
    latest_published_at = max(publication_times) if publication_times else None
    story_identity = f"personal-news-story:{retained.canonical_url}"
    story_id = uuid5(NAMESPACE_URL, story_identity)
    return Story(
        story_id=story_id,
        members=members,
        representative_article_id=retained.article_id,
        first_seen_at=min(article.retrieved_at for article in members).astimezone(UTC),
        latest_published_at=latest_published_at,
        duplicate_method=duplicate_method,
        duplicate_matches=matches,
    )


def _article_order_key(article: Article) -> tuple[bool, datetime, str, str, str]:
    published_at = article.published_at
    return (
        published_at is None,
        published_at or datetime.max.replace(tzinfo=UTC),
        article.source_id.casefold(),
        article.canonical_url,
        str(article.article_id),
    )
