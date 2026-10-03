"""Thin in-memory coordinator for one preview or send pipeline run."""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from app.config import SourceConfig, load_sources
from app.database.base import PipelineRunRepository
from app.email.base import (
    DeliveryResult,
    EmailConfiguration,
    EmailProvider,
    SMTPSettings,
    deliver_or_preview,
    email_settings_from_env,
    newsletter_idempotency_key,
)
from app.email.credentials import smtp_credential_configured
from app.llm.base import (
    AIEnrichedStory,
    LLMSettings,
    ProviderDiagnostic,
    Summarizer,
    source_description_fallback,
)
from app.models.article import Article
from app.newsletter.renderer import NewsletterDocument, render_newsletter
from app.processing.categorize import (
    CategorizedStory,
    CategoryRule,
    categorize_stories,
    load_category_rules,
)
from app.processing.deduplicate import Story, deduplicate_articles
from app.processing.normalize import NormalizationResult, normalize_feed
from app.processing.rank import RankedStory, RankingConfig, load_ranking_config, rank_stories
from app.processing.select import SelectionConfig, load_selection_config, select_stories
from app.sources.base import FetchedFeed, SourceFetchError
from app.sources.rss_atom import RSSAtomSource

DEFAULT_CONFIG_DIRECTORY = Path(__file__).resolve().parents[2] / "config"
DEFAULT_PREVIEW_DIRECTORY = Path(__file__).resolve().parents[2] / "data" / "previews"
RunMode = Literal["preview", "send"]
RunStatus = Literal[
    "preview",
    "sent",
    "partial",
    "all_sources_failed",
    "no_sources",
    "no_eligible_stories",
    "configuration_failed",
    "pipeline_failed",
    "delivery_failed",
    "delivery_unknown",
    "duplicate_send_skipped",
    "persistence_failed",
]

_LOGGER = logging.getLogger("personal_news_agent.pipeline")


class SourceAdapter(Protocol):
    """Minimal source boundary consumed by the runner."""

    def fetch(self) -> FetchedFeed: ...


class StorySummarizer(Protocol):
    """Minimal injectable summary boundary."""

    def summarize(self, story: RankedStory, *, generated_at: datetime) -> AIEnrichedStory: ...


@dataclass(frozen=True, slots=True)
class PipelineDependencies:
    """Optional test/runtime collaborators; defaults are created at the boundary."""

    source_factory: Callable[[SourceConfig, datetime], SourceAdapter] | None = None
    summarizer: StorySummarizer | None = None
    email_provider: EmailProvider | None = None
    email_settings: EmailConfiguration | None = None
    run_repository: PipelineRunRepository | None = None


@dataclass(frozen=True, slots=True)
class PipelineCounts:
    sources_attempted: int = 0
    sources_succeeded: int = 0
    sources_failed: int = 0
    raw_entries: int = 0
    normalized_articles: int = 0
    rejected_entries: int = 0
    normalization_issues: int = 0
    duplicate_articles: int = 0
    stories: int = 0
    categorized_stories: int = 0
    ranked_stories: int = 0
    selected_stories: int = 0
    summarized_stories: int = 0
    fallback_stories: int = 0
    summary_failures: int = 0
    omitted_stories: int = 0


@dataclass(frozen=True, slots=True)
class StageFailure:
    """Non-secret diagnostic for a failed source or pipeline stage."""

    stage: str
    error_type: str
    message: str
    source_id: str | None = None
    category: str | None = None
    http_status: int | None = None
    provider_error_type: str | None = None
    provider_error_code: str | None = None
    attempt_count: int = 0


@dataclass(frozen=True, slots=True)
class PipelineRunResult:
    """Structured outcome and in-memory artifacts for a single pipeline run."""

    run_id: UUID
    mode: RunMode
    as_of: datetime
    status: RunStatus
    counts: PipelineCounts
    source_failures: tuple[StageFailure, ...] = ()
    stage_failures: tuple[StageFailure, ...] = ()
    newsletter: NewsletterDocument | None = None
    delivery: DeliveryResult | None = None
    error: str | None = None

    @property
    def exit_code(self) -> int:
        return 0 if self.status in {
            "preview", "sent", "partial", "no_eligible_stories", "duplicate_send_skipped"
        } else 1


