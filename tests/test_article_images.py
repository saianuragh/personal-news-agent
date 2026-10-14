"""Images: extraction from feeds, safe normalization, and classic-layout rendering."""

import re
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

import pytest
from app.config import load_sources
from app.models.article import Article
from app.processing.normalize import normalize_feed
from app.sources.base import FetchedFeed, RawProviderEntry
from app.sources.rss_atom import _find_image

from tests.test_newsletter_renderer import enriched, ranked, render

ROOT = Path(__file__).resolve().parents[1]
MEDIA = 'xmlns:media="http://search.yahoo.com/mrss/" xmlns:content="http://purl.org/rss/1.0/modules/content/"'


def item(inner: str) -> ElementTree.Element:
    return ElementTree.fromstring(f"<item {MEDIA}><title>T</title>{inner}</item>")


# ---- feed extraction -------------------------------------------------------------
def test_media_content_image_is_found() -> None:
    node = item('<media:content url="https://img.example/a.jpg" medium="image"/>')
    assert _find_image(node) == "https://img.example/a.jpg"


def test_priority_media_content_then_enclosure_then_thumbnail() -> None:
    node = item(
        '<media:thumbnail url="https://img.example/thumb.jpg"/>'
        '<enclosure url="https://img.example/enc.jpg" type="image/jpeg"/>'
        '<media:content url="https://img.example/big.jpg" type="image/jpeg"/>'
    )
    assert _find_image(node) == "https://img.example/big.jpg"
    node = item(
        '<media:thumbnail url="https://img.example/thumb.jpg"/>'
        '<enclosure url="https://img.example/enc.jpg" type="image/jpeg"/>'
    )
    assert _find_image(node) == "https://img.example/enc.jpg"


def test_first_img_in_description_is_a_fallback() -> None:
    markup = '<p><img src="https://img.example/d.png" alt="x">Hi</p>'
    node = item(f"<description><![CDATA[{markup}]]></description>")
    assert _find_image(node) == "https://img.example/d.png"


def test_non_image_enclosure_and_missing_image_give_none() -> None:
    assert _find_image(item('<enclosure url="https://x.example/a.mp3" type="audio/mpeg"/>')) is None
    assert _find_image(item("<description>No picture here</description>")) is None


@pytest.mark.parametrize(
    "inner",
    [
        '<media:content url="https://img.example/1x1.gif" medium="image"/>',
        '<media:content url="https://img.example/pixel.png" medium="image"/>',
        '<media:content url="https://img.example/a.jpg" medium="image" width="1" height="1"/>',
    ],
)
def test_tracking_pixels_are_skipped(inner: str) -> None:
    assert _find_image(item(inner)) is None


def test_widest_picture_wins_within_the_same_source_type() -> None:
    node = item(
        '<media:content url="https://img.example/small.jpg" width="140" medium="image"/>'
        '<media:content url="https://img.example/large.jpg" width="460" medium="image"/>'
    )
    assert _find_image(node) == "https://img.example/large.jpg"
    node = item(
        '<media:thumbnail url="https://img.example/a.jpg" width="240"/>'
        '<media:thumbnail url="https://img.example/b.jpg" width="976"/>'
    )
    assert _find_image(node) == "https://img.example/b.jpg"


# ---- normalization ---------------------------------------------------------------
def normalized(image):
    source = load_sources(ROOT / "config" / "sources.yaml")[0]
    fields = {"title": "Story with picture", "link": "https://example.com/story/pic"}
    if image is not None:
        fields["image"] = image
    feed = FetchedFeed(
        source, datetime(2026, 9, 29, 10, tzinfo=UTC), (RawProviderEntry("rss", fields),)
    )
    return normalize_feed(feed)


def test_valid_image_url_is_kept_and_entities_decoded() -> None:
    result = normalized("https://img.example/a.jpg?w=600&amp;h=400")
    assert result.articles[0].image_url == "https://img.example/a.jpg?w=600&h=400"


def test_protocol_relative_image_becomes_https() -> None:
    assert normalized("//img.example/a.jpg").articles[0].image_url == "https://img.example/a.jpg"


@pytest.mark.parametrize(
    "bad",
    [
        "/relative/a.jpg",
        "javascript:alert(1)",
        "data:image/png;base64,AAAA",
        "ftp://x/a.jpg",
        "https://u:p@x.example/a.jpg",
        "https://x.example/a b.jpg",
    ],
)
def test_unsafe_or_relative_image_is_omitted_but_article_is_kept(bad: str) -> None:
    result = normalized(bad)
    assert len(result.articles) == 1
    assert result.articles[0].image_url is None


