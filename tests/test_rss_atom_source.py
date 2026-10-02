"""Mocked HTTP tests for the RSS/Atom raw-entry adapter."""

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from app.config import load_sources
from app.sources.base import SourceFetchError
from app.sources.rss_atom import RSSAtomSource

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
FETCHED_AT = datetime(2026, 9, 29, 10, tzinfo=UTC)


def source_config():
    return load_sources(ROOT / "config" / "sources.yaml")[0]


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def make_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_parses_rss_into_raw_provider_entries_without_creating_articles() -> None:
    client = make_client(
        lambda request: httpx.Response(200, content=fixture_bytes("bbc_world_sample.rss"))
    )
    with client:
        feed = RSSAtomSource(source_config(), client=client, clock=lambda: FETCHED_AT).fetch()

    assert feed.retrieved_at == FETCHED_AT
    assert len(feed.entries) == 5
    assert feed.entries[0].format == "rss"
    assert feed.entries[0].fields["pubDate"] == "Tue, 29 Sep 2026 08:30:00 GMT"
    assert feed.entries[0].fields["categories"] == ("World", "Policy")
    assert "published_at" not in feed.entries[0].fields
    assert "content_hash" not in feed.entries[0].fields


def test_parses_atom_entries_and_alternate_links() -> None:
    client = make_client(lambda request: httpx.Response(200, content=fixture_bytes("sample.atom")))
    with client:
        feed = RSSAtomSource(source_config(), client=client, clock=lambda: FETCHED_AT).fetch()

    assert len(feed.entries) == 2
    assert feed.entries[0].format == "atom"
    assert feed.entries[0].fields["published"] == "2026-09-29T13:45:00+05:30"
    assert feed.entries[0].fields["links"] == (
        ("alternate", "https://example.com/atom/story-1?utm_campaign=sample&ref=42#section"),
    )


def test_http_503_retries_once_then_succeeds() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503)
        return httpx.Response(200, content=fixture_bytes("bbc_world_sample.rss"))

    client = make_client(handler)
    with client:
        feed = RSSAtomSource(source_config(), client=client, clock=lambda: FETCHED_AT).fetch()

    assert attempts == 2
    assert feed.entries


def test_http_503_on_both_attempts_raises_contextual_source_error() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503)

    client = make_client(handler)
    error_match = "HTTP 503 fetching source bbc_world"
    with client, pytest.raises(SourceFetchError, match=error_match) as raised:
        RSSAtomSource(source_config(), client=client, clock=lambda: FETCHED_AT).fetch()

    assert attempts == 2
    assert raised.value.http_status == 503


def test_sets_explicit_connect_and_read_timeouts() -> None:
    observed_timeouts: dict[str, float | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed_timeouts.update(request.extensions["timeout"])
        return httpx.Response(200, content=fixture_bytes("bbc_world_sample.rss"))

    client = make_client(handler)
    with client:
        RSSAtomSource(source_config(), client=client, clock=lambda: FETCHED_AT).fetch()

    assert observed_timeouts["connect"] == source_config().timeout_seconds
    assert observed_timeouts["read"] == source_config().timeout_seconds


def test_http_error_is_reported() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(404)

    client = make_client(handler)
    with client, pytest.raises(SourceFetchError, match="HTTP 404"):
        RSSAtomSource(source_config(), client=client).fetch()
    assert attempts == 1


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_transient_http_statuses_receive_exactly_two_total_attempts(status: int) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status)

    client = make_client(handler)
    with client, pytest.raises(SourceFetchError, match=f"HTTP {status}") as raised:
        RSSAtomSource(source_config(), client=client).fetch()

    assert attempts == 2
    assert raised.value.http_status == status


def test_malformed_and_oversized_success_responses_are_not_retried() -> None:
    for content, expected_error in (
        (fixture_bytes("malformed.xml"), "Malformed XML"),
        (b"x" * (5 * 1024 * 1024 + 1), "exceeds the size limit"),
    ):
        attempts = 0

        def handler(request: httpx.Request, body: bytes = content) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(200, content=body)

        client = make_client(handler)
        with client, pytest.raises(SourceFetchError, match=expected_error):
            RSSAtomSource(source_config(), client=client).fetch()
        assert attempts == 1


def test_http_failure_context_does_not_echo_response_body_or_request_secret() -> None:
    secret = "fixture-secret-that-must-not-appear"
    response_body = f"credential={secret}".encode()
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, content=response_body)

    client = make_client(handler)
    with client, pytest.raises(SourceFetchError) as raised:
        RSSAtomSource(source_config(), client=client).fetch()

    assert attempts == 2
    assert raised.value.http_status == 503
    assert secret not in str(raised.value)
    assert "credential=" not in str(raised.value)


def test_repeated_timeout_is_reported_after_bounded_retry() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("read timed out", request=request)

    client = make_client(handler)
    with client, pytest.raises(SourceFetchError, match="Timed out"):
        RSSAtomSource(source_config(), client=client).fetch()
    assert attempts == 2


def test_malformed_xml_is_reported() -> None:
    client = make_client(
        lambda request: httpx.Response(200, content=fixture_bytes("malformed.xml"))
    )
    with client, pytest.raises(SourceFetchError, match="Malformed XML"):
        RSSAtomSource(source_config(), client=client).fetch()