@dataclass(frozen=True, slots=True)
class _RunConfiguration:
    sources: tuple[SourceConfig, ...]
    category_rules: tuple[CategoryRule, ...]
    ranking: RankingConfig
    selection: SelectionConfig
    timezone_name: str
    llm_settings: LLMSettings | None
    email_settings: EmailConfiguration | None


class PipelineRunner:
    """Coordinate existing components while keeping their business rules local."""

    def __init__(self, dependencies: PipelineDependencies | None = None) -> None:
        self.dependencies = dependencies or PipelineDependencies()

    def run(
        self,
        mode: RunMode,
        *,
        as_of: datetime | None = None,
        config_directory: str | Path = DEFAULT_CONFIG_DIRECTORY,
        preview_directory: str | Path = DEFAULT_PREVIEW_DIRECTORY,
        ai_in_preview: bool = False,
        environ: Mapping[str, str] | None = None,
    ) -> PipelineRunResult:
        """Persist lifecycle around one orchestrated preview or send run."""
        run_time = as_of if as_of is not None else datetime.now(UTC)
        if run_time.tzinfo is None or run_time.utcoffset() is None:
            raise ValueError("as_of must be a timezone-aware datetime")
        run_time = run_time.astimezone(UTC)
        if mode not in {"preview", "send"}:
            raise ValueError("mode must be 'preview' or 'send'")

        run_id = uuid4()
        started_at = datetime.now(UTC)
        _log_run_started(run_id, mode, run_time, started_at)
        repository = self.dependencies.run_repository
        if repository is not None:
            try:
                repository.create_run(
                    run_id,
                    started_at=started_at,
                    as_of=run_time,
                    mode=mode,
                )
            except Exception as error:
                failure = _safe_failure("database_start", error)
                result = PipelineRunResult(
                    run_id,
                    mode,
                    run_time,
                    "persistence_failed",
                    PipelineCounts(),
                    stage_failures=(failure,),
                    error="Pipeline was not started because run history could not be persisted.",
                )
                _log_run_finished(result, datetime.now(UTC))
                return result

        try:
            result = self._run_pipeline(
                run_id,
                mode,
                run_time,
                config_directory=config_directory,
                preview_directory=preview_directory,
                ai_in_preview=ai_in_preview,
                environ=environ,
            )
        except Exception as error:
            failure = _safe_failure("pipeline", error)
            result = PipelineRunResult(
                run_id,
                mode,
                run_time,
                "pipeline_failed",
                PipelineCounts(),
                stage_failures=(failure,),
                error="Pipeline failed before producing a result.",
            )

        if repository is not None:
            try:
                repository.complete_run(
                    result,
                    started_at=started_at,
                    completed_at=datetime.now(UTC),
                )
            except Exception as error:
                failure = _safe_failure("database_completion", error)
                result = replace(
                    result,
                    status="persistence_failed",
                    stage_failures=(*result.stage_failures, failure),
                    error="Pipeline finished, but run history could not be completed.",
                )
                _log_run_finished(result, datetime.now(UTC))
                return result
        _log_run_finished(result, datetime.now(UTC))
        return result

    def _run_pipeline(
        self,
        run_id: UUID,
        mode: RunMode,
        run_time: datetime,
        *,
        config_directory: str | Path,
        preview_directory: str | Path,
        ai_in_preview: bool,
        environ: Mapping[str, str] | None,
    ) -> PipelineRunResult:
        """Coordinate processing stages without persistence or delivery policy."""

        try:
            configuration = _load_configuration(
                Path(config_directory),
                mode=mode,
                ai_in_preview=ai_in_preview,
                dependencies=self.dependencies,
                environ=environ,
            )
        except (OSError, ValueError) as error:
            failure = _safe_failure("configuration", error)
            return PipelineRunResult(
                run_id,
                mode,
                run_time,
                "configuration_failed",
                PipelineCounts(),
                stage_failures=(failure,),
                error=failure.message,
            )

        repository = self.dependencies.run_repository
        if mode == "send" and repository is not None:
            email_settings = self.dependencies.email_settings or configuration.email_settings
            if email_settings is not None:
                local_date = run_time.astimezone(ZoneInfo(configuration.timezone_name)).date()
                delivery_key = newsletter_idempotency_key(email_settings.recipient, local_date)
                try:
                    already_claimed = repository.delivery_claim_exists(delivery_key)
                except Exception as error:
                    failure = _safe_failure("delivery_claim_check", error)
                    return PipelineRunResult(
                        run_id,
                        mode,
                        run_time,
                        "persistence_failed",
                        PipelineCounts(),
                        stage_failures=(failure,),
                        error=(
                            "Daily delivery history could not be checked safely; "
                            "no email was sent."
                        ),
                    )
                if already_claimed:
                    return PipelineRunResult(
                        run_id,
                        mode,
                        run_time,
                        "duplicate_send_skipped",
                        PipelineCounts(),
                        delivery=_duplicate_delivery_result(email_settings),
                        error="A delivery for this recipient and local date was already attempted.",
                    )

        enabled_sources = tuple(source for source in configuration.sources if source.enabled)
        if not enabled_sources:
            return PipelineRunResult(
                run_id,
                mode,
                run_time,
                "no_sources",
                PipelineCounts(),
                error="No enabled sources are configured.",
            )

        source_failures: list[StageFailure] = []
        feeds: list[FetchedFeed] = []
        raw_count = 0
        for source_config in enabled_sources:
            try:
                source = _make_source(self.dependencies, source_config, run_time)
                feed = source.fetch()
                feeds.append(feed)
                raw_count += len(feed.entries)
            except Exception as error:
                if isinstance(error, SourceFetchError):
                    source_failures.append(
                        StageFailure(
                            stage="source_fetch",
                            error_type=type(error).__name__,
                            message=(
                                f"HTTP {error.http_status} fetching source "
                                f"{source_config.source_id}"
                                if error.http_status is not None
                                else f"Source fetch failed ({type(error).__name__})."
                            ),
                            source_id=source_config.source_id,
                            http_status=error.http_status,
                        )
                    )
                else:
                    source_failures.append(
                        _safe_failure("source_fetch", error, source_config.source_id)
                    )

        counts = PipelineCounts(
            sources_attempted=len(enabled_sources),
            sources_succeeded=len(feeds),
            sources_failed=len(source_failures),
            raw_entries=raw_count,
        )
        if not feeds:
            return PipelineRunResult(
                run_id,
                mode,
                run_time,
                "all_sources_failed",
                counts,
                source_failures=tuple(source_failures),
                error="All enabled sources failed; no newsletter was rendered or sent.",
            )

        articles: list[Article] = []
        issue_count = 0
        for feed in feeds:
            normalized: NormalizationResult = normalize_feed(feed)
            articles.extend(normalized.articles)
            issue_count += len(normalized.issues)
        counts = _replace_counts(
            counts,
            normalized_articles=len(articles),
            rejected_entries=raw_count - len(articles),
            normalization_issues=issue_count,
        )

        try:
            deduplication = deduplicate_articles(articles)
            stories: tuple[Story, ...] = deduplication.stories
            categorized: tuple[CategorizedStory, ...] = categorize_stories(
                stories, configuration.category_rules
            )
            ranked = rank_stories(
                categorized,
                [source for source in enabled_sources],
                as_of=run_time,
                config=configuration.ranking,
            )
            selected: tuple[RankedStory, ...] = select_stories(ranked, configuration.selection)
        except Exception as error:
            failure = _safe_failure("processing", error)
            return PipelineRunResult(
                run_id,
                mode,
                run_time,
                "pipeline_failed",
                _replace_counts(
                    counts,
                    duplicate_articles=0,
                    stories=0,
                ),
                source_failures=tuple(source_failures),
                stage_failures=(failure,),
                error="A processing stage failed; no newsletter was sent.",
            )

        counts = _replace_counts(
            counts,
            duplicate_articles=len(articles) - len(stories),
            stories=len(stories),
            categorized_stories=sum(item.status != "unclassified" for item in categorized),
            ranked_stories=len(ranked),
            selected_stories=len(selected),
        )

        enriched: list[AIEnrichedStory] = []
        stage_failures: list[StageFailure] = []
        summarizer = self.dependencies.summarizer
        if selected and (mode == "send" or ai_in_preview):
            if summarizer is None and configuration.llm_settings is None:
                stage_failures.append(
                    StageFailure(
                        "llm_configuration",
                        "ValueError",
                        "Free LLM is not configured; source-description fallback used.",
                        category="missing_configuration",
                    )
                )
                enriched.extend(
                    source_description_fallback(item, generated_at=run_time) for item in selected
                )
            else:
                try:
                    summarizer = summarizer or _configured_summarizer(configuration.llm_settings)
                    for item in selected:
                        try:
                            summary = summarizer.summarize(item, generated_at=run_time)
                            enriched.append(summary)
                            if summary.failure_reason:
                                diagnostic = summary.diagnostic
                                stage_failures.append(
                                    StageFailure(
                                        "summarization",
                                        summary.failure_reason,
                                        _safe_llm_diagnostic_message(diagnostic),
                                        category=_safe_llm_category(
                                            diagnostic.category if diagnostic else None
                                        ),
                                        http_status=_safe_http_status(diagnostic),
                                        provider_error_type=(
                                            _safe_llm_identifier(diagnostic.provider_error_type)
                                            if diagnostic
                                            else None
                                        ),
                                        provider_error_code=(
                                            _safe_llm_identifier(diagnostic.provider_error_code)
                                            if diagnostic
                                            else None
                                        ),
                                        attempt_count=summary.attempt_count,
                                    )
                                )
                        except Exception as error:
                            stage_failures.append(
                                StageFailure(
                                    "summarization",
                                    type(error).__name__,
                                    "LLM enrichment failed; source-summary fallback used.",
                                    category="unknown",
                                )
                            )
                            enriched.append(
                                source_description_fallback(item, generated_at=run_time)
                            )
                finally:
                    if self.dependencies.summarizer is None:
                        provider_instance = getattr(summarizer, "provider", None)
                        close = getattr(provider_instance, "close", None)
                        if close is not None:
                            close()
        else:
            enriched.extend(
                source_description_fallback(item, generated_at=run_time) for item in selected
            )

        counts = _replace_counts(
            counts,
            summarized_stories=sum(item.summary is not None for item in enriched),
            fallback_stories=sum(item.status == "fallback" for item in enriched),
            summary_failures=(
                max(
                    sum(item.status == "failed" for item in enriched),
                    sum(failure.stage == "summarization" for failure in stage_failures),
                )
                if mode == "send" or ai_in_preview
                else 0
            ),
            omitted_stories=sum(item.summary is None for item in enriched),
        )

        if mode == "send" or ai_in_preview:
            _log_llm_enrichment(
                model=configuration.llm_settings.model if configuration.llm_settings else None,
                api_key=(
                    configuration.llm_settings.api_key
                    if configuration.llm_settings
                    else None
                ),
                selected_count=len(selected),
                enriched=enriched,
                stage_failures=stage_failures,
            )

        try:
            newsletter = render_newsletter(
                enriched,
                generated_at=run_time,
                timezone_name=configuration.timezone_name,
            )
        except Exception as error:
            failure = _safe_failure("render", error)
            return PipelineRunResult(
                run_id,
                mode,
                run_time,
                "pipeline_failed",
                counts,
                source_failures=tuple(source_failures),
                stage_failures=tuple(stage_failures + [failure]),
                error="Newsletter rendering failed.",
            )

        if mode == "preview" or (
            mode == "send"
            and self.dependencies.email_settings is None
            and configuration.email_settings is None
        ):
            local_date = run_time.astimezone(ZoneInfo(configuration.timezone_name)).date()
            delivery = deliver_or_preview(
                newsletter,
                subject=_subject(run_time, configuration.timezone_name),
                idempotency_key=f"preview/{local_date.isoformat()}",
                preview=True,
                preview_directory=preview_directory,
            )
            status: RunStatus = "partial" if source_failures else "preview"
            return PipelineRunResult(
                run_id,
                mode,
                run_time,
                status,
                counts,
                source_failures=tuple(source_failures),
                stage_failures=tuple(stage_failures),
                newsletter=newsletter,
                delivery=delivery,
            )

        if not newsletter.included_story_ids:
            status = "partial" if source_failures else "no_eligible_stories"
            return PipelineRunResult(
                run_id,
                mode,
                run_time,
                status,
                counts,
                source_failures=tuple(source_failures),
                stage_failures=tuple(stage_failures),
                newsletter=newsletter,
                error="No summarized eligible stories; email delivery was skipped.",
            )

        email_settings = self.dependencies.email_settings or configuration.email_settings
        if email_settings is None:
            failure = StageFailure(
                "email_configuration", "ValueError", "Email configuration is unavailable."
            )
            return PipelineRunResult(
                run_id,
                mode,
                run_time,
                "configuration_failed",
                counts,
                source_failures=tuple(source_failures),
                stage_failures=tuple(stage_failures + [failure]),
                newsletter=newsletter,
                error=failure.message,
            )
        briefing_date = run_time.astimezone(ZoneInfo(configuration.timezone_name)).date()
        key = newsletter_idempotency_key(email_settings.recipient, briefing_date)
        repository = self.dependencies.run_repository
        if repository is not None:
            try:
                claimed = repository.claim_delivery(key, run_id=run_id, claimed_at=run_time)
            except Exception as error:
                failure = _safe_failure("delivery_claim", error)
                return PipelineRunResult(
                    run_id,
                    mode,
                    run_time,
                    "persistence_failed",
                    counts,
                    source_failures=tuple(source_failures),
                    stage_failures=tuple(stage_failures + [failure]),
                    newsletter=newsletter,
                    error="Daily delivery could not be reserved safely; no email was sent.",
                )
            if not claimed:
                duplicate_delivery = _duplicate_delivery_result(email_settings)
                return PipelineRunResult(
                    run_id,
                    mode,
                    run_time,
                    "duplicate_send_skipped",
                    counts,
                    source_failures=tuple(source_failures),
                    stage_failures=tuple(stage_failures),
                    newsletter=newsletter,
                    delivery=duplicate_delivery,
                    error=duplicate_delivery.error,
                )
        try:
            delivery = deliver_or_preview(
                newsletter,
                subject=_subject(run_time, configuration.timezone_name),
                idempotency_key=key,
                settings=email_settings,
                provider=self.dependencies.email_provider,
            )
        except Exception as error:
            failure = _safe_failure("email_delivery", error)
            if repository is not None:
                try:
                    repository.complete_delivery_claim(key, outcome="unknown")
                except Exception:
                    failure = _safe_failure("delivery_claim_completion", error)
            return PipelineRunResult(
                run_id,
                mode,
                run_time,
                "delivery_failed",
                counts,
                source_failures=tuple(source_failures),
                stage_failures=tuple(stage_failures + [failure]),
                newsletter=newsletter,
                error="Email delivery failed before a provider result was available.",
            )

        if repository is not None:
            try:
                delivery_outcome = (
                    delivery.status
                    if delivery.status in {"accepted", "rejected", "failed", "unknown"}
                    else "unknown"
                )
                repository.complete_delivery_claim(
                    key,
                    outcome=delivery_outcome,
                    provider_message_id=delivery.provider_message_id,
                )
            except Exception as error:
                failure = _safe_failure("delivery_claim_completion", error)
                return PipelineRunResult(
                    run_id,
                    mode,
                    run_time,
                    "persistence_failed",
                    counts,
                    source_failures=tuple(source_failures),
                    stage_failures=tuple(stage_failures + [failure]),
                    newsletter=newsletter,
                    delivery=delivery,
                    error=(
                        "Email delivery was attempted, but its persistent outcome could not be "
                        "recorded; duplicate sends remain blocked."
                    ),
                )

        if delivery.status == "accepted":
            status = "partial" if source_failures or stage_failures else "sent"
        elif delivery.status == "unknown":
            status = "delivery_unknown"
        else:
            status = "delivery_failed"
        return PipelineRunResult(
            run_id,
            mode,
            run_time,
            status,
            counts,
            source_failures=tuple(source_failures),
            stage_failures=tuple(stage_failures),
            newsletter=newsletter,
            delivery=delivery,
            error=delivery.error,
        )