def test_no_image_is_fine() -> None:
    assert normalized(None).articles[0].image_url is None


def test_article_model_rejects_non_http_image_url() -> None:
    with pytest.raises(ValueError):
        Article(
            article_id="3f2b8c2e-6c53-4b1f-9a8e-0a8e3c0e9d11",
            source_id="s",
            publisher="P",
            title="T",
            url="https://example.com/a",
            canonical_url="https://example.com/a",
            retrieved_at=datetime(2026, 9, 29, tzinfo=UTC),
            content_hash="0" * 64,
            image_url="javascript:alert(1)",
        )


# ---- rendering -------------------------------------------------------------------
def test_story_image_is_rendered_with_alt_text_and_never_a_placeholder() -> None:
    with_image = enriched(
        ranked("Picture story headline", image_url="https://img.example/p.jpg?a=1&b=2")
    )
    without = enriched(ranked("Plain story headline"))
    result = render([with_image, without])

    assert result.html.count("<img ") == 1
    assert 'src="https://img.example/p.jpg?a=1&amp;b=2"' in result.html
    assert 'alt="Picture story headline"' in result.html
    assert "placeholder" not in result.html.lower()


def test_no_images_means_no_img_tags() -> None:
    assert "<img" not in render([enriched(ranked("Only text here"))]).html


def photo_items(count: int, *, photo_when=lambda i: True, category: str = "Technology"):
    return [
        enriched(
            ranked(
                f"Headline number {i:02d}",
                url=f"https://news.example/h-{i}",
                categories=(category,),
                image_url=f"https://img.example/{i}.jpg" if photo_when(i) else None,
            )
        )
        for i in range(count)
    ]


def test_front_page_photos_use_lead_side_and_column_widths() -> None:
    html = render(photo_items(8)).html
    assert html.count('width="333"') == 1  # lead story
    assert html.count('width="185"') == 2  # two sidebar stories
    assert html.count('width="266"') == 2  # two stories in columns
    assert html.count('width="170"') == 3  # remaining stories form the photo grid


def test_photo_grid_holds_only_photo_stories_and_never_repeats_a_story() -> None:
    result = render(photo_items(12, photo_when=lambda i: i % 2 == 0))
    html = result.html
    assert html.count('<div class="tile"') == 3
    grid_block = html.split('<div class="photo-grid"', 1)[1].split(
        '<div class="category-section"', 1
    )[0]
    category_block = html.split('<div class="category-section"', 1)[1]
    pattern = r'href="(https://news\.example/h-\d+)"'
    grid_urls = set(re.findall(pattern, grid_block))
    category_urls = set(re.findall(pattern, category_block))
    assert len(grid_urls) == 3
    assert not grid_urls & category_urls
    assert "<img" not in category_block


def test_no_photo_grid_with_fewer_than_three_photos() -> None:
    html = render(photo_items(10, photo_when=lambda i: i in (6, 7))).html
    assert "In Pictures" not in html
    assert 'class="tile"' not in html


def test_category_sections_are_text_columns() -> None:
    html = render(photo_items(14, photo_when=lambda i: False)).html
    category_block = html.split('<div class="category-section"', 1)[1]
    assert category_block.count('<div class="story compact"') == 3
    assert category_block.count('class="col col-right"') == 2  # three columns
    assert "<img" not in html


def test_plain_text_lists_the_photo_grid_once() -> None:
    text = render(photo_items(12, photo_when=lambda i: i % 2 == 0)).plain_text
    assert "IN PICTURES" in text
    for i in range(12):
        assert len(re.findall(rf"news\.example/h-{i}\b", text)) <= 1


def test_top_stories_use_two_columns_and_stack_on_mobile() -> None:
    items = [enriched(ranked(f"Headline number {i}")) for i in range(5)]
    html = render(items).html
    assert html.count('class="col"') == 2  # stories 2-3 and 4-5 form two rows
    assert html.count('class="col col-right"') == 2
    assert ".col { display:block !important" in html


def test_image_markup_stays_email_safe() -> None:
    html = render(
        [enriched(ranked("Safe image story", image_url="https://img.example/p.jpg"))]
    ).html
    assert "<script" not in html and "javascript:" not in html
    assert "background-image" not in html


def test_plain_text_never_contains_image_urls() -> None:
    result = render(
        [enriched(ranked("Picture story headline", image_url="https://img.example/p.jpg"))]
    )
    assert "img.example" not in result.plain_text
