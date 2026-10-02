"""Deterministic, explainable ranking of categorized stories."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID

import yaml

from app.config import SourceConfig
from app.processing.categorize import CategorizedStory
from app.processing.deduplicate import Story

DEFAULT_RANKING_CONFIG = Path(__file__).resolve().parents[2] / "config" / "ranking.yaml"
_SIGNALS = ("freshness", "source_quality", "category_relevance", "corroboration")
_CATEGORY_EVIDENCE_SIGNALS = ("source_id", "source_category", "title", "description")

SelectionStatus = Literal["candidate", "unclassified"]


@dataclass(frozen=True, slots=True)
class RankingConfig:
    """Validated scoring weights and normalization limits."""

    weights: tuple[tuple[str, float], ...]
    freshness_window_hours: float
    independent_publisher_cap: int
    category_evidence_weights: tuple[tuple[str, float], ...]

    @classmethod
    def from_mapping(cls, value: object) -> RankingConfig:
        if not isinstance(value, dict):
            raise ValueError("Ranking configuration must be a mapping")
        weights_value = value.get("weights")
        if not isinstance(weights_value, dict) or set(weights_value) != set(_SIGNALS):
            raise ValueError(f"weights must define exactly: {', '.join(_SIGNALS)}")
        weights = tuple(
            (name, _number(weights_value[name], f"weights.{name}", 0, 1)) for name in _SIGNALS
        )
        if not math.isclose(sum(weight for _, weight in weights), 1.0, rel_tol=0, abs_tol=1e-9):
            raise ValueError("ranking weights must sum to 1.0")

        evidence_value = value.get("category_evidence_weights")
        if not isinstance(evidence_value, dict) or set(evidence_value) != set(
            _CATEGORY_EVIDENCE_SIGNALS
        ):
            raise ValueError(
                "category_evidence_weights must define exactly: "
                f"{', '.join(_CATEGORY_EVIDENCE_SIGNALS)}"
            )
        evidence_weights = tuple(
            (name, _number(evidence_value[name], f"category_evidence_weights.{name}", 0, 1))
            for name in _CATEGORY_EVIDENCE_SIGNALS
        )

        freshness_window_hours = _number(
            value.get("freshness_window_hours"), "freshness_window_hours", 0.001, 8_760
        )
        publisher_cap = value.get("independent_publisher_cap")
        if (
            isinstance(publisher_cap, bool)
            or not isinstance(publisher_cap, int)
            or publisher_cap < 2
        ):
            raise ValueError("independent_publisher_cap must be an integer of at least 2")

        return cls(
            weights=weights,
            freshness_window_hours=freshness_window_hours,
            independent_publisher_cap=publisher_cap,
            category_evidence_weights=evidence_weights,
        )


@dataclass(frozen=True, slots=True)
class ScoreSignal:
    """One normalized score component, its configured weight, and evidence."""

    name: str
    value: float
    weight: float
    contribution: float
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CategoryRank:
    """The story's one-based position within one of its categories."""

    category: str
    rank: int


@dataclass(frozen=True, slots=True)
class RankedStory:
    """A categorized story with score evidence and global/category order."""

    categorized_story: CategorizedStory
    score: float
    score_breakdown: tuple[ScoreSignal, ...]
    rank: int
    category_ranks: tuple[CategoryRank, ...]
    selection_status: SelectionStatus
    selection_reason: str

    @property
    def story(self):
        """Preserve direct access to the original Story and its Article provenance."""
        return self.categorized_story.story

    @property
    def categories(self) -> tuple[str, ...]:
        return self.categorized_story.categories


def load_ranking_config(path: str | Path = DEFAULT_RANKING_CONFIG) -> RankingConfig:
    """Load and validate the editable ranking configuration."""
    config_path = Path(path)
    try:
        document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"Unable to load ranking configuration at {config_path}") from error
    return RankingConfig.from_mapping(document)


