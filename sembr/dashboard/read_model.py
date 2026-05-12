"""Aggregate read queries for the dashboard.

This module is the only place that joins SQLite + Qdrant + app.state.embedder
into the snapshot/drill-down response shapes. Routes call these helpers; helpers
do not import FastAPI types — they take primitives and return Pydantic models.

The qdrant-client *models* (Filter, FieldCondition, ...) stay lazy-imported
inside the few helpers that need them — Windows dev machines may not have
qdrant_client installed and we want this module to import cleanly there.
The AsyncQdrantClient is always handed in by the caller, never constructed here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from sembr.collector.host_limiter import derive_group_key
from sembr.dashboard.schemas import (
    ArticleBucket,
    ArticleDetail,
    ArticleListItem,
    ArticlesBlock,
    ComponentsBlock,
    EmbedCallEvent,
    EmbedderBlock,
    EmbedderCalls24h,
    Fetch24hBlock,
    FeedFetchEvent,
    FeedListResponse,
    FeedRow,
    FeedRowExtended,
    SnapshotResponse,
)
from sembr.db.feeds import list_feeds
from sembr.db.feed_tags import list_all_tags
from sembr.db.sqlite import sqlite_ok
from sembr.vector_store.news import ALIAS_NAME as _NEWS_ALIAS

logger = logging.getLogger(__name__)

_SPARKLINE_BUCKETS = 24


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hour_buckets_back(now: datetime, hours: int = _SPARKLINE_BUCKETS) -> list[str]:
    """List of N hour-keys 'YYYY-MM-DD HH', oldest → newest, ending at now."""
    base = now.replace(minute=0, second=0, microsecond=0)
    return [(base - timedelta(hours=h)).strftime("%Y-%m-%d %H") for h in range(hours - 1, -1, -1)]


# Max rows per feed scanned for last_outcome / consecutive_failures. Large enough
# to surface plausible failure streaks (a feed polling every 5 min for 24 h fits
# in 288 rows; a 50-row window captures any practically relevant streak).
_RECENT_ROWS_PER_FEED = 50


async def _fetch_24h_all_feeds(
    conn: aiosqlite.Connection, feed_ids: list[int], now: datetime
) -> dict[int, Fetch24hBlock]:
    """Build Fetch24hBlock for every feed_id in two queries (no N+1).

    Q1 — per-(feed, hour) aggregate for sparkline + ok/fail/total counts.
    Q2 — recent rows per feed (window-bounded) for last_outcome, last_error,
    consecutive_failures.

    feed_ids that have zero rows in the window still get a "never"/empty block
    in the result so callers can iterate `feeds_list` directly.
    """
    if not feed_ids:
        return {}
    cutoff = (now - timedelta(hours=_SPARKLINE_BUCKETS)).isoformat()
    bucket_keys = _hour_buckets_back(now)

    # Q1: aggregate buckets — one round-trip across all feeds.
    async with conn.execute(
        "SELECT feed_id, "
        "       strftime('%Y-%m-%d %H', started_at) AS h, "
        "       SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END) AS oks, "
        "       COUNT(*) AS total "
        "FROM feed_fetch_log "
        "WHERE started_at >= ? "
        "GROUP BY feed_id, h",
        (cutoff,),
    ) as cur:
        agg_rows = await cur.fetchall()

    by_feed_buckets: dict[int, dict[str, int]] = {fid: {} for fid in feed_ids}
    by_feed_total: dict[int, int] = dict.fromkeys(feed_ids, 0)
    by_feed_ok: dict[int, int] = dict.fromkeys(feed_ids, 0)
    for fid, h, oks, total in agg_rows:
        if fid in by_feed_buckets:
            by_feed_buckets[fid][h] = int(oks or 0)
            by_feed_total[fid] += int(total or 0)
            by_feed_ok[fid] += int(oks or 0)

    # Q2: recent rows per feed via ROW_NUMBER window function (SQLite ≥ 3.25).
    async with conn.execute(
        "SELECT feed_id, ok, error_message FROM ("
        "  SELECT feed_id, ok, error_message, id, "
        "         ROW_NUMBER() OVER (PARTITION BY feed_id ORDER BY id DESC) AS rn "
        "  FROM feed_fetch_log WHERE started_at >= ?"
        ") WHERE rn <= ? ORDER BY feed_id, rn",
        (cutoff, _RECENT_ROWS_PER_FEED),
    ) as cur:
        recent_rows = await cur.fetchall()

    recent_by_feed: dict[int, list[tuple[int, str | None]]] = {fid: [] for fid in feed_ids}
    for fid, ok, err in recent_rows:
        if fid in recent_by_feed:
            recent_by_feed[fid].append((int(ok), err))

    out: dict[int, Fetch24hBlock] = {}
    for fid in feed_ids:
        rows = recent_by_feed[fid]  # already newest-first (rn ASC = id DESC)
        total = by_feed_total[fid]
        ok_count = by_feed_ok[fid]
        fail_count = total - ok_count

        last_outcome: str = "never"
        last_error_message: str | None = None
        consecutive_failures = 0
        if rows:
            last_ok = rows[0][0]
            last_outcome = "ok" if last_ok == 1 else "fail"
            if last_ok == 0:
                last_error_message = rows[0][1]
            for r in rows:
                if r[0] == 0:
                    consecutive_failures += 1
                else:
                    break

        sparkline = [by_feed_buckets[fid].get(h, 0) for h in bucket_keys]

        out[fid] = Fetch24hBlock(
            total=total,
            ok=ok_count,
            fail=fail_count,
            last_outcome=last_outcome,  # type: ignore[arg-type]
            last_error_message=last_error_message,
            consecutive_failures=consecutive_failures,
            sparkline_buckets=sparkline,
        )
    return out


async def _embedder_calls_24h(conn: aiosqlite.Connection, now: datetime) -> EmbedderCalls24h:
    cutoff = (now - timedelta(hours=_SPARKLINE_BUCKETS)).isoformat()
    async with conn.execute(
        "SELECT COUNT(*), "
        "       SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END), "
        "       AVG(elapsed_ms) "
        "FROM embed_call_log WHERE started_at >= ?",
        (cutoff,),
    ) as cur:
        agg = await cur.fetchone()
    total = int(agg[0] or 0)
    ok_count = int(agg[1] or 0)
    avg_ms = int(agg[2] or 0) if agg[2] is not None else 0
    fail_count = total - ok_count

    p95_ms = 0
    if total > 0:
        p95_offset = max(0, int(round(0.95 * total)) - 1)
        async with conn.execute(
            "SELECT elapsed_ms FROM embed_call_log "
            "WHERE started_at >= ? "
            "ORDER BY elapsed_ms "
            "LIMIT 1 OFFSET ?",
            (cutoff, p95_offset),
        ) as cur:
            p95_row = await cur.fetchone()
        if p95_row:
            p95_ms = int(p95_row[0])

    async with conn.execute(
        "SELECT strftime('%Y-%m-%d %H', started_at) AS h, "
        "       AVG(elapsed_ms) AS mean_ms "
        "FROM embed_call_log "
        "WHERE started_at >= ? "
        "GROUP BY h",
        (cutoff,),
    ) as cur:
        agg = {row[0]: int(row[1] or 0) for row in await cur.fetchall()}

    sparkline = [agg.get(h, 0) for h in _hour_buckets_back(now)]

    return EmbedderCalls24h(
        total=total,
        ok=ok_count,
        fail=fail_count,
        avg_total_elapsed_ms=avg_ms,
        p95_total_elapsed_ms=p95_ms,
        sparkline_latency_ms=sparkline,
    )


_QDRANT_TIMEOUT = 3.0  # seconds; prevents slow Qdrant from stalling the snapshot


async def _qdrant_count(qdrant_client: Any | None) -> int:
    """Approximate count of news_current; -1 on error so the UI can show a hint."""
    if qdrant_client is None:
        return 0
    try:
        result = await asyncio.wait_for(
            qdrant_client.count(collection_name=_NEWS_ALIAS, exact=False),
            timeout=_QDRANT_TIMEOUT,
        )
        # qdrant-client returns CountResult(count=int)
        return int(getattr(result, "count", 0))
    except Exception as exc:
        logger.warning("qdrant count failed: %s", exc)
        return -1


async def _component_status(qdrant_handle: Any | None, embedder: Any | None) -> ComponentsBlock:
    sqlite_status = "ok" if await sqlite_ok() else "down"
    if qdrant_handle is None:
        qdrant_status = "down"
    else:
        try:
            ok = await asyncio.wait_for(qdrant_handle.ping(), timeout=_QDRANT_TIMEOUT)
        except asyncio.TimeoutError:
            ok = False
        qdrant_status = "ok" if ok else "down"
    embedder_status = getattr(embedder, "status", "error") if embedder is not None else "error"
    return ComponentsBlock(
        qdrant=qdrant_status,  # type: ignore[arg-type]
        sqlite=sqlite_status,  # type: ignore[arg-type]
        embedder=embedder_status,  # type: ignore[arg-type]
    )


async def build_snapshot(
    conn: aiosqlite.Connection,
    qdrant_handle: Any | None,
    embedder: Any | None,
    metrics_collector: Any | None = None,
) -> SnapshotResponse:
    """Top-level snapshot for the polling client (D5 + D6).

    ``metrics_collector`` is the lifespan-owned ``SystemMetricsCollector``
    (or None if the dashboard wasn't bootstrapped with one). Per design D6
    we inject it as a function argument rather than reading from
    ``app.state`` here — this module must not import FastAPI types.
    The caller (``routes.get_snapshot``) is the only place that touches
    ``request.app.state``.
    """
    now = _utcnow()
    qdrant_client = getattr(qdrant_handle, "client", None) if qdrant_handle is not None else None
    # Fire Qdrant network tasks immediately so they run while SQLite queries execute.
    component_task = asyncio.create_task(_component_status(qdrant_handle, embedder))
    qdrant_count_task = asyncio.create_task(_qdrant_count(qdrant_client))
    try:
        feeds_list = await list_feeds(conn)
        fetch_blocks = await _fetch_24h_all_feeds(conn, [f.id for f in feeds_list], now)
        calls = await _embedder_calls_24h(conn, now)
        async with conn.execute("SELECT COUNT(*) FROM pending_articles") as cur:
            pending_count = int((await cur.fetchone())[0])
        async with conn.execute("SELECT COUNT(*) FROM dead_articles") as cur:
            dead_count = int((await cur.fetchone())[0])
        components, qdrant_count_value = await asyncio.gather(component_task, qdrant_count_task)
    except BaseException:
        component_task.cancel()
        qdrant_count_task.cancel()
        raise

    feed_rows: list[FeedRow] = [
        FeedRow(
            id=f.id,
            name=f.name,
            url=str(f.url),
            poll_interval_minutes=f.poll_interval_minutes,
            last_collected_at=f.last_collected_at,
            fetch_24h=fetch_blocks[f.id],
        )
        for f in feeds_list
    ]
    embedder_block = EmbedderBlock(
        status=components.embedder,
        model_version=getattr(embedder, "model_version", None) if embedder else None,
        calls_24h=calls,
    )
    system_metrics = metrics_collector.read() if metrics_collector is not None else None
    return SnapshotResponse(
        schema_version=1,
        generated_at=now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        components=components,
        feeds=feed_rows,
        embedder=embedder_block,
        articles=ArticlesBlock(
            pending_count=pending_count,
            dead_count=dead_count,
            qdrant_count=qdrant_count_value,
        ),
        system_metrics=system_metrics,
    )


# ---------------------------------------------------------------------------
# Drill-down readers
# ---------------------------------------------------------------------------


async def list_feed_events(
    conn: aiosqlite.Connection, feed_id: int, limit: int, offset: int = 0
) -> list[FeedFetchEvent]:
    async with conn.execute(
        "SELECT id, started_at, elapsed_ms, ok, items_seen, items_new, "
        "       error_class, error_message "
        "FROM feed_fetch_log WHERE feed_id=? "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        (feed_id, limit, offset),
    ) as cur:
        rows = await cur.fetchall()
    return [
        FeedFetchEvent(
            id=r[0],
            started_at=r[1],
            elapsed_ms=r[2],
            ok=bool(r[3]),
            items_fetched=r[4],
            items_new=r[5],
            error_class=r[6],
            error_message=r[7],
        )
        for r in rows
    ]


async def list_embed_events(conn: aiosqlite.Connection, limit: int) -> list[EmbedCallEvent]:
    async with conn.execute(
        "SELECT id, started_at, elapsed_ms, ok, batch_size, total_chars, "
        "       timeout_seconds, error_class, error_message "
        "FROM embed_call_log "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ) as cur:
        rows = await cur.fetchall()
    return [
        EmbedCallEvent(
            id=r[0],
            started_at=r[1],
            elapsed_ms=r[2],
            ok=bool(r[3]),
            batch_size=r[4],
            total_chars=r[5],
            timeout_seconds=r[6],
            error_class=r[7],
            error_message=r[8],
        )
        for r in rows
    ]


async def list_articles_pending(
    conn: aiosqlite.Connection, limit: int, offset: int
) -> list[ArticleListItem]:
    async with conn.execute(
        "SELECT md5, feed_id, url, title, published_at, retry_count "
        "FROM pending_articles ORDER BY rowid DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ) as cur:
        rows = await cur.fetchall()
    return [
        ArticleListItem(
            md5=r[0],
            feed_id=r[1],
            url=r[2],
            title=r[3],
            published_at=r[4],
            retry_count=r[5],
            bucket="pending",
        )
        for r in rows
    ]


async def list_articles_dead(
    conn: aiosqlite.Connection, limit: int, offset: int
) -> list[ArticleListItem]:
    async with conn.execute(
        "SELECT md5, feed_id, url, title, published_at, error_message, failed_at "
        "FROM dead_articles ORDER BY failed_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ) as cur:
        rows = await cur.fetchall()
    return [
        ArticleListItem(
            md5=r[0],
            feed_id=r[1],
            url=r[2],
            title=r[3],
            published_at=r[4],
            error_message=r[5],
            failed_at=r[6],
            bucket="dead",
        )
        for r in rows
    ]


async def _scroll_articles_qdrant(
    qdrant_client: Any,
    *,
    limit: int,
    offset: int,
    scroll_filter: Any | None = None,
    log_label: str,
) -> list[ArticleListItem]:
    """Newest-first scroll over `news_current` with client-side offset.

    Qdrant docs (manage-data/points): "When you use the order_by parameter,
    pagination is disabled. ... next_page_offset is not returned within the
    response. However, you can still do pagination by combining
    ``order_by.start_from`` with a ``must_not.has_id`` filter."

    So each iteration after the first sets ``order_by.start_from = last_ts``
    AND appends a ``HasIdCondition`` with every already-seen point id to
    ``must_not``. Qdrant excludes those ids server-side, which sidesteps the
    "boundary cluster of equal ts" duplicate problem entirely.

    Without this loop the dashboard silently capped each result page at one
    Qdrant page (~64 hits), making a search like ``title_q=伊朗`` look like it
    only had ~24h of coverage when the collection actually held >700 hits over
    >10 days.
    """
    from qdrant_client.models import (  # noqa: PLC0415
        Filter,
        HasIdCondition,
    )

    items: list[ArticleListItem] = []
    seen_ids: set[str] = set()
    target = limit + offset
    last_ts: int | None = None

    base_must = list(getattr(scroll_filter, "must", None) or []) if scroll_filter else []
    base_should = list(getattr(scroll_filter, "should", None) or []) if scroll_filter else []
    base_must_not = list(getattr(scroll_filter, "must_not", None) or []) if scroll_filter else []

    try:
        while len(items) < target:
            order_by_spec: dict[str, Any] = {
                "key": "ingested_at_ts",
                "direction": "desc",
            }
            if last_ts is not None:
                order_by_spec["start_from"] = last_ts

            iter_must_not = list(base_must_not)
            if seen_ids:
                iter_must_not.append(HasIdCondition(has_id=list(seen_ids)))

            if base_must or base_should or iter_must_not:
                iter_filter: Any | None = Filter(
                    must=base_must or None,
                    should=base_should or None,
                    must_not=iter_must_not or None,
                )
            else:
                iter_filter = None

            scroll_kwargs: dict[str, Any] = {
                "collection_name": _NEWS_ALIAS,
                "with_payload": True,
                "with_vectors": False,
                "order_by": order_by_spec,
                "limit": min(target - len(items), 64),
            }
            if iter_filter is not None:
                scroll_kwargs["scroll_filter"] = iter_filter

            points, _next_unused = await qdrant_client.scroll(**scroll_kwargs)
            if not points:
                break

            page_last_ts: int | None = None
            for p in points:
                pid = str(getattr(p, "id", ""))
                payload = getattr(p, "payload", {}) or {}
                page_last_ts = payload.get("ingested_at_ts") or page_last_ts
                seen_ids.add(pid)
                items.append(
                    ArticleListItem(
                        md5=pid,
                        feed_id=payload.get("feed_id"),
                        url=payload.get("url", ""),
                        title=payload.get("title", ""),
                        published_at=payload.get("published_at"),
                        ingested_at_ts=payload.get("ingested_at_ts"),
                        bucket="qdrant",
                    )
                )
                if len(items) >= target:
                    break

            if page_last_ts is None:
                break
            last_ts = page_last_ts
    except Exception as exc:
        logger.warning("qdrant scroll %s failed: %s", log_label, exc)
        return []
    return items[offset : offset + limit]


async def list_articles_qdrant(
    qdrant_client: Any | None,
    limit: int,
    offset: int,
    *,
    ingested_from: datetime | None = None,
    ingested_to: datetime | None = None,
    feed_id: int | None = None,
    title_q: str | None = None,
) -> list[ArticleListItem]:
    """Newest-first list of news_current points (D7).

    Optional filters compose into a single qdrant ``Filter(must=[...])``:

    - ``ingested_from`` / ``ingested_to``: closed range on ``ingested_at_ts``.
      Both ends optional and independent. Bounds are inclusive on
      ``ingested_from`` and exclusive on ``ingested_to`` (caller passes
      end-of-day + 1 second to get an inclusive end-date in UI terms).
    - ``feed_id``: integer match.
    - ``title_q``: ``MatchText`` against the title text-index added in D8.

    All filtered fields have payload indexes (``ingested_at_ts``, ``feed_id``,
    ``title``) so qdrant uses index intersect rather than a full scroll
    (feedback_qdrant_client rule: scroll filter without payload index =
    full-collection scan).
    """
    if qdrant_client is None:
        return []

    has_filter = any(v is not None for v in (ingested_from, ingested_to, feed_id, title_q))
    if not has_filter:
        return await _scroll_articles_qdrant(
            qdrant_client, limit=limit, offset=offset, log_label="(all)"
        )

    from qdrant_client.models import (  # noqa: PLC0415
        FieldCondition,
        Filter,
        MatchText,
        MatchValue,
        Range,
    )

    must: list[Any] = []
    if ingested_from is not None or ingested_to is not None:
        must.append(
            FieldCondition(
                key="ingested_at_ts",
                range=Range(
                    gte=int(ingested_from.timestamp()) if ingested_from else None,
                    lt=int(ingested_to.timestamp()) if ingested_to else None,
                ),
            )
        )
    if feed_id is not None:
        must.append(FieldCondition(key="feed_id", match=MatchValue(value=int(feed_id))))
    if title_q:
        must.append(FieldCondition(key="title", match=MatchText(text=title_q)))

    qfilter = Filter(must=must)
    return await _scroll_articles_qdrant(
        qdrant_client,
        limit=limit,
        offset=offset,
        scroll_filter=qfilter,
        log_label=f"(filter feed={feed_id} title={title_q!r} range={ingested_from!s}-{ingested_to!s})",
    )


async def get_article_detail(
    conn: aiosqlite.Connection,
    qdrant_client: Any | None,
    md5: str,
    bucket: ArticleBucket,
) -> ArticleDetail | None:
    if bucket == "pending":
        async with conn.execute(
            "SELECT md5, feed_id, url, title, body, published_at, retry_count "
            "FROM pending_articles WHERE md5=?",
            (md5,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return ArticleDetail(
            md5=row[0],
            feed_id=row[1],
            url=row[2],
            title=row[3],
            body=row[4],
            published_at=row[5],
            retry_count=row[6],
            bucket="pending",
        )
    if bucket == "dead":
        async with conn.execute(
            "SELECT md5, feed_id, url, title, body, published_at, "
            "       error_message, failed_at "
            "FROM dead_articles WHERE md5=?",
            (md5,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return ArticleDetail(
            md5=row[0],
            feed_id=row[1],
            url=row[2],
            title=row[3],
            body=row[4],
            published_at=row[5],
            error_message=row[6],
            failed_at=row[7],
            bucket="dead",
        )
    # bucket == "qdrant"
    if qdrant_client is None:
        return None
    try:
        point_id = str(uuid.UUID(hex=md5))
        result = await qdrant_client.retrieve(
            collection_name=_NEWS_ALIAS,
            ids=[point_id],
            with_payload=True,
            with_vectors=False,
        )
        if not result:
            return None
        p = result[0]
        payload = getattr(p, "payload", {}) or {}
        return ArticleDetail(
            md5=md5,
            feed_id=payload.get("feed_id"),
            url=payload.get("url", ""),
            title=payload.get("title", ""),
            body=payload.get("body", ""),
            published_at=payload.get("published_at"),
            ingested_at_ts=payload.get("ingested_at_ts"),
            bucket="qdrant",
        )
    except Exception as exc:
        logger.warning("qdrant retrieve failed for md5=%s: %s", md5, exc)
        return None


async def list_feeds_with_meta(
    conn: aiosqlite.Connection,
    *,
    limit: int,
    offset: int,
    tag: str | None,
    q: str | None,
    proxy_hosts: frozenset[str],
    scheduler: Any | None = None,
    now: datetime | None = None,
) -> FeedListResponse:
    """Paginated feeds list for the Feeds tab (D8).

    SQLite holds the source-of-truth feed rows; tags come from feed_tags;
    group_key is derived in Python (D5) — no schema drift; next_run_iso comes
    from APScheduler when available so the UI can show "next run" countdowns.
    """
    # Build a single filtered base query so total + page share the same WHERE clause.
    where_parts: list[str] = []
    params: list[Any] = []
    if q:
        where_parts.append("LOWER(name) LIKE ?")
        params.append(f"%{q.lower()}%")
    if tag:
        # Subquery is cheaper than JOIN+DISTINCT here because tag uniqueness
        # is already enforced by feed_tags PK.
        where_parts.append("id IN (SELECT feed_id FROM feed_tags WHERE tag=?)")
        params.append(tag.lower())
    where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

    async with conn.execute(f"SELECT COUNT(*) FROM feeds{where_sql}", params) as cur:
        total = (await cur.fetchone())[0]

    async with conn.execute(
        f"SELECT id, name, url, source_type, config, poll_interval_minutes, "
        f"       last_collected_at, created_at, enabled "
        f"FROM feeds{where_sql} ORDER BY id ASC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ) as cur:
        rows = await cur.fetchall()

    feed_ids = [r[0] for r in rows]
    if not feed_ids:
        return FeedListResponse(items=[], total=total)

    tag_map = await list_all_tags(conn)
    fetch_map = await _fetch_24h_all_feeds(conn, feed_ids, now or _utcnow())

    items: list[FeedRowExtended] = []
    for r in rows:
        fid, name, url, source_type, config_json, poll_min, last_collected, created_at, enabled = r
        next_run_iso: str | None = None
        if scheduler is not None:
            try:
                # D17: newsapi feeds collapse onto a singleton master job, so
                # all of them share the same next_run_iso (the master tick's
                # next firing). RSS keeps the per-feed lookup.
                job_id = "source_newsapi_master" if source_type == "newsapi" else f"feed_{fid}"
                job = scheduler.get_job(job_id)
                if job is not None and job.next_run_time is not None:
                    next_run_iso = job.next_run_time.astimezone(timezone.utc).isoformat()
            except Exception:
                next_run_iso = None
        try:
            config = json.loads(config_json) if config_json else {}
        except Exception:
            config = {}
        items.append(
            FeedRowExtended(
                id=fid,
                name=name,
                url=url,
                source_type=source_type,
                config=config,
                poll_interval_minutes=poll_min,
                last_collected_at=last_collected,
                fetch_24h=fetch_map.get(fid)
                or Fetch24hBlock(
                    total=0,
                    ok=0,
                    fail=0,
                    last_outcome="never",
                    last_error_message=None,
                    consecutive_failures=0,
                    sparkline_buckets=[0] * _SPARKLINE_BUCKETS,
                ),
                tags=tag_map.get(fid, []),
                enabled=bool(enabled),
                # newsapi feeds share a single master job + a single API
                # endpoint, so they collapse into one display group regardless
                # of the per-feed source.uri. Bare hostnames also fail
                # urlparse-based grouping (no scheme → parsed.hostname=None
                # → empty key), so the special-case is also a correctness fix.
                group_key=(
                    "newsapi" if source_type == "newsapi" else derive_group_key(url, proxy_hosts)
                ),
                next_run_iso=next_run_iso,
                created_at=created_at,
            )
        )
    return FeedListResponse(items=items, total=total)


async def list_feed_articles_qdrant(
    qdrant_client: Any | None,
    feed_id: int,
    *,
    limit: int,
    offset: int,
) -> list[ArticleListItem]:
    """Per-feed Qdrant-only article list. Relies on the payload index added
    in vector_store.news.ensure_news_collection."""
    if qdrant_client is None:
        return []
    from qdrant_client.models import (  # noqa: PLC0415
        FieldCondition,
        Filter,
        MatchValue,
    )

    qfilter = Filter(must=[FieldCondition(key="feed_id", match=MatchValue(value=int(feed_id)))])
    return await _scroll_articles_qdrant(
        qdrant_client,
        limit=limit,
        offset=offset,
        scroll_filter=qfilter,
        log_label=f"for feed_id={feed_id}",
    )
