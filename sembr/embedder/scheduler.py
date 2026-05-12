# SPDX-License-Identifier: Apache-2.0
"""APScheduler job for embedding articles from pending_articles.

Runs on a 30 s IntervalTrigger; each tick pulls a batch, embeds via the configured
backend, upserts the resulting vectors into Qdrant first, then deletes the source
rows from pending_articles. Transient Qdrant errors (timeout, connection reset) skip
the attempt-counter increment so the row is retried indefinitely; non-transient
errors increment the counter and eventually evict via the maintenance sweeper.
"""

from __future__ import annotations

import logging
import time
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


from sembr.dashboard.events import log_embed_event
from sembr.db.articles import (
    PendingRow,
    delete_pending,
    demote_md5s_to_dead,
    increment_retry,
    pull_pending_batch,
)
from sembr.db.sqlite import get_conn
from sembr.vector_store.news import md5_to_uuid, upsert_news_points

if TYPE_CHECKING:
    from sembr.embedder.base import BaseEmbedder
    from sembr.vector_store.qdrant import QdrantHandle

logger = logging.getLogger(__name__)

# SiliconFlow accepts up to 32 inputs per request. The read timeout is computed
# dynamically per-batch from total char count (see embedder_worker), so the batch
# size does not need to be conservative.
BATCH_SIZE = 32

# Per-text character cap is read from `embedder.max_input_chars` so each backend
# can publish its own bound (tied to the model's context window).
MAX_ATTEMPTS = 3  # total embed+upsert attempts before a row is demoted to dead_articles
POLL_INTERVAL_SECONDS = 30


def _to_point(row: PendingRow, vector: list[float], model_version: str) -> PointStruct:
    return PointStruct(
        id=md5_to_uuid(row.md5),
        vector=vector,
        payload={
            "url": row.url,
            "title": row.title,
            "body": row.body,
            "published_at": row.published_at,
            "feed_id": row.feed_id,
            "embedding_model_version": model_version,
            # Integer epoch seconds so Qdrant Range filter in matcher scan can compare directly.
            "ingested_at_ts": int(datetime.now(timezone.utc).timestamp()),
        },
    )


def add_embedder_worker_job(
    scheduler: AsyncIOScheduler,
    embedder: "BaseEmbedder",
    qdrant: "QdrantHandle",
    app=None,
) -> None:
    scheduler.add_job(
        embedder_worker,
        trigger=IntervalTrigger(seconds=POLL_INTERVAL_SECONDS),
        id="embedder_worker",
        args=[embedder, qdrant, app],
        coalesce=True,
        max_instances=1,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=30),
        replace_existing=True,
    )


async def _emit_embed_event(
    *,
    started_at: datetime,
    ok: bool,
    batch_size: int,
    total_chars: int,
    timeout_seconds: float,
    error_class: str | None,
    error_message: str | None,
) -> None:
    """Best-effort wrapper: observability faults must never poison embedder_worker."""
    try:
        elapsed_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
        await log_embed_event(
            started_at=started_at,
            elapsed_ms=elapsed_ms,
            ok=ok,
            batch_size=batch_size,
            total_chars=total_chars,
            timeout_seconds=timeout_seconds,
            error_class=error_class,
            error_message=error_message,
        )
    except Exception as exc:
        logger.warning("log_embed_event failed: %s", exc)


