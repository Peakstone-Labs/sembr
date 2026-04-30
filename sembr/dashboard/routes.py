"""APIRouter for /api/dashboard/* (D5 + D6 + D10).

Endpoints:
  GET /api/dashboard/snapshot                              snapshot for polling
  GET /api/dashboard/feeds/{feed_id}/events?limit=         drill-down: feed_fetch_log
  GET /api/dashboard/embedder/events?limit=                drill-down: embed_call_log
  GET /api/dashboard/articles?bucket=&limit=&offset=       list pending/dead/qdrant
  GET /api/dashboard/articles/{md5}?bucket=                single article detail
  GET /api/dashboard/config                                public: poll cadence + auth flag
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request

from sembr.config import get_settings
from sembr.dashboard import read_model
from sembr.dashboard.schemas import (
    ArticleBucket,
    ArticleDetail,
    ArticleListItem,
    ConfigResponse,
    EmbedCallEvent,
    FeedFetchEvent,
    SnapshotResponse,
)
from sembr.db.sqlite import get_conn

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/config", response_model=ConfigResponse)
async def get_config() -> ConfigResponse:
    """Public endpoint (auth-free) so the login page knows whether a token is required."""
    settings = get_settings()
    return ConfigResponse(
        poll_interval_seconds=settings.dashboard_poll_interval_seconds,
        auth_required=bool(settings.dashboard_token.get_secret_value()),
    )


@router.get("/snapshot", response_model=SnapshotResponse)
async def get_snapshot(request: Request) -> SnapshotResponse:
    qdrant = getattr(request.app.state, "qdrant", None)
    embedder = getattr(request.app.state, "embedder", None)
    return await read_model.build_snapshot(get_conn(), qdrant, embedder)


@router.get("/feeds/{feed_id}/events", response_model=list[FeedFetchEvent])
async def get_feed_events(
    feed_id: int, limit: int = Query(default=100, ge=1, le=500)
) -> list[FeedFetchEvent]:
    return await read_model.list_feed_events(get_conn(), feed_id, limit)


@router.get("/embedder/events", response_model=list[EmbedCallEvent])
async def get_embedder_events(
    limit: int = Query(default=100, ge=1, le=500),
) -> list[EmbedCallEvent]:
    return await read_model.list_embed_events(get_conn(), limit)


@router.get("/articles", response_model=list[ArticleListItem])
async def get_articles(
    request: Request,
    bucket: ArticleBucket = Query(...),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[ArticleListItem]:
    conn = get_conn()
    if bucket == "pending":
        return await read_model.list_articles_pending(conn, limit, offset)
    if bucket == "dead":
        return await read_model.list_articles_dead(conn, limit, offset)
    qdrant = getattr(request.app.state, "qdrant", None)
    qclient = getattr(qdrant, "client", None) if qdrant is not None else None
    return await read_model.list_articles_qdrant(qclient, limit, offset)


@router.get("/articles/{md5}", response_model=ArticleDetail)
async def get_article(
    md5: str,
    request: Request,
    bucket: ArticleBucket = Query(...),
) -> ArticleDetail:
    qdrant = getattr(request.app.state, "qdrant", None)
    qclient = getattr(qdrant, "client", None) if qdrant is not None else None
    detail = await read_model.get_article_detail(get_conn(), qclient, md5, bucket)
    if detail is None:
        raise HTTPException(status_code=404, detail="article not found")
    return detail
