"""Deterministic coverage-aware selection of ranked stories for a newsletter."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import yaml

from app.processing.rank import RankedStory

DEFAULT_SELECTION_CONFIG = Path(__file__).resolve().parents[2] / "config" / "selection.yaml"
CATEGORY_ORDER = (
    "India",
    "World",
    "AI",
    "Technology",
    "Business & Economy",
    "Science & Space",
    "Sports",
    "Entertainment",
)


@cache
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ExcludeRules:
    """Regular expressions for stories the digest should skip.

    live_updates: matched against every member headline and URL, in any category.
    minor_sports: matched against headlines of stories categorized as Sports.
    """

    live_updates: tuple[str, ...] = ()
    minor_sports: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: object) -> ExcludeRules:
        if value is None:
            return cls()
        if not isinstance(value, dict):
            raise ValueError("exclude must be a mapping")
        parsed: dict[str, tuple[str, ...]] = {}
        for name in ("live_updates", "minor_sports"):
            raw = value.get(name, [])
            if not isinstance(raw, list) or not all(
                isinstance(pattern, str) and pattern.strip() for pattern in raw
            ):
                raise ValueError(f"exclude.{name} must be a list of non-empty patterns")
            for pattern in raw:
                try:
                    re.compile(pattern)
                except re.error as error:
                    raise ValueError(f"exclude.{name} contains an invalid pattern") from error
            parsed[name] = tuple(raw)
        return cls(**parsed)

    def reason(self, item: RankedStory) -> str | None:
        """Return why a story is skipped, or None to keep it."""
        members = item.story.members
        titles = [article.title for article in members]
        live_text = [*titles]
        for article in members:
            live_text.extend((article.url, article.canonical_url))
        if _any_match(self.live_updates, live_text):
            return "live_updates"
        if "Sports" in item.categories and _any_match(self.minor_sports, titles):
            return "minor_sports"
        return None


def _any_match(patterns: tuple[str, ...], texts: list[str]) -> bool:
    return any(_compiled(pattern).search(text) for pattern in patterns for text in texts)


@dataclass(frozen=True, slots=True)
class SelectionConfig:
    """Limits applied after deterministic scoring to keep a balanced digest."""

    max_total_stories: int
    max_per_category: int
    exclude: ExcludeRules = ExcludeRules()

    @classmethod
    def from_mapping(cls, value: object) -> SelectionConfig:
        if not isinstance(value, dict):
            raise ValueError("Selection configuration must be a mapping")
        return cls(
            _positive_integer(value.get("max_total_stories"), "max_total_stories", 1, 100),
            _positive_integer(value.get("max_per_category"), "max_per_category", 1, 25),
            ExcludeRules.from_mapping(value.get("exclude")),
        )


def load_selection_config(path: str | Path = DEFAULT_SELECTION_CONFIG) -> SelectionConfig:
    config_path = Path(path)
    try:
        document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"Unable to load selection configuration at {config_path}") from error
    return SelectionConfig.from_mapping(document)


def select_stories(
    ranked_stories: tuple[RankedStory, ...] | list[RankedStory],
    config: SelectionConfig,
) -> tuple[RankedStory, ...]:
    """Choose by category rank in repeated section passes, then restore score order.

    Each selected multi-category story consumes one slot in every category it
    belongs to. The final output is returned in deterministic global rank order.
    """
    eligible = [
        item
        for item in ranked_stories
        if item.selection_status == "candidate" and config.exclude.reason(item) is None
    ]
    categories = [
        name for name in CATEGORY_ORDER if any(name in item.categories for item in eligible)
    ]
    categories.extend(
        sorted(
            {name for item in eligible for name in item.categories} - set(CATEGORY_ORDER),
            key=str.casefold,
        )
    )
    selected: dict[str, RankedStory] = {}
    category_counts = dict.fromkeys(categories, 0)
    per_category = {
        category: sorted(
            (item for item in eligible if category in item.categories),
            key=lambda item: (
                next(
                    (entry.rank for entry in item.category_ranks if entry.category == category),
                    item.rank,
                ),
                item.rank,
                str(item.story.story_id),
            ),
        )
        for category in categories
    }

    made_progress = True
    while made_progress and len(selected) < config.max_total_stories:
        made_progress = False
        for category in categories:
            if len(selected) >= config.max_total_stories:
                break
            if category_counts[category] >= config.max_per_category:
                continue
            candidate = next(
                (
                    item
                    for item in per_category[category]
                    if str(item.story.story_id) not in selected
                    and all(
                        category_counts[member_category] < config.max_per_category
                        for member_category in item.categories
                        if member_category in category_counts
                    )
                ),
                None,
            )
            if candidate is None:
                continue
            selected[str(candidate.story.story_id)] = candidate
            for member_category in candidate.categories:
                if member_category in category_counts:
                    category_counts[member_category] += 1
            made_progress = True

    return tuple(sorted(selected.values(), key=lambda item: (item.rank, str(item.story.story_id))))


def _positive_integer(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value