def _load_configuration(
    directory: Path,
    *,
    mode: RunMode,
    ai_in_preview: bool,
    dependencies: PipelineDependencies,
    environ: Mapping[str, str] | None,
) -> _RunConfiguration:
    env = os.environ if environ is None else environ
    sources = load_sources(directory / "sources.yaml")
    category_rules = load_category_rules(directory / "categories.yaml")
    ranking = load_ranking_config(directory / "ranking.yaml")
    selection = load_selection_config(directory / "selection.yaml")
    timezone_name = env.get("NEWSLETTER_TIMEZONE", "").strip() or "UTC"
    try:
        ZoneInfo(timezone_name)  # Fail early on a bad configured timezone.
    except Exception as error:
        raise ValueError("NEWSLETTER_TIMEZONE must be a valid IANA timezone") from error

    need_llm = mode == "send" or ai_in_preview
    llm_settings = None
    if need_llm and dependencies.summarizer is None:
        try:
            llm_settings = LLMSettings.from_env(dict(env))
        except ValueError:
            # AI is optional enrichment; missing/malformed configuration uses the
            # already-supported source-description fallback.
            llm_settings = None
    email_settings = (
        dependencies.email_settings
        if mode == "send" and dependencies.email_settings is not None
        else _optional_email_settings(dict(env), dependencies)
        if mode == "send"
        else None
    )
    return _RunConfiguration(
        sources,
        category_rules,
        ranking,
        selection,
        timezone_name,
        llm_settings,
        email_settings,
    )


