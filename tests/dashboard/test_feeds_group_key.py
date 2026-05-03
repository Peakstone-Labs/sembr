"""SC#7: derive_group_key URL table — direct hosts vs proxy hosts."""
from __future__ import annotations

import pytest

from sembr.collector.host_limiter import derive_group_key

PROXY = frozenset({"rsshub:1200"})


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://feeds.bbci.co.uk/news/rss.xml", "feeds.bbci.co.uk"),
        ("https://news.ycombinator.com/rss", "news.ycombinator.com"),
        ("http://rsshub:1200/twitter/user/elonmusk", "rsshub:1200:twitter"),
        ("http://rsshub:1200/github/issue/anthropics/claude-code", "rsshub:1200:github"),
        ("http://rsshub:1200/v8/news", "rsshub:1200:v8"),
        ("http://rsshub:1200/", "rsshub:1200"),
        ("https://Example.COM/path", "example.com"),
        ("https://localhost:8080/feed.xml", "localhost:8080"),
    ],
)
def test_derive_group_key(url: str, expected: str) -> None:
    assert derive_group_key(url, PROXY) == expected


def test_proxy_hosts_set_normalisation() -> None:
    """Settings.proxy_hosts_set must tolerate trailing slashes / scheme prefixes."""
    from sembr.config import Settings

    s = Settings(proxy_hosts="http://Rsshub:1200/, foo.example.com , https://bar.io/")
    out = s.proxy_hosts_set
    assert "rsshub:1200" in out
    assert "foo.example.com" in out
    assert "bar.io" in out
    # No raw entries with whitespace or scheme.
    assert not any(" " in e or e.startswith("http") for e in out)
