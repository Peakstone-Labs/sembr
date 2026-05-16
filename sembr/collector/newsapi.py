# SPDX-License-Identifier: Apache-2.0
"""NewsAPI.ai (eventregistry.org) source + master-tick aggregator.

This module bundles three responsibilities so RSS-side code stays untouched:

1. ``NewsApiSource`` — single-source ``BaseSource`` implementation used by the
   feed-fire dry_run / real_run path. Instantiated per feed with a hostname
   like ``"reuters.com"``. ``fetch(since)`` calls ``article/getArticles`` with
   ``sourceUri=[self._url]`` (1 token).

2. ``NewsApiMaster`` — the master tick body. ``tick()`` reads all enabled
   ``source_type='newsapi'`` feeds in one DB query, batches them into one
   API call (``sourceUri=list(uri_map)``, 1 token regardless of feed count),
   then dispatches results back to ``insert_article_pending`` per ``feed_id``.
   The hard constraint is 1 token per poll cycle.

3. ``RECOMMENDED_SOURCES`` / ``normalize_source_uri`` — datalist data + the
   single normalization function shared between ``FeedCreate.url`` validator
   (write path) and master tick dispatch (read path). Same function on both
   sides keeps the route ``article.source.uri → feed_id`` predictable.

Failures (HTTP error, JSON parse, missing ``req-tokens``, empty uri_map) skip
both the cursor advance and the fetch_event row, matching ``collect_feed``'s
``FetchError`` semantics so a failed tick doesn't lose articles in the next
window.
"""

from __future__ import annotations

import hashlib
import logging
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import httpx

from sembr.collector.base import BaseSource, RawArticle
from sembr.collector.host_limiter import HostLimiter
from sembr.collector.rss import FetchError
from sembr.config import Settings, get_settings

logger = logging.getLogger(__name__)


_NEWSAPI_BASE_URL = "https://eventregistry.org/api/v1"
_GET_ARTICLES_PATH = "/article/getArticles"
_NEWSAPI_HOST_KEY = "eventregistry.org"

# Fire-path lookback when since is None. NewsApiSource only serves fire (master
# tick has its own _date_window with a 1-day first-pull fallback that has to
# stay tight so 30 feeds × ~50 articles/day fit inside max_pages × 100). Fire
# touches a single source at page=1 with no such constraint, so widen the
# window enough that low-volume sources (reuters ~13/day on EventRegistry)
# still fill the 100-article page on the 1 token already paid.
_FIRE_LOOKBACK_DAYS = 30

# Fixed request-body fields for /article/getArticles. articlesPage was in this
# dict originally (always 1, no pagination) and is now parameterized via
# _build_request_body(page=...) since the master tick walks 1..max_pages. The
# single-feed fire path passes page=1 explicitly to keep the wire format
# identical to the legacy single-page behaviour.
_NEWSAPI_REQUEST_FIXED: dict[str, Any] = {
    "articlesCount": 100,
    "articlesSortBy": "date",
    "resultType": "articles",
    "dataType": "news",
    "articleBodyLen": -1,  # -1 = full body
    # `keepAll`: do NOT collapse cross-source near-duplicates server-side.
    # Master tick bundles ~20 sourceUris into one call; `skipDuplicates` was
    # silently dropping Reuters/BBC/NYT coverage of the same event in favor
    # of newsapi's chosen canonical version, leaving items_seen=0–3 per tick
    # for big-name feeds. Client-side MD5(url+title) dedup catches true URL
    # duplicates at insert time, so the API filter is redundant.
    "isDuplicateFilter": "keepAll",
    "lang": "eng",
    "timezone": "UTC",
}

