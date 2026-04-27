"""FastAPI entrypoint.

设计决策 #16 / #17: lifespan startup = SQLite init → Qdrant client init;
shutdown reverses order. Qdrant client construction never blocks on server readiness —
if Qdrant isn't up yet, /health reports 503 and the platform's readiness probe retries.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from sembr.api.health import router as health_router
from sembr.config import get_settings
from sembr.db.sqlite import close_sqlite, init_sqlite
from sembr.vector_store.qdrant import QdrantHandle


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    await init_sqlite(settings.sqlite_path)
    qdrant = QdrantHandle(settings.qdrant_url)
    app.state.qdrant = qdrant
    app.state.settings = settings
    try:
        yield
    finally:
        await qdrant.close()
        await close_sqlite()


app = FastAPI(title="sembr", version="0.1.0.dev0", lifespan=lifespan)
app.include_router(health_router)