def _optional_email_settings(
    environ: dict[str, str], dependencies: PipelineDependencies
) -> EmailConfiguration | None:
    """Return email settings only when complete settings and credentials are available."""
    provider = environ.get("EMAIL_PROVIDER", "smtp").strip().casefold() or "smtp"
    if provider == "smtp":
        username = environ.get("SMTP_USERNAME", "").strip()
        recipient = environ.get("NEWSLETTER_RECIPIENT", "").strip()
        if not username or not recipient:
            return None
        if dependencies.email_provider is None and not smtp_credential_configured(
            username, environ=environ
        ):
            return None
        return SMTPSettings.from_env(environ)
    if provider == "resend":
        if not environ.get("RESEND_API_KEY", "").strip():
            return None
        return email_settings_from_env(environ)
    raise ValueError("EMAIL_PROVIDER must be either 'smtp' or 'resend'")


def _make_source(
    dependencies: PipelineDependencies,
    source_config: SourceConfig,
    as_of: datetime,
) -> SourceAdapter:
    if dependencies.source_factory is not None:
        return dependencies.source_factory(source_config, as_of)
    return RSSAtomSource(source_config, clock=lambda: as_of)


def _configured_summarizer(settings: LLMSettings | None) -> Summarizer:
    if settings is None:
        settings = LLMSettings.from_env()
    from app.llm.openai_compatible import OpenAICompatibleProvider

    return Summarizer(
        OpenAICompatibleProvider(settings),
        max_attempts=settings.max_attempts,
    )