async def embedder_worker(embedder: "BaseEmbedder", qdrant: "QdrantHandle", app=None) -> None:
    # Embedder not yet loaded → not a call attempt; don't write an event row.
    if not embedder.is_loaded:
        return

    conn = get_conn()
    batch = await pull_pending_batch(conn, BATCH_SIZE, MAX_ATTEMPTS)
    # Empty queue → no work, no event row.
    if not batch:
        return

    char_cap = embedder.max_input_chars
    texts = [(row.title + "\n\n" + row.body)[:char_cap] for row in batch]
    md5s = [row.md5 for row in batch]

    # Compute which rows are one retry away from exhaustion BEFORE incrementing
    md5s_at_limit = [r.md5 for r in batch if r.retry_count + 1 >= MAX_ATTEMPTS]

    # Dynamic read-timeout: SiliconFlow processing time scales with total chars.
    # Calibration (2026-04-30): batches of ~80k chars timed out at 30s, while
    # smaller batches succeeded comfortably. ~1500 chars/sec is a conservative
    # throughput estimate covering server queueing + BGE-M3 forward + RTT.
    # 30s floor avoids underseeding tiny batches; full 32×8000 batch = ~171s.
    total_chars = sum(len(t) for t in texts)
    embed_timeout = max(30.0, total_chars / 1500)
    logger.info(
        "embed start: batch=%d total_chars=%d timeout=%.1fs",
        len(batch),
        total_chars,
        embed_timeout,
    )

    started_at = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    try:
        vectors: list[list[float]] = await embedder.aembed(texts, timeout=embed_timeout)
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        logger.warning(
            "embed failed for batch of %d after %.1fs (timeout=%.1fs): %s",
            len(batch),
            elapsed,
            embed_timeout,
            exc,
            exc_info=True,
        )
        await increment_retry(conn, md5s)
        # Demote ONLY the rows from this batch whose error actually exhausted retries (🔴-2)
        if md5s_at_limit:
            await demote_md5s_to_dead(conn, md5s_at_limit, error_message=str(exc))
        await _emit_embed_event(
            started_at=started_at,
            ok=False,
            batch_size=len(batch),
            total_chars=total_chars,
            timeout_seconds=embed_timeout,
            error_class=exc.__class__.__name__,
            error_message=str(exc),
        )
        return

    # Upsert Qdrant first; delete pending rows only after confirmed success so a
    # crash mid-tick leaves the rows for re-embedding rather than losing them.
    # Only transient connection errors skip the retry-counter increment.
    points = [_to_point(row, vec, embedder.model_version) for row, vec in zip(batch, vectors)]
    try:
        await upsert_news_points(qdrant.client, points, wait=True)
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        logger.warning("qdrant transient, retrying next tick: %s", exc)
        await _emit_embed_event(
            started_at=started_at,
            ok=False,
            batch_size=len(batch),
            total_chars=total_chars,
            timeout_seconds=embed_timeout,
            error_class="qdrant_transient",
            error_message=str(exc),
        )
        return
    except Exception as exc:
        logger.warning("qdrant error for batch of %d: %s", len(batch), exc)
        await increment_retry(conn, md5s)
        if md5s_at_limit:
            await demote_md5s_to_dead(conn, md5s_at_limit, error_message=str(exc))
        await _emit_embed_event(
            started_at=started_at,
            ok=False,
            batch_size=len(batch),
            total_chars=total_chars,
            timeout_seconds=embed_timeout,
            error_class="qdrant_error",
            error_message=str(exc),
        )
        return

    # Log before delete: upsert success == forward progress; delete is a cleanup step.
    # Logging here ensures throughput metrics are correct even when delete blips (🟡-10).
    logger.info("embedded %d articles, dim=%d", len(batch), len(vectors[0]) if vectors else 0)
    await _emit_embed_event(
        started_at=started_at,
        ok=True,
        batch_size=len(batch),
        total_chars=total_chars,
        timeout_seconds=embed_timeout,
        error_class=None,
        error_message=None,
    )

    # Event-driven matching — after upsert success, before delete_pending. Synchronous
    # await so a failed event_match does not skip the delete (the never-raise wrapper
    # is inside event_match_batch). Local import: a top-level
    # `from sembr.matcher.event_match import ...` would create a cycle
    # (matcher → vector_store → embedder.scheduler → matcher); inverting the
    # dependency (matcher subscribes to embedder events via a bus) is a longer-term
    # refactor.
    if app is not None:
        from sembr.matcher.event_match import event_match_batch  # noqa: PLC0415

        await event_match_batch(app, points, conn)

    # If delete fails, rows re-embed next tick — idempotent via deterministic UUID.
    try:
        await delete_pending(conn, md5s)
    except Exception as exc:
        logger.warning("post-upsert delete failed (will retry next tick): %s", exc)
