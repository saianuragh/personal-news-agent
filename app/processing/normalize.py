"""Convert provider-native feed entries into canonical Article records."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from app.models.article import (
    MAX_CATEGORY_LENGTH,
    MAX_DESCRIPTION_LENGTH,
    MAX_SOURCE_CATEGORIES,
    Article,
)
from app.sources.base import FetchedFeed, ProviderFormat, RawProviderEntry

_LANGUAGE_TAG_PATTERN = re.compile(r"[A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*\Z")
_TRACKING_QUERY_KEYS = {"fbclid", "gclid", "dclid", "mc_cid", "mc_eid", "_hsenc", "_hsmi"}


@dataclass(frozen=True, slots=True)
class NormalizationIssue:
    """A non-fatal validation issue tied to one raw feed entry."""

    entry_index: int
    field: str
    message: str


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    """Valid canonical articles and non-fatal per-entry validation issues."""

    articles: tuple[Article, ...]
    issues: tuple[NormalizationIssue, ...]


class _PlainTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def normalize_feed(feed: FetchedFeed) -> NormalizationResult:
    """Normalize every raw feed entry independently into an ``Article``."""
    articles: list[Article] = []
    issues: list[NormalizationIssue] = []
    for index, entry in enumerate(feed.entries):
        try:
            article, entry_issues = _normalize_entry(feed, entry, index)
        except (TypeError, ValueError) as error:
            issues.append(NormalizationIssue(index, "article", str(error)))
            continue
        issues.extend(entry_issues)
        if article is not None:
            articles.append(article)
    return NormalizationResult(tuple(articles), tuple(issues))


def _normalize_entry(
    feed: FetchedFeed,
    entry: RawProviderEntry,
    index: int,
) -> tuple[Article | None, list[NormalizationIssue]]:
    issues: list[NormalizationIssue] = []
    fields = entry.fields
    if entry.format == "rss":
        title = _optional_string(fields.get("title"))
        url = _optional_string(fields.get("link"))
        published_raw = fields.get("pubDate")
        description_raw = fields.get("description")
        raw_categories = fields.get("categories", ())
        language_raw = fields.get("language")
    else:
        title = _optional_string(fields.get("title"))
        url = _atom_alternate_link(fields.get("links"))
        published_raw = fields.get("published")
        description_raw = fields.get("summary")
        raw_categories = fields.get("categories", ())
        language_raw = fields.get("language")

    if title is None or not title.strip():
        issues.append(NormalizationIssue(index, "title", "Missing required title; entry rejected"))
        return None, issues
    title = title.strip()
    if url is None or not url.strip():
        issues.append(
            NormalizationIssue(index, "url", "Missing required article URL; entry rejected")
        )
        return None, issues

    try:
        canonical_url = canonicalize_url(url)
    except ValueError as error:
        issues.append(NormalizationIssue(index, "url", f"Invalid required article URL: {error}"))
        return None, issues

    published_at = _publication_time(entry.format, published_raw)
    if published_raw is not None and published_at is None:
        issues.append(
            NormalizationIssue(
                index, "published_at", "Invalid publication timestamp; kept as unknown"
            )
        )

    if description_raw is not None and not isinstance(description_raw, str):
        issues.append(NormalizationIssue(index, "description", "Invalid description; omitted"))
    description = _plain_text(description_raw)
    if description is not None and len(description) > MAX_DESCRIPTION_LENGTH:
        description = description[:MAX_DESCRIPTION_LENGTH].rstrip()
        issues.append(
            NormalizationIssue(
                index, "description", "Description truncated to the supported length"
            )
        )

    categories = _categories(raw_categories, index, issues)
    language = _language(language_raw, index, issues)
    content_hash = _content_hash(
        source_id=feed.source.source_id,
        publisher=feed.source.name,
        title=title,
        canonical_url=canonical_url,
        published_at=published_at,
        description=description,
        categories=categories,
        language=language,
    )

    article = Article(
        article_id=uuid4(),
        source_id=feed.source.source_id,
        publisher=feed.source.name,
        title=title,
        url=url.strip(),
        canonical_url=canonical_url,
        retrieved_at=feed.retrieved_at,
        content_hash=content_hash,
        published_at=published_at,
        description=description,
        source_categories=categories,
        language=language,
    )
    return article, issues


def canonicalize_url(url: str) -> str:
    """Normalize safe URL identity components without changing the display URL."""
    if not isinstance(url, str) or not url.strip() or any(char.isspace() for char in url):
        raise ValueError("URL is empty or contains whitespace")
    try:
        parts = urlsplit(url.strip())
        hostname = parts.hostname
        port = parts.port
    except ValueError as error:
        raise ValueError("URL is malformed") from error
    if parts.scheme.lower() not in {"http", "https"} or not hostname:
        raise ValueError("URL must be absolute and use HTTP or HTTPS")
    if parts.username is not None or parts.password is not None:
        raise ValueError("URL must not include embedded credentials")

    host = hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}{f':{port}' if port is not None else ''}"

    query = parse_qsl(parts.query, keep_blank_values=True)
    retained_query = [
        (key, value)
        for key, value in query
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_QUERY_KEYS
    ]
    return urlunsplit(
        (parts.scheme.lower(), netloc, parts.path, urlencode(retained_query, doseq=True), "")
    )


def _publication_time(format_name: ProviderFormat, raw_value: object) -> datetime | None:
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    try:
        if format_name == "rss":
            value = parsedate_to_datetime(raw_value)
        else:
            atom_value = raw_value.strip()
            if atom_value.endswith("Z"):
                atom_value = f"{atom_value[:-1]}+00:00"
            value = datetime.fromisoformat(atom_value)
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def _plain_text(raw_value: object) -> str | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        return None
    extractor = _PlainTextExtractor()
    try:
        extractor.feed(raw_value)
        extractor.close()
    except Exception:
        return None
    text = " ".join(" ".join(extractor.parts).split())
    return text or None


def _categories(
    raw_value: object, index: int, issues: list[NormalizationIssue]
) -> tuple[str, ...]:
    if not isinstance(raw_value, (list, tuple)):
        if raw_value in (None, ()):
            return ()
        issues.append(NormalizationIssue(index, "source_categories", "Invalid categories; omitted"))
        return ()
    cleaned = [value.strip() for value in raw_value if isinstance(value, str) and value.strip()]
    invalid_values = len(cleaned) != len(raw_value) or any(
        len(value) > MAX_CATEGORY_LENGTH for value in cleaned
    )
    if invalid_values or len(cleaned) > MAX_SOURCE_CATEGORIES:
        issues.append(NormalizationIssue(index, "source_categories", "Invalid categories; omitted"))
        return ()
    return tuple(cleaned)


def _language(raw_value: object, index: int, issues: list[NormalizationIssue]) -> str | None:
    if raw_value is None:
        return None
    if (
        isinstance(raw_value, str)
        and len(raw_value.strip()) <= 35
        and _LANGUAGE_TAG_PATTERN.fullmatch(raw_value.strip())
    ):
        return raw_value.strip()
    issues.append(NormalizationIssue(index, "language", "Invalid language tag; omitted"))
    return None


def _atom_alternate_link(raw_value: object) -> str | None:
    if not isinstance(raw_value, (list, tuple)):
        return None
    first_link: str | None = None
    for item in raw_value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        relation, href = item
        if not isinstance(href, str) or not href:
            continue
        if first_link is None:
            first_link = href
        if relation in {"alternate", ""}:
            return href
    return first_link


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _content_hash(
    *,
    source_id: str,
    publisher: str,
    title: str,
    canonical_url: str,
    published_at: datetime | None,
    description: str | None,
    categories: tuple[str, ...],
    language: str | None,
) -> str:
    """Hash stable normalized fields using canonical UTF-8 JSON.

    The payload contains source ID, publisher, title, canonical URL, UTC
    publication time (ISO 8601 or null), plain-text description, categories,
    and language. JSON keys are sorted and separators are compact. Generated
    article IDs and retrieval timestamps are excluded so unchanged feed
    content gets the same SHA-256 hash on later fetches.
    """
    canonical_payload = {
        "canonical_url": canonical_url,
        "categories": list(categories),
        "description": description,
        "language": language,
        "published_at": published_at.isoformat() if published_at else None,
        "publisher": publisher,
        "source_id": source_id,
        "title": title,
    }
    encoded_payload = json.dumps(
        canonical_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded_payload).hexdigest()