def _subject(as_of: datetime, timezone_name: str) -> str:
    local = as_of.astimezone(ZoneInfo(timezone_name))
    local_date = f"{local:%A, %B} {local.day}"
    return f"Personal News Briefing — {local_date}"


def _log_run_started(
    run_id: UUID, mode: RunMode, as_of: datetime, started_at: datetime
) -> None:
    _LOGGER.info(
        "%s",
        json.dumps(
            {
                "event": "pipeline.run.started",
                "run_id": str(run_id),
                "mode": mode,
                "as_of": as_of.isoformat(),
                "started_at": started_at.isoformat(),
            },
            sort_keys=True,
        ),
    )


def _log_run_finished(result: PipelineRunResult, completed_at: datetime) -> None:
    """Emit operational metadata only; never log provider messages or newsletter text."""
    _LOGGER.info(
        "%s",
        json.dumps(
            {
                "event": "pipeline.run.finished",
                "run_id": str(result.run_id),
                "mode": result.mode,
                "as_of": result.as_of.isoformat(),
                "completed_at": completed_at.isoformat(),
                "status": result.status,
                "counts": {
                    name: getattr(result.counts, name)
                    for name in result.counts.__dataclass_fields__
                },
                "delivery_attempted": result.mode == "send"
                and result.delivery is not None
                and result.delivery.provider != "preview"
                and result.delivery.status != "duplicate_skipped",
                "delivery_status": result.delivery.status if result.delivery else None,
                "source_failures": [
                    {
                        "source_id": failure.source_id,
                        "stage": failure.stage,
                        "error_type": failure.error_type,
                    }
                    for failure in result.source_failures
                ],
                "stage_failures": [
                    {
                        "stage": failure.stage,
                        "error_type": failure.error_type,
                        "category": failure.category,
                        "http_status": failure.http_status,
                        "provider_error_type": failure.provider_error_type,
                        "provider_error_code": failure.provider_error_code,
                        "attempt_count": failure.attempt_count,
                        "retry_count": max(0, failure.attempt_count - 1),
                    }
                    for failure in result.stage_failures
                ],
            },
            sort_keys=True,
        ),
    )


