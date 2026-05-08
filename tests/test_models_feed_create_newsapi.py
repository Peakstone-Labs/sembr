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
from sembr.models import Feed, FeedCreate


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


@pytest.mark.parametrize("ip_url", ["127.0.0.1", "8.8.8.8", "192.168.1.1"])
def test_feed_create_newsapi_rejects_ip_addresses(monkeypatch, ip_url: str) -> None:
    """💡-2 (loop1 review): TLD-must-contain-alpha rejects bare IPs since
    newsapi.ai source.uri values are always real domains."""
    monkeypatch.setenv("NEWSAPI_POLL_INTERVAL_MINUTES", "30")
    get_settings.cache_clear()
    with pytest.raises(ValidationError):
        FeedCreate(name="Bad", url=ip_url, source_type="newsapi")


def test_feed_create_newsapi_accepts_all_recommended_sources(monkeypatch) -> None:
    """All 30 RECOMMENDED_SOURCES URIs must pass the hostname regex —
    regression guard for the 💡-2 stricter regex."""
    monkeypatch.setenv("NEWSAPI_POLL_INTERVAL_MINUTES", "30")
    get_settings.cache_clear()
    from sembr.collector.newsapi import RECOMMENDED_SOURCES
    for s in RECOMMENDED_SOURCES:
        f = FeedCreate(name=s["title"], url=s["uri"], source_type="newsapi")
        assert f.url == s["uri"]


def test_frontend_hostname_regex_mirrors_backend() -> None:
    """Drift guard (review-loop2 🟡-1): the JS-side hostname regex in
    feeds.js submitCreate() must use the same pattern as the Python regex
    or users see backend 422 instead of a friendly client-side hint."""
    import re as _re
    from pathlib import Path
    from sembr.models import _NEWSAPI_HOSTNAME_RE
    js_path = Path(__file__).resolve().parent.parent / "web" / "static" / "feeds.js"
    js_src = js_path.read_text(encoding="utf-8")
    # Locate the JS regex literal in submitCreate. The `i` flag is allowed
    # since hostnames are already lower-cased before the test, but the
    # *body* of the regex must match the Python pattern exactly.
    m = _re.search(
        r"if \(!/(?P<body>\^\[a-z0-9\][^/]+\$)/\.test\(url\)\)",
        js_src,
    )
    assert m is not None, (
        "could not locate the newsapi hostname regex in feeds.js; "
        "if you renamed the assertion, update this test"
    )
    js_pattern = m.group("body")
    py_pattern = _NEWSAPI_HOSTNAME_RE.pattern
    assert js_pattern == py_pattern, (
        f"frontend regex out of sync with backend.\n"
        f"  feeds.js:    {js_pattern}\n"
        f"  models.py:   {py_pattern}"
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


# ---------------------------------------------------------------------------
# 🟡-1 (loop1 review): Feed read model does NOT inherit FeedCreate's
# side-effecting validator. Constructing Feed with an "unnormalized" URL or
# an out-of-sync poll_interval is valid (DB rows already normalized at write).
# ---------------------------------------------------------------------------


def test_feed_read_model_does_not_run_url_validator(monkeypatch) -> None:
    """Feed reads should not normalize/coerce — DB rows are write-time normalized."""
    monkeypatch.setenv("NEWSAPI_POLL_INTERVAL_MINUTES", "60")
    get_settings.cache_clear()
    feed = Feed(
        id=1,
        name="Reuters",
        url="reuters.com",
        source_type="newsapi",
        config={},
        poll_interval_minutes=99,  # divergent — would be coerced to 60 if validator ran
        last_collected_at=None,
        created_at="2026-05-08T00:00:00Z",
        enabled=True,
    )
    assert feed.poll_interval_minutes == 99
    assert feed.url == "reuters.com"


def test_feed_read_model_accepts_unnormalized_url() -> None:
    """Feed model is plain — accepts whatever the row holds (write path enforces format)."""
    feed = Feed(
        id=2,
        name="X",
        url="HTTPS://www.Foo.com/",  # would fail FeedCreate's normalize check
        source_type="newsapi",
        config={},
        poll_interval_minutes=30,
        last_collected_at=None,
        created_at="2026-05-08T00:00:00Z",
        enabled=True,
    )
    assert feed.url == "HTTPS://www.Foo.com/"


def test_feed_read_model_does_not_call_get_settings(monkeypatch) -> None:
    """Reads must not depend on Settings — guards against 🟡-1 regression
    where the Settings cache is bypassed and a malformed .env breaks /feeds."""
    import sembr.config as cfg
    calls: list[str] = []
    real = cfg.get_settings

    def _spy() -> cfg.Settings:
        calls.append("called")
        return real()

    monkeypatch.setattr(cfg, "get_settings", _spy)
    Feed(
        id=3,
        name="X",
        url="reuters.com",
        source_type="newsapi",
        config={},
        poll_interval_minutes=30,
        last_collected_at=None,
        created_at="2026-05-08T00:00:00Z",
        enabled=True,
    )
    assert calls == []
