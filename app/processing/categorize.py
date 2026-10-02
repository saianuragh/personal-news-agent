"""Explainable, deterministic story categorization from configured rules."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from app.processing.deduplicate import Story

DEFAULT_RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "categories.yaml"
EXPECTED_CATEGORIES = (
    "India",
    "World",
    "AI",
    "Technology",
    "Business & Economy",
    "Science & Space",
    "Sports",
    "Entertainment",
)
_SIGNAL_ORDER = ("source_id", "source_category", "title", "description")
_TOKEN_SEPARATOR = re.compile(r"[^\w]+", re.UNICODE)

CategoryStatus = Literal["categorized", "ambiguous", "unclassified"]


@dataclass(frozen=True, slots=True)
class CategoryRule:
    """Rule inputs for one configured newsletter section."""

    name: str
    source_ids: tuple[str, ...]
    source_tags: tuple[str, ...]
    terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CategoryEvidence:
    """The exact configured signals that caused a category match."""

    category: str
    signals: tuple[str, ...]
    matched_values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CategorizedStory:
    """Categorization output without adding ranking or AI fields."""

    story: Story
    categories: tuple[str, ...]
    evidence: tuple[CategoryEvidence, ...]
    status: CategoryStatus
    reason: str | None = None


def load_category_rules(path: str | Path = DEFAULT_RULES_PATH) -> tuple[CategoryRule, ...]:
    """Load all category rules and require the eight configured V1 sections."""
    config_path = Path(path)
    try:
        document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"Unable to load category rules at {config_path}") from error

    if not isinstance(document, dict) or not isinstance(document.get("categories"), list):
        raise ValueError("Category rules must contain a 'categories' list")

    rules = tuple(_parse_rule(item) for item in document["categories"])
    names = tuple(rule.name for rule in rules)
    if len(names) != len(set(names)):
        raise ValueError("Category rule names must be unique")
    if set(names) != set(EXPECTED_CATEGORIES):
        missing = sorted(set(EXPECTED_CATEGORIES) - set(names))
        extra = sorted(set(names) - set(EXPECTED_CATEGORIES))
        raise ValueError(f"Category rule set mismatch; missing={missing}, extra={extra}")
    return rules


def categorize_story(
    story: Story,
    rules: tuple[CategoryRule, ...] | None = None,
) -> CategorizedStory:
    """Assign every clearly matching section; preserve ambiguity explicitly.

    Matching is case-insensitive and token/phrase-boundary based. It does not
    use substring guesses, fuzzy matching, ranking, or an LLM. Multiple
    matching categories are retained and marked ambiguous rather than
    selecting an arbitrary winner.
    """
    configured_rules = rules if rules is not None else load_category_rules()
    evidence = tuple(
        category_evidence
        for rule in configured_rules
        if (category_evidence := _match_rule(story, rule)) is not None
    )
    categories = tuple(match.category for match in evidence)
    if not categories:
        return CategorizedStory(
            story=story,
            categories=(),
            evidence=(),
            status="unclassified",
            reason="No configured category rule matched the story metadata.",
        )
    if len(categories) > 1:
        return CategorizedStory(
            story=story,
            categories=categories,
            evidence=evidence,
            status="ambiguous",
            reason="Multiple deterministic category rules matched; all matches are retained.",
        )
    return CategorizedStory(
        story=story,
        categories=categories,
        evidence=evidence,
        status="categorized",
    )


def categorize_stories(
    stories: tuple[Story, ...] | list[Story],
    rules: tuple[CategoryRule, ...] | None = None,
) -> tuple[CategorizedStory, ...]:
    """Categorize a batch while loading configured rules at most once."""
    configured_rules = rules if rules is not None else load_category_rules()
    return tuple(categorize_story(story, configured_rules) for story in stories)


def _parse_rule(value: object) -> CategoryRule:
    if not isinstance(value, dict):
        raise ValueError("Each category rule must be a mapping")
    name = _required_string(value.get("name"), "name")
    return CategoryRule(
        name=name,
        source_ids=_string_list(value.get("source_ids", []), "source_ids"),
        source_tags=_string_list(value.get("source_tags", []), "source_tags"),
        terms=_string_list(value.get("terms", []), "terms"),
    )


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Category rule {field_name} must be a non-empty string")
    return value.strip()


def _string_list(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"Category rule {field_name} must be a list of strings")
    return tuple(_required_string(item, field_name) for item in value)


def _match_rule(story: Story, rule: CategoryRule) -> CategoryEvidence | None:
    matched_values: set[str] = set()
    matched_signals: set[str] = set()
    source_ids = {_normalize_text(source_id) for source_id in rule.source_ids}
    source_tags = {_normalize_text(source_tag) for source_tag in rule.source_tags}
    normalized_terms = tuple((term, _normalize_text(term)) for term in rule.terms)

    for article in story.members:
        if _normalize_text(article.source_id) in source_ids:
            matched_signals.add("source_id")
            matched_values.add(f"source_id:{article.source_id}")

        for source_category in article.source_categories:
            if _normalize_text(source_category) in source_tags:
                matched_signals.add("source_category")
                matched_values.add(f"source_category:{source_category}")

        for field_name, field_value in (
            ("title", article.title),
            ("description", article.description),
        ):
            normalized_value = _normalize_text(field_value or "")
            if not normalized_value:
                continue
            padded_value = f" {normalized_value} "
            for configured_term, normalized_term in normalized_terms:
                if f" {normalized_term} " in padded_value:
                    matched_signals.add(field_name)
                    matched_values.add(f"{field_name}:{configured_term}")

    if not matched_signals:
        return None
    ordered_signals = tuple(signal for signal in _SIGNAL_ORDER if signal in matched_signals)
    return CategoryEvidence(
        category=rule.name,
        signals=ordered_signals,
        matched_values=tuple(sorted(matched_values, key=str.casefold)),
    )


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    tokens = _TOKEN_SEPARATOR.split(normalized)
    return " ".join(token for token in tokens if token)
