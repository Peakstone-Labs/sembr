"""APScheduler job for embedding articles from pending_articles.

Implements D1 (30s IntervalTrigger), D2 (upsert-then-delete ordering),
D17 (module constants), D20 (transient Qdrant error handling).
"""
from __future__ import annotations

import logging
import time
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

from sembr.dashboard.events import log_embed_event
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
# Read timeout is computed dynamically per-batch from total char count
# (see embedder_worker), so batch size doesn't need to be conservative.
BATCH_SIZE = 32

# BGE-M3 context window is 8192 tokens.
# Chinese tokenizes at ~1 char/token (BERT BPE), English at ~4 chars/token.
# 8 000 chars is the safe upper bound for pure Chinese; English articles get ~2000 tokens
# which is more than enough for semantic matching on news headlines + lead paragraphs.
_EMBED_CHARS_MAX = 8_000
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
            # C1/D14: integer epoch seconds for Qdrant Range filter in matcher scan
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
        elapsed_ms = int(
            (datetime.now(timezone.utc) - started_at).total_seconds() * 1000
        )
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
    # Embedder not yet loaded → not a call attempt; per D4 don't write an event.
    if not embedder.is_loaded:
        return

    conn = get_conn()
    batch = await pull_pending_batch(conn, BATCH_SIZE, MAX_ATTEMPTS)
    # Empty queue → no work, no event row (per D4).
    if not batch:
        return

    texts = [(row.title + "\n\n" + row.body)[:_EMBED_CHARS_MAX] for row in batch]
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
        len(batch), total_chars, embed_timeout,
    )

    started_at = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    try:
        vectors: list[list[float]] = await embedder.aembed(texts, timeout=embed_timeout)
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        logger.warning(
            "embed failed for batch of %d after %.1fs (timeout=%.1fs): %s",
            len(batch), elapsed, embed_timeout, exc, exc_info=True,
        )
        await increment_retry(conn, md5s)
        # Demote ONLY the rows from this batch whose error actually exhausted retries (🔴-2)
        if md5s_at_limit:
            await demote_md5s_to_dead(conn, md5s_at_limit, error_message=str(exc))
        await _emit_embed_event(
            started_at=started_at, ok=False,
            batch_size=len(batch), total_chars=total_chars,
            timeout_seconds=embed_timeout,
            error_class=exc.__class__.__name__, error_message=str(exc),
        )
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
        await _emit_embed_event(
            started_at=started_at, ok=False,
            batch_size=len(batch), total_chars=total_chars,
            timeout_seconds=embed_timeout,
            error_class="qdrant_transient", error_message=str(exc),
        )
        return
    except Exception as exc:
        logger.warning("qdrant error for batch of %d: %s", len(batch), exc)
        await increment_retry(conn, md5s)
        if md5s_at_limit:
            await demote_md5s_to_dead(conn, md5s_at_limit, error_message=str(exc))
        await _emit_embed_event(
            started_at=started_at, ok=False,
            batch_size=len(batch), total_chars=total_chars,
            timeout_seconds=embed_timeout,
            error_class="qdrant_error", error_message=str(exc),
        )
        return

    # Log before delete: upsert success == forward progress; delete is a cleanup step.
    # Logging here ensures throughput metrics are correct even when delete blips (🟡-10).
    logger.info("embedded %d articles, dim=%d", len(batch), len(vectors[0]) if vectors else 0)
    await _emit_embed_event(
        started_at=started_at, ok=True,
        batch_size=len(batch), total_chars=total_chars,
        timeout_seconds=embed_timeout,
        error_class=None, error_message=None,
    )

    # D12: event-driven matching — after upsert success, before delete_pending.
    # Synchronous await so failed event_match does not skip delete (Risk 7 catch is inside).
    # Local import: top-level `from sembr.matcher.event_match import ...` would create a
    # cycle (matcher → vector_store → embedder.scheduler → matcher). Long-term fix is to
    # invert the dependency (matcher subscribes to embedder events via a bus); tracked in
    # ../sembr-dev-docs/development/event-driven-intent/.
    if app is not None:
        from sembr.matcher.event_match import event_match_batch  # noqa: PLC0415
        await event_match_batch(app, points, conn)

    # If delete fails, rows re-embed next tick — idempotent via deterministic UUID (D4).
    try:
        await delete_pending(conn, md5s)
    except Exception as exc:
        logger.warning("post-upsert delete failed (will retry next tick): %s", exc)
