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
    photo_grid = _pick_photo_grid(displays, {display.story_id for display in top_five})
    sections = _build_sections(displays, [*top_five, *photo_grid])
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
        top_stories=_render_top_stories(top_five) + _render_photo_grid(photo_grid),
        categories="\n".join(
            _render_html_category(category, members) for category, members in sections
        ),
    )

    text_sections = [_render_text_story(display) for display in top_five]
    category_indexes = [_render_text_category(category, members) for category, members in sections]
    if photo_grid:
        category_indexes.insert(0, _render_text_category("In Pictures", photo_grid, limit=6))
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


# variant: (headline px, summary px, image width px, justified text)
_VARIANTS = {
    "lead": (30, 14, 333, True),
    "side": (17, 12, 185, False),
    "column": (21, 13, 266, True),
    "compact": (15, 12, 0, False),
}
PHOTO_GRID_MAX = 3
PHOTO_GRID_MIN = 3


def _pick_photo_grid(
    displays: Sequence[_StoryDisplay], excluded_ids: set[UUID]
) -> list[_StoryDisplay]:
    """Stories that carry a feed-supplied photo, for the 'In Pictures' row (3 tiles).

    One photo story per category comes first so the grid is varied, then the rest by rank.
    """
    candidates = [
        display
        for display in displays
        if display.story_id not in excluded_ids and display.sources[0].image_url
    ]
    count = min(len(candidates), PHOTO_GRID_MAX)
    count -= count % PHOTO_GRID_MIN
    picked: list[_StoryDisplay] = []
    seen_categories: set[str] = set()
    for display in candidates:
        category = display.categories[0] if display.categories else ""
        if category not in seen_categories:
            seen_categories.add(category)
            picked.append(display)
    chosen = {display.story_id for display in picked}
    picked.extend(display for display in candidates if display.story_id not in chosen)
    return picked[:count]


def _render_html_story(
    display: _StoryDisplay,
    *,
    variant: str,
    show_image: bool = True,
    rule_above: bool = False,
) -> str:
    """One story: lead (front page), side (sidebar), column (two-up) or compact (section)."""
    headline_px, summary_px, image_width, justified = _VARIANTS[variant]
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
    image = _image_tag(display, width=image_width) if show_image and image_width else ""
    figure = f'<div style="margin:0 0 6px">{image}</div>' if image else ""
    if image and variant == "lead":
        figure += (
            f'<p style="margin:0 0 10px;color:{MUTED};font-family:{SERIF};font-size:10px;'
            f'font-style:italic;text-align:right">Photo: {escape(source.publisher)}</p>'
        )
    align = "justify" if justified else "left"
    top_rule = f"border-top:1px solid {PAPER_RULE};padding-top:12px;" if rule_above else ""
    class_name = {"lead": "story lead", "compact": "story compact"}.get(variant, "story")
    letter_spacing = "letter-spacing:-0.5px;" if variant == "lead" else ""
    return (
        f'<div class="{class_name}" style="{top_rule}padding-bottom:14px">'
        f"{'' if variant == 'compact' else kicker}{figure}"
        f'<h3 style="margin:0 0 6px;font-family:{SERIF};font-size:{headline_px}px;'
        f'line-height:1.15;font-weight:bold;{letter_spacing}">{headline}</h3>'
        f'<p class="summary" style="margin:0 0 6px;color:#262626;font-family:{SERIF};'
        f'font-size:{summary_px}px;line-height:1.5;text-align:{align}">'
        f"{escape(display.summary)}</p>"
        f"{meta}{_read_link(url, '→ READ' if variant == 'compact' else 'READ MORE →')}</div>"
    )