# §A5 — datalist for the create-feed combobox. Sources known to be either
# unavailable on the free tier or otherwise problematic (CNN / ABC News /
# Forbes / Nature) are intentionally absent.
RECOMMENDED_SOURCES: list[dict[str, Any]] = [
    # High-volume general news (>100 articles/day)
    {"uri": "reuters.com", "title": "Reuters", "paywalled": False},
    {"uri": "bbc.com", "title": "BBC", "paywalled": False},
    {"uri": "independent.co.uk", "title": "The Independent", "paywalled": False},
    {"uri": "cbsnews.com", "title": "CBS News", "paywalled": False},
    {"uri": "bloomberg.com", "title": "Bloomberg", "paywalled": True},
    {"uri": "wsj.com", "title": "WSJ", "paywalled": True},
    # Mid-volume (50–100/day)
    {"uri": "theguardian.com", "title": "The Guardian", "paywalled": False},
    {"uri": "nytimes.com", "title": "NYT", "paywalled": False},
    {"uri": "foxnews.com", "title": "Fox News", "paywalled": False},
    {"uri": "ft.com", "title": "Financial Times", "paywalled": False},
    {"uri": "cnbc.com", "title": "CNBC", "paywalled": False},
    {"uri": "apnews.com", "title": "AP News", "paywalled": False},
    # Lower-volume (<50/day)
    {"uri": "businessinsider.com", "title": "Business Insider", "paywalled": False},
    {"uri": "latimes.com", "title": "LA Times", "paywalled": False},
    {"uri": "axios.com", "title": "Axios", "paywalled": False},
    {"uri": "chicagotribune.com", "title": "Chicago Tribune", "paywalled": False},
    {"uri": "politico.com", "title": "Politico", "paywalled": False},
    {"uri": "nbcnews.com", "title": "NBC News", "paywalled": False},
    {"uri": "usatoday.com", "title": "USA Today", "paywalled": False},
    {"uri": "seattletimes.com", "title": "Seattle Times", "paywalled": False},
    {"uri": "techcrunch.com", "title": "TechCrunch", "paywalled": False},
    {"uri": "npr.org", "title": "NPR", "paywalled": False},
    {"uri": "theverge.com", "title": "The Verge", "paywalled": False},
    {"uri": "wired.com", "title": "Wired", "paywalled": False},
    {"uri": "washingtonpost.com", "title": "Washington Post", "paywalled": False},
    {"uri": "arstechnica.com", "title": "Ars Technica", "paywalled": False},
    {"uri": "economist.com", "title": "The Economist", "paywalled": False},
    {"uri": "theatlantic.com", "title": "The Atlantic", "paywalled": False},
    {"uri": "newyorker.com", "title": "New Yorker", "paywalled": False},
    {"uri": "vox.com", "title": "Vox", "paywalled": False},
]


def normalize_source_uri(s: str) -> str:
    """O2-A: shared normalizer for write path (``FeedCreate.url``) and read
    path (master tick ``article.source.uri`` lookup). Strict and minimal —
    lower + scheme prefix + ``www.`` prefix + trailing slash. Anything fancier
    risks subdomain false-positives (see O2-B's rejection)."""
    out = s.strip().lower()
    for prefix in ("https://", "http://"):
        if out.startswith(prefix):
            out = out[len(prefix) :]
    if out.startswith("www."):
        out = out[4:]
    return out.rstrip("/")


def _classify_quality(body_len: int) -> Literal["full", "summary", "stub", "title_only"]:
    """§A4 length thresholds. Bloomberg / WSJ paywalled bodies (~300 chars)
    naturally land in 'stub'; embedder/summarizer already tolerate stub."""
    if body_len > 2000:
        return "full"
    if body_len > 500:
        return "summary"
    if body_len > 100:
        return "stub"
    return "title_only"


def _md5_url_title(url: str, title: str) -> str:
    # Same algorithm as collector.rss._compute_md5 so the same article
    # arriving via two source_types collides on the same fingerprint row.
    return hashlib.md5((url + title).encode(), usedforsecurity=False).hexdigest()


