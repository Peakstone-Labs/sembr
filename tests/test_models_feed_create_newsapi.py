"""D11/R6 tests: FeedCreate.url validator branches by source_type.

* RSS keeps the existing http(s):// scheme requirement.
* newsapi expects a bare hostname; normalize_source_uri runs on write so the
  feeds.url UNIQUE constraint catches scheme/case/www-prefix duplicates.
* newsapi feeds get poll_interval_minutes coerced to settings to keep the
  list-row column consistent with the global master-tick interval (R6).
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from sembr.config import get_settings
from sembr.models import FeedCreate


# ---------------------------------------------------------------------------
# RSS branch — backward compatible
# ---------------------------------------------------------------------------


def test_feed_create_rss_requires_scheme() -> None:
    with pytest.raises(ValidationError):
        FeedCreate(name="Bad", url="example.com/rss", source_type="rss")


def test_feed_create_rss_accepts_http() -> None:
    f = FeedCreate(name="A", url="http://example.com/rss", source_type="rss")
    assert f.url == "http://example.com/rss"


# ---------------------------------------------------------------------------
# newsapi branch
# ---------------------------------------------------------------------------


def test_feed_create_newsapi_url_normalized(monkeypatch) -> None:
    monkeypatch.setenv("NEWSAPI_POLL_INTERVAL_MINUTES", "30")
    get_settings.cache_clear()
    f = FeedCreate(name="Reuters", url="Reuters.com", source_type="newsapi")
    assert f.url == "reuters.com"


def test_feed_create_newsapi_strips_scheme_and_www(monkeypatch) -> None:
    monkeypatch.setenv("NEWSAPI_POLL_INTERVAL_MINUTES", "30")
    get_settings.cache_clear()
    f = FeedCreate(name="NYT", url="HTTPS://www.NYTimes.com/", source_type="newsapi")
    assert f.url == "nytimes.com"


def test_feed_create_newsapi_rejects_non_hostname(monkeypatch) -> None:
    monkeypatch.setenv("NEWSAPI_POLL_INTERVAL_MINUTES", "30")
    get_settings.cache_clear()
    with pytest.raises(ValidationError):
        FeedCreate(name="Bad", url="not_a_host", source_type="newsapi")


def test_feed_create_newsapi_rejects_path(monkeypatch) -> None:
    """Hosts with paths after normalize would still fail the hostname regex."""
    monkeypatch.setenv("NEWSAPI_POLL_INTERVAL_MINUTES", "30")
    get_settings.cache_clear()
    with pytest.raises(ValidationError):
        FeedCreate(
            name="Bad",
            url="https://reuters.com/world/something",
            source_type="newsapi",
        )


def test_feed_create_newsapi_coerces_poll_interval_to_settings(monkeypatch) -> None:
    """R6: front-end disables the field but a stale form may still POST 30;
    backend forces it to the settings value so the list view stays consistent."""
    monkeypatch.setenv("NEWSAPI_POLL_INTERVAL_MINUTES", "60")
    get_settings.cache_clear()
    f = FeedCreate(
        name="Reuters",
        url="reuters.com",
        source_type="newsapi",
        poll_interval_minutes=30,
    )
    assert f.poll_interval_minutes == 60


def test_feed_create_rss_keeps_supplied_poll_interval(monkeypatch) -> None:
    monkeypatch.setenv("NEWSAPI_POLL_INTERVAL_MINUTES", "60")
    get_settings.cache_clear()
    f = FeedCreate(
        name="X",
        url="http://example.com/rss",
        source_type="rss",
        poll_interval_minutes=15,
    )
    assert f.poll_interval_minutes == 15