def _log_llm_enrichment(
    *,
    model: str | None,
    api_key: str | None,
    selected_count: int,
    enriched: list[AIEnrichedStory],
    stage_failures: list[StageFailure],
) -> None:
    """Emit aggregate LLM outcomes only; prompts and response bodies stay private."""
    model_name = model or next(
        (item.model for item in enriched if item.provider != "not_called"), None
    )
    if model_name and api_key and api_key in model_name:
        model_name = None
    failures: Counter[str] = Counter()
    for failure in stage_failures:
        if failure.stage == "llm_configuration":
            failures[_safe_llm_category(failure.category)] += selected_count
        elif failure.stage == "summarization":
            failures[_safe_llm_category(failure.category)] += 1
    _LOGGER.info(
        "%s",
        json.dumps(
            {
                "event": "llm.enrichment.completed",
                "model": model_name,
                "selected_stories": selected_count,
                "attempted_stories": sum(item.attempt_count > 0 for item in enriched),
                "request_attempts": sum(item.attempt_count for item in enriched),
                "retry_count": sum(max(0, item.attempt_count - 1) for item in enriched),
                "generated_stories": sum(item.status == "generated" for item in enriched),
                "fallback_stories": sum(item.status == "fallback" for item in enriched),
                "failed_stories": sum(item.status == "failed" for item in enriched),
                "failure_categories": dict(sorted(failures.items())),
            },
            sort_keys=True,
        ),
    )


