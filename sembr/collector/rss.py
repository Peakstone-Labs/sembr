"""RSS feed collector using httpx + feedparser."""
from __future__ import annotations

import hashlib
import logging
from calendar import timegm
from datetime import datetime, timezone

import feedparser
import httpx

from sembr.collector.base import BaseSource, RawArticle

logger = logging.getLogger(__name__)

_USER_AGENT = "sembr/0.1 feedparser"


def _compute_md5(url: str, title: str) -> str:
    return hashlib.md5((url + title).encode()).hexdigest()


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
        body = content_list[0].get("value", "").strip()
        if body:
            return body, "full"

    summary = getattr(entry, "summary", "").strip()
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
            logger.error("HTTP error fetching %s: %s", self._url, exc)
            return []

        feed = feedparser.parse(resp.content)
        if feed.bozo and not feed.entries:
            logger.error("feedparser bozo on %s: %s", self._url, feed.bozo_exception)
            return []

        articles: list[RawArticle] = []
        for entry in feed.entries:
            url = getattr(entry, "link", "").strip()
            title = getattr(entry, "title", "").strip()
            if not url or not title:
                continue

            published_at = _entry_published(entry)
            # D4: err on inclusion — if no timestamp, include the entry (MD5 dedup is the real guard)
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
                "timeout": {"type": "number", "default": 30.0, "description": "HTTP timeout in seconds"},
            },
        }
