"""Pure HTML and plain-text rendering for summarized ranked stories."""

from __future__ import annotations

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

INK = "#1a1a1a"
PAPER_RULE = "#cfc6b2"
MUTED = "#6e675b"
ACCENT = "#7a1f1f"
SERIF = "Georgia,'Times New Roman',Times,serif"
IMAGE_FILTER = "filter:grayscale(1) contrast(1.05);"


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
    image_url: str | None = None


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
    architecture's fallback-or-omit policy. Each story appears at most once
    in the newsletter, in both HTML and plain text.
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
                image_url=article.image_url,
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

    local_date = generated_at.astimezone(zone).strftime("%d %B %Y")
    displays = [display_by_id[item.ranked_story.story.story_id] for item in accepted]
    top_five = displays[:5]
    sections = _build_sections(displays, top_five)
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
        addons="",
        top_stories=_render_top_stories(top_five),
        categories="\n".join(
            _render_html_category(category, members) for category, members in sections
        ),
    )

    text_sections = [_render_text_story(display) for display in top_five]
    category_indexes = [_render_text_category(category, members) for category, members in sections]
    plain_text = "\n\n".join(
        (
            "PERSONAL NEWS",
            local_date,
            "TOP 5",
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


SUMMARY_LIMIT = 180


MAX_PER_CATEGORY_SECTION = 3


def _build_sections(
    displays: Sequence[_StoryDisplay], top_five: Sequence[_StoryDisplay]
) -> list[tuple[str, list[_StoryDisplay]]]:
    """Group stories by category so every story appears at most once in the email.

    Top 5 stories are never repeated. A multi-category story appears only in the
    first category (in SECTION_ORDER) that picks it.
    """
    shown = {display.story_id for display in top_five}
    sections: list[tuple[str, list[_StoryDisplay]]] = []
    for category in SECTION_ORDER[:-1]:
        picked: list[_StoryDisplay] = []
        for display in displays:
            if display.story_id in shown or category not in display.categories:
                continue
            picked.append(display)
            shown.add(display.story_id)
            if len(picked) >= MAX_PER_CATEGORY_SECTION:
                break
        if picked:
            sections.append((category, picked))
    return sections


def _compact_summary(value: str, maximum: int = SUMMARY_LIMIT) -> str:
    """Keep the email index scannable without changing the underlying story data."""
    text = " ".join(value.split())
    if len(text) <= maximum:
        return text
    excerpt = text[: maximum - 1]
    if " " in excerpt:
        excerpt = excerpt.rsplit(" ", 1)[0]
    return f"{excerpt.rstrip(' ,;:.-')}…"


def _image_tag(display: _StoryDisplay, *, width: int) -> str:
    """An <img> only when the feed supplied an image URL. Never a placeholder."""
    image_url = display.sources[0].image_url
    if not image_url:
        return ""
    return (
        f'<img src="{escape(image_url, quote=True)}" width="{width}" '
        f'alt="{escape(display.headline)}" border="0" '
        f'style="display:block;width:100%;max-width:100%;height:auto;border:0;{IMAGE_FILTER}">'
    )


def _read_link(url: str, label: str) -> str:
    return (
        f'<a class="read-more" style="color:{ACCENT};font-family:{SERIF};font-size:12px;'
        f'font-style:italic;text-decoration:underline" href="{url}">{label}</a>'
    )


def _render_html_story(
    display: _StoryDisplay,
    *,
    featured: bool,
    compact: bool = False,
    number: int | None = None,
    show_image: bool = True,
    rule_above: bool = False,
) -> str:
    """One story. Lead = full width; default = column story; compact = category story."""
    source = display.sources[0]
    category = display.categories[0] if display.categories else "NEWS"
    url = escape(source.url, quote=True)
    metadata = " · ".join(value for value in (source.publisher, source.published) if value)
    headline = (
        f'<a style="color:{INK};text-decoration:none" href="{url}">{escape(display.headline)}</a>'
    )
    meta = (
        f'<p class="metadata" style="margin:0 0 6px;color:{MUTED};font-family:{SERIF};'
        f'font-size:11px;font-style:italic;line-height:1.4">{escape(metadata)}</p>'
    )
    kicker = (
        f'<p class="category" style="margin:0 0 5px;color:{INK};font-family:{SERIF};'
        f"font-size:10px;font-weight:bold;letter-spacing:2px;text-transform:uppercase;"
        f'border-bottom:1px solid {PAPER_RULE};padding-bottom:3px">{escape(category)}</p>'
    )

    if featured:
        hero = _image_tag(display, width=560) if show_image else ""
        figure = (
            f'<div style="margin:0 0 4px">{hero}</div>'
            f'<p style="margin:0 0 12px;color:{MUTED};font-family:{SERIF};font-size:10px;'
            f'font-style:italic;text-align:right">Photo: {escape(source.publisher)}</p>'
            if hero
            else ""
        )
        return (
            f'<div class="story lead" style="padding:18px 0 20px">{kicker}'
            f'<h3 style="margin:0 0 12px;font-family:{SERIF};font-size:36px;line-height:1.08;'
            f'font-weight:bold;letter-spacing:-0.5px">{headline}</h3>'
            f"{figure}"
            f'<p class="summary" style="margin:0 0 8px;color:#262626;font-family:{SERIF};'
            f'font-size:15px;line-height:1.55;text-align:justify">{escape(display.summary)}</p>'
            f"{meta}{_read_link(url, 'READ MORE →')}</div>"
        )

    image = _image_tag(display, width=266) if show_image else ""
    figure = f'<div style="margin:0 0 8px">{image}</div>' if image else ""
    head_size, summary_size = (17, 12) if compact else (21, 13)
    top_rule = f"border-top:1px solid {PAPER_RULE};padding-top:12px;" if rule_above else ""
    return (
        f'<div class="{"story compact" if compact else "story"}" '
        f'style="{top_rule}padding-bottom:14px">'
        f"{'' if compact else kicker}{figure}"
        f'<h3 style="margin:0 0 6px;font-family:{SERIF};font-size:{head_size}px;'
        f'line-height:1.18;font-weight:bold">{headline}</h3>'
        f'<p class="summary" style="margin:0 0 6px;color:#262626;font-family:{SERIF};'
        f'font-size:{summary_size}px;line-height:1.5;text-align:justify">'
        f"{escape(display.summary)}</p>"
        f"{meta}{_read_link(url, '→ READ' if compact else 'READ MORE →')}</div>"
    )


def _columns(left: str, right: str) -> str:
    """Two newspaper columns with a thin rule between them (stack on small screens)."""
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%"><tr>'
        f'<td class="col" width="50%" valign="top" style="width:50%;padding:0 14px 0 0">{left}</td>'
        f'<td class="col col-right" width="50%" valign="top" style="width:50%;padding:0 0 0 14px;'
        f'border-left:1px solid {INK}">{right}</td></tr></table>'
    )


def _render_top_stories(top: Sequence[_StoryDisplay]) -> str:
    """Story 1 spans the page; stories 2-5 sit in two columns."""
    if not top:
        return ""
    parts = [_render_html_story(top[0], featured=True, number=1)]
    rest = list(top[1:])
    for index in range(0, len(rest), 2):
        pair = rest[index : index + 2]
        left = _render_html_story(pair[0], featured=False, number=index + 2)
        right = (
            _render_html_story(pair[1], featured=False, number=index + 3) if len(pair) > 1 else ""
        )
        grid = _columns(left, right)
        parts.append(f'<div style="border-top:1px solid {INK};padding-top:14px">{grid}</div>')
    return "\n".join(parts)


def _render_html_category(category: str, members: list[_StoryDisplay]) -> str:
    """Section lead (with its photo) on the left; up to two shorter stories on the right."""
    members = members[:3]
    lead = _render_html_story(members[0], featured=False, compact=True)
    if len(members) == 1:
        body = f'<div style="padding-top:14px">{lead}</div>'
    else:
        others = "".join(
            _render_html_story(
                item, featured=False, compact=True, show_image=False, rule_above=i > 0
            )
            for i, item in enumerate(members[1:])
        )
        body = f'<div style="padding-top:14px">{_columns(lead, others)}</div>'
    return (
        '<div class="category-section" style="margin-top:26px">'
        f'<h2 class="section-heading" style="margin:0;padding:7px 0;border-top:4px double '
        f"{INK};border-bottom:1px solid {INK};color:{INK};font-family:{SERIF};font-size:13px;"
        f'font-weight:bold;letter-spacing:3px;text-align:center;text-transform:uppercase">'
        f"{escape(category)}</h2>{body}</div>"
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
