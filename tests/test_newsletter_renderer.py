"""Offline tests for HTML and plain-text newsletter rendering."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from html.parser import HTMLParser
from uuid import uuid4

from app.llm.base import AIEnrichedStory
from app.models.article import Article
from app.newsletter.renderer import render_newsletter
from app.processing.categorize import categorize_story
from app.processing.deduplicate import deduplicate_articles
from app.processing.rank import CategoryRank, rank_stories

GENERATED = datetime(2026, 10, 1, 5, tzinfo=UTC)


def ranked(
    title: str,
    *,
    url: str | None = None,
    publisher: str = "Example & News",
    published_at: datetime | None = GENERATED - timedelta(hours=1),
    categories: tuple[str, ...] = ("Technology",),
    description: str | None = "A source description <with markup-like text> & details.",
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
    categorized = categorize_story(story)
    return rank_stories([categorized], as_of=GENERATED)[0]


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


def render(items, *, timezone_name: str = "Asia/Kolkata"):
    return render_newsletter(
        items,
        generated_at=GENERATED,
        timezone_name=timezone_name,
    )


def test_html_and_plain_text_contain_story_fields_and_original_source_link() -> None:
    item = ranked("New technology announcement", url="https://news.example/item?a=1&b=2")

    result = render([enriched(item)])

    assert "Personal News Briefing" in result.html
    assert "Technology" in result.html
    assert "New technology announcement" in result.html
    assert "AI-generated summary" in result.html
    assert "WHY IT MATTERS" in result.html
    assert 'href="https://news.example/item?a=1&amp;b=2"' in result.html
    assert "https://news.example/item?a=1&b=2" in result.plain_text
    assert "Example & News" in result.plain_text
    assert result.included_story_ids == (item.story.story_id,)


def test_publication_and_generation_times_are_localized_without_fabrication() -> None:
    item = ranked(
        "Science mission update",
        categories=("Science",),
        published_at=datetime(2026, 10, 1, 4, 15, tzinfo=UTC),
    )
    unknown_time = ranked("Technology story with no date", published_at=None, description=None)

    result = render([enriched(item), enriched(unknown_time)])

    assert "01 Oct 2026 09:45" in result.html
    assert "Publication time not provided" in result.html
    assert "01 Oct 2026 09:45" in result.plain_text
    assert "Publication time not provided" in result.plain_text
    assert "10:30 IST" in result.html


def test_html_escapes_headline_summary_significance_publisher_and_url() -> None:
    item = ranked(
        '<script>alert("x")</script> & update',
        url="https://news.example/story?x=1&y=2",
    )
    result = render(
        [
            enriched(
                item,
                summary='<img src=x onerror="alert(1)"> & facts',
                why="Impact < unknown > & debated",
            )
        ]
    )

    assert "<script>" not in result.html
    assert "<img src=x" not in result.html
    assert '&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; update' in result.html
    assert '&lt;img src=x onerror=&quot;alert(1)&quot;&gt; &amp; facts' in result.html
    assert "Impact &lt; unknown &gt; &amp; debated" in result.html
    assert "&amp; update" in result.html
    assert "x=1&amp;y=2" in result.html
    assert '<script>alert("x")</script> & update' in result.plain_text
    assert '<img src=x onerror="alert(1)"> & facts' in result.plain_text
    assert "Impact < unknown > & debated" in result.plain_text
    assert "https://news.example/story?x=1&y=2" in result.plain_text
    assert "<article" not in result.plain_text
    assert "<a href=" not in result.plain_text


def test_fallback_and_dynamic_html_values_are_escaped_without_changing_source_url() -> None:
    class LinkCollector(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.links: list[dict[str, str | None]] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag == "a":
                self.links.append(dict(attrs))

    source_url = 'https://news.example/story?x=1&raw="onmouseover="alert(1)'
    malicious = '<script>alert("x")</script>'
    item = ranked(
        malicious,
        url=source_url,
        publisher='<img src=x onerror="alert(1)"> Publisher',
        description=malicious,
    )
    malicious_category = '<svg onload="alert(1)">World'
    categorized = replace(
        item.categorized_story,
        categories=(malicious_category,),
        status="categorized",
    )
    item = replace(
        item,
        categorized_story=categorized,
        category_ranks=(CategoryRank(malicious_category, 1),),
    )

    result = render(
        [
            enriched(
                item,
                summary=f"Source description: {malicious}",
                why=malicious,
                status="fallback",
            )
        ]
    )

    assert "<script>" not in result.html
    assert "<img src=x" not in result.html
    assert "<svg onload=" not in result.html
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in result.html
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt; Publisher" in result.html
    assert "&lt;svg onload=&quot;alert(1)&quot;&gt;World" in result.html
    assert "Source description (fallback)" in result.html
    assert "AI-generated summary" not in result.html
    assert "&quot;onmouseover=&quot;alert(1)" in result.html

    parser = LinkCollector()
    parser.feed(result.html)
    assert any(link.get("href") == source_url for link in parser.links)
    assert all(link.get("href") for link in parser.links)
    assert source_url in result.plain_text
    assert "Source description (fallback)" in result.plain_text
    assert malicious in result.plain_text


def test_fallback_summary_is_labeled_and_missing_significance_is_allowed() -> None:
    item = ranked("Technology release")

    result = render(
        [
            enriched(
                item,
                summary="Publisher supplied detail.",
                why=None,
                status="fallback",
            )
        ]
    )

    assert "Source description (fallback)" in result.html
    assert result.html.count("Source description (fallback)") == 2
    assert result.plain_text.count("Source description (fallback)") == 1
    assert "Publisher supplied detail." in result.html
    assert "Publisher supplied detail." in result.plain_text
    assert "AI-generated summary" not in result.html
    assert "Why it matters" not in result.html


def test_long_title_and_fallback_text_render_without_truncation_or_markup() -> None:
    long_title = ("Technology update " * 24).strip()
    long_fallback = "Useful source detail. " * 25
    item = ranked(long_title)

    result = render([enriched(item, summary=long_fallback, why=None, status="fallback")])

    assert long_title in result.html
    assert long_title in result.plain_text
    assert "Useful source detail." in result.html
    assert "<script>" not in result.html


def test_failed_summary_and_unclassified_story_are_omitted() -> None:
    failed_ranked = ranked("Technology article")
    failed = enriched(failed_ranked, summary=None, why=None, status="failed")
    unclassified_ranked = ranked("Local traffic changes", categories=())

    result = render([failed, enriched(unclassified_ranked)])

    assert "No eligible stories are available today." in result.html
    assert "No eligible stories are available today." in result.plain_text
    assert result.included_story_ids == ()
    assert result.omitted_story_ids == (
        failed_ranked.story.story_id,
        unclassified_ranked.story.story_id,
    )


def test_multi_category_story_appears_in_each_dedicated_category_view() -> None:
    item = ranked(
        "India announces new AI research program",
        categories=("India", "AI"),
    )

    result = render([enriched(item)])

    assert result.html.count("India announces new AI research program") == 3
    assert '<section class="newsletter-view category-view category-view-india">' in result.html
    assert '<section class="newsletter-view category-view category-view-ai">' in result.html
    assert 'id="category-india-story-01"' in result.html
    assert 'id="category-ai-story-01"' in result.html
    assert result.included_story_ids == (item.story.story_id,)


def test_stories_are_ordered_by_category_rank_and_render_deterministically() -> None:
    first = ranked("Technology report one", published_at=GENERATED - timedelta(minutes=10))
    second = ranked("Technology report two", published_at=GENERATED - timedelta(minutes=2))
    ranked_items = rank_stories(
        [first.categorized_story, second.categorized_story], as_of=GENERATED
    )
    enriched_items = [enriched(item) for item in ranked_items]

    output_one = render(enriched_items)
    output_two = render(enriched_items)

    assert output_one == output_two
    assert output_one.html.index("Technology report two") < output_one.html.index(
        "Technology report one"
    )
    assert output_one.plain_text.index("Technology report two") < output_one.plain_text.index(
        "Technology report one"
    )


def test_featured_story_numbering_category_buttons_and_back_to_top_links() -> None:
    first = ranked("AI breakthrough improves research", categories=("AI",))
    second = ranked("Technology companies expand", categories=("Technology",))
    third = ranked("New AI technology reaches India", categories=("AI", "Technology"))
    fourth = ranked("World leaders meet", categories=("World",))
    ordered = rank_stories(
        [
            first.categorized_story,
            second.categorized_story,
            third.categorized_story,
            fourth.categorized_story,
        ],
        as_of=GENERATED,
    )
    result = render([enriched(item) for item in reversed(ordered)])

    assert "THE DAY'S MOST IMPORTANT STORIES · 4 stories" in result.html
    assert 'class="story-card featured-card" id="story-01"' in result.html
    assert 'class="story-card feed-card" id="story-02"' in result.html
    assert 'class="story-card feed-card" id="story-03"' in result.html
    assert 'class="story-card feed-card" id="story-04"' in result.html
    assert result.html.index("TOP STORY") < result.html.index("story-02")
    assert 'id="newsletter-top"' in result.html
    assert 'href="#view-home"' in result.html
    assert 'href="#category-india"' in result.html
    assert 'href="#category-world"' in result.html
    assert 'href="#category-ai"' in result.html
    assert 'href="#category-technology"' in result.html
    for anchor in ("india", "world", "ai", "technology"):
        assert f'<span class="view-state" id="category-{anchor}"></span>' in result.html
        assert f"#category-{anchor}:target ~ .edition-content" in result.html
    assert result.html.count('class="back-to-home" href="#view-home"') == 8
    assert result.html.count('class="story-cta" href="https://news.example/') >= 4
    assert "#f7f5f0" in result.html
    assert "TODAY'S STORIES" in result.html
    assert result.included_story_ids == tuple(item.story.story_id for item in ordered)


def test_all_standard_category_names_have_stable_fragment_ids() -> None:
    item = ranked("A policy briefing")
    categories = (
        "India",
        "World",
        "AI",
        "Technology",
        "Business & Economy",
        "Science & Space",
        "Sports",
        "Entertainment",
    )
    categorized = replace(item.categorized_story, categories=categories, status="categorized")
    result = render([enriched(replace(item, categorized_story=categorized))])

    for anchor in (
        "category-india",
        "category-world",
        "category-ai",
        "category-technology",
        "category-business",
        "category-science",
        "category-sports",
        "category-entertainment",
    ):
        assert f'href="#{anchor}"' in result.html
        assert f'id="{anchor}"' in result.html
        assert f"#{anchor}:target ~ .edition-content" in result.html
    assert result.html.count('class="back-to-home" href="#view-home"') == 8


def test_category_view_contains_only_stories_assigned_to_that_category() -> None:
    ai = ranked("AI research makes a breakthrough", categories=("AI",))
    technology = ranked("Software technology gets an update", categories=("Technology",))
    both = ranked("AI technology changes the field", categories=("AI", "Technology"))
    ordered = rank_stories(
        [ai.categorized_story, technology.categorized_story, both.categorized_story],
        as_of=GENERATED,
    )
    result = render([enriched(item) for item in ordered])

    def view_content(category: str) -> str:
        marker = f'<section class="newsletter-view category-view category-view-{category}">'
        start = result.html.index(marker)
        end = result.html.index("</section>", start) + len("</section>")
        return result.html[start:end]

    ai_view = view_content("ai")
    technology_view = view_content("technology")
    assert "AI research makes a breakthrough" in ai_view
    assert "AI technology changes the field" in ai_view
    assert "Software technology gets an update" not in ai_view
    assert "Software technology gets an update" in technology_view
    assert "AI technology changes the field" in technology_view
    assert "AI research makes a breakthrough" not in technology_view


def test_html_email_layout_includes_mobile_styles_and_real_link_ctas() -> None:
    source_url = "https://news.example/read-this?edition=morning&lang=en"
    item = ranked("A fresh morning headline", url=source_url)
    result = render([enriched(item)])

    assert "@media only screen and (max-width:520px)" in result.html
    assert "READ FULL STORY →" in result.html
    assert f'href="{source_url.replace("&", "&amp;")}"' in result.html
    assert "<script" not in result.html
    assert "<link rel=\"stylesheet\"" not in result.html
    assert "@import" not in result.html
    assert "fonts.googleapis.com" not in result.html
    assert "javascript:" not in result.html
    assert "padding:13px 6px" in result.html
    assert "A fresh morning headline" in result.plain_text
    assert f"Read: {source_url}" in result.plain_text
    assert source_url in result.plain_text


def test_unicode_apostrophes_and_markup_like_text_remain_readable_and_escaped() -> None:
    item = ranked(
        "L'été & café — 東京 <update>",
        publisher="O'Brien & News",
        url="https://news.example/story?edition=été&lang=ja",
    )
    result = render(
        [enriched(item, summary="Résumé: 東京 & useful context.", why="Readers' choices matter.")]
    )

    assert "L&#x27;été &amp; café — 東京 &lt;update&gt;" in result.html
    assert "O&#x27;Brien &amp; News" in result.html
    assert "Résumé: 東京 &amp; useful context." in result.html
    assert "Readers&#x27; choices matter." in result.html
    assert "https://news.example/story?edition=été&amp;lang=ja" in result.html
    assert "L'été & café — 東京 <update>" in result.plain_text


def test_empty_newsletter_keeps_category_views_with_clear_empty_states() -> None:
    result = render([])

    assert "THE DAY'S MOST IMPORTANT STORIES · 0 stories" in result.html
    assert "No eligible stories are available today." in result.html
    assert "No eligible stories are available today." in result.plain_text
    assert 'href="#view-home"' in result.html
    assert 'href="#category-india"' in result.html
    assert 'href="#category-entertainment"' in result.html
    assert result.html.count('class="empty-category"') == 8
    assert result.html.count('class="back-to-home" href="#view-home"') == 8


def test_empty_newsletter_and_invalid_timezone_or_timestamp() -> None:
    empty = render([])
    assert "No eligible stories" in empty.html
    assert "No eligible stories" in empty.plain_text

    try:
        render([], timezone_name="Not/A_Timezone")
    except ValueError as error:
        assert "Unknown IANA timezone" in str(error)
    else:
        raise AssertionError("invalid timezone should fail")

    try:
        render_newsletter([], generated_at=datetime(2026, 10, 1, 5), timezone_name="UTC")
    except ValueError as error:
        assert "timezone-aware" in str(error)
    else:
        raise AssertionError("naive generation time should fail")


def test_duplicate_story_inputs_are_rejected_to_avoid_double_rendering() -> None:
    item = enriched(ranked("Technology announcement"))

    try:
        render([item, item])
    except ValueError as error:
        assert "duplicate story" in str(error)
    else:
        raise AssertionError("duplicate story input should fail")