def _parse_date_time(raw: str) -> datetime:
    """Parse newsapi ``dateTime`` (e.g. '2026-05-08T14:32:11Z') to UTC tz-aware."""
    s = raw.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class _PerFeedSince:
    """Cursor + bookkeeping for a single feed within one master tick."""

    feed_id: int
    since: datetime | None  # last_collected_at parsed (None on first pull)
    items_seen: int = 0
    items_new: int = 0


# ---------------------------------------------------------------------------
# Pagination helpers
# ---------------------------------------------------------------------------


def _universal_since_for_pagination(
    per_feed: dict[str, _PerFeedSince],
) -> datetime | None:
    """Earliest non-null since across all feeds; None when ANY feed is on first
    pull. None forces the master tick to skip the watermark stop and rely on
    the defensive max-pages cap — semantics match the existing _date_window
    fallback to now-1d for first-pull bootstrap."""
    sinces = [p.since for p in per_feed.values()]
    if not sinces or any(s is None for s in sinces):
        return None
    return min(s for s in sinces if s is not None)


def _should_stop_paginating(
    page_results: list[dict[str, Any]],
    universal_since: datetime | None,
    indexing_lag: timedelta = timedelta(0),
) -> bool:
    """Pure stop predicate for the master-tick pagination loop.

    Returns True when the **oldest** article in this page is at or before
    ``universal_since - indexing_lag`` — i.e. the next page would be entirely
    below the "could-still-be-newly-indexed" line. NewsAPI indexes articles
    asynchronously after publication (Reuters / USA Today: ~1-2h delay), so
    an article with ``dateTime < cursor`` may still be brand-new to us if it
    was indexed *after* the previous tick. Walking back ``indexing_lag``
    beyond the cursor lets us catch those; MD5 dedup absorbs the duplicates
    in pages we revisit. Robust to ``articlesSortBy`` upstream silently
    flipping desc→asc because we use ``min(dateTime)`` rather than "the last
    element".

    Returns False when:
    - ``universal_since`` is None (first-pull bootstrap; let cap handle it)
    - ``page_results`` is empty (caller already breaks separately on empty)
    - no result has a parseable ``dateTime`` (we have no signal to stop on)
    """
    if universal_since is None:
        return False
    oldest: datetime | None = None
    for art in page_results:
        if not isinstance(art, dict):
            continue
        raw = art.get("dateTime")
        if not isinstance(raw, str) or not raw:
            continue
        try:
            dt = _parse_date_time(raw)
        except ValueError:
            continue
        if oldest is None or dt < oldest:
            oldest = dt
    if oldest is None:
        return False
    return oldest <= universal_since - indexing_lag


# ---------------------------------------------------------------------------
# NewsApiSource — single-source BaseSource for feed fire (dry_run / real_run)
# ---------------------------------------------------------------------------


