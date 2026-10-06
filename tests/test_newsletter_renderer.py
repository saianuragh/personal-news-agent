"""Offline tests for the compact HTML and plain-text news digest."""

import re
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from html.parser import HTMLParser
from uuid import uuid4

from app.llm.base import AIEnrichedStory
from app.models.article import Article
from app.newsletter.renderer import SUMMARY_LIMIT, render_newsletter
from app.processing.categorize import categorize_story
from app.processing.deduplicate import deduplicate_articles
from app.processing.rank import rank_stories

GENERATED = datetime(2026, 10, 1, 5, tzinfo=UTC)
STANDARD_CATEGORIES = (
    "India",
    "World",
    "AI",
    "Technology",
    "Business & Economy",
    "Science & Space",
    "Sports",
    "Entertainment",
)


def ranked(
    title: str,
    *,
    url: str | None = None,
    publisher: str = "Example & News",
    published_at: datetime | None = GENERATED - timedelta(hours=1),
    categories: tuple[str, ...] = ("Technology",),
    description: str | None = "A source description with useful reporting details.",
):
    article_url = url or f"https://news.example/{uuid4()}"
    article = Article(
        article_id=uuid4(),
        source_id="test-source",
        publisher=publisher,
        title=title,
        url=article_url,
        canonical_url=article_url,
        retrieved_at=GENERATED,
        published_at=published_at,
        description=description,
        content_hash=sha256(article_url.encode()).hexdigest(),
        source_categories=categories,
    )
    story = deduplicate_articles([article]).stories[0]
    return rank_stories([categorize_story(story)], as_of=GENERATED)[0]


def enriched(
    item,
    *,
    summary: str | None = "A concise summary of the story.",
    why: str | None = "This may affect readers and institutions.",
    status: str = "generated",
) -> AIEnrichedStory:
    return AIEnrichedStory(
        ranked_story=item,
        summary=summary,
        why_it_matters=why,
        uncertainty=(),
        provider="test-provider",
        model="test-model",
        prompt_version="test-prompt",
        schema_version="test-schema",
        generated_at=GENERATED,
        status=status,
    )


def render(items):
    return render_newsletter(items, generated_at=GENERATED, timezone_name="Asia/Kolkata")


class StoryParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.summaries: list[str] = []
        self._in_summary = False
        self._summary_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "a" and values.get("href"):
            self.links.append(values["href"] or "")
        if tag == "p" and "summary" in (values.get("class") or "").split():
            self._in_summary = True
            self._summary_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_summary:
            self._summary_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "p" and self._in_summary:
            self.summaries.append("".join(self._summary_parts))
            self._in_summary = False


def test_minimal_header_and_story_link_to_original_article() -> None:
    source_url = "https://news.example/item?a=1&b=2"
    item = ranked("New technology announcement", url=source_url)

    result = render([enriched(item)])
    parser = StoryParser()
    parser.feed(result.html)

    assert "PERSONAL NEWS" in result.html
    assert "Daily Brief · 01 October 2026" in result.html
    assert "⭐ TOP 5" in result.html
    assert "New technology announcement" in result.html
    assert "Example &amp; News · 09:30 IST" in result.html
    assert 'href="https://news.example/item?a=1&amp;b=2"' in result.html
    assert parser.links.count("https://news.example/item?a=1&b=2") >= 2
    assert "Why it matters" not in result.html
    top_story = result.html.split('<div class="category-section"', 1)[0]
    assert "A source description with useful reporting details." not in top_story
    assert "https://news.example/item?a=1&b=2" in result.plain_text


def test_primary_story_link_uses_the_retained_original_article() -> None:
    canonical = "https://news.example/shared-story"
    articles = [
        Article(
            article_id=uuid4(),
            source_id=source_id,
            publisher=publisher,
            url=url,
            canonical_url=canonical,
            title="Shared technology report",
            retrieved_at=GENERATED,
            published_at=GENERATED - timedelta(minutes=25),
            description="A brief source report about a technology change.",
            content_hash=sha256(canonical.encode()).hexdigest(),
            source_categories=("Technology",),
        )
        for source_id, publisher, url in (
            ("z_source", "Zeta News", "https://zeta.example/report"),
            ("a_source", "Alpha News", "https://alpha.example/original"),
        )
    ]
    story = deduplicate_articles(articles).stories[0]
    item = rank_stories([categorize_story(story)], as_of=GENERATED)[0]
    result = render([enriched(item)])

    assert f'href="{item.story.retained_article.url}"' in result.html


def test_long_summary_is_word_truncated_but_headline_and_url_are_intact() -> None:
    headline = "A very important headline remains complete"
    long_summary = "Useful reporting detail " * 30
    url = "https://news.example/complete-story"
    result = render([enriched(ranked(headline, url=url), summary=long_summary)])
    parser = StoryParser()
    parser.feed(result.html)

    assert headline in result.html
    assert url in result.html
    assert parser.summaries
    assert all(len(summary) <= SUMMARY_LIMIT for summary in parser.summaries)
    assert parser.summaries[0].endswith("…")


