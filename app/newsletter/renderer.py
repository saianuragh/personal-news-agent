"""Pure HTML and plain-text rendering for summarized ranked stories."""

from __future__ import annotations

import re
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
NAV_LABELS = {
    "Technology": "TECH",
    "Business & Economy": "BUSINESS",
    "Science & Space": "SCIENCE",
}
CATEGORY_SLUGS = {
    "India": "india",
    "World": "world",
    "AI": "ai",
    "Technology": "technology",
    "Business & Economy": "business",
    "Science & Space": "science",
    "Sports": "sports",
    "Entertainment": "entertainment",
    "Unclassified": "unclassified",
}


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
    published: str
    published_iso: str | None
    description: str | None


@dataclass(frozen=True, slots=True)
class _StoryDisplay:
    story_id: UUID
    number: int
    anchor: str
    headline: str
    summary: str
    summary_label: str
    why_it_matters: str | None
    categories: tuple[str, ...]
    sources: tuple[_SourceDisplay, ...]
    status: str


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
    for number, enriched in enumerate(accepted, 1):
        ranked = enriched.ranked_story
        story_id = ranked.story.story_id
        ordered_articles = sorted(
            ranked.story.members,
            key=lambda article: (
                article.source_id.casefold(),
                article.publisher.casefold(),
                article.url,
            ),
        )
        sources = tuple(
            _SourceDisplay(
                publisher=article.publisher,
                url=article.url,
                published=(
                    _format_time(article.published_at, zone)
                    if article.published_at is not None
                    else "Publication time not provided"
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
            number=number,
            anchor=f"story-{number:02d}",
            headline=ranked.story.retained_article.title,
            summary=enriched.summary or "",
            summary_label=(
                "AI-generated summary"
                if enriched.status == "generated"
                else "Source description (fallback)"
            ),
            why_it_matters=enriched.why_it_matters,
            categories=ranked.categories,
            sources=sources,
            status=enriched.status,
        )

    category_groups: dict[str, list[_StoryDisplay]] = defaultdict(list)
    for enriched in accepted:
        display = display_by_id[enriched.ranked_story.story.story_id]
        for category in enriched.ranked_story.categories:
            category_groups[category].append(display)

    timestamp = _format_time(generated_at, zone)
    local_date = generated_at.astimezone(zone).strftime("%d %B %Y").upper()
    categories = list(SECTION_ORDER[:-1])
    categories.extend(sorted(set(category_groups) - set(categories), key=str.casefold))
    category_anchors = _category_anchor_ids(categories)
    displays = [display_by_id[item.ranked_story.story.story_id] for item in accepted]
    html_categories = "\n".join(
        _render_html_category(
            category,
            category_groups.get(category, []),
            category_anchors[category],
            generated_date=local_date,
        )
        for category in categories
    )
    view_states = '<span class="view-state" id="view-home"></span>' + "".join(
        f'<span class="view-state" id="{category_anchors[category]}"></span>'
        for category in categories
    )
    view_rules = (
        "#view-home:target ~ .edition-nav a[href='#view-home'] "
        "{color:#b3261e!important;border-bottom:2px solid #b3261e!important;}"
        "#view-home:target ~ .edition-content .home-view {display:block!important;}"
        "#view-home:target ~ .edition-content .category-view {display:none!important;}"
        + "".join(
            f"#{category_anchors[category]}:target ~ .edition-nav "
            f"a[href='#{category_anchors[category]}'] "
            "{color:#b3261e!important;border-bottom:2px solid #b3261e!important;}"
            f"#{category_anchors[category]}:target ~ .edition-content .home-view "
            "{display:none!important;}"
            f"#{category_anchors[category]}:target ~ .edition-content "
            f".{_view_class(category_anchors[category])} {{display:block!important;}}"
            for category in categories
        )
    )
    html_navigation = (
        '<a class="nav-link today-link" href="#view-home">TODAY</a>'
        + "".join(
            f'<a class="nav-link" href="#{category_anchors[category]}">'
            f'{escape(NAV_LABELS.get(category, category.upper()))}</a>'
            for category in categories
        )
    )
    count = len(displays)
    story_word = "story" if count == 1 else "stories"
    addon_html, addon_text = _render_addons(displays, generated_at)
    template_source = (
        template_path.read_text(encoding="utf-8")
        if not isinstance(template_path, str)
        else Path(template_path).read_text(encoding="utf-8")
    )
    template = Template(template_source)
    html_body = template.substitute(
        title=escape(title),
        generated_at=escape(timestamp),
        generated_iso=escape(generated_at.isoformat()),
        local_date=escape(local_date),
        count=str(count),
        story_word=story_word,
        view_rules=view_rules,
        navigation=html_navigation,
        view_states=view_states,
        empty_notice=(
            '<p class="empty-state">No eligible stories are available today. '
            "Check back soon for your next briefing.</p>"
            if not displays
            else ""
        ),
        addons=addon_html,
        featured=_render_html_story(displays[0], featured=True) if displays else "",
        feed=(
            "\n".join(_render_html_story(item, featured=False) for item in displays[1:])
            if len(displays) > 1
            else ""
        ),
        categories=html_categories,
    )

    text_sections = [_render_text_story(display) for display in displays]
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
            "DAILY NEWS",
            local_date,
            f"Generated: {timestamp}",
            *addon_text,
            "TODAY'S STORIES",
            *(text_sections or ["No eligible stories are available today. Check back soon."]),
            *category_indexes,
        )
    )
    return NewsletterDocument(
        html=html_body,
        plain_text=plain_text,
        included_story_ids=tuple(item.story_id for item in displays),
        omitted_story_ids=tuple(omitted),
    )


