"""Tests for YAML source configuration."""

from pathlib import Path

import pytest
from app.config import SourceConfig, load_sources

ROOT = Path(__file__).resolve().parents[1]


def test_loads_configured_real_feeds_for_the_newspaper_sections() -> None:
    sources = load_sources(ROOT / "config" / "sources.yaml")

    assert len(sources) >= 12
    by_id = {source.source_id: source for source in sources}
    bbc = by_id["bbc_world"]
    guardian = by_id["guardian_world"]
    assert (bbc.source_id, bbc.name, bbc.kind, bbc.endpoint) == (
        "bbc_world",
        "BBC News",
        "rss_atom",
        "https://feeds.bbci.co.uk/news/world/rss.xml",
    )
    assert (guardian.source_id, guardian.name, guardian.kind, guardian.endpoint) == (
        "guardian_world",
        "The Guardian",
        "rss_atom",
        "https://www.theguardian.com/world/rss",
    )
    assert all(source.enabled for source in sources)
    assert bbc.categories == guardian.categories == ("World",)
    assert bbc.quality_weight == 1.0
    assert guardian.quality_weight == 0.9
    assert bbc.source_id != guardian.source_id
    assert by_id["guardian_india"].categories == ("India",)
    assert by_id["bbc_business"].categories == ("Business & Economy",)
    assert by_id["bbc_science"].categories == ("Science & Space",)
    assert by_id["bbc_sport"].categories == ("Sports",)
    assert by_id["bbc_entertainment"].categories == ("Entertainment",)


def test_rejects_unsupported_source_kind() -> None:
    with pytest.raises(ValueError, match="Unsupported source kind"):
        SourceConfig.from_mapping(
            {
                "source_id": "example",
                "enabled": True,
                "name": "Example",
                "kind": "json_api",
                "endpoint": "https://example.com/feed",
                "categories": [],
                "quality_weight": 0.5,
                "timeout_seconds": 10,
                "request_interval_seconds": 60,
            }
        )


def test_source_endpoint_rejects_embedded_credentials_without_echoing_them() -> None:
    with pytest.raises(ValueError, match="embedded credentials") as raised:
        SourceConfig.from_mapping(
            {
                "source_id": "example",
                "enabled": True,
                "name": "Example",
                "kind": "rss_atom",
                "endpoint": "https://account:private-password@example.com/feed.xml",
                "categories": [],
                "quality_weight": 0.5,
                "timeout_seconds": 10,
                "request_interval_seconds": 60,
            }
        )

    assert "private-password" not in str(raised.value)
