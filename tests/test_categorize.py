"""Deterministic categorization tests with no live feed dependencies."""

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from app.models.article import Article
from app.processing.categorize import (
    EXPECTED_CATEGORIES,
    categorize_story,
    load_category_rules,
)
from app.processing.deduplicate import deduplicate_articles

ROOT = Path(__file__).resolve().parents[1]
PUBLISHED_AT = datetime(2026, 9, 30, 7, tzinfo=UTC)


def make_story(
    *,
    title: str,
    description: str | None = None,
    source_id: str = "test-source",
    source_categories: tuple[str, ...] = (),
):
    article_id = uuid4()
    article = Article(
        article_id=article_id,
        source_id=source_id,
        publisher="Test Publisher",
        title=title,
        url=f"https://example.com/{article_id}",
        canonical_url=f"https://example.com/{article_id}",
        retrieved_at=PUBLISHED_AT,
        content_hash=sha256(str(article_id).encode()).hexdigest(),
        published_at=PUBLISHED_AT,
        description=description,
        source_categories=source_categories,
    )
    return deduplicate_articles([article]).stories[0]


@pytest.mark.parametrize(
    ("title", "category"),
    [
        ("India's parliament debates a new bill", "India"),
        ("NATO leaders announce a new security agreement", "World"),
        ("OpenAI releases a new large language model", "AI"),
        ("Cybersecurity flaw affects smartphone software", "Technology"),
        ("Central bank raises interest rates after inflation report", "Business & Economy"),
        ("NASA telescope detects a distant planet", "Science & Space"),
        ("Tennis champion wins the Grand Slam final", "Sports"),
        ("New film breaks the international box office record", "Entertainment"),
    ],
)
def test_clear_story_maps_to_expected_category(title: str, category: str) -> None:
    result = categorize_story(make_story(title=title))

    assert result.categories == (category,)
    assert result.status == "categorized"
    assert result.evidence[0].category == category


def test_matching_is_case_insensitive() -> None:
    result = categorize_story(make_story(title="nAsA ANNOUNCES A NEW SPACE MISSION"))

    assert result.categories == ("Science & Space",)


def test_multiple_matching_signals_are_recorded_for_explanation() -> None:
    result = categorize_story(
        make_story(
            title="United Nations summit opens",
            source_id="BBC_WORLD",
            source_categories=("wOrLd",),
        )
    )

    evidence = result.evidence[0]
    assert result.categories == ("World",)
    assert evidence.signals == ("source_id", "source_category", "title")
    assert "source_id:BBC_WORLD" in evidence.matched_values
    assert "source_category:wOrLd" in evidence.matched_values
    assert "title:United Nations" in evidence.matched_values


def test_description_is_an_explainable_matching_signal() -> None:
    result = categorize_story(
        make_story(title="New research published", description="NASA confirms a space mission")
    )

    assert result.categories == ("Science & Space",)
    assert "description" in result.evidence[0].signals


def test_multiple_categories_are_retained_and_marked_ambiguous() -> None:
    result = categorize_story(
        make_story(title="OpenAI reports record earnings from new AI technology")
    )

    assert result.categories == ("AI", "Technology", "Business & Economy")
    assert result.status == "ambiguous"
    assert "all matches are retained" in (result.reason or "")
    assert len(result.categories) == len(set(result.categories))


def test_openai_story_matches_ai_and_technology_using_configured_signals() -> None:
    result = categorize_story(
        make_story(
            title="OpenAI announces a new AI model for developers",
            description=(
                "The new model improves developer tooling and software engineering workflows."
            ),
            source_categories=("Technology",),
        )
    )

    assert result.categories == ("AI", "Technology")
    assert result.status == "ambiguous"
    assert len(result.categories) == len(set(result.categories))
    assert tuple(rule.name for rule in load_category_rules() if rule.name in result.categories) == (
        "AI",
        "Technology",
    )
    ai_evidence = next(evidence for evidence in result.evidence if evidence.category == "AI")
    technology_evidence = next(
        evidence for evidence in result.evidence if evidence.category == "Technology"
    )
    assert "title" in ai_evidence.signals
    assert "source_category" in technology_evidence.signals
    assert "description" in technology_evidence.signals
    assert "source_category:Technology" in technology_evidence.matched_values


def test_ai_case_matching_is_consistent_for_uppercase_and_lowercase() -> None:
    uppercase = categorize_story(make_story(title="OPENAI AI MODEL"))
    lowercase = categorize_story(make_story(title="openai ai model"))

    assert uppercase.categories == lowercase.categories == ("AI",)
    assert uppercase.status == lowercase.status == "categorized"


def test_neural_network_phrase_matches_ai_category() -> None:
    result = categorize_story(make_story(title="New neural network model released"))

    assert result.categories == ("AI",)
    assert result.status == "categorized"
    assert "title:neural network" in result.evidence[0].matched_values


def test_neural_network_term_does_not_match_inside_a_larger_word() -> None:
    result = categorize_story(make_story(title="New neuralnetwork model released"))

    assert result.categories == ()
    assert result.status == "unclassified"


def test_partial_words_do_not_match_ai_or_technology_terms() -> None:
    result = categorize_story(make_story(title="Retail biotech report published"))

    assert result.categories == ()
    assert result.status == "unclassified"
    assert result.reason == "No configured category rule matched the story metadata."


def test_no_matching_category_is_unclassified_not_forced() -> None:
    result = categorize_story(make_story(title="Local bridge reopens after repairs"))

    assert result.categories == ()
    assert result.evidence == ()
    assert result.status == "unclassified"
    assert result.reason


def test_repeated_categorization_has_deterministic_order_and_evidence() -> None:
    story = make_story(title="India's AI technology industry reports growth")

    first = categorize_story(story)
    second = categorize_story(story)

    assert first == second
    assert first.categories == ("India", "AI", "Technology")


def test_configured_category_rules_include_all_required_categories() -> None:
    rules = load_category_rules(ROOT / "config" / "categories.yaml")

    assert tuple(rule.name for rule in rules) == EXPECTED_CATEGORIES
    world_rule = next(rule for rule in rules if rule.name == "World")
    assert "bbc_world" in world_rule.source_ids
    assert "guardian_world" in world_rule.source_ids
    assert "NATO" in world_rule.terms


def test_custom_category_rule_file_changes_matches_without_code_changes(tmp_path: Path) -> None:
    rules_file = tmp_path / "categories.yaml"
    categories = "\n".join(
        f"  - name: {category}\n    source_ids: []\n    source_tags: []\n    terms: "
        + ("[Novel Concept]" if category == "AI" else "[]")
        for category in EXPECTED_CATEGORIES
    )
    rules_file.write_text(f"categories:\n{categories}\n", encoding="utf-8")
    rules = load_category_rules(rules_file)

    result = categorize_story(make_story(title="A NOVEL concept arrives"), rules)

    assert result.categories == ("AI",)


def test_category_file_with_missing_category_is_rejected(tmp_path: Path) -> None:
    rules_file = tmp_path / "categories.yaml"
    rules_file.write_text("categories: []\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Category rule set mismatch"):
        load_category_rules(rules_file)
