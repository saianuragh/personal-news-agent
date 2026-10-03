"""Pure HTML and plain-text rendering for summarized ranked stories."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from html import escape
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from string import Template
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.llm.base import AIEnrichedStory

DEFAULT_TEMPLATE = files("app.newsletter").joinpath("templates", "newsletter.html")
SECTION_ORDER = (
    "India",
    "World",
    "AI",
    "Technology",
    "Business & Economy",
    "Science & Space",
    "Sports",
    "Entertainment",
    "Unclassified",
)
@dataclass(frozen=True, slots=True)
class NewsletterDocument:
    """Rendered alternatives and the story IDs included in the artifact."""

    html: str
    plain_text: str
    included_story_ids: tuple[UUID, ...]
    omitted_story_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class _SourceDisplay:
    publisher: str
    url: str
    published: str | None
    published_iso: str | None
    description: str | None


@dataclass(frozen=True, slots=True)
class _StoryDisplay:
    story_id: UUID
    headline: str
    summary: str
    categories: tuple[str, ...]
    sources: tuple[_SourceDisplay, ...]


def render_newsletter(
    stories: Sequence[AIEnrichedStory],
    *,
    generated_at: datetime,
    timezone_name: str,
    title: str = "Personal News Briefing",
    template_path: str | Path | Traversable = DEFAULT_TEMPLATE,
) -> NewsletterDocument:
    """Render selected enriched stories without fetching or mutating pipeline data.

    Failed enrichment results, stories without usable summaries, and
    unclassified/ineligible ranked stories are omitted, matching the approved
    architecture's fallback-or-omit policy. Stories in multiple categories
    appear once in the Home view and in every applicable category view.
    """
    zone = _load_timezone(timezone_name)
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("generated_at must be timezone-aware")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title must be a non-empty string")

    seen_ids: set[UUID] = set()
    accepted: list[AIEnrichedStory] = []
    omitted: list[UUID] = []
    for enriched in stories:
        ranked = enriched.ranked_story
        story_id = ranked.story.story_id
        if story_id in seen_ids:
            raise ValueError(f"duplicate story supplied to renderer: {story_id}")
        seen_ids.add(story_id)
        if (
            ranked.selection_status != "candidate"
            or not ranked.categories
            or enriched.status == "failed"
            or not enriched.summary
        ):
            omitted.append(story_id)
        else:
            accepted.append(enriched)

    # Ranking is the editorial ordering contract; caller ordering must not
    # accidentally promote a lower-ranked selected story to the feature.
    accepted.sort(key=lambda item: (item.ranked_story.rank, str(item.ranked_story.story.story_id)))
    display_by_id: dict[UUID, _StoryDisplay] = {}
    for enriched in accepted:
        ranked = enriched.ranked_story
        story_id = ranked.story.story_id
        retained_article = ranked.story.retained_article
        other_articles = sorted(
            (
                article
                for article in ranked.story.members
                if article.article_id != retained_article.article_id
            ),
            key=lambda article: (
                article.source_id.casefold(),
                article.publisher.casefold(),
                article.url,
            ),
        )
        ordered_articles = [retained_article, *other_articles]
        sources = tuple(
            _SourceDisplay(
                publisher=article.publisher,
                url=article.url,
                published=(
                    _format_time(article.published_at, zone)
                    if article.published_at is not None
                    else None
                ),
                published_iso=(
                    article.published_at.isoformat() if article.published_at is not None else None
                ),
                description=article.description,
            )
            for article in ordered_articles
        )
        display_by_id[story_id] = _StoryDisplay(
            story_id=story_id,
            headline=ranked.story.retained_article.title,
            summary=_compact_summary(enriched.summary or ""),
            categories=ranked.categories,
            sources=sources,
        )

    category_groups: dict[str, list[_StoryDisplay]] = defaultdict(list)
    for enriched in accepted:
        display = display_by_id[enriched.ranked_story.story.story_id]
        for category in enriched.ranked_story.categories:
            category_groups[category].append(display)

    local_date = generated_at.astimezone(zone).strftime("%d %B %Y")
    displays = [display_by_id[item.ranked_story.story.story_id] for item in accepted]
    html_categories = "\n".join(
        _render_html_category(
            category,
            category_groups[category][:3],
        )
        for category in SECTION_ORDER[:-1]
        if category_groups[category]
    )
    addon_html, addon_text = _render_addons(displays, generated_at)
    template_source = (
        template_path.read_text(encoding="utf-8")
        if not isinstance(template_path, str)
        else Path(template_path).read_text(encoding="utf-8")
    )
    template = Template(template_source)
    html_body = template.substitute(
        title=escape(title),
        generated_iso=escape(generated_at.isoformat()),
        local_date=escape(local_date),
        empty_notice=(
            '<p class="empty-state">No eligible stories are available today. '
            "Check back soon for your next briefing.</p>"
            if not displays
            else ""
        ),
        addons=addon_html,
        top_stories="\n".join(
            _render_html_story(item, featured=index == 1)
            for index, item in enumerate(displays[:5], 1)
        ),
        categories=html_categories,
    )

    text_sections = [_render_text_story(display) for display in displays[:5]]
    category_indexes = [
        _render_text_category(category, category_groups[category])
        for category in SECTION_ORDER[:-1]
        if category_groups[category]
    ]
    category_indexes.extend(
        _render_text_category(category, category_groups[category])
        for category in sorted(
            set(category_groups) - set(SECTION_ORDER[:-1]), key=str.casefold
        )
    )
    plain_text = "\n\n".join(
        (
            "PERSONAL NEWS",
            local_date,
            "TOP 5",
            *(text_sections or ["No eligible stories are available today. Check back soon."]),
            *category_indexes,
            *addon_text,
        )
    )
    return NewsletterDocument(
        html=html_body,
        plain_text=plain_text,
        included_story_ids=tuple(item.story_id for item in displays),
        omitted_story_ids=tuple(omitted),
    )


SUMMARY_LIMIT = 180


def _compact_summary(value: str, maximum: int = SUMMARY_LIMIT) -> str:
    """Keep the email index scannable without changing the underlying story data."""
    text = " ".join(value.split())
    if len(text) <= maximum:
        return text
    excerpt = text[: maximum - 1]
    if " " in excerpt:
        excerpt = excerpt.rsplit(" ", 1)[0]
    return f"{excerpt.rstrip(' ,;:.-')}…"


def _render_addons(
    stories: list[_StoryDisplay], generated_at: datetime
) -> tuple[str, list[str]]:
    """Render compact, source-linked editorial extras from existing selected stories."""
    blocks: list[str] = []
    text_blocks: list[str] = []

    recent: list[_StoryDisplay] = []
    cutoff = generated_at.timestamp() - 24 * 60 * 60
    for story in stories:
        published = story.sources[0].published_iso
        if published:
            try:
                if datetime.fromisoformat(published).timestamp() >= cutoff:
                    recent.append(story)
            except ValueError:
                continue
        if len(recent) == 3:
            break
    if recent:
        links = "".join(
            '<li style="padding:3px 0;color:#383838;font-size:12px;line-height:1.45">'
            f'<a style="color:#383838;text-decoration:none" '
            f'href="{escape(story.sources[0].url, quote=True)}">'
            f'{escape(story.headline)} — {escape(story.sources[0].publisher)}</a></li>'
            for story in recent
        )
        blocks.append(_addon_section("⚡ IMPORTANT TODAY", f"<ul>{links}</ul>"))
        text_blocks.append(
            "IMPORTANT TODAY\n"
            + "\n".join(
                f"• {story.headline} — {story.sources[0].publisher}: {story.sources[0].url}"
                for story in recent
            )
        )

    ai_stories = [story for story in stories if "AI" in story.categories][:3]
    if ai_stories:
        blocks.append(
            _addon_section(
                "🤖 AI WATCH",
                "".join(
                    _render_html_story(story, featured=False, compact=True)
                    for story in ai_stories
                ),
            )
        )
        text_blocks.append(
            "AI WATCH\n"
            + "\n\n".join(_render_text_story(story) for story in ai_stories)
        )

    fact_story = next(
        (
            story
            for story in stories
            if story.sources[0].description and story.sources[0].description.strip()
        ),
        None,
    )
    if fact_story is not None:
        source = fact_story.sources[0]
        fact = _compact_summary(_first_sentence(source.description or ""), 200)
        if fact:
            blocks.append(
                _addon_section(
                    "🧠 FACT OF THE DAY",
                    '<p style="margin:0 0 7px;color:#383838;font-size:13px;line-height:1.5">'
                    f'{escape(fact)}</p><p class="metadata" style="margin:0;color:#77736d;'
                    'font-size:10px;line-height:1.4">Source-reported · '
                    f'{escape(source.publisher)} · '
                    f'<a href="{escape(source.url, quote=True)}">Read</a></p>',
                )
            )
            text_blocks.append(
                f"FACT OF THE DAY\n{fact}\nSource: {source.publisher} · {source.url}"
            )

    return "\n".join(blocks), text_blocks


def _addon_section(label: str, content: str) -> str:
    return (
        '<section class="addon" style="margin-top:22px;padding-top:14px;'
        'border-top:1px solid #dedbd5"><h2 class="section-heading" '
        'style="margin:0 0 4px;color:#77736d;font-size:10px;font-weight:bold;'
        'letter-spacing:1.4px">'
        f'{escape(label)}</h2>{content}</section>'
    )


def _first_sentence(text: str) -> str:
    """Keep a source-provided fact excerpt brief without generating new claims."""
    cleaned = " ".join(text.split())
    sentence_end = next((index for index, char in enumerate(cleaned) if char in ".!?"), -1)
    return cleaned[: sentence_end + 1] if sentence_end >= 0 else cleaned


def _render_html_story(
    display: _StoryDisplay, *, featured: bool, compact: bool = False
) -> str:
    source = display.sources[0]
    category = display.categories[0] if display.categories else "NEWS"
    metadata = " · ".join(
        value for value in (source.publisher, source.published) if value
    )
    class_name = "story lead" if featured else "story compact" if compact else "story"
    return (
        f'<article class="{class_name}" style="padding:15px 0;'
        'border-bottom:1px solid #e7e3dc;">'
        f'<p class="category" style="margin:0;color:#9e2924;font-size:9px;font-weight:bold;'
        f'letter-spacing:1.2px">{escape(category.upper())}</p>'
        f'<h3 style="margin:3px 0 6px;font-family:Georgia,\'Times New Roman\',serif;'
        f'font-size:{15 if compact else 19}px;line-height:1.3">'
        f'<a style="color:#171717;text-decoration:none" '
        f'href="{escape(source.url, quote=True)}">{escape(display.headline)}</a></h3>'
        f'<p class="summary" style="margin:0 0 7px;color:#383838;font-size:'
        f'{12 if compact else 13}px;line-height:1.5">{escape(display.summary)}</p>'
        f'<p class="metadata" style="margin:0 0 5px;color:#77736d;font-size:10px;'
        f'line-height:1.4">{escape(metadata)}</p>'
        f'<a class="read-more" style="display:inline-block;padding:5px 0;color:#9e2924;'
        f'font-size:10px;font-weight:bold;letter-spacing:.7px;text-decoration:none" '
        f'href="{escape(source.url, quote=True)}">'
        f'{"→ READ" if compact else "READ MORE →"}</a></article>'
    )


def _render_html_category(category: str, members: list[_StoryDisplay]) -> str:
    stories = "".join(
        _render_html_story(display, featured=False, compact=True) for display in members[:3]
    )
    return (
        '<section class="category-section" style="margin-top:20px">'
        f'<h2 class="section-heading" style="margin:0;padding:14px 0 4px;border-top:1px solid '
        f'#dedbd5;color:#171717;font-family:Georgia,\'Times New Roman\',serif;font-size:17px">'
        f'{escape(category)}</h2>{stories}</section>'
    )


def _render_text_story(display: _StoryDisplay, *, compact: bool = False) -> str:
    source = display.sources[0]
    metadata = " · ".join(value for value in (source.publisher, source.published) if value)
    lines = [
        display.categories[0] if display.categories else "NEWS",
        display.headline,
        display.summary,
        metadata,
    ]
    lines.append(f"→ Read: {source.url}" if compact else f"READ MORE: {source.url}")
    return "\n".join(line for line in lines if line)


def _render_text_category(category: str, members: list[_StoryDisplay]) -> str:
    lines = [category.upper()]
    lines.extend(_render_text_story(display, compact=True) for display in members[:3])
    return "\n\n".join(lines)


def _format_time(value: datetime, zone: ZoneInfo) -> str:
    return value.astimezone(zone).strftime("%H:%M %Z")


def _load_timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, TypeError) as error:
        raise ValueError(f"Unknown IANA timezone: {timezone_name}") from error
