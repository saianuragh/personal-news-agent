"""Live blogs and minor sports are skipped; the digest is capped at 12 stories."""

import pytest
import yaml
from app.processing.rank import rank_stories
from app.processing.select import (
    ExcludeRules,
    SelectionConfig,
    load_selection_config,
    select_stories,
)

from tests.test_rank import AS_OF, article, categorized_story, source_config


def selected_titles(*stories) -> list[str]:
    ranked = rank_stories(list(stories), [source_config()], as_of=AS_OF)
    chosen = select_stories(ranked, load_selection_config())
    return [item.story.retained_article.title for item in chosen]


def world(title: str, url: str | None = None):
    return categorized_story(article(title=title, url=url))


KEEP = "NATO leaders discuss security"


@pytest.mark.parametrize(
    "title",
    [
        "Live: NATO leaders discuss security",
        "Gaza war live: NATO leaders discuss security",
        "NATO leaders discuss security - live",
        "NATO leaders discuss security live updates",
        "NATO leaders discuss security as it happened",
        "NATO leaders discuss security minute-by-minute",
    ],
)
def test_live_blog_headlines_are_skipped(title: str) -> None:
    titles = selected_titles(
        world(title, "https://example.com/a"), world(KEEP, "https://example.com/b")
    )
    assert titles == [KEEP]


def test_live_blog_url_is_skipped_even_when_the_headline_looks_normal() -> None:
    titles = selected_titles(
        world(
            "NATO leaders discuss security today",
            "https://news.example/world/live/2026/oct/14/nato",
        ),
        world(KEEP, "https://example.com/b"),
    )
    assert titles == [KEEP]


@pytest.mark.parametrize(
    "title",
    [
        "Alive again: NATO leaders discuss security",
        "Live Aid anniversary: NATO leaders discuss security",
        "Olive growers and NATO leaders discuss security",
    ],
)
def test_lookalike_headlines_are_kept(title: str) -> None:
    assert selected_titles(world(title, "https://example.com/a")) == [title]


@pytest.mark.parametrize(
    "title",
    [
        "Football match report: late goal settles the derby",
        "Football transfer news: striker set for a move",
        "Cricket transfer rumours swirl before the auction",
        "Football gossip: manager eyes a new keeper",
        "Football player ratings from the weekend",
        "Football team news and predicted lineup",
    ],
)
def test_minor_sports_stories_are_skipped(title: str) -> None:
    titles = selected_titles(
        world(title, "https://example.com/s"), world(KEEP, "https://example.com/b")
    )
    assert titles == [KEEP]


def test_major_sports_stories_are_kept() -> None:
    title = "Olympics committee confirms the host city"
    assert selected_titles(world(title, "https://example.com/s")) == [title]


def test_minor_sports_words_do_not_remove_non_sports_stories() -> None:
    title = "Bank transfer fees rise as inflation affects the economy"
    assert selected_titles(world(title, "https://example.com/t")) == [title]


def test_shipped_config_caps_the_digest_at_12_with_three_per_category() -> None:
    config = load_selection_config()
    assert (config.max_total_stories, config.max_per_category) == (12, 3)
    assert config.exclude.live_updates and config.exclude.minor_sports


def test_selection_without_exclude_section_still_loads_and_excludes_nothing() -> None:
    config = SelectionConfig.from_mapping({"max_total_stories": 4, "max_per_category": 2})
    assert config.exclude == ExcludeRules()


@pytest.mark.parametrize(
    "exclude",
    [{"live_updates": "not-a-list"}, {"minor_sports": ["("]}, {"live_updates": [""]}, ["x"]],
)
def test_invalid_exclude_rules_are_rejected(exclude) -> None:
    document = yaml.safe_load("max_total_stories: 4\nmax_per_category: 2")
    document["exclude"] = exclude
    with pytest.raises(ValueError):
        SelectionConfig.from_mapping(document)