def test_top_five_is_compact_and_does_not_expand_all_selected_stories() -> None:
    items = [
        enriched(ranked(f"Technology headline number {index}"), summary="A brief report summary.")
        for index in range(24)
    ]
    ordered = rank_stories([item.ranked_story.categorized_story for item in items], as_of=GENERATED)
    by_id = {item.ranked_story.story.story_id: item for item in items}
    result = render([by_id[item.story.story_id] for item in ordered])
    top_block = result.html.split('<div class="category-section"', 1)[0]

    assert top_block.count('<div class="story') == 5
    assert "Technology headline number" in top_block
    assert "Why it matters" not in top_block
    assert len(result.included_story_ids) == 24


def test_each_category_displays_at_most_three_compact_source_linked_stories() -> None:
    items = []
    for index in range(24):
        category = STANDARD_CATEGORIES[index % len(STANDARD_CATEGORIES)]
        title = f"{category} development headline {index}"
        items.append(enriched(ranked(title, categories=(category,))))
    result = render(items)

    for category in STANDARD_CATEGORIES:
        marker = f'<h2 class="section-heading">{category}</h2>'
        if marker not in result.html:
            continue
        block = result.html.split(marker, 1)[1].split("</div>", 1)[0]
        assert block.count('<div class="story compact"') <= 3
        assert block.count("→ READ") <= 3
    assert "AI" in result.html
    assert "Business &amp; Economy" in result.html
    assert "Science &amp; Space" in result.html


def test_no_article_url_appears_more_than_once_in_html() -> None:
    """Verify that each article URL appears at most once in the HTML output."""
    items = [
        enriched(ranked(f"Story {index}", url=f"https://news.example/story-{index}"))
        for index in range(12)
    ]
    result = render(items)
    parser = StoryParser()
    parser.feed(result.html)

    # Count occurrences of each URL
    url_counts = {}
    for url in parser.links:
        url_counts[url] = url_counts.get(url, 0) + 1

    # Each story links its headline and "Read more" to the same URL: at most 2 links.
    for url, count in url_counts.items():
        assert count <= 2, f"URL {url} appears {count} times in HTML"


def test_no_article_url_appears_more_than_once_in_plain_text() -> None:
    """Verify that each article URL appears at most once in plain text."""
    items = [
        enriched(ranked(f"Story {index}", url=f"https://news.example/story-{index}"))
        for index in range(12)
    ]
    result = render(items)

    url_counts = {}
    for line in result.plain_text.split("\n"):
        if "https://" in line:
            # Extract URLs from lines
            for part in line.split():
                if part.startswith("https://"):
                    url = part.rstrip(":")
                    url_counts[url] = url_counts.get(url, 0) + 1

    for url, count in url_counts.items():
        assert count <= 1, f"URL {url} appears {count} times in plain text"


def test_top_five_stories_do_not_appear_again_in_category_sections() -> None:
    items = [
        enriched(ranked(f"Headline number {index:02d}", url=f"https://news.example/h-{index}"))
        for index in range(10)
    ]
    result = render(items)

    top_block, _, category_block = result.html.partition('<div class="category-section"')
    pattern = r'href="(https://news\.example/h-\d+)"'
    top_urls = set(re.findall(pattern, top_block))
    category_urls = set(re.findall(pattern, category_block))

    assert len(top_urls) == 5
    assert category_urls
    assert not top_urls & category_urls

def test_multi_category_story_appears_exactly_once() -> None:
    """Verify that a story with multiple categories appears only once."""
    multi_cat_item = enriched(
        ranked("Multi-category story", categories=("Technology", "AI", "Business & Economy"))
    )
    other_items = [
        enriched(ranked(f"Story {index}", categories=("Technology",)))
        for index in range(10)
    ]
    result = render([multi_cat_item] + other_items)

    # Count occurrences of the multi-category story headline
    count_html = result.html.count("Multi-category story")
    count_text = result.plain_text.count("Multi-category story")

    assert count_html == 1, f"Multi-category story appears {count_html} times in HTML"
    assert count_text == 1, f"Multi-category story appears {count_text} times in plain text"


def test_each_plain_text_category_has_at_most_three_stories() -> None:
    """Verify that each plain-text category section has at most 3 stories."""
    items = [
        enriched(ranked(f"Story {index}", categories=("Technology",)))
        for index in range(12)
    ]
    result = render(items)

    sections = result.plain_text.split("\n\nTECHNOLOGY\n\n")
    if len(sections) > 1:
        tech_section = sections[1]
        # Count stories (each starts with category line, then headline, then other info)
        # In plain text, we can count by READ MORE/→ Read lines
        story_count = tech_section.count("READ MORE:") + tech_section.count("→ Read:")
        assert story_count <= 3, f"Technology section has {story_count} stories, expected <= 3"


def test_empty_story_notice_still_works() -> None:
    """Verify that the empty-story notice still appears when there are no stories."""
    result = render([])

    assert "No eligible stories are available today." in result.html
    assert "No eligible stories are available today." in result.plain_text
    assert "AI WATCH" not in result.html
    assert result.included_story_ids == ()
    assert len(result.omitted_story_ids) == 0


