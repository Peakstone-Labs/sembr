"""FastAPI entrypoint.

设计决策 #16 / #17: lifespan startup = SQLite init → Qdrant client init;
shutdown reverses order. Qdrant client construction never blocks on server readiness —
if Qdrant isn't up yet, /health reports 503 and the platform's readiness probe retries.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

logging.getLogger("sembr").setLevel(logging.INFO)

from sembr.api.feeds import router as feeds_router
from sembr.api.health import router as health_router
from sembr.collector.scheduler import add_feed_job, make_scheduler
from sembr.config import get_settings
from sembr.db.feeds import init_feed_tables, list_feeds, seed_initial_feeds
from sembr.db.sqlite import close_sqlite, init_sqlite
from sembr.vector_store.qdrant import QdrantHandle


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    conn = await init_sqlite(settings.sqlite_path)
    await init_feed_tables(conn)
    await seed_initial_feeds(conn)
    qdrant = QdrantHandle(settings.qdrant_url)
    scheduler = make_scheduler()
    feeds = await list_feeds(conn)
    for i, feed in enumerate(feeds):
        await add_feed_job(scheduler, feed, jitter_seconds=i * 2)
    scheduler.start()
    app.state.qdrant = qdrant
    app.state.scheduler = scheduler
    app.state.settings = settings
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        await qdrant.close()
        await close_sqlite()


app = FastAPI(title="sembr", version="0.1.0.dev0", lifespan=lifespan)
app.include_router(health_router)
app.include_router(feeds_router)