_LLM_CATEGORIES = {
    "http_error",
    "rate_limit",
    "timeout",
    "invalid_response",
    "empty_response",
    "validation_error",
    "provider_error",
    "missing_configuration",
    "unknown",
}


def _safe_llm_category(category: str | None) -> str:
    if category in {"rate_limiting"}:
        return "rate_limit"
    return category if category in _LLM_CATEGORIES else "unknown"


def _safe_llm_identifier(value: str | None) -> str | None:
    if value is None or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value):
        return None
    return value


def _safe_http_status(diagnostic: ProviderDiagnostic | None) -> int | None:
    if diagnostic is None:
        return None
    status = diagnostic.http_status
    return status if isinstance(status, int) and 100 <= status <= 599 else None


def _safe_llm_diagnostic_message(diagnostic: ProviderDiagnostic | None) -> str:
    category = _safe_llm_category(diagnostic.category if diagnostic else None)
    messages = {
        "http_error": "LLM provider returned an HTTP error.",
        "rate_limit": "LLM provider rate limit was reached.",
        "timeout": "LLM provider request timed out.",
        "invalid_response": "LLM provider response was invalid.",
        "empty_response": "LLM provider returned empty content.",
        "validation_error": "LLM response failed schema validation.",
        "provider_error": "LLM provider request failed.",
        "missing_configuration": "LLM configuration is missing.",
        "unknown": "LLM enrichment failed; source-summary fallback used.",
    }
    return messages[category]


def _safe_failure(stage: str, error: Exception, source_id: str | None = None) -> StageFailure:
    error_type = type(error).__name__
    message = f"{stage.replace('_', ' ').capitalize()} failed ({error_type})."
    return StageFailure(stage, error_type, message, source_id)


def _duplicate_delivery_result(settings: EmailConfiguration) -> DeliveryResult:
    return DeliveryResult(
        "duplicate_skipped",
        False,
        False,
        "smtp" if isinstance(settings, SMTPSettings) else "resend",
        error="A delivery for this recipient and local date was already attempted.",
    )


def _replace_counts(counts: PipelineCounts, **updates: int) -> PipelineCounts:
    values = {
        field_name: getattr(counts, field_name)
        for field_name in PipelineCounts.__dataclass_fields__
    }
    values.update(updates)
    return PipelineCounts(**values)
