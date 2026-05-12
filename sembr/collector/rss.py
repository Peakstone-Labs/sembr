# SPDX-License-Identifier: Apache-2.0
"""RSS feed collector using httpx + feedparser."""

from __future__ import annotations

import hashlib
import html
import logging
import re
from calendar import timegm
from datetime import datetime, timezone
from html.parser import HTMLParser

import feedparser
import httpx

from sembr.collector.base import BaseSource, RawArticle

logger = logging.getLogger(__name__)

_USER_AGENT = "sembr/0.1 feedparser"


class FetchError(Exception):
    """HTTP request failed or feedparser could not parse the response.

    Raised instead of returning [] so callers can distinguish a genuine fetch
    failure (don't advance since cursor) from a legitimate empty result
    (source has no new articles — cursor should still advance).
    """


class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        return re.sub(r"\s+", " ", "".join(self._parts)).strip()


def _strip_html(text: str) -> str:
    """Strip tags and decode HTML entities.

    `html.parser.HTMLParser.handle_data` only emits character data, so entities
    like `&amp;` / `&#39;` / `&quot;` arrive at the embedder unchanged unless we
    run `html.unescape` afterward. The raw `&amp;` form degrades semantic
    matching on text the article actually intended as `&`.
    """
    s = _HTMLStripper()
    s.feed(text)
    return html.unescape(s.get_text())


def _compute_md5(url: str, title: str) -> str:
    # MD5 is used here purely as a non-cryptographic content fingerprint.
    # `usedforsecurity=False` keeps this importable on FIPS-mode systems where
    # the cryptographic MD5 path is disabled at the OS level.
    return hashlib.md5((url + title).encode(), usedforsecurity=False).hexdigest()


def _entry_published(entry: feedparser.FeedParserDict) -> datetime | None:
    tp = getattr(entry, "published_parsed", None)
    if tp is None:
        tp = getattr(entry, "updated_parsed", None)
    if tp is None:
        return None
    return datetime.fromtimestamp(timegm(tp), tz=timezone.utc)


def _best_body(entry: feedparser.FeedParserDict) -> tuple[str, str]:
    """Return (body_text, content_quality)."""
    content_list = getattr(entry, "content", None)
    if content_list:
        body = _strip_html(content_list[0].get("value", ""))
        if body:
            return body, "full"

    summary = _strip_html(getattr(entry, "summary", ""))
    if len(summary) > 100:
        return summary, "summary"
    if summary:
        return summary, "stub"

    title = getattr(entry, "title", "").strip()
    return title, "title_only"


class RSSSource(BaseSource):
    def __init__(self, url: str, timeout: float = 30.0) -> None:
        self._url = url
        self._timeout = timeout

    async def fetch(self, since: datetime | None = None) -> list[RawArticle]:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
            ) as client:
                resp = await client.get(self._url, headers={"User-Agent": _USER_AGENT})
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(f"HTTP error: {type(exc).__name__}: {exc!s} | {exc!r}") from exc

        feed = feedparser.parse(resp.content)
        # Raise on parse failure: bozo (malformed XML) or unrecognized format (version="")
        # with no entries. If entries exist despite bozo, process them (err on inclusion).
        if not feed.entries and (feed.bozo or not feed.version):
            detail = feed.bozo_exception if feed.bozo else "unrecognized feed format"
            raise FetchError(f"feedparser: {detail}")

        articles: list[RawArticle] = []
        for entry in feed.entries:
            url = getattr(entry, "link", "").strip()
            title = getattr(entry, "title", "").strip()
            if not url or not title:
                continue

            published_at = _entry_published(entry)
            # Err on inclusion — if no timestamp, include the entry (MD5 dedup is the real guard)
            if since and published_at is not None and published_at <= since:
                continue

            body, quality = _best_body(entry)
            articles.append(
                RawArticle(
                    url=url,
                    title=title,
                    body=body,
                    content_quality=quality,  # type: ignore[arg-type]
                    published_at=published_at,
                    feed_md5=_compute_md5(url, title),
                )
            )
        return articles

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                resp = await client.head(self._url, headers={"User-Agent": _USER_AGENT})
            return resp.status_code < 400
        except httpx.HTTPError:
            return False

    @classmethod
    def config_schema(cls) -> dict:
        return {
            "type": "object",
            "properties": {
                "timeout": {
                    "type": "number",
                    "default": 30.0,
                    "description": "HTTP timeout in seconds",
                },
            },
        }
