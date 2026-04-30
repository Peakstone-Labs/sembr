"""Aggregate read queries for the dashboard.

This module is the only place that joins SQLite + Qdrant + app.state.embedder
into the snapshot/drill-down response shapes. Routes call these helpers; helpers
do not import FastAPI types — they take primitives and return Pydantic models.

Lazy Qdrant import: Windows dev machine has no qdrant_client installed, so the
top-level imports stay pure-stdlib + Pydantic. Qdrant calls are dispatched on
the AsyncQdrantClient handed in by the caller.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

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
    FeedRow,
    SnapshotResponse,
)
from sembr.db.feeds import list_feeds
from sembr.db.sqlite import sqlite_ok

logger = logging.getLogger(__name__)

_QDRANT_COLLECTION = "news_current"
_SPARKLINE_BUCKETS = 24


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hour_buckets_back(now: datetime, hours: int = _SPARKLINE_BUCKETS) -> list[str]:
    """List of N hour-keys 'YYYY-MM-DD HH', oldest → newest, ending at now."""
    base = now.replace(minute=0, second=0, microsecond=0)
    return [
        (base - timedelta(hours=h)).strftime("%Y-%m-%d %H")
        for h in range(hours - 1, -1, -1)
    ]


async def _fetch_24h_for_feed(
    conn: aiosqlite.Connection, feed_id: int, now: datetime
) -> Fetch24hBlock:
    cutoff = (now - timedelta(hours=_SPARKLINE_BUCKETS)).isoformat()
    async with conn.execute(
        "SELECT ok, error_message FROM feed_fetch_log "
        "WHERE feed_id=? AND started_at >= ? ORDER BY id DESC",
        (feed_id, cutoff),
    ) as cur:
        rows: list[tuple[int, str | None]] = list(await cur.fetchall())

    total = len(rows)
    ok_count = sum(1 for r in rows if r[0] == 1)
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

    # Sparkline: hourly count of ok==1 fetches.
    async with conn.execute(
        "SELECT strftime('%Y-%m-%d %H', started_at) AS h, "
        "       SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END) AS oks "
        "FROM feed_fetch_log "
        "WHERE feed_id=? AND started_at >= ? "
        "GROUP BY h",
        (feed_id, cutoff),
    ) as cur:
        agg = {row[0]: int(row[1] or 0) for row in await cur.fetchall()}

    sparkline = [agg.get(h, 0) for h in _hour_buckets_back(now)]

    return Fetch24hBlock(
        total=total,
        ok=ok_count,
        fail=fail_count,
        last_outcome=last_outcome,  # type: ignore[arg-type]
        last_error_message=last_error_message,
        consecutive_failures=consecutive_failures,
        sparkline_buckets=sparkline,
    )


async def _embedder_calls_24h(
    conn: aiosqlite.Connection, now: datetime
) -> EmbedderCalls24h:
    cutoff = (now - timedelta(hours=_SPARKLINE_BUCKETS)).isoformat()
    async with conn.execute(
        "SELECT ok, elapsed_ms FROM embed_call_log "
        "WHERE started_at >= ?",
        (cutoff,),
    ) as cur:
        rows = await cur.fetchall()

    total = len(rows)
    ok_count = sum(1 for r in rows if r[0] == 1)
    fail_count = total - ok_count
    elapsed_values = [int(r[1]) for r in rows]
    avg_ms = int(sum(elapsed_values) / len(elapsed_values)) if elapsed_values else 0

    p95_ms = 0
    if elapsed_values:
        sorted_vals = sorted(elapsed_values)
        # nearest-rank p95
        idx = max(0, int(round(0.95 * len(sorted_vals))) - 1)
        p95_ms = sorted_vals[idx]

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


async def _qdrant_count(qdrant_client: Any | None) -> int:
    """Approximate count of news_current; -1 on error so the UI can show a hint."""
    if qdrant_client is None:
        return 0
    try:
        result = await qdrant_client.count(
            collection_name=_QDRANT_COLLECTION, exact=False
        )
        # qdrant-client returns CountResult(count=int)
        return int(getattr(result, "count", 0))
    except Exception as exc:
        logger.warning("qdrant count failed: %s", exc)
        return -1


async def _component_status(
    qdrant_handle: Any | None, embedder: Any | None
) -> ComponentsBlock:
    sqlite_status = "ok" if await sqlite_ok() else "down"
    if qdrant_handle is None:
        qdrant_status = "down"
    else:
        qdrant_status = "ok" if await qdrant_handle.ping() else "down"
    embedder_status = (
        getattr(embedder, "status", "error") if embedder is not None else "error"
    )
    return ComponentsBlock(
        qdrant=qdrant_status,  # type: ignore[arg-type]
        sqlite=sqlite_status,  # type: ignore[arg-type]
        embedder=embedder_status,  # type: ignore[arg-type]
    )


async def build_snapshot(
    conn: aiosqlite.Connection,
    qdrant_handle: Any | None,
    embedder: Any | None,
) -> SnapshotResponse:
    """Top-level snapshot for the polling client (D5)."""
    now = _utcnow()
    components = await _component_status(qdrant_handle, embedder)

    feeds_list = await list_feeds(conn)
    feed_rows: list[FeedRow] = []
    for f in feeds_list:
        fetch_block = await _fetch_24h_for_feed(conn, f.id, now)
        feed_rows.append(
            FeedRow(
                id=f.id,
                name=f.name,
                url=str(f.url),
                poll_interval_minutes=f.poll_interval_minutes,
                last_collected_at=f.last_collected_at,
                fetch_24h=fetch_block,
            )
        )

    calls = await _embedder_calls_24h(conn, now)
    embedder_block = EmbedderBlock(
        status=components.embedder,
        model_version=getattr(embedder, "model_version", None) if embedder else None,
        calls_24h=calls,
    )

    async with conn.execute("SELECT COUNT(*) FROM pending_articles") as cur:
        pending_count = int((await cur.fetchone())[0])
    async with conn.execute("SELECT COUNT(*) FROM dead_articles") as cur:
        dead_count = int((await cur.fetchone())[0])
    qdrant_count_value = await _qdrant_count(
        getattr(qdrant_handle, "client", None) if qdrant_handle is not None else None
    )

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
    )


# ---------------------------------------------------------------------------
# Drill-down readers
# ---------------------------------------------------------------------------

async def list_feed_events(
    conn: aiosqlite.Connection, feed_id: int, limit: int
) -> list[FeedFetchEvent]:
    async with conn.execute(
        "SELECT id, started_at, elapsed_ms, ok, items_seen, items_new, "
        "       error_class, error_message "
        "FROM feed_fetch_log WHERE feed_id=? "
        "ORDER BY id DESC LIMIT ?",
        (feed_id, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [
        FeedFetchEvent(
            id=r[0],
            started_at=r[1],
            elapsed_ms=r[2],
            ok=bool(r[3]),
            items_seen=r[4],
            items_new=r[5],
            error_class=r[6],
            error_message=r[7],
        )
        for r in rows
    ]


async def list_embed_events(
    conn: aiosqlite.Connection, limit: int
) -> list[EmbedCallEvent]:
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
            md5=r[0], feed_id=r[1], url=r[2], title=r[3],
            published_at=r[4], retry_count=r[5], bucket="pending",
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
            md5=r[0], feed_id=r[1], url=r[2], title=r[3],
            published_at=r[4], error_message=r[5], failed_at=r[6],
            bucket="dead",
        )
        for r in rows
    ]


async def list_articles_qdrant(
    qdrant_client: Any | None, limit: int, offset: int
) -> list[ArticleListItem]:
    """Newest-first list of news_current points. offset implemented client-side
    (Qdrant scroll uses next_page_offset, not numeric offset) so we scroll
    forward limit+offset times then drop."""
    if qdrant_client is None:
        return []
    items: list[ArticleListItem] = []
    seen = 0
    next_offset = None
    target = limit + offset
    try:
        while seen < target:
            page_size = min(target - seen, 64)
            points, next_offset = await qdrant_client.scroll(
                collection_name=_QDRANT_COLLECTION,
                limit=page_size,
                with_payload=True,
                with_vectors=False,
                offset=next_offset,
                # Order by ingested_at desc so the latest articles come first.
                order_by={"key": "ingested_at_ts", "direction": "desc"},
            )
            for p in points:
                payload = getattr(p, "payload", {}) or {}
                items.append(
                    ArticleListItem(
                        md5=str(getattr(p, "id", "")),
                        feed_id=payload.get("feed_id"),
                        url=payload.get("url", ""),
                        title=payload.get("title", ""),
                        published_at=payload.get("published_at"),
                        ingested_at_ts=payload.get("ingested_at_ts"),
                        bucket="qdrant",
                    )
                )
                seen += 1
                if seen >= target:
                    break
            if next_offset is None:
                break
    except Exception as exc:
        logger.warning("qdrant scroll failed: %s", exc)
        return []
    return items[offset : offset + limit]


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
            md5=row[0], feed_id=row[1], url=row[2], title=row[3], body=row[4],
            published_at=row[5], retry_count=row[6], bucket="pending",
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
            md5=row[0], feed_id=row[1], url=row[2], title=row[3], body=row[4],
            published_at=row[5], error_message=row[6], failed_at=row[7],
            bucket="dead",
        )
    # bucket == "qdrant"
    if qdrant_client is None:
        return None
    try:
        import uuid
        point_id = str(uuid.UUID(hex=md5))
        result = await qdrant_client.retrieve(
            collection_name=_QDRANT_COLLECTION,
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
