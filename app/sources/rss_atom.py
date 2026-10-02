"""RSS and Atom adapter that returns raw provider entries only."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from types import MappingProxyType
from typing import cast
from xml.etree import ElementTree

import httpx

from app.config import SourceConfig
from app.sources.base import FetchedFeed, RawProviderEntry, SourceFetchError

MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_FETCH_ATTEMPTS = 2
_XML_NAMESPACE = "http://www.w3.org/XML/1998/namespace"


class RSSAtomSource:
    """Fetch one configured RSS/Atom feed and extract provider-native fields."""

    def __init__(
        self,
        source: SourceConfig,
        *,
        client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if source.kind != "rss_atom":
            raise ValueError("RSSAtomSource requires a source with kind='rss_atom'")
        self.source = source
        self._client = client
        self._clock = clock or (lambda: datetime.now(UTC))

    def fetch(self) -> FetchedFeed:
        """Retrieve and parse a feed; individual article validation is deferred."""
        if self._client is not None:
            return self._fetch_with_client(self._client)
        with httpx.Client(follow_redirects=True) as client:
            return self._fetch_with_client(client)

    def _fetch_with_client(self, client: httpx.Client) -> FetchedFeed:
        timeout = httpx.Timeout(
            self.source.timeout_seconds,
            connect=self.source.timeout_seconds,
            read=self.source.timeout_seconds,
            write=self.source.timeout_seconds,
            pool=self.source.timeout_seconds,
        )
        response: httpx.Response | None = None
        for attempt in range(MAX_FETCH_ATTEMPTS):
            try:
                response = client.get(self.source.endpoint, timeout=timeout)
            except httpx.TimeoutException as error:
                if attempt + 1 < MAX_FETCH_ATTEMPTS:
                    continue
                raise SourceFetchError(
                    f"Timed out fetching source {self.source.source_id}"
                ) from error
            except httpx.HTTPError as error:
                if attempt + 1 < MAX_FETCH_ATTEMPTS:
                    continue
                raise SourceFetchError(
                    "Network error fetching source "
                    f"{self.source.source_id} ({type(error).__name__})"
                ) from error

            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 < MAX_FETCH_ATTEMPTS:
                    continue
            if response.status_code < 200 or response.status_code >= 300:
                raise SourceFetchError(
                    f"HTTP {response.status_code} fetching source {self.source.source_id}",
                    http_status=response.status_code,
                )
            break

        if response is None:
            raise SourceFetchError(f"No response from source {self.source.source_id}")
        if len(response.content) > MAX_FEED_BYTES:
            raise SourceFetchError(f"Feed from {self.source.source_id} exceeds the size limit")

        retrieved_at = self._clock()
        if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        retrieved_at = retrieved_at.astimezone(UTC)

        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as error:
            raise SourceFetchError(f"Malformed XML from source {self.source.source_id}") from error

        format_name = _local_name(root.tag).lower()
        if format_name == "rss":
            entries = _parse_rss(root)
        elif format_name == "feed":
            entries = _parse_atom(root)
        else:
            raise SourceFetchError(
                f"Unsupported feed document from {self.source.source_id}: root {format_name!r}"
            )

        return FetchedFeed(self.source, retrieved_at, tuple(entries))


def _parse_rss(root: ElementTree.Element) -> list[RawProviderEntry]:
    channel = next((child for child in root if _local_name(child.tag) == "channel"), None)
    if channel is None:
        raise SourceFetchError("Malformed RSS feed: missing channel")
    feed_language = _child_text(channel, "language")
    entries: list[RawProviderEntry] = []
    for item in (child for child in channel if _local_name(child.tag) == "item"):
        fields: dict[str, object] = {
            "title": _child_text(item, "title"),
            "link": _child_text(item, "link"),
            "pubDate": _child_text(item, "pubDate"),
            "description": _child_text(item, "description"),
            "categories": tuple(
                text
                for category in item
                if _local_name(category.tag) == "category"
                if (text := _element_text(category))
            ),
            "language": item.attrib.get(f"{{{_XML_NAMESPACE}}}lang") or feed_language,
        }
        entries.append(RawProviderEntry("rss", MappingProxyType(fields)))
    return entries


def _parse_atom(root: ElementTree.Element) -> list[RawProviderEntry]:
    feed_language = root.attrib.get(f"{{{_XML_NAMESPACE}}}lang")
    entries: list[RawProviderEntry] = []
    for entry in (child for child in root if _local_name(child.tag) == "entry"):
        links = tuple(
            (
                link.attrib.get("rel", "alternate"),
                link.attrib.get("href", ""),
            )
            for link in entry
            if _local_name(link.tag) == "link"
        )
        fields: dict[str, object] = {
            "title": _child_text(entry, "title"),
            "links": links,
            "published": _child_text(entry, "published"),
            "summary": _child_text(entry, "summary"),
            "categories": tuple(
                category.attrib.get("term", "")
                for category in entry
                if _local_name(category.tag) == "category"
            ),
            "language": entry.attrib.get(f"{{{_XML_NAMESPACE}}}lang") or feed_language,
        }
        entries.append(RawProviderEntry("atom", MappingProxyType(fields)))
    return entries


def _local_name(tag: object) -> str:
    return cast(str, tag).rsplit("}", 1)[-1]


def _child_text(element: ElementTree.Element, name: str) -> str | None:
    child = next((item for item in element if _local_name(item.tag) == name), None)
    return _element_text(child) if child is not None else None


def _element_text(element: ElementTree.Element) -> str:
    return "".join(element.itertext()).strip()