def _render_addons(
    stories: list[_StoryDisplay], generated_at: datetime
) -> tuple[str, list[str]]:
    """Build editorial extras only from selected, source-linked stories."""
    blocks: list[str] = []
    text_blocks: list[str] = []

    top_stories = stories[:5]
    if top_stories:
        cards: list[str] = []
        text_lines = ["TOP STORIES"]
        for number, story in enumerate(top_stories, 1):
            source = story.sources[0]
            why = story.why_it_matters
            cards.append(
                '<article class="addon-story"><p class="addon-number">'
                f'{number:02d} · {escape(", ".join(story.categories))}</p>'
                f'<h3><a href="{escape(source.url, quote=True)}">{escape(story.headline)}</a></h3>'
                f'<p>{escape(story.summary)}</p>'
                + (f'<p class="addon-why">Why it matters: {escape(why)}</p>' if why else "")
                + f'<p class="addon-source">{escape(source.publisher)} · '
                f'<a href="{escape(source.url, quote=True)}">Original report</a></p></article>'
            )
            text_lines.extend(
                [
                    f"{number:02d}. {story.headline}",
                    story.summary,
                    *([f"Why it matters: {why}"] if why else []),
                    f"Source: {source.publisher} — {source.url}",
                ]
            )
        blocks.append(_addon_section("TOP STORIES", "Five highest-ranked stories", cards))
        text_blocks.append("\n".join(text_lines))

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
        if len(recent) == 5:
            break
    if recent:
        blocks.append(
            _addon_section(
                "IMPORTANT TODAY",
                "Recent coverage prioritized by the story ranking",
                [_addon_link_story(story) for story in recent],
            )
        )
        text_blocks.append(
            "IMPORTANT TODAY\n" + "\n".join(f"• {story.headline}" for story in recent)
        )

    ai_stories = [story for story in stories if "AI" in story.categories][:3]
    if ai_stories:
        blocks.append(
            _addon_section(
                "AI WATCH",
                "Leading AI developments in today's coverage",
                [_addon_link_story(story, include_summary=True) for story in ai_stories],
            )
        )
        text_blocks.append(
            "AI WATCH\n"
            + "\n\n".join(f"{story.headline}\n{story.summary}" for story in ai_stories)
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
        fact = _first_sentence(source.description or "")[:240]
        if fact:
            blocks.append(
                '<section class="newspaper-addon"><p class="addon-label">FACT OF THE DAY</p>'
                f'<p>{escape(fact)}</p><p class="addon-source">Source-reported · '
                f'{escape(source.publisher)} · <a href="{escape(source.url, quote=True)}">'
                "Original report</a></p></section>"
            )
            text_blocks.append(
                f"FACT OF THE DAY\n{fact}\nSource-reported by {source.publisher}: {source.url}"
            )

    return "\n".join(blocks), text_blocks


def _addon_section(label: str, subtitle: str, cards: list[str]) -> str:
    return (
        '<section class="newspaper-addon"><p class="addon-label">'
        f'{escape(label)}</p><p class="addon-subtitle">{escape(subtitle)}</p>'
        + "".join(cards)
        + "</section>"
    )


def _addon_link_story(story: _StoryDisplay, *, include_summary: bool = False) -> str:
    source = story.sources[0]
    summary = f"<p>{escape(story.summary)}</p>" if include_summary else ""
    return (
        '<article class="addon-story"><h3>'
        f'<a href="{escape(source.url, quote=True)}">{escape(story.headline)}</a></h3>'
        f'{summary}<p class="addon-source">{escape(source.publisher)} · '
        f'<a href="{escape(source.url, quote=True)}">Original report</a></p></article>'
    )


def _first_sentence(text: str) -> str:
    """Keep a source-provided fact excerpt brief without generating new claims."""
    cleaned = " ".join(text.split())
    sentence_end = next((index for index, char in enumerate(cleaned) if char in ".!?"), -1)
    return cleaned[: sentence_end + 1] if sentence_end >= 0 else cleaned


def _render_html_story(
    display: _StoryDisplay,
    *,
    featured: bool,
    number: int | None = None,
    anchor: str | None = None,
) -> str:
    category_badges = "".join(
        f'<span class="category-badge">{escape(category)}</span>'
        for category in display.categories
    )
    primary_source = display.sources[0]
    publisher_line = f"{escape(primary_source.publisher)} · {escape(primary_source.published)}"
    additional_sources = "".join(
        f'<a class="source-link" href="{escape(source.url, quote=True)}">'
        f"{escape(source.publisher)}</a>"
        for source in display.sources[1:]
    )
    summary = (
        f'<p class="summary-label">{escape(display.summary_label)}</p>'
        f'<p class="summary">{escape(display.summary)}</p>'
    )
    significance = (
        '<div class="why"><p class="why-label">WHY IT MATTERS</p>'
        f'<p>{escape(display.why_it_matters)}</p></div>'
        if display.why_it_matters
        else ""
    )
    css_class = "story-card featured-card" if featured else "story-card feed-card"
    inline_story_style = (
        "margin:0 0 27px;padding:22px 0 24px;background:#fffefa;"
        "border-top:2px solid #b3261e;border-bottom:1px solid #d7d3cc;"
        if featured
        else "margin:0;padding:20px 0;background:transparent;border-bottom:1px solid #d7d3cc;"
    )
    kicker = '<span class="top-story">TOP STORY</span>' if featured else ""
    cta_label = "READ FULL STORY →" if featured else "READ STORY →"
    story_number = display.number if number is None else number
    story_anchor = display.anchor if anchor is None else anchor
    return (
        f'<article class="{css_class}" id="{story_anchor}" '
        f'style="{inline_story_style}">'
        '<table role="presentation" class="story-layout"><tbody><tr>'
        f'<td class="story-number">{story_number:02d}</td>'
        '<td class="story-content">'
        f'<p class="story-kicker">{kicker}{category_badges}</p>'
        f'<h2 class="story-title">{escape(display.headline)}</h2>'
        f'<p class="metadata">{publisher_line}</p>'
        f'{summary}{significance}'
        f'<a class="story-cta" href="{escape(primary_source.url, quote=True)}" '
        'style="display:inline-block;margin:5px 0 0;padding:10px 0;'
        'border-bottom:1px solid #b3261e;color:#b3261e;font-size:10px;'
        'font-weight:bold;letter-spacing:.8px;text-decoration:none">'
        f"{cta_label}</a>"
        f'<p class="additional-sources">{additional_sources}</p>'
        '</td></tr></tbody></table></article>'
    )


def _render_html_category(
    category: str,
    members: list[_StoryDisplay],
    anchor: str,
    *,
    generated_date: str,
) -> str:
    class_name = _view_class(anchor)
    stories = "".join(
        _render_html_story(
            display,
            featured=False,
            number=index,
            anchor=f"{anchor}-story-{index:02d}",
        )
        for index, display in enumerate(members, 1)
    )
    empty = (
        '<p class="empty-category">No stories in this category in today\'s briefing.</p>'
        if not members
        else ""
    )
    return (
        f'<section class="newsletter-view category-view {class_name}">'
        f'<p class="view-kicker">CATEGORY EDITION · {escape(generated_date)}</p>'
        f'<h2 class="category-title">{escape(category)}</h2>'
        f'<p class="view-subtitle">YOUR DAILY NEWS, SELECTED</p>'
        f'{stories}{empty}'
        '<p class="back-row"><a class="back-to-home" href="#view-home" '
        'style="display:inline-block;padding:12px 0;color:#5f5f5f;font-size:10px;'
        'font-weight:bold;letter-spacing:.8px;text-decoration:none;border-top:1px solid #d7d3cc">'
        "← BACK TO HOME</a></p></section>"
    )


def _category_anchor_ids(categories: list[str]) -> dict[str, str]:
    """Create stable, email-friendly fragment IDs for populated categories."""
    result: dict[str, str] = {}
    used: set[str] = set()
    for category in categories:
        slug = CATEGORY_SLUGS.get(category)
        if slug is None:
            slug = re.sub(r"[^a-z0-9]+", "-", category.casefold()).strip("-") or "category"
        candidate = f"category-{slug}"
        suffix = 2
        while candidate in used:
            candidate = f"category-{slug}-{suffix}"
            suffix += 1
        used.add(candidate)
        result[category] = candidate
    return result


def _render_text_story(display: _StoryDisplay) -> str:
    lines = [
        f"{display.number:02d}. {display.headline}",
        f"   {display.sources[0].publisher} · {display.sources[0].published}",
        f"{display.summary_label}: {display.summary}",
    ]
    if display.why_it_matters:
        lines.append(f"Why it matters: {display.why_it_matters}")
    lines.append(f"Read: {display.sources[0].url}")
    if len(display.sources) > 1:
        lines.append("Other sources:")
        lines.extend(f"- {source.publisher}: {source.url}" for source in display.sources[1:])
    return "\n".join(lines)


def _render_text_category(category: str, members: list[_StoryDisplay]) -> str:
    lines = [f"{category}\n{'=' * len(category)}"]
    lines.extend(f"{display.number:02d}. {display.headline}" for display in members)
    return "\n".join(lines)


def _view_class(anchor: str) -> str:
    return f"category-view-{anchor.removeprefix('category-')}"


def _format_time(value: datetime, zone: ZoneInfo) -> str:
    return value.astimezone(zone).strftime("%a, %d %b %Y %H:%M %Z")


def _load_timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, TypeError) as error:
        raise ValueError(f"Unknown IANA timezone: {timezone_name}") from error