class NewsApiSource(BaseSource):
    """Same ``__init__(url, timeout)`` signature as RSSSource so
    ``collect_feed`` can construct it without a special branch. Per-feed fetch
    consumes 1 token; the 60s fire rate limit (see fire_tasks.py) keeps token
    spend bounded."""

    def __init__(self, url: str, timeout: float = 30.0) -> None:
        self._url = normalize_source_uri(url)
        self._timeout = timeout

    async def fetch(self, since: datetime | None = None) -> list[RawArticle]:
        settings = get_settings()
        api_key = settings.newsapi_api_key.get_secret_value()
        if not api_key:
            # Configuration error, not a fetch failure — raise so the
            # collect_feed contract (FetchError → don't advance cursor) holds
            # for newsapi feeds the same way it does for misconfigured RSS.
            raise FetchError("NEWSAPI_API_KEY is empty; cannot fetch newsapi feed")

        if since is None:
            now = datetime.now(timezone.utc)
            date_start = (now - timedelta(days=_FIRE_LOOKBACK_DAYS)).date().isoformat()
            date_end = now.date().isoformat()
        else:
            date_start, date_end = _date_window([since])
        body = _build_request_body(
            api_key=api_key,
            source_uris=[self._url],
            settings=settings,
            date_start=date_start,
            date_end=date_end,
            page=1,  # fire path stays on page 1 (no pagination here)
        )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.post(_NEWSAPI_BASE_URL + _GET_ARTICLES_PATH, json=body)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                # Mirror RSSSource.fetch's failure contract so collect_feed's
                # FetchError branch fires (no cursor advance, fetch_event
                # ok=False) for both source types.
                raise FetchError(
                    f"newsapi HTTP error for {self._url!r}: {type(exc).__name__}: {exc!s}"
                ) from exc
            # Log token usage as soon as the response is in hand so even a
            # JSON parse failure leaves a breadcrumb for the spent token.
            _log_token_usage(resp.headers, where=f"fetch[{self._url}]")
            try:
                data = resp.json()
            except ValueError as exc:
                raise FetchError(f"newsapi JSON parse failed for {self._url!r}: {exc}") from exc
        results = _extract_results(data)
        articles: list[RawArticle] = []
        for raw in results:
            article = _to_raw_article(raw)
            if article is None:
                continue
            if (
                since is not None
                and article.published_at is not None
                and article.published_at <= since
            ):
                continue
            articles.append(article)
        return articles

    async def health(self) -> bool:
        """Validate api_key is present without a remote probe so /health
        returns 503 when newsapi is unconfigured but doesn't burn a token on
        every probe."""
        return bool(get_settings().newsapi_api_key.get_secret_value())

    @classmethod
    def config_schema(cls) -> dict:
        # No per-feed config — all newsapi knobs live in Settings. Empty
        # properties block makes the dashboard create form skip rendering the
        # source-config section entirely.
        return {"type": "object", "properties": {}}


# ---------------------------------------------------------------------------
# NewsApiMaster — single-shot tick that fans out to every enabled newsapi feed
# ---------------------------------------------------------------------------