def _columns(*cells: str, widths: tuple[int, ...] | None = None) -> str:
    """Newspaper columns with a thin rule between them (they stack on small screens)."""
    count = len(cells)
    widths = widths or tuple([100 // count] * (count - 1) + [100 - (100 // count) * (count - 1)])
    tds = []
    for index, (cell, width) in enumerate(zip(cells, widths, strict=True)):
        first, last = index == 0, index == count - 1
        rule = "" if first else f"border-left:1px solid {INK};"
        padding = f"0 {0 if last else 12}px 0 {0 if first else 12}px"
        class_name = "col" if first else "col col-right"
        tds.append(
            f'<td class="{class_name}" width="{width}%" valign="top" '
            f'style="width:{width}%;padding:{padding};{rule}">{cell}</td>'
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:100%"><tr>{"".join(tds)}</tr></table>'
    )


def _render_top_stories(top: Sequence[_StoryDisplay]) -> str:
    """Front page: lead story with two sidebar stories, then two more in columns."""
    if not top:
        return ""
    lead = _render_html_story(top[0], variant="lead")
    sidebar = top[1:3]
    if sidebar:
        side_html = "".join(
            _render_html_story(item, variant="side", rule_above=index > 0)
            for index, item in enumerate(sidebar)
        )
        front = _columns(lead, side_html, widths=(62, 38))
    else:
        front = lead
    parts = [f'<div style="padding-top:6px">{front}</div>']
    rest = top[3:5]
    if rest:
        grid = _columns(*(_render_html_story(item, variant="column") for item in rest))
        parts.append(f'<div style="border-top:1px solid {INK};padding-top:14px">{grid}</div>')
    return "\n".join(parts)


def _render_tile(display: _StoryDisplay) -> str:
    source = display.sources[0]
    category = display.categories[0] if display.categories else "NEWS"
    url = escape(source.url, quote=True)
    return (
        f'<div class="tile" style="padding-bottom:8px">{_image_tag(display, width=170)}'
        f'<p style="margin:6px 0 3px;color:{ACCENT};font-family:{SERIF};font-size:10px;'
        f'font-weight:bold;letter-spacing:1.5px;text-transform:uppercase">{escape(category)}</p>'
        f'<h3 style="margin:0 0 4px;font-family:{SERIF};font-size:14px;line-height:1.2;'
        f'font-weight:bold"><a style="color:{INK};text-decoration:none" href="{url}">'
        f"{escape(display.headline)}</a></h3>"
        f'<p class="metadata" style="margin:0 0 4px;color:{MUTED};font-family:{SERIF};'
        f'font-size:11px;font-style:italic">{escape(source.publisher)}</p>'
        f"{_read_link(url, '→ READ')}</div>"
    )


def _section_heading(title: str) -> str:
    return (
        f'<h2 class="section-heading" style="margin:0;padding:7px 0;border-top:4px double '
        f"{INK};border-bottom:1px solid {INK};color:{INK};font-family:{SERIF};font-size:13px;"
        f'font-weight:bold;letter-spacing:3px;text-align:center;text-transform:uppercase">'
        f"{escape(title)}</h2>"
    )


def _render_photo_grid(grid: Sequence[_StoryDisplay]) -> str:
    """'In Pictures': a row of three photo tiles. Only stories with a real photo."""
    if not grid:
        return ""
    rows = "".join(
        f'<div style="border-top:1px solid {PAPER_RULE};padding-top:12px">'
        f"{_columns(*(_render_tile(display) for display in grid[index : index + 3]))}</div>"
        for index in range(0, len(grid), 3)
    )
    return (
        f'<div class="photo-grid" style="margin-top:26px">{_section_heading("In Pictures")}'
        f"{rows}</div>"
    )


def _render_html_category(category: str, members: list[_StoryDisplay]) -> str:
    """Up to three text stories side by side in columns."""
    stories = [_render_html_story(item, variant="compact") for item in members[:3]]
    body = _columns(*stories) if len(stories) > 1 else stories[0]
    return (
        '<div class="category-section" style="margin-top:26px">'
        f'{_section_heading(category)}<div style="padding-top:14px">{body}</div></div>'
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


def _render_text_category(category: str, members: list[_StoryDisplay], *, limit: int = 3) -> str:
    lines = [category.upper()]
    lines.extend(_render_text_story(display, compact=True) for display in members[:limit])
    return "\n\n".join(lines)


def _format_time(value: datetime, zone: ZoneInfo) -> str:
    return value.astimezone(zone).strftime("%H:%M %Z")


def _load_timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, TypeError) as error:
        raise ValueError(f"Unknown IANA timezone: {timezone_name}") from error