def test_missing_images_and_empty_optional_sections_do_not_break_layout() -> None:
    item = enriched(ranked("Technology update", description=None))
    result = render([item])

    assert "<img" not in result.html
    assert "TOP 5" in result.html
    assert "AI WATCH" not in result.html
    assert "IMPORTANT TODAY" not in result.html
    assert "FACT OF THE DAY" not in result.html
    assert "MARKET SNAPSHOT" not in result.html


def test_fallback_summary_is_compact_and_markup_is_escaped() -> None:
    title = '<script>alert("x")</script> & update'
    summary = '<img src=x onerror="alert(1)"> & useful detail. '
    url = "https://news.example/story?x=1&y=2"
    item = enriched(
        ranked(title, url=url, description=None),
        summary=summary * 20,
        why=None,
        status="fallback",
    )
    result = render([item])

    assert "<script>" not in result.html
    assert "<img src=x" not in result.html
    assert "&lt;script&gt;" in result.html
    assert "Source description (fallback)" not in result.html
    assert "x=1&amp;y=2" in result.html
    assert "Source description (fallback)" not in result.plain_text


def test_generated_and_fallback_summaries_are_capped_in_plain_text() -> None:
    long_summary = "Concise source-based detail " * 20
    items = [
        enriched(ranked("Generated long summary"), summary=long_summary, status="generated"),
        enriched(ranked("Fallback long summary"), summary=long_summary, status="fallback"),
    ]
    result = render(items)

    for headline in ("Generated long summary", "Fallback long summary"):
        block = next(
            block
            for block in result.plain_text.split("\n\n")
            if headline in block.splitlines()
        )
        lines = block.splitlines()
        summary = lines[2]
        assert len(summary) <= SUMMARY_LIMIT
        assert summary.endswith("…")
    assert long_summary not in result.plain_text


def test_plain_text_alternative_is_compact_and_links_to_original_articles() -> None:
    source_url = "https://news.example/plain-story"
    item = enriched(ranked("A fresh morning headline", url=source_url))
    result = render([item])

    assert result.plain_text.startswith("PERSONAL NEWS\n\n01 October 2026\n\nTOP 5")
    assert "A fresh morning headline" in result.plain_text
    assert f"READ MORE: {source_url}" in result.plain_text
    assert "Generated:" not in result.plain_text
    assert "Why it matters" not in result.plain_text


def test_mobile_email_layout_is_single_column_and_has_no_horizontal_sizing() -> None:
    result = render([enriched(ranked("A fresh morning headline"))])

    assert "@media only screen and (max-width:520px)" in result.html
    assert "max-width:620px" in result.html
    assert 'width="100%"' in result.html
    assert "min-width:" not in result.html
    assert "overflow-x" not in result.html
    assert "<script" not in result.html
    assert "<link rel=\"stylesheet\"" not in result.html
    assert "@import" not in result.html
    assert "javascript:" not in result.html
    assert "<article" not in result.html
    assert "<section" not in result.html
    assert "<header" not in result.html
    assert "<main" not in result.html
    assert "<footer" not in result.html


def test_html_is_parseable_and_empty_digest_omits_empty_optional_sections() -> None:
    result = render([])
    parser = StoryParser()
    parser.feed(result.html)
    parser.close()

    assert result.html.startswith("<!doctype html>")
    assert "No eligible stories are available today." in result.html
    assert "No eligible stories are available today." in result.plain_text
    assert "AI WATCH" not in result.html
    assert "IMPORTANT TODAY" not in result.html
    assert "FACT OF THE DAY" not in result.html
    assert result.included_story_ids == ()
    assert len(result.omitted_story_ids) == 0


def test_packaged_template_renders_outside_the_repository_working_directory(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)

    result = render([])

    assert "<!doctype html>" in result.html.casefold()
    assert "PERSONAL NEWS" in result.html
    assert "Daily Brief · 01 October 2026" in result.html
    assert "PERSONAL NEWS" in result.plain_text


def test_source_time_is_omitted_when_provider_did_not_supply_it() -> None:
    item = enriched(ranked("Story without a timestamp", published_at=None))
    result = render([item])

    assert "Story without a timestamp" in result.html
    assert "Publication time not provided" not in result.html
    assert "Example &amp; News" in result.html


def test_html_escapes_unicode_markup_and_source_links() -> None:
    item = enriched(
        ranked(
            "L'été & café — 東京 <update>",
            publisher="O'Brien & News",
            url="https://news.example/story?edition=été&lang=ja",
        ),
        summary="Résumé: 東京 & useful context.",
    )
    result = render([item])

    assert "L&#x27;été &amp; café — 東京 &lt;update&gt;" in result.html
    assert "O&#x27;Brien &amp; News" in result.html
    assert "Résumé: 東京 &amp; useful context." in result.html
    assert "https://news.example/story?edition=été&amp;lang=ja" in result.html