class NewsApiMaster:
    """Master-tick body. Stateless aside from constructor params so APScheduler
    can re-use one instance across ticks safely."""

    def __init__(self, *, host_limiter: HostLimiter | None = None) -> None:
        self._host_limiter = host_limiter

    async def tick(self) -> None:
        # Read enabled+source_type='newsapi' feeds at tick time so newly added
        # feeds participate immediately (no caching). The feed list is read
        # ONCE here even though the HTTP loop can run up to max_pages
        # iterations — concurrent feed inserts/deletes during a tick must not
        # change the per_feed map mid-pagination.
        from sembr.dashboard.events import log_fetch_event  # noqa: PLC0415
        from sembr.db.articles import insert_article_pending  # noqa: PLC0415
        from sembr.db.feeds import update_last_collected  # noqa: PLC0415
        from sembr.db.sqlite import get_conn  # noqa: PLC0415

        conn = get_conn()
        async with conn.execute(
            "SELECT id, url, last_collected_at FROM feeds WHERE source_type='newsapi' AND enabled=1"
        ) as cur:
            rows = await cur.fetchall()

        if not rows:
            logger.debug("newsapi master tick: no enabled newsapi feeds; skipping")
            return

        settings = get_settings()
        api_key = settings.newsapi_api_key.get_secret_value()
        if not api_key:
            # Missing key is a configuration failure, not a fetch failure;
            # don't advance cursor and don't emit a fetch_event (matches the
            # collect_feed FetchError semantic — next tick retries).
            logger.warning(
                "newsapi master tick: NEWSAPI_API_KEY empty; skipping (%d feeds)",
                len(rows),
            )
            return

        per_feed: dict[str, _PerFeedSince] = {}
        for fid, url, last_iso in rows:
            normalized = normalize_source_uri(url)
            since = _parse_iso_or_none(last_iso)
            per_feed[normalized] = _PerFeedSince(feed_id=int(fid), since=since)

        date_start, date_end = _date_window([p.since for p in per_feed.values()])
        max_pages = settings.newsapi_max_pages
        universal_since = _universal_since_for_pagination(per_feed)
        indexing_lag = timedelta(hours=settings.newsapi_indexing_lag_hours)

        # Route the call through host_limiter so newsapi never burns parallel
        # requests when scheduler ticks coincide with feed fires. group_key
        # matches host_limiter.derive_group_key(url) for the eventregistry.org
        # host, which is bare hostname without proxy.
        limiter_ctx = (
            self._host_limiter.acquire(_NEWSAPI_HOST_KEY)
            if self._host_limiter is not None
            else nullcontext()
        )
        started_at = datetime.now(timezone.utc)

        # Per-feed feed_fetch_log row on EVERY failure path (cap, HTTP, JSON,
        # malformed body) so the dashboard sparkline can tell "feed stuck on
        # cap" apart from "feed idle". Cursor still does NOT advance —
        # atomicity unchanged; this only marks the failed *attempt* in the log
        # table. Mirrors collect_feed's RSS failure-row pattern.
        async def _emit_failure_logs(error_class: str, error_message: str) -> None:
            elapsed_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
            for slot in per_feed.values():
                try:
                    await log_fetch_event(
                        feed_id=slot.feed_id,
                        started_at=started_at,
                        elapsed_ms=elapsed_ms,
                        ok=False,
                        items_seen=0,
                        items_new=0,
                        error_class=error_class,
                        error_message=error_message,
                    )
                except Exception as exc:
                    logger.warning(
                        "log_fetch_event(ok=False, %s) failed for newsapi feed_id=%d: %s",
                        error_class,
                        slot.feed_id,
                        exc,
                    )

        # Atomic semantics — accumulate all pages' results in memory; any page
        # failure → return early without dispatch (no pending_articles insert,
        # no update_last_collected). Failure paths DO emit ok=False
        # feed_fetch_log rows. Memory budget: max_pages=10 × 100 articles ×
        # ~5KB ≈ 5MB; cap=20 → ~10MB worst case.
        all_results: list[dict[str, Any]] = []
        async with limiter_ctx:
            async with httpx.AsyncClient(timeout=settings.newsapi_timeout_seconds) as client:
                stopped_naturally = False
                for page in range(1, max_pages + 1):
                    body = _build_request_body(
                        api_key=api_key,
                        source_uris=list(per_feed.keys()),
                        settings=settings,
                        date_start=date_start,
                        date_end=date_end,
                        page=page,
                    )
                    try:
                        resp = await client.post(_NEWSAPI_BASE_URL + _GET_ARTICLES_PATH, json=body)
                        resp.raise_for_status()
                    except httpx.HTTPError as exc:
                        # Any page HTTP failure → integral rollback. No
                        # dispatch yet, no cursor advance.
                        logger.warning(
                            "newsapi master tick: page=%d HTTP failed (%d feeds, since=%s): %s",
                            page,
                            len(per_feed),
                            date_start,
                            exc,
                        )
                        await _emit_failure_logs(
                            "http_error",
                            f"newsapi master tick page={page} HTTP failed: "
                            f"{type(exc).__name__}: {exc!s}",
                        )
                        return
                    # Log req-tokens as soon as the response is in hand —
                    # keeps the breadcrumb even if JSON parse fails.
                    _log_token_usage(resp.headers, where=f"master[p{page}]")
                    try:
                        data = resp.json()
                    except ValueError as exc:
                        logger.warning(
                            "newsapi master tick: page=%d JSON parse failed: %s",
                            page,
                            exc,
                        )
                        await _emit_failure_logs(
                            "json_error",
                            f"newsapi master tick page={page} JSON parse failed: {exc}",
                        )
                        return

                    articles_block = data.get("articles") if isinstance(data, dict) else None
                    if not isinstance(articles_block, dict):
                        logger.warning(
                            "newsapi master tick: page=%d response missing 'articles' "
                            "block; payload keys=%s",
                            page,
                            list(data.keys()) if isinstance(data, dict) else type(data).__name__,
                        )
                        await _emit_failure_logs(
                            "bad_response",
                            f"newsapi master tick page={page}: response missing 'articles' block",
                        )
                        return

                    page_results = articles_block.get("results") or []
                    if not isinstance(page_results, list):
                        page_results = []
                    all_results.extend(page_results)

                    if not page_results:
                        # Server reports no more articles in window — natural end
                        # before max_pages; safe to dispatch what we have.
                        stopped_naturally = True
                        break
                    if _should_stop_paginating(page_results, universal_since, indexing_lag):
                        # Watermark stop: this page's oldest article is at or
                        # below (universal_since - indexing_lag). Below that
                        # line, NewsAPI is unlikely to have anything new since
                        # the previous tick — subsequent pages would be
                        # dispatched but MD5 dedup'd to ~0 net inserts.
                        stopped_naturally = True
                        break

                if not stopped_naturally:
                    # Defensive cap reached without watermark trigger. Treat
                    # as soft failure (same atomicity rule as HTTP errors):
                    # don't dispatch, don't advance cursor. Next tick retries
                    # with the same dateStart. Operator response: lower the
                    # poll cadence or raise newsapi_max_pages in Settings.
                    logger.warning(
                        "newsapi master tick: max_pages=%d cap reached "
                        "(since=%s, %d feeds); dropping tick to retry next cycle",
                        max_pages,
                        universal_since,
                        len(per_feed),
                    )
                    await _emit_failure_logs(
                        "cap_reached",
                        f"newsapi master tick: max_pages={max_pages} cap reached "
                        f"without watermark stop (since={universal_since}); "
                        f"dropping tick to retry next cycle",
                    )
                    return

        # Unified dispatch — only reached after all fetched pages returned 2xx
        # + valid JSON. From this point on, behavior matches the legacy
        # single-page path.
        for raw in all_results:
            src = raw.get("source") if isinstance(raw, dict) else None
            src_uri = normalize_source_uri(src.get("uri", "")) if isinstance(src, dict) else ""
            slot = per_feed.get(src_uri)
            if slot is None:
                # newsapi.ai filters by sourceUri server-side, so an unmapped
                # source.uri is unusual but not fatal — drop with a breadcrumb.
                logger.warning(
                    "newsapi master tick: source.uri %r not in uri_map; dropped article %r",
                    src_uri,
                    raw.get("url"),
                )
                continue

            article = _to_raw_article(raw)
            if article is None:
                continue
            slot.items_seen += 1

            # No client-side since-cut: NewsAPI has ~1h indexing delay for some
            # sources (Reuters worst case), so an article first surfaced in the
            # API response often has published_at < slot.since (which advances
            # to now() every tick regardless of items_new). Cutting here drops
            # the article on first sight; on subsequent ticks slot.since has
            # moved further past it → permanent loss. MD5 dedup in
            # insert_article_pending is the correctness layer; the round-trip
            # cost is dwarfed by the article-loss it prevents.
            try:
                is_new = await insert_article_pending(conn, article, slot.feed_id)
                if is_new:
                    slot.items_new += 1
            except Exception as exc:
                # One bad article must not poison the whole tick — log and
                # keep going (matches collect_feed's per-article try/except).
                logger.error(
                    "newsapi master tick: insert_article_pending failed feed_id=%d url=%r: %s",
                    slot.feed_id,
                    article.url,
                    exc,
                    exc_info=True,
                )

        # Every enabled feed (including 0-hit ones) gets a cursor advance +
        # fetch_event so the dashboard sparkline and last_collected_at progress
        # together. items_seen / items_new reflect totals across all fetched
        # pages (accumulated in slot during the dispatch loop above).
        elapsed_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
        for slot in per_feed.values():
            await update_last_collected(conn, slot.feed_id)
            try:
                await log_fetch_event(
                    feed_id=slot.feed_id,
                    started_at=started_at,
                    elapsed_ms=elapsed_ms,
                    ok=True,
                    items_seen=slot.items_seen,
                    items_new=slot.items_new,
                    error_class=None,
                    error_message=None,
                )
            except Exception as exc:
                logger.warning(
                    "log_fetch_event failed for newsapi feed_id=%d: %s",
                    slot.feed_id,
                    exc,
                )


