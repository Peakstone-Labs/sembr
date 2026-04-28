"""FastAPI entrypoint.

设计决策 #16 / #17: lifespan startup = SQLite init → Qdrant client init;
shutdown reverses order. Qdrant client construction never blocks on server readiness —
if Qdrant isn't up yet, /health reports 503 and the platform's readiness probe retries.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

# Attach a stream handler so INFO-level app logs actually reach stderr.
# Setting only the level is not enough: without a handler, the lastResort
# handler kicks in at WARNING, silently dropping every logger.info() call.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("sembr").setLevel(logging.INFO)

from sembr.api.feeds import router as feeds_router
from sembr.api.health import router as health_router
from sembr.api.intents import router as intents_router
from sembr.collector.scheduler import add_feed_job, make_scheduler
from sembr.config import get_settings
from sembr.db.articles import init_article_tables
from sembr.db.feeds import init_feed_tables, list_feeds, seed_initial_feeds
from sembr.db.intents import get_intent, init_intent_tables, list_intents
from sembr.db.match_seen import init_match_seen_tables
from sembr.db.sqlite import close_sqlite, init_sqlite
from sembr.embedder.factory import build_embedder
from sembr.embedder.scheduler import add_embedder_worker_job
from sembr.matcher.jobs import register_all_enabled
from sembr.summarizer.llm.factory import build_llm_backend
from sembr.summarizer.pipeline import SummaryPipeline
from sembr.vector_store.intents import ensure_intents_collection
from sembr.vector_store.news import ensure_news_collection
from sembr.vector_store.qdrant import QdrantHandle


async def _get_intent_prompt_ctx(conn, intent_id: int) -> tuple[str | None, str]:
    intent = await get_intent(conn, intent_id)
    if intent is None:
        return None, ""
    return intent.custom_prompt, intent.text


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # Validate embedder config before any I/O — raises ValueError if EMBEDDER_API_KEY unset.
    embedder = build_embedder(settings)
    conn = await init_sqlite(settings.sqlite_path)
    await init_feed_tables(conn)
    await init_article_tables(conn)
    await init_intent_tables(conn)
    await init_match_seen_tables(conn)  # D17: after intents (FK dependency)
    await seed_initial_feeds(conn)
    qdrant = QdrantHandle(settings.qdrant_url)
    await ensure_news_collection(qdrant.client)
    await ensure_intents_collection(qdrant.client)
    load_task = asyncio.create_task(embedder.load())  # background; /health probes status
    scheduler = make_scheduler()
    feeds = await list_feeds(conn)
    for i, feed in enumerate(feeds):
        await add_feed_job(scheduler, feed, jitter_seconds=i * 2)
    add_embedder_worker_job(scheduler, embedder, qdrant)
    # D18: register per-intent jobs for all currently-enabled intents (restart recovery)
    enabled_intents = await list_intents(conn, enabled=True)
    await register_all_enabled(scheduler, enabled_intents, app, qdrant.client)
    # R5: assign on_match before scheduler.start() so first ticks always find a callback
    llm_backend = build_llm_backend(settings)
    pipeline = SummaryPipeline(
        llm=llm_backend,
        grouping_threshold=settings.llm_grouping_threshold,
        get_intent_prompt_ctx=lambda iid: _get_intent_prompt_ctx(conn, iid),
    )
    app.state.on_match = pipeline.handle
    app.state.qdrant = qdrant
    app.state.scheduler = scheduler
    app.state.settings = settings
    app.state.embedder = embedder
    scheduler.start()
    try:
        yield
    finally:
        # wait=False: for AsyncIOScheduler, wait=True only blocks on ThreadPoolExecutor
        # jobs — it does NOT await async coroutines like embedder_worker. Blocking the
        # event loop here risks hitting Docker's 10s SIGKILL before aclose/close_sqlite
        # complete. Async jobs that finish naturally between shutdown() and aclose() are
        # fine; those still mid-flight see ClientClosed → increment_retry (idempotent).
        scheduler.shutdown(wait=False)
        load_task.cancel()
        try:
            # Await so the thread pool doesn't race with interpreter teardown.
            await asyncio.wait_for(load_task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        if hasattr(embedder, "aclose"):
            await embedder.aclose()
        if hasattr(llm_backend, "aclose"):
            await llm_backend.aclose()
        await qdrant.close()
        await close_sqlite()


app = FastAPI(title="sembr", version="0.1.0.dev0", lifespan=lifespan)
app.include_router(health_router)
app.include_router(feeds_router)
app.include_router(intents_router)
