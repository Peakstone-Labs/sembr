"""Tests for sembr.logbus.router — TAG_PREFIX_MAP routing logic."""

import logging

import pytest

from sembr.logbus.router import ALL_TAGS, route


def _make_record(name: str, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=level,
        pathname="",
        lineno=0,
        msg="test",
        args=(),
        exc_info=None,
    )


@pytest.mark.parametrize(
    "logger_name,expected_tag",
    [
        # Exact collector prefix
        ("sembr.collector", "collector"),
        ("sembr.collector.rss", "collector"),
        ("sembr.collector.http", "collector"),
        # Embedder
        ("sembr.embedder", "embedder"),
        ("sembr.embedder.factory", "embedder"),
        # vector_store → embedder
        ("sembr.vector_store", "embedder"),
        ("sembr.vector_store.qdrant", "embedder"),
        # Matcher
        ("sembr.matcher", "matcher"),
        ("sembr.matcher.scheduler", "matcher"),
        # Summarizer → matcher
        ("sembr.summarizer", "matcher"),
        ("sembr.summarizer.pipeline", "matcher"),
        # Notifier
        ("sembr.notifier", "notifier"),
        ("sembr.notifier.telegram", "notifier"),
        # API / dashboard / db → api
        ("sembr.api", "api"),
        ("sembr.api.feeds", "api"),
        ("sembr.dashboard", "api"),
        ("sembr.dashboard.routes", "api"),
        ("sembr.db", "api"),
        ("sembr.db.sqlite", "api"),
        # Scheduler
        ("apscheduler", "scheduler"),
        ("apscheduler.executors", "scheduler"),
        ("apscheduler.scheduler", "scheduler"),
        # HTTP
        ("uvicorn.access", "http"),
        ("httpx", "http"),
        ("httpx._client", "http"),
        ("httpcore", "http"),
        ("httpcore.connection", "http"),
        # uvicorn.error / uvicorn → api
        ("uvicorn.error", "api"),
        ("uvicorn", "api"),
        # sembr fallback
        ("sembr", "api"),
        # Unknown → default api
        ("some.unknown.library", "api"),
        ("", "api"),
    ],
)
def test_route(logger_name: str, expected_tag: str) -> None:
    record = _make_record(logger_name)
    assert route(record) == expected_tag, f"logger={logger_name!r} expected={expected_tag!r}"


def test_all_tags_coverage() -> None:
    """Ensure ALL_TAGS contains exactly the 7 expected tags."""
    assert set(ALL_TAGS) == {
        "collector",
        "embedder",
        "matcher",
        "notifier",
        "api",
        "scheduler",
        "http",
    }
    assert len(ALL_TAGS) == 7


def test_route_does_not_match_partial_prefix() -> None:
    """'sembr.collectorfoo' must NOT route to collector."""
    record = _make_record("sembr.collectorfoo")
    # Falls through to sembr.* → api (not collector)
    assert route(record) == "api"
