"""Validated application configuration loaded from YAML."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """Configuration shared by a source adapter and the normalizer."""

    source_id: str
    enabled: bool
    name: str
    kind: str
    endpoint: str
    categories: tuple[str, ...]
    quality_weight: float
    timeout_seconds: float
    request_interval_seconds: float
    credential_env: str | None = None
    language: str | None = None

    @classmethod
    def from_mapping(cls, value: Any) -> SourceConfig:
        if not isinstance(value, dict):
            raise ValueError("Each source configuration must be a mapping")

        required = (
            "source_id",
            "enabled",
            "name",
            "kind",
            "endpoint",
            "categories",
            "quality_weight",
            "timeout_seconds",
            "request_interval_seconds",
        )
        missing = [field for field in required if field not in value]
        if missing:
            missing_fields = ", ".join(missing)
            raise ValueError(
                f"Source configuration is missing required fields: {missing_fields}"
            )

        source_id = _non_empty_string(value["source_id"], "source_id", 200)
        name = _non_empty_string(value["name"], "name", 200)
        kind = _non_empty_string(value["kind"], "kind", 30)
        if kind != "rss_atom":
            raise ValueError(f"Unsupported source kind: {kind}")

        endpoint = _non_empty_string(value["endpoint"], "endpoint", 2048)
        parsed_endpoint = urlsplit(endpoint)
        if (
            parsed_endpoint.scheme.lower() not in {"http", "https"}
            or not parsed_endpoint.hostname
            or any(character.isspace() for character in endpoint)
        ):
            raise ValueError("endpoint must be an absolute HTTP(S) URL")
        if parsed_endpoint.username is not None or parsed_endpoint.password is not None:
            raise ValueError("endpoint must not include embedded credentials")

        if not isinstance(value["enabled"], bool):
            raise ValueError("enabled must be a boolean")
        categories = value["categories"]
        if not isinstance(categories, list) or not all(
            isinstance(category, str) and category.strip() for category in categories
        ):
            raise ValueError("categories must be a list of non-empty strings")

        quality_weight = _bounded_number(value["quality_weight"], "quality_weight", 0.0, 1.0)
        timeout_seconds = _bounded_number(value["timeout_seconds"], "timeout_seconds", 0.1, 60.0)
        request_interval_seconds = _bounded_number(
            value["request_interval_seconds"], "request_interval_seconds", 0.1, 86_400.0
        )

        credential_env = value.get("credential_env")
        if credential_env is not None:
            credential_env = _non_empty_string(credential_env, "credential_env", 200)
        language = value.get("language")
        if language is not None:
            language = _non_empty_string(language, "language", 35)

        return cls(
            source_id=source_id,
            enabled=value["enabled"],
            name=name,
            kind=kind,
            endpoint=endpoint,
            categories=tuple(category.strip() for category in categories),
            quality_weight=quality_weight,
            timeout_seconds=timeout_seconds,
            request_interval_seconds=request_interval_seconds,
            credential_env=credential_env,
            language=language,
        )


def load_sources(path: str | Path) -> tuple[SourceConfig, ...]:
    """Load and validate the configured source list."""
    config_path = Path(path)
    try:
        document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"Unable to load source configuration at {config_path}") from error

    if not isinstance(document, dict) or not isinstance(document.get("sources"), list):
        raise ValueError("Source configuration must contain a 'sources' list")

    sources = tuple(SourceConfig.from_mapping(item) for item in document["sources"])
    source_ids = [source.source_id for source in sources]
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("source_id values must be unique")
    return sources


def _non_empty_string(value: Any, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{field_name} must be non-empty and at most {max_length} characters")
    return normalized


def _bounded_number(value: Any, field_name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field_name} must be a number")
    number = float(value)
    if not minimum <= number <= maximum:
        raise ValueError(f"{field_name} must be between {minimum} and {maximum}")
    return number
