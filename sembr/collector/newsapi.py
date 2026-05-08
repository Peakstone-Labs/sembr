"""NewsAPI.ai (eventregistry.org) source + master-tick aggregator.

D2: this module bundles three responsibilities so RSS-side code stays untouched:

1. ``NewsApiSource`` — single-source ``BaseSource`` implementation used by the
   feed-fire dry_run / real_run path. Instantiated per feed with a hostname
   like ``"reuters.com"``. ``fetch(since)`` calls ``article/getArticles`` with
   ``sourceUri=[self._url]`` (1 token).

2. ``NewsApiMaster`` — the master tick body. ``tick()`` reads all enabled
   ``source_type='newsapi'`` feeds in one DB query, batches them into one
   API call (``sourceUri=list(uri_map)``, 1 token regardless of feed count),
   then dispatches results back to ``insert_article_pending`` per ``feed_id``.
   This is the design's hard constraint (1 token / poll cycle).

3. ``RECOMMENDED_SOURCES`` / ``normalize_source_uri`` — datalist data + the
   single normalization function shared between ``FeedCreate.url`` validator
   (write path) and master tick dispatch (read path). Same function on both
   sides keeps the route ``article.source.uri → feed_id`` predictable.

Failures (HTTP error, JSON parse, missing ``req-tokens``, empty uri_map) skip
both the cursor advance and the fetch_event row, matching ``collect_feed``'s
``FetchError`` semantics so a failed tick doesn't lose articles in the next
window. See D6 / D20.
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

# D18: fixed request-body fields for /article/getArticles. Hardcoded so callers
# can't accidentally diverge from the API contract documented in design §A2.
# articlesCount=100 is the per-call ceiling; we don't paginate (Non-Goal).
_NEWSAPI_REQUEST_FIXED: dict[str, Any] = {
    "articlesCount": 100,
    "articlesPage": 1,
    "articlesSortBy": "date",
    "resultType": "articles",
    "dataType": "news",
    "articleBodyLen": -1,           # -1 = full body
    "isDuplicateFilter": "skipDuplicates",
    "lang": "eng",
    "timezone": "UTC",
}

# §A5 — datalist for the create-feed combobox. Sources known to be either
# unavailable on the free tier or otherwise problematic (CNN / ABC News /
# Forbes / Nature) are intentionally absent.
RECOMMENDED_SOURCES: list[dict[str, Any]] = [
    # High-volume general news (>100 articles/day)
    {"uri": "reuters.com",        "title": "Reuters",          "paywalled": False},
    {"uri": "bbc.com",            "title": "BBC",              "paywalled": False},
    {"uri": "independent.co.uk",  "title": "The Independent",  "paywalled": False},
    {"uri": "cbsnews.com",        "title": "CBS News",         "paywalled": False},
    {"uri": "bloomberg.com",      "title": "Bloomberg",        "paywalled": True},
    {"uri": "wsj.com",            "title": "WSJ",              "paywalled": True},
    # Mid-volume (50–100/day)
    {"uri": "theguardian.com",    "title": "The Guardian",     "paywalled": False},
    {"uri": "nytimes.com",        "title": "NYT",              "paywalled": False},
    {"uri": "foxnews.com",        "title": "Fox News",         "paywalled": False},
    {"uri": "ft.com",             "title": "Financial Times",  "paywalled": False},
    {"uri": "cnbc.com",           "title": "CNBC",             "paywalled": False},
    {"uri": "apnews.com",         "title": "AP News",          "paywalled": False},
    # Lower-volume (<50/day)
    {"uri": "businessinsider.com", "title": "Business Insider", "paywalled": False},
    {"uri": "latimes.com",        "title": "LA Times",         "paywalled": False},
    {"uri": "axios.com",          "title": "Axios",            "paywalled": False},
    {"uri": "chicagotribune.com", "title": "Chicago Tribune",  "paywalled": False},
    {"uri": "politico.com",       "title": "Politico",         "paywalled": False},
    {"uri": "nbcnews.com",        "title": "NBC News",         "paywalled": False},
    {"uri": "usatoday.com",       "title": "USA Today",        "paywalled": False},
    {"uri": "seattletimes.com",   "title": "Seattle Times",    "paywalled": False},
    {"uri": "techcrunch.com",     "title": "TechCrunch",       "paywalled": False},
    {"uri": "npr.org",            "title": "NPR",              "paywalled": False},
    {"uri": "theverge.com",       "title": "The Verge",        "paywalled": False},
    {"uri": "wired.com",          "title": "Wired",            "paywalled": False},
    {"uri": "washingtonpost.com", "title": "Washington Post",  "paywalled": False},
    {"uri": "arstechnica.com",    "title": "Ars Technica",     "paywalled": False},
    {"uri": "economist.com",      "title": "The Economist",    "paywalled": False},
    {"uri": "theatlantic.com",    "title": "The Atlantic",     "paywalled": False},
    {"uri": "newyorker.com",      "title": "New Yorker",       "paywalled": False},
    {"uri": "vox.com",            "title": "Vox",              "paywalled": False},
]


def normalize_source_uri(s: str) -> str:
    """O2-A: shared normalizer for write path (``FeedCreate.url``) and read
    path (master tick ``article.source.uri`` lookup). Strict and minimal —
    lower + scheme prefix + ``www.`` prefix + trailing slash. Anything fancier
    risks subdomain false-positives (see O2-B's rejection)."""
    out = s.strip().lower()
    for prefix in ("https://", "http://"):
        if out.startswith(prefix):
            out = out[len(prefix):]
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
    since: datetime | None     # last_collected_at parsed (None on first pull)
    items_seen: int = 0
    items_new: int = 0


# ---------------------------------------------------------------------------
# NewsApiSource — single-source BaseSource for feed fire (dry_run / real_run)
# ---------------------------------------------------------------------------


class NewsApiSource(BaseSource):
    """D5/D23: same ``__init__(url, timeout)`` signature as RSSSource so
    ``collect_feed`` can construct it without a special branch. Per-feed
    fetch consumes 1 token; the 60s fire rate limit (see fire_tasks.py)
    keeps token spend bounded."""

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

        date_start, date_end = _date_window([since])
        body = _build_request_body(
            api_key=api_key,
            source_uris=[self._url],
            settings=settings,
            date_start=date_start,
            date_end=date_end,
        )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.post(_NEWSAPI_BASE_URL + _GET_ARTICLES_PATH, json=body)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                # Mirror RSSSource.fetch's failure contract so collect_feed's
                # FetchError branch fires (no cursor advance, fetch_event ok=False)
                # for both source types — D5 / D20 / review-loop1 🔴-1.
                raise FetchError(
                    f"newsapi HTTP error for {self._url!r}: "
                    f"{type(exc).__name__}: {exc!s}"
                ) from exc
            # 🟢-3: log token usage as soon as the response is in hand so even a
            # JSON parse failure leaves a breadcrumb for the spent token.
            _log_token_usage(resp.headers, where=f"fetch[{self._url}]")
            try:
                data = resp.json()
            except ValueError as exc:
                raise FetchError(
                    f"newsapi JSON parse failed for {self._url!r}: {exc}"
                ) from exc
        results = _extract_results(data)
        articles: list[RawArticle] = []
        for raw in results:
            article = _to_raw_article(raw)
            if article is None:
                continue
            if since is not None and article.published_at is not None and article.published_at <= since:
                continue
            articles.append(article)
        return articles

    async def health(self) -> bool:
        """D4: requirements Non-Goals forbid remote probes; just validate the
        api_key is present so /health returns 503 when newsapi is unconfigured."""
        return bool(get_settings().newsapi_api_key.get_secret_value())

    @classmethod
    def config_schema(cls) -> dict:
        # D3: no per-feed config — all newsapi knobs live in Settings. Empty
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
        # D6: read enabled+source_type='newsapi' feeds at tick time so newly
        # added feeds participate immediately (Goal #3, no caching).
        from sembr.dashboard.events import log_fetch_event  # noqa: PLC0415
        from sembr.db.articles import insert_article_pending  # noqa: PLC0415
        from sembr.db.feeds import update_last_collected  # noqa: PLC0415
        from sembr.db.sqlite import get_conn  # noqa: PLC0415

        conn = get_conn()
        async with conn.execute(
            "SELECT id, url, last_collected_at FROM feeds "
            "WHERE source_type='newsapi' AND enabled=1"
        ) as cur:
            rows = await cur.fetchall()

        if not rows:
            logger.debug("newsapi master tick: no enabled newsapi feeds; skipping")
            return

        settings = get_settings()
        api_key = settings.newsapi_api_key.get_secret_value()
        if not api_key:
            # D20: missing key is a configuration failure, not a fetch failure;
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
        body = _build_request_body(
            api_key=api_key,
            source_uris=list(per_feed.keys()),
            settings=settings,
            date_start=date_start,
            date_end=date_end,
        )

        # D21: route the call through host_limiter so newsapi never burns
        # parallel requests when scheduler ticks coincide with feed fires.
        # group_key matches host_limiter.derive_group_key(url) for the
        # eventregistry.org host, which is bare hostname without proxy.
        limiter_ctx = (
            self._host_limiter.acquire(_NEWSAPI_HOST_KEY)
            if self._host_limiter is not None
            else nullcontext()
        )
        started_at = datetime.now(timezone.utc)
        async with limiter_ctx:
            try:
                async with httpx.AsyncClient(timeout=settings.newsapi_timeout_seconds) as client:
                    resp = await client.post(
                        _NEWSAPI_BASE_URL + _GET_ARTICLES_PATH, json=body
                    )
                    resp.raise_for_status()
            except httpx.HTTPError as exc:
                # D20: log + early-return without cursor advance; next tick uses
                # the same since window so articles published during the outage
                # aren't lost. Includes 401/403/quota-exhausted (§A7).
                logger.warning(
                    "newsapi master tick: HTTP failed (%d feeds, since=%s): %s",
                    len(per_feed), date_start, exc,
                )
                return
            # 🟢-3: log token usage as soon as response is in hand so a JSON
            # parse failure still leaves the spent-token breadcrumb.
            _log_token_usage(resp.headers, where="master")
            try:
                data = resp.json()
            except ValueError as exc:
                logger.warning("newsapi master tick: JSON parse failed: %s", exc)
                return

        articles_block = data.get("articles") if isinstance(data, dict) else None
        if not isinstance(articles_block, dict):
            logger.warning(
                "newsapi master tick: response missing 'articles' block; payload keys=%s",
                list(data.keys()) if isinstance(data, dict) else type(data).__name__,
            )
            return

        results = articles_block.get("results") or []
        total_results = articles_block.get("totalResults")
        if isinstance(total_results, int) and total_results > _NEWSAPI_REQUEST_FIXED["articlesCount"]:
            # D24: pagination intentionally not implemented; surface the drop
            # so we can spot the day token economy assumptions break down.
            logger.warning(
                "newsapi totalResults=%d exceeds articlesCount=%d; %d articles dropped this tick",
                total_results,
                _NEWSAPI_REQUEST_FIXED["articlesCount"],
                total_results - _NEWSAPI_REQUEST_FIXED["articlesCount"],
            )

        for raw in results:
            src = raw.get("source") if isinstance(raw, dict) else None
            src_uri = normalize_source_uri(src.get("uri", "")) if isinstance(src, dict) else ""
            slot = per_feed.get(src_uri)
            if slot is None:
                # newsapi.ai filters by sourceUri server-side, so an unmapped
                # source.uri is unusual but not fatal — drop with a breadcrumb.
                logger.warning(
                    "newsapi master tick: source.uri %r not in uri_map; dropped article %r",
                    src_uri, raw.get("url"),
                )
                continue

            article = _to_raw_article(raw)
            if article is None:
                continue
            slot.items_seen += 1

            # D22: client-side since cut. newsapi date granularity is per-day
            # (see §A2), so same-day re-ticks return overlapping articles.
            # MD5 dedup catches the duplicate writes, but early-dropping here
            # also saves a pending_articles INSERT round-trip.
            if slot.since is not None and article.published_at is not None and article.published_at <= slot.since:
                continue
            try:
                is_new = await insert_article_pending(conn, article, slot.feed_id)
                if is_new:
                    slot.items_new += 1
            except Exception as exc:
                # One bad article must not poison the whole tick — log and
                # keep going (matches collect_feed's per-article try/except).
                logger.error(
                    "newsapi master tick: insert_article_pending failed feed_id=%d url=%r: %s",
                    slot.feed_id, article.url, exc, exc_info=True,
                )

        # D7: every enabled feed (including 0-hit ones) gets cursor advance
        # + fetch_event so dashboard sparkline + last_collected_at progress.
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
                    slot.feed_id, exc,
                )


# ---------------------------------------------------------------------------
# Helpers — request-body construction, response parsing, etc.
# ---------------------------------------------------------------------------


def _date_window(sinces: list[datetime | None]) -> tuple[str, str]:
    """D8: dateStart = min(non-null since).date() OR (now-1d).date() on first
    pull. dateEnd = now.date(). Both ISO 'YYYY-MM-DD' (newsapi only accepts
    day granularity)."""
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
) -> dict[str, Any]:
    body: dict[str, Any] = dict(_NEWSAPI_REQUEST_FIXED)
    body["apiKey"] = api_key
    body["sourceUri"] = source_uris
    body["categoryUri"] = settings.newsapi_category_uris
    body["dateStart"] = date_start
    body["dateEnd"] = date_end
    return body


def _log_token_usage(headers: httpx.Headers, *, where: str) -> None:
    """R2: req-tokens header is the unit cost confirmation. httpx headers are
    case-insensitive. Missing header → warning but tick still proceeds."""
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
    """D19/§A3: map a newsapi article dict to RawArticle. Returns None if the
    minimum fields (url + title) are missing."""
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