def rank_stories(
    stories: tuple[CategorizedStory, ...] | list[CategorizedStory],
    source_configs: tuple[SourceConfig, ...] | list[SourceConfig] = (),
    *,
    as_of: datetime,
    config: RankingConfig | None = None,
) -> tuple[RankedStory, ...]:
    """Score stories using configured deterministic signals and return ranked order.

    ``as_of`` is required so repeated runs against identical inputs use the same
    freshness scores. No story or Article is mutated or discarded.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be a timezone-aware datetime")
    reference_time = as_of.astimezone(UTC)
    ranking_config = config if config is not None else load_ranking_config()
    sources_by_id = _source_lookup(source_configs)
    preliminaries = [
        _score_story(story, sources_by_id, ranking_config, reference_time) for story in stories
    ]

    ordered = sorted(preliminaries, key=_ranking_key)
    global_rank_by_id = {
        item.categorized_story.story.story_id: index for index, item in enumerate(ordered, 1)
    }

    category_positions: dict[str, list[_ScoredStory]] = defaultdict(list)
    for item in ordered:
        for category in item.categorized_story.categories:
            category_positions[category].append(item)
    category_rank_by_story: dict[UUID, list[CategoryRank]] = defaultdict(list)
    for category, members in category_positions.items():
        for index, item in enumerate(members, 1):
            category_rank_by_story[item.categorized_story.story.story_id].append(
                CategoryRank(category, index)
            )

    return tuple(
        RankedStory(
            categorized_story=item.categorized_story,
            score=item.score,
            score_breakdown=item.score_breakdown,
            rank=global_rank_by_id[item.categorized_story.story.story_id],
            category_ranks=tuple(
                sorted(
                    category_rank_by_story[item.categorized_story.story.story_id],
                    key=lambda item: item.category,
                )
            ),
            selection_status=("candidate" if item.categorized_story.categories else "unclassified"),
            selection_reason=(
                "Ranked candidate with at least one category."
                if item.categorized_story.categories
                else "No category matched; informational score only, not newsletter eligible."
            ),
        )
        for item in ordered
    )


@dataclass(frozen=True, slots=True)
class _ScoredStory:
    categorized_story: CategorizedStory
    score: float
    score_breakdown: tuple[ScoreSignal, ...]


def _score_story(
    categorized_story: CategorizedStory,
    sources_by_id: dict[str, SourceConfig],
    config: RankingConfig,
    as_of: datetime,
) -> _ScoredStory:
    story = categorized_story.story
    freshness, freshness_evidence = _freshness(story.latest_published_at, as_of, config)
    quality, quality_evidence = _source_quality(story, sources_by_id)
    category_relevance, category_evidence = _category_relevance(categorized_story, config)
    corroboration, corroboration_evidence = _corroboration(story, sources_by_id, config)

    values = {
        "freshness": (freshness, freshness_evidence),
        "source_quality": (quality, quality_evidence),
        "category_relevance": (category_relevance, category_evidence),
        "corroboration": (corroboration, corroboration_evidence),
    }
    weights = dict(config.weights)
    breakdown = tuple(
        ScoreSignal(
            name=name,
            value=round(values[name][0], 4),
            weight=weights[name],
            contribution=round(values[name][0] * weights[name], 4),
            evidence=values[name][1],
        )
        for name in _SIGNALS
    )
    score = round(sum(signal.contribution for signal in breakdown), 2)
    return _ScoredStory(categorized_story, min(100.0, max(0.0, score)), breakdown)


def _freshness(
    published_at: datetime | None,
    as_of: datetime,
    config: RankingConfig,
) -> tuple[float, tuple[str, ...]]:
    if published_at is None:
        return 0.0, ("publication time unknown; freshness receives no credit",)
    age = as_of - published_at.astimezone(UTC)
    if age < timedelta(0):
        return 0.0, (
            "publication time is after the ranking reference time; freshness receives no credit",
        )
    age_hours = age.total_seconds() / 3_600
    window = config.freshness_window_hours
    value = max(0.0, 100.0 * (1.0 - age_hours / window))
    return value, (f"age_hours={age_hours:.4f}", f"freshness_window_hours={window:g}")


def _source_quality(
    story: Story,
    sources_by_id: dict[str, SourceConfig],
) -> tuple[float, tuple[str, ...]]:
    weights: dict[str, float] = {}
    unknown_sources: set[str] = set()
    for article in story.members:
        source = sources_by_id.get(article.source_id)
        if source is None:
            unknown_sources.add(article.source_id)
        else:
            weights[article.source_id] = source.quality_weight
    if not weights:
        evidence = ("no configured source quality weights; signal receives no credit",)
        if unknown_sources:
            evidence += (f"unconfigured_source_ids={','.join(sorted(unknown_sources))}",)
        return 0.0, evidence
    evidence = tuple(f"{source_id}={weight:g}" for source_id, weight in sorted(weights.items()))
    if unknown_sources:
        evidence += (f"unconfigured_source_ids={','.join(sorted(unknown_sources))}",)
    return 100.0 * sum(weights.values()) / len(weights), evidence


def _category_relevance(
    categorized_story: CategorizedStory,
    config: RankingConfig,
) -> tuple[float, tuple[str, ...]]:
    if not categorized_story.evidence:
        return 0.0, ("no category evidence",)
    evidence_weights = dict(config.category_evidence_weights)
    category_scores: list[tuple[str, float, tuple[str, ...]]] = []
    for match in categorized_story.evidence:
        component_scores = [evidence_weights[signal] for signal in match.signals]
        value = max(component_scores, default=0.0)
        category_scores.append((match.category, value, match.signals))
    best_value = max(value for _, value, _ in category_scores)
    reasons = tuple(
        f"{category}={value:g} via {','.join(signals)}"
        for category, value, signals in category_scores
    )
    return best_value * 100.0, reasons


def _corroboration(
    story: Story,
    sources_by_id: dict[str, SourceConfig],
    config: RankingConfig,
) -> tuple[float, tuple[str, ...]]:
    publishers: dict[str, str] = {}
    unconfigured_sources: set[str] = set()
    for article in story.members:
        source = sources_by_id.get(article.source_id)
        if source is None:
            publisher = article.publisher
            unconfigured_sources.add(article.source_id)
        else:
            publisher = source.name
        publishers.setdefault(publisher.casefold(), publisher)

    count = len(publishers)
    cap = config.independent_publisher_cap
    value = min(100.0, 100.0 * max(0, count - 1) / (cap - 1))
    evidence = (f"distinct_publishers={count}", f"publisher_cap={cap}")
    if unconfigured_sources:
        evidence += (
            "article publisher names used for unconfigured source IDs="
            + ",".join(sorted(unconfigured_sources)),
        )
    return value, evidence


def _source_lookup(
    source_configs: tuple[SourceConfig, ...] | list[SourceConfig],
) -> dict[str, SourceConfig]:
    result: dict[str, SourceConfig] = {}
    for source in source_configs:
        if source.source_id in result:
            raise ValueError(f"Duplicate source configuration for {source.source_id}")
        if not math.isfinite(source.quality_weight) or not 0 <= source.quality_weight <= 1:
            raise ValueError(f"Invalid quality_weight for source {source.source_id}")
        result[source.source_id] = source
    return result


def _ranking_key(item: _ScoredStory) -> tuple[float, bool, timedelta, str, str]:
    story = item.categorized_story.story
    published_at = story.latest_published_at
    age_proxy = (
        datetime.max.replace(tzinfo=UTC) - published_at.astimezone(UTC)
        if published_at is not None
        else timedelta.max
    )
    return (
        -item.score,
        published_at is None,
        age_proxy,
        story.retained_article.canonical_url.casefold(),
        str(story.story_id),
    )


def _number(value: object, field_name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field_name} must be a number")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError(f"{field_name} must be between {minimum} and {maximum}")
    return result