# ---------------------------------------------------------------------------
# Helpers — request-body construction, response parsing, etc.
# ---------------------------------------------------------------------------


def _date_window(sinces: list[datetime | None]) -> tuple[str, str]:
    """dateStart = min(non-null since).date() OR (now-1d).date() on first pull.
    dateEnd = now.date(). Both ISO 'YYYY-MM-DD' (newsapi only accepts day
    granularity)."""
    now = datetime.now(timezone.utc)
    non_null = [s for s in sinces if s is not None]
    if non_null and len(non_null) == len(sinces):
        start = min(non_null).astimezone(timezone.utc).date()
    else:
        start = (now - timedelta(days=1)).date()
    return start.isoformat(), now.date().isoformat()


def _build_request_body(
    *,
    api_key: str,
    source_uris: list[str],
    settings: Settings,
    date_start: str,
    date_end: str,
    page: int = 1,
) -> dict[str, Any]:
    body: dict[str, Any] = dict(_NEWSAPI_REQUEST_FIXED)
    body["apiKey"] = api_key
    body["sourceUri"] = source_uris
    body["categoryUri"] = settings.newsapi_category_uris
    body["dateStart"] = date_start
    body["dateEnd"] = date_end
    body["articlesPage"] = page
    return body


def _log_token_usage(headers: httpx.Headers, *, where: str) -> None:
    """The req-tokens header confirms the unit cost charged for this request.
    httpx headers are case-insensitive. Missing header → warning but tick
    still proceeds."""
    raw = headers.get("req-tokens")
    if raw is None:
        logger.warning("newsapi %s: req-tokens header absent on response", where)
        return
    try:
        tokens = float(raw)
    except ValueError:
        logger.warning("newsapi %s: req-tokens header not a number: %r", where, raw)
        return
    logger.info("newsapi %s: req-tokens=%s", where, tokens)


