"""APScheduler job for embedding articles from pending_articles.

Implements D1 (30s IntervalTrigger), D2 (upsert-then-delete ordering),
D17 (module constants), D20 (transient Qdrant error handling).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

try:
    from qdrant_client.models import PointStruct
except ImportError:
    # qdrant_client not installed on Windows dev machine; tests mock upsert so
    # a plain dataclass is sufficient to keep the test-visible payload API intact.
    from dataclasses import dataclass as _dc

    @_dc
    class PointStruct:  # type: ignore[no-redef]
        id: str
        vector: list
        payload: dict

from sembr.db.articles import (
    PendingRow,
    delete_pending,
    demote_md5s_to_dead,
    increment_retry,
    pull_pending_batch,
)
from sembr.db.sqlite import get_conn

if TYPE_CHECKING:
    from sembr.embedder.base import BaseEmbedder
    from sembr.vector_store.qdrant import QdrantHandle

logger = logging.getLogger(__name__)

# SiliconFlow accepts up to 32 inputs per request. Verify worst-case token budget
# (top-32 longest articles) on Mac Mini before shipping — see design Risk row 5.
BATCH_SIZE = 32
MAX_ATTEMPTS = 3  # total embed+upsert attempts before a row is demoted to dead_articles
POLL_INTERVAL_SECONDS = 30

ALIAS_NAME = "news_current"


def _md5_to_uuid(md5: str) -> str:
    return str(uuid.UUID(hex=md5))


def _to_point(row: PendingRow, vector: list[float], model_version: str) -> PointStruct:
    return PointStruct(
        id=_md5_to_uuid(row.md5),
        vector=vector,
        payload={
            "url": row.url,
            "title": row.title,
            "body": row.body,
            "published_at": row.published_at,
            "feed_id": row.feed_id,
            "embedding_model_version": model_version,
        },
    )


def add_embedder_worker_job(
    scheduler: AsyncIOScheduler,
    embedder: "BaseEmbedder",
    qdrant: "QdrantHandle",
) -> None:
    scheduler.add_job(
        embedder_worker,
        trigger=IntervalTrigger(seconds=POLL_INTERVAL_SECONDS),
        id="embedder_worker",
        args=[embedder, qdrant],
        coalesce=True,
        max_instances=1,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=30),
        replace_existing=True,
    )


async def embedder_worker(embedder: "BaseEmbedder", qdrant: "QdrantHandle") -> None:
    if not embedder.is_loaded:
        return

    conn = get_conn()
    batch = await pull_pending_batch(conn, BATCH_SIZE, MAX_ATTEMPTS)
    if not batch:
        return

    texts = [row.title + "\n\n" + row.body for row in batch]
    md5s = [row.md5 for row in batch]

    # Compute which rows are one retry away from exhaustion BEFORE incrementing
    md5s_at_limit = [r.md5 for r in batch if r.retry_count + 1 >= MAX_ATTEMPTS]

    try:
        vectors: list[list[float]] = await embedder.aembed(texts)
    except Exception as exc:
        logger.warning("embed failed for batch of %d: %s", len(batch), exc)
        await increment_retry(conn, md5s)
        # Demote ONLY the rows from this batch whose error actually exhausted retries (🔴-2)
        if md5s_at_limit:
            await demote_md5s_to_dead(conn, md5s_at_limit, error_message=str(exc))
        return

    # D2: upsert Qdrant first; delete pending only after confirmed success.
    # Only transient connection errors skip retry increment (D20).
    points = [_to_point(row, vec, embedder.model_version) for row, vec in zip(batch, vectors)]
    try:
        await qdrant.client.upsert(
            collection_name=ALIAS_NAME,
            points=points,
            wait=True,
        )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        logger.warning("qdrant transient, retrying next tick: %s", exc)
        return
    except Exception as exc:
        logger.warning("qdrant error for batch of %d: %s", len(batch), exc)
        await increment_retry(conn, md5s)
        if md5s_at_limit:
            await demote_md5s_to_dead(conn, md5s_at_limit, error_message=str(exc))
        return

    # Log before delete: upsert success == forward progress; delete is a cleanup step.
    # Logging here ensures throughput metrics are correct even when delete blips (🟡-10).
    logger.info("embedded %d articles, dim=%d", len(batch), len(vectors[0]) if vectors else 0)
    # If delete fails, rows re-embed next tick — idempotent via deterministic UUID (D4).
    try:
        await delete_pending(conn, md5s)
    except Exception as exc:
        logger.warning("post-upsert delete failed (will retry next tick): %s", exc)
