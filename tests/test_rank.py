"""Offline tests for deterministic, explainable story ranking."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
import yaml
from app.config import SourceConfig
from app.models.article import Article
from app.processing.categorize import categorize_story
from app.processing.deduplicate import deduplicate_articles
from app.processing.rank import RankingConfig, load_ranking_config, rank_stories
from app.processing.select import SelectionConfig, load_selection_config, select_stories

AS_OF = datetime(2026, 9, 30, 12, tzinfo=UTC)


def source_config(
    source_id: str = "source-a",
    name: str = "Publisher A",
    quality_weight: float = 0.8,
) -> SourceConfig:
    return SourceConfig(
        source_id=source_id,
        enabled=True,
        name=name,
        kind="rss_atom",
        endpoint=f"https://{source_id}.example/feed.xml",
        categories=(),
        quality_weight=quality_weight,
        timeout_seconds=10,
        request_interval_seconds=60,
    )


def article(
    *,
    title: str = "Technology company releases a new processor",
    url: str | None = None,
    source_id: str = "source-a",
    publisher: str = "Publisher A",
    published_at: datetime | None = AS_OF - timedelta(hours=12),
    description: str | None = None,
    source_categories: tuple[str, ...] = (),
) -> Article:
    article_id = uuid4()
    article_url = url or f"https://example.com/{article_id}"
    return Article(
        article_id=article_id,
        source_id=source_id,
        publisher=publisher,
        title=title,
        url=article_url,
        canonical_url=article_url,
        retrieved_at=AS_OF,
        content_hash=sha256(str(article_id).encode()).hexdigest(),
        published_at=published_at,
        description=description,
        source_categories=source_categories,
    )


def categorized_story(*articles: Article):
    deduplicated = deduplicate_articles(list(articles))
    assert len(deduplicated.stories) == 1
    return categorize_story(deduplicated.stories[0])


def rank_one(
    story,
    configs: tuple[SourceConfig, ...] | list[SourceConfig] = (),
    *,
    as_of: datetime = AS_OF,
    config: RankingConfig | None = None,
):
    return rank_stories([story], configs, as_of=as_of, config=config)[0]


def test_fresh_story_scores_above_an_older_story() -> None:
    fresh = categorized_story(article(published_at=AS_OF - timedelta(hours=1)))
    older = categorized_story(article(published_at=AS_OF - timedelta(hours=36)))
    sources = [source_config()]

    fresh_rank = rank_one(fresh, sources)
    older_rank = rank_one(older, sources)

    assert fresh_rank.score > older_rank.score
    assert fresh_rank.score_breakdown[0].value > older_rank.score_breakdown[0].value


def test_freshness_is_full_at_reference_time_and_zero_at_or_after_window() -> None:
    config = load_ranking_config()
    window = timedelta(hours=config.freshness_window_hours)
    stories = (
        categorized_story(article(published_at=AS_OF)),
        categorized_story(article(published_at=AS_OF - window)),
        categorized_story(article(published_at=AS_OF - window - timedelta(seconds=1))),
        categorized_story(article(published_at=AS_OF - window + timedelta(seconds=1))),
        categorized_story(article(published_at=AS_OF + timedelta(seconds=1))),
        categorized_story(article(published_at=None)),
    )

    values = [
        _signal(rank_one(story, config=config), "freshness").value for story in stories
    ]

    assert values[:3] == [100.0, 0.0, 0.0]
    assert values[3] == pytest.approx(
        round(100.0 / window.total_seconds(), 4)
    )
    assert values[3] > 0.0
    assert values[4:] == [0.0, 0.0]


def test_loading_changed_yaml_freshness_window_changes_ranking_score(tmp_path) -> None:
    default_config = load_ranking_config()
    config_values = _default_config_mapping()
    config_values["freshness_window_hours"] = 24
    custom_path = tmp_path / "ranking.yaml"
    custom_path.write_text(yaml.safe_dump(config_values), encoding="utf-8")
    custom_config = load_ranking_config(custom_path)
    story = categorized_story(article(published_at=AS_OF - timedelta(hours=12)))

    default_value = _signal(rank_one(story, config=default_config), "freshness").value
    custom_signal = _signal(rank_one(story, config=custom_config), "freshness")

    assert custom_config.freshness_window_hours == 24
    assert default_value == pytest.approx(
        100.0 * (1.0 - 12.0 / default_config.freshness_window_hours)
    )
    assert custom_signal.value == pytest.approx(50.0)
    assert "freshness_window_hours=24" in custom_signal.evidence
    assert default_value != custom_signal.value


def test_sixty_hour_story_has_freshness_but_story_older_than_configured_window_does_not() -> None:
    config = load_ranking_config()
    inside_window = categorized_story(
        article(published_at=AS_OF - timedelta(hours=60))
    )
    outside_window = categorized_story(
        article(published_at=AS_OF - timedelta(hours=73))
    )

    inside_value = _signal(rank_one(inside_window, config=config), "freshness").value
    outside_value = _signal(rank_one(outside_window, config=config), "freshness").value

    assert inside_value == pytest.approx(
        100.0 * (1.0 - 60.0 / config.freshness_window_hours), abs=0.0001
    )
    assert inside_value > 0.0
    assert outside_value == 0.0


def test_source_quality_weight_changes_source_quality_component() -> None:
    story = categorized_story(article())
    source = source_config(quality_weight=0.9)
    low_quality = replace(source, quality_weight=0.2)

    high_rank = rank_one(story, [source])
    low_rank = rank_one(story, [low_quality])

    assert _signal(high_rank, "source_quality").value == 90.0
    assert _signal(low_rank, "source_quality").value == 20.0
    assert high_rank.score > low_rank.score


def test_independent_publishers_increase_corroboration_component() -> None:
    url = "https://example.com/shared-story"
    story = categorized_story(
        article(url=url, source_id="source-a", publisher="Publisher A"),
        article(
            url=url,
            source_id="source-b",
            publisher="Publisher B",
            published_at=AS_OF - timedelta(hours=10),
        ),
    )
    sources = [
        source_config("source-a", "Publisher A", 0.8),
        source_config("source-b", "Publisher B", 0.6),
    ]

    ranked = rank_one(story, sources)

    assert _signal(ranked, "corroboration").value == 50.0
    assert "distinct_publishers=2" in _signal(ranked, "corroboration").evidence
    assert ranked.story.members == story.story.members


def test_category_evidence_strength_affects_score() -> None:
    headline_match = categorized_story(article(title="New technology release"))
    source_hint = categorized_story(
        article(
            title="Headline with no category term",
            source_id="source-a",
            source_categories=("Technology",),
        )
    )
    rules = load_ranking_config()

    headline_rank = rank_one(headline_match, [source_config()], config=rules)
    tag_rank = rank_one(source_hint, [source_config()], config=rules)

    assert _signal(headline_rank, "category_relevance").value == 100.0
    assert _signal(tag_rank, "category_relevance").value == 100.0
    assert (
        _signal(headline_rank, "category_relevance").evidence
        != _signal(tag_rank, "category_relevance").evidence
    )


def test_source_id_category_hint_has_lower_relevance_than_headline_match() -> None:
    headline_match = categorized_story(article(title="Technology company reports news"))
    source_hint = categorized_story(article(title="General news", source_id="bbc_world"))
    bbc = source_config("bbc_world", "BBC News", 1.0)

    headline_rank = rank_one(headline_match, [source_config()])
    source_rank = rank_one(source_hint, [bbc])

    assert _signal(headline_rank, "category_relevance").value == 100.0
    assert _signal(source_rank, "category_relevance").value == 75.0


def test_missing_optional_metadata_gets_no_fabricated_freshness_credit() -> None:
    story = categorized_story(
        article(
            title="General story",
            published_at=None,
            description=None,
            source_categories=("Technology",),
        )
    )

    ranked = rank_one(story, [])

    assert _signal(ranked, "freshness").value == 0.0
    assert "publication time unknown" in _signal(ranked, "freshness").evidence[0]
    assert _signal(ranked, "source_quality").value == 0.0
    assert _signal(ranked, "source_quality").evidence[0].startswith("no configured")
    assert ranked.score >= 0.0


def test_missing_publication_and_source_weight_leave_other_signals_explainable() -> None:
    corroborated_story = categorized_story(
        article(
            title="Technology company reports a new processor",
            url="https://example.com/corroborated",
            source_id="source-a",
            publisher="Publisher A",
            published_at=AS_OF - timedelta(hours=1),
            source_categories=("Technology",),
        ),
        article(
            title="Technology company reports a new processor",
            url="https://example.com/corroborated",
            source_id="source-b",
            publisher="Publisher B",
            published_at=AS_OF - timedelta(hours=1),
            source_categories=("Technology",),
        ),
    )
    missing_signal_story = categorized_story(
        article(
            title="General report",
            url="https://example.com/unknown-source",
            source_id="unconfigured-source",
            publisher="Unconfigured Publisher",
            published_at=None,
            source_categories=("Technology",),
        )
    )
    config = load_ranking_config()
    ranked = rank_stories(
        [missing_signal_story, corroborated_story],
        [
            source_config("source-a", "Publisher A", 0.9),
            source_config("source-b", "Publisher B", 0.8),
        ],
        as_of=AS_OF,
        config=config,
    )

    assert [item.story.story_id for item in ranked] == [
        corroborated_story.story.story_id,
        missing_signal_story.story.story_id,
    ]
    missing_ranked = ranked[1]
    signals = {signal.name: signal for signal in missing_ranked.score_breakdown}
    assert tuple(signals) == (
        "freshness",
        "source_quality",
        "category_relevance",
        "corroboration",
    )
    assert signals["freshness"].value == 0.0
    assert "publication time unknown" in signals["freshness"].evidence[0]
    assert missing_ranked.story.latest_published_at is None
    assert signals["source_quality"].value == 0.0
    assert signals["source_quality"].evidence[0].startswith("no configured")
    assert "unconfigured-source" in signals["source_quality"].evidence[1]
    assert signals["category_relevance"].value == 100.0
    assert signals["category_relevance"].contribution == 20.0
    assert signals["corroboration"].value == 0.0
    assert "distinct_publishers=1" in signals["corroboration"].evidence
    assert {signal.name: signal.weight for signal in signals.values()} == dict(config.weights)
    assert missing_ranked.score == pytest.approx(
        sum(signal.contribution for signal in signals.values())
    )
    assert missing_ranked.rank == 2
    assert missing_ranked.category_ranks[0].category == "Technology"
    assert missing_ranked.category_ranks[0].rank == 2
    assert missing_ranked.selection_status == "candidate"
    assert "at least one category" in missing_ranked.selection_reason

    corroborated_signals = {
        signal.name: signal for signal in ranked[0].score_breakdown
    }
    assert corroborated_signals["source_quality"].value == 85.0
    assert corroborated_signals["corroboration"].value == 50.0
    assert "distinct_publishers=2" in corroborated_signals["corroboration"].evidence


def test_score_breakdown_retains_weights_contributions_and_evidence() -> None:
    story = categorized_story(
        article(
            title="Technology company releases a processor",
            source_categories=("Technology",),
        )
    )
    config = load_ranking_config()
    ranked = rank_one(story, [source_config(quality_weight=0.8)], config=config)

    assert ranked.score == 73.33
    assert ranked.rank == 1
    assert ranked.category_ranks[0].category == "Technology"
    assert ranked.category_ranks[0].rank == 1
    assert ranked.selection_status == "candidate"
    assert ranked.selection_reason == "Ranked candidate with at least one category."
    for signal in ranked.score_breakdown:
        assert signal.weight == dict(config.weights)[signal.name]
        assert signal.contribution == pytest.approx(signal.value * signal.weight)
        assert signal.evidence


def test_score_breakdown_matches_weighted_formula() -> None:
    story = categorized_story(article(title="Technology company releases a processor"))
    config = load_ranking_config()
    ranked = rank_one(story, [source_config(quality_weight=0.8)], config=config)
    signals = {signal.name: signal for signal in ranked.score_breakdown}
    configured_weights = dict(config.weights)

    assert set(signals) == set(configured_weights)
    assert signals["freshness"].value == pytest.approx(
        100.0 * (1.0 - 12.0 / config.freshness_window_hours)
    )
    assert signals["source_quality"].value == 80.0
    assert signals["category_relevance"].value == 100.0
    assert signals["corroboration"].value == 0.0
    for name, signal in signals.items():
        assert signal.weight == configured_weights[name]
        assert signal.contribution == pytest.approx(signal.value * configured_weights[name])
    expected_score = round(sum(signal.contribution for signal in signals.values()), 2)
    assert ranked.score == pytest.approx(expected_score)


def test_equal_scores_break_ties_by_publication_time_then_url() -> None:
    older = categorized_story(
        article(
            title="Technology item",
            url="https://example.com/a",
            published_at=AS_OF - timedelta(hours=74),
        )
    )
    newer = categorized_story(
        article(
            title="Technology item",
            url="https://example.com/z",
            published_at=AS_OF - timedelta(hours=73),
        )
    )
    tied_url_first = categorized_story(
        article(
            title="Technology item",
            url="https://example.com/b",
            published_at=AS_OF - timedelta(hours=73),
        )
    )

    ranked = rank_stories([older, newer, tied_url_first], as_of=AS_OF)

    assert ranked[0].story.retained_article.url == "https://example.com/b"
    assert ranked[1].story.retained_article.url == "https://example.com/z"
    assert ranked[2].story.retained_article.url == "https://example.com/a"
    assert len({item.score for item in ranked}) == 1


def test_equal_score_and_casefolded_url_tie_breaks_by_story_uuid() -> None:
    upper_url_story = categorized_story(
        article(
            title="Technology item",
            url="https://example.com/Story",
            published_at=AS_OF - timedelta(hours=60),
        )
    )
    lower_url_story = categorized_story(
        article(
            title="Technology item",
            url="https://example.com/story",
            published_at=AS_OF - timedelta(hours=60),
        )
    )

    first = rank_stories(
        [upper_url_story, lower_url_story], as_of=AS_OF
    )
    repeated = rank_stories(
        [lower_url_story, upper_url_story], as_of=AS_OF
    )

    expected_story_ids = sorted(
        (str(upper_url_story.story.story_id), str(lower_url_story.story.story_id))
    )
    assert len({item.score for item in first}) == 1
    assert [str(item.story.story_id) for item in first] == expected_story_ids
    assert [str(item.story.story_id) for item in repeated] == expected_story_ids


def test_repeated_ranking_is_deterministic_and_preserves_provenance() -> None:
    first = categorized_story(article(title="Technology product release"))
    second = categorized_story(article(title="India parliament debates new law"))

    ranked_first = rank_stories([first, second], [source_config()], as_of=AS_OF)
    ranked_second = rank_stories([second, first], [source_config()], as_of=AS_OF)

    by_story_id = {item.story.story_id: item for item in ranked_first}
    reordered_by_id = {item.story.story_id: item for item in ranked_second}
    assert {
        key: (value.score, value.rank, value.score_breakdown) for key, value in by_story_id.items()
    } == {
        key: (value.score, value.rank, value.score_breakdown)
        for key, value in reordered_by_id.items()
    }
    assert all(item.story is item.categorized_story.story for item in ranked_first)
    assert all(item.story.members for item in ranked_first)


def test_empty_input_and_single_story() -> None:
    assert rank_stories([], as_of=AS_OF) == ()

    story = categorized_story(article())
    ranked = rank_stories([story], as_of=AS_OF)

    assert len(ranked) == 1
    assert ranked[0].rank == 1
    assert ranked[0].category_ranks[0].rank == 1


def test_selection_enforces_limits_and_covers_sections_deterministically() -> None:
    stories = [
        categorized_story(article(title="India parliament passes a new bill")),
        categorized_story(
            article(
                title="World leaders meet for a summit",
                url="https://example.com/world",
            )
        ),
        categorized_story(article(title="Technology company releases processor")),
        categorized_story(
            article(
                title="Technology company releases second processor",
                url="https://example.com/technology-2",
                published_at=AS_OF - timedelta(hours=2),
            )
        ),
        categorized_story(article(title="Local bridge reopens", url="https://example.com/local")),
    ]
    ranked = rank_stories(stories, [source_config()], as_of=AS_OF)
    config = SelectionConfig(max_total_stories=3, max_per_category=1)

    selected = select_stories(ranked, config)
    reordered = select_stories(tuple(reversed(ranked)), config)

    assert len(selected) == 3
    assert [item.story.story_id for item in selected] == [
        item.story.story_id for item in reordered
    ]
    assert [item.rank for item in selected] == sorted(item.rank for item in selected)
    assert all(item.selection_status == "candidate" for item in selected)
    assert {category for item in selected for category in item.categories} >= {
        "India",
        "World",
        "Technology",
    }
    assert sum("Technology" in item.categories for item in selected) <= 1
    assert all("Local" not in item.categories for item in selected)


def test_configured_selection_allows_five_per_category_and_caps_total_at_24() -> None:
    stories = [
        categorized_story(
            article(
                title=f"Neural network system {index} announced",
                url=f"https://example.com/ai-{index}",
            )
        )
        for index in range(6)
    ]
    stories.extend(
        categorized_story(
            article(
                title=f"{title} {index} announced",
                url=f"https://example.com/{slug}-{index}",
            )
        )
        for slug, title in (
            ("india", "India parliament debates policy"),
            ("world", "NATO leaders discuss security"),
            ("technology", "Cybersecurity software tool launches"),
            ("business", "Inflation affects the economy"),
        )
        for index in range(5)
    )
    ranked = rank_stories(stories, [source_config()], as_of=AS_OF)
    config = load_selection_config()

    selected = select_stories(ranked, config)

    assert config.max_total_stories == 24
    assert config.max_per_category == 5
    assert len(selected) == 24
    assert sum("AI" in item.categories for item in selected) == 5


def test_multi_category_story_consumes_each_matching_category_slot() -> None:
    multi_category = categorized_story(
        article(
            title="AI technology company launches a model",
            url="https://example.com/multi",
            published_at=AS_OF,
        )
    )
    ai_only = categorized_story(
        article(
            title="AI researchers publish a new model",
            url="https://example.com/ai",
            published_at=AS_OF - timedelta(hours=12),
        )
    )
    technology_only = categorized_story(
        article(
            title="Technology firm releases a processor",
            url="https://example.com/tech",
            published_at=AS_OF - timedelta(hours=12),
        )
    )
    ranked = rank_stories([multi_category, ai_only, technology_only], as_of=AS_OF)

    selected = select_stories(
        ranked, SelectionConfig(max_total_stories=3, max_per_category=1)
    )

    assert multi_category.categories == ("AI", "Technology")
    assert len(selected) == 1
    assert selected[0].categories == ("AI", "Technology")


def test_unclassified_story_is_ranked_but_marked_ineligible() -> None:
    story = categorized_story(article(title="Local bridge reopens"))

    ranked = rank_one(story)

    assert ranked.selection_status == "unclassified"
    assert ranked.category_ranks == ()
    assert "not newsletter eligible" in ranked.selection_reason


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("freshness_window_hours", -1),
        ("freshness_window_hours", 0),
        ("freshness_window_hours", "72"),
        ("freshness_window_hours", True),
        ("independent_publisher_cap", 1),
        (
            "weights",
            {
                "freshness": -0.1,
                "source_quality": 0.4,
                "category_relevance": 0.4,
                "corroboration": 0.3,
            },
        ),
        (
            "weights",
            {
                "freshness": 0.4,
                "source_quality": 0.3,
                "category_relevance": 0.2,
                "corroboration": 0.2,
            },
        ),
    ],
)
def test_invalid_ranking_configuration_is_rejected(field: str, value: object) -> None:
    document = _default_config_mapping()
    document[field] = value

    with pytest.raises(ValueError):
        RankingConfig.from_mapping(document)


def test_naive_reference_timestamp_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        rank_stories([], as_of=datetime(2026, 9, 30, 12))


def test_invalid_source_quality_weight_is_rejected() -> None:
    story = categorized_story(article())

    with pytest.raises(ValueError, match="Invalid quality_weight"):
        rank_one(story, [replace(source_config(), quality_weight=-0.1)])


def _signal(ranked, name: str):
    return next(signal for signal in ranked.score_breakdown if signal.name == name)


def _default_config_mapping() -> dict[str, object]:
    config = load_ranking_config()
    return {
        "weights": dict(config.weights),
        "freshness_window_hours": config.freshness_window_hours,
        "independent_publisher_cap": config.independent_publisher_cap,
        "category_evidence_weights": dict(config.category_evidence_weights),
    }
