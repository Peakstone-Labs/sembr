# SPDX-License-Identifier: Apache-2.0
"""FeedCreate validation for source_type='wisburg-report' (design D2).

The url must normalize onto the three-member ENDPOINT_URLS whitelist;
anything else (other wisburg endpoints, arbitrary URLs) is rejected with an
error message that lists the valid values.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sembr.models import FeedCreate

REPORTS_URL = "https://api-omen.wisburg.com/api/reports"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://api-omen.wisburg.com/api/reports", REPORTS_URL),
        ("HTTPS://API-OMEN.WISBURG.COM/API/REPORTS/", REPORTS_URL),
        ("http://api-omen.wisburg.com/api/reports", REPORTS_URL),
        (
            "https://api-omen.wisburg.com/api/earningscalls",
            "https://api-omen.wisburg.com/api/earningscalls",
        ),
        (
            "https://api-omen.wisburg.com/api/am-reports/",
            "https://api-omen.wisburg.com/api/am-reports",
        ),
    ],
)
def test_wisburg_url_whitelist_accepts_and_normalizes(raw: str, expected: str) -> None:
    feed = FeedCreate(name="wisburg", url=raw, source_type="wisburg-report")
    assert feed.url == expected


@pytest.mark.parametrize(
    "bad",
    [
        "https://api-omen.wisburg.com/api/feed",  # endpoint outside this round's scope
        "https://api-omen.wisburg.com/api/articles",
        "https://api-omen.wisburg.com/api/reports/123",  # detail URL, not the list endpoint
        "https://example.com/rss.xml",
        "reports",  # bare slug
    ],
)
def test_wisburg_url_outside_whitelist_rejected(bad: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        FeedCreate(name="wisburg", url=bad, source_type="wisburg-report")
    # Error message must teach the caller the valid values (design D2)
    assert "am-reports" in str(exc_info.value)


def test_wisburg_poll_interval_not_coerced() -> None:
    """Unlike newsapi, wisburg feeds keep their per-feed interval (design D14)."""
    feed = FeedCreate(
        name="wisburg",
        url=REPORTS_URL,
        source_type="wisburg-report",
        poll_interval_minutes=60,
    )
    assert feed.poll_interval_minutes == 60


def test_rss_branch_unaffected() -> None:
    """The new elif must not leak into the rss else-branch behaviour."""
    with pytest.raises(ValidationError):
        FeedCreate(name="x", url="not-a-url", source_type="rss")
    feed = FeedCreate(name="x", url="https://example.com/rss.xml", source_type="rss")
    assert feed.url == "https://example.com/rss.xml"