def _extract_results(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    block = data.get("articles")
    if not isinstance(block, dict):
        return []
    raw = block.get("results")
    return raw if isinstance(raw, list) else []


def _parse_iso_or_none(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _to_raw_article(raw: dict[str, Any]) -> RawArticle | None:
    """Map a newsapi article dict to RawArticle. Returns None if the minimum
    fields (url + title) are missing."""
    if not isinstance(raw, dict):
        return None
    # ⚠ url is article["url"] (real link), NOT article["uri"] (internal id)
    url = (raw.get("url") or "").strip()
    title = (raw.get("title") or "").strip()
    if not url or not title:
        return None
    body = raw.get("body") or ""
    quality = _classify_quality(len(body))
    published_at: datetime | None = None
    raw_dt = raw.get("dateTime")
    if isinstance(raw_dt, str) and raw_dt:
        try:
            published_at = _parse_date_time(raw_dt)
        except ValueError:
            published_at = None
    return RawArticle(
        url=url,
        title=title,
        body=body,
        content_quality=quality,
        published_at=published_at,
        feed_md5=_md5_url_title(url, title),
    )


# ---------------------------------------------------------------------------
# Module-level entry-point invoked by APScheduler (master job)
# ---------------------------------------------------------------------------


async def newsapi_master_tick() -> None:
    """APScheduler job target. Pulls the host limiter from the scheduler-time
    module ref so unit tests that bypass lifespan still work."""
    from sembr.collector.scheduler import get_host_limiter  # noqa: PLC0415

    master = NewsApiMaster(host_limiter=get_host_limiter())
    await master.tick()
