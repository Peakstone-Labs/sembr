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
from datetime import datetime, time as dtime, timedelta, timezone

from apscheduler.jobstores.base import JobLookupError
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from sembr.api.settings import require_header_token
from sembr.collector.newsapi import RECOMMENDED_SOURCES as _NEWSAPI_RECOMMENDED
from sembr.collector.scheduler import (
    NEWSAPI_MASTER_JOB_ID,
    SOURCE_REGISTRY,
    ensure_newsapi_master_job,
)
from sembr.config import get_settings
from sembr.dashboard import read_model
from sembr.dashboard.schemas import (
    ArticleBucket,
    ArticleDetail,
    ArticleListItem,
    ConfigResponse,
    EmbedCallEvent,
    FeedFetchEvent,
    FeedListResponse,
    SnapshotResponse,
    SourceSchemaResponse,
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
        display_timezone=settings.display_timezone,
    )


@router.get("/snapshot", response_model=SnapshotResponse)
async def get_snapshot(request: Request) -> SnapshotResponse:
    qdrant = getattr(request.app.state, "qdrant", None)
    embedder = getattr(request.app.state, "embedder", None)
    metrics_collector = getattr(request.app.state, "metrics_collector", None)
    return await read_model.build_snapshot(get_conn(), qdrant, embedder, metrics_collector)


@router.get("/feeds", response_model=FeedListResponse)
async def get_feeds_paged(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    tag: str | None = Query(default=None, max_length=32),
    q: str | None = Query(default=None, max_length=200),
) -> FeedListResponse:
    settings = get_settings()
    scheduler = getattr(request.app.state, "scheduler", None)
    return await read_model.list_feeds_with_meta(
        get_conn(),
        limit=limit,
        offset=offset,
        tag=tag,
        q=q,
        proxy_hosts=settings.proxy_hosts_set,
        scheduler=scheduler,
    )


@router.get("/feeds/{feed_id}/events", response_model=list[FeedFetchEvent])
async def get_feed_events(
    feed_id: int,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[FeedFetchEvent]:
    return await read_model.list_feed_events(get_conn(), feed_id, limit, offset)


@router.get("/feeds/{feed_id}/articles", response_model=list[ArticleListItem])
async def get_feed_articles(
    feed_id: int,
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[ArticleListItem]:
    # Symmetric with /feeds/{id}/events: 404 on missing feed instead of 200 + [].
    # Avoids confusing UX where a deleted feed's articles silently appear empty.
    # (Loop 2 review #🟢-3)
    conn = get_conn()
    async with conn.execute("SELECT 1 FROM feeds WHERE id=?", (feed_id,)) as cur:
        if not await cur.fetchone():
            raise HTTPException(status_code=404, detail="feed not found")
    qdrant = getattr(request.app.state, "qdrant", None)
    qclient = getattr(qdrant, "client", None) if qdrant is not None else None
    return await read_model.list_feed_articles_qdrant(qclient, feed_id, limit=limit, offset=offset)


@router.get("/sources/newsapi/recommended_sources")
async def get_newsapi_recommended_sources() -> list[dict]:
    """D14: combobox datalist source for the create-feed modal. Static list
    is cheap to return on every modal open; frontend caches client-side."""
    return list(_NEWSAPI_RECOMMENDED)


class NewsApiFireResponse(BaseModel):
    job_id: str
    next_run_time: str
    note: str


@router.post(
    "/sources/newsapi/fire",
    response_model=NewsApiFireResponse,
    dependencies=[Depends(require_header_token)],
)
async def post_newsapi_fire(request: Request) -> NewsApiFireResponse:
    """Trigger the newsapi master tick immediately instead of waiting for
    the next scheduled fire. Costs 1 NewsAPI token like a regular tick.

    Header-token guarded (CSRF) — the action consumes paid quota, so it
    matches the same auth posture as POST /api/settings/save.

    Behavior: if the master job is registered (i.e. at least one enabled
    newsapi feed exists), modify_job sets next_run_time=now so the
    scheduler picks it up on the next executor tick. APScheduler's
    max_instances=1 guard handles the corner case of a tick currently
    running — the manual fire queues but won't double-up. If the master
    job is absent (no enabled newsapi feeds), 404."""
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="scheduler not initialised",
        )
    try:
        job = scheduler.get_job(NEWSAPI_MASTER_JOB_ID)
    except Exception as exc:  # noqa: BLE001
        logger.error("newsapi fire: get_job failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"scheduler error: {exc}",
        ) from exc
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "newsapi master job not registered — create at least one "
                "enabled feed with source_type='newsapi' first"
            ),
        )
    now = datetime.now(timezone.utc)
    try:
        scheduler.modify_job(NEWSAPI_MASTER_JOB_ID, next_run_time=now)
    except JobLookupError as exc:
        # Race: job removed between get_job and modify_job — surface as 404.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="newsapi master job disappeared mid-request",
        ) from exc
    return NewsApiFireResponse(
        job_id=NEWSAPI_MASTER_JOB_ID,
        next_run_time=now.isoformat(),
        note=(
            "master tick scheduled for immediate execution; check "
            "/api/dashboard/feeds/<id>/events for per-feed fetch_event rows"
        ),
    )


@router.get("/sources/schemas", response_model=SourceSchemaResponse)
async def get_source_schemas() -> SourceSchemaResponse:
    """D15: source_type → JSON-Schema map. Frontend uses this to render the
    create-feed form dynamically. Read directly from SOURCE_REGISTRY so a
    plugin registered via entry_points appears without a code change here."""
    schemas: dict[str, dict] = {}
    for stype, cls in SOURCE_REGISTRY.items():
        try:
            schemas[stype] = cls.config_schema()
        except Exception as exc:
            logger.warning("source %r config_schema() failed: %s", stype, exc)
    return SourceSchemaResponse(schemas=schemas)


@router.get("/embedder/events", response_model=list[EmbedCallEvent])
async def get_embedder_events(
    limit: int = Query(default=100, ge=1, le=500),
) -> list[EmbedCallEvent]:
    return await read_model.list_embed_events(get_conn(), limit)


def _parse_iso_date(value: str, *, end_of_day: bool = False) -> datetime:
    """Parse an ISO-8601 date string ("YYYY-MM-DD") into a UTC datetime.

    The qdrant articles filter exposes ``ingested_from`` / ``ingested_to`` as
    date-granularity inputs (the dashboard's date pickers emit YYYY-MM-DD).
    ``end_of_day`` shifts to the next-day boundary so the filter range stays
    half-open ``[from, to)`` in seconds while presenting as inclusive in UI.
    """
    try:
        d = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid date: {value!r} (expected YYYY-MM-DD)",
        ) from exc
    if end_of_day:
        return datetime.combine(d, dtime.min, tzinfo=timezone.utc) + timedelta(days=1)
    return datetime.combine(d, dtime.min, tzinfo=timezone.utc)


@router.get("/articles", response_model=list[ArticleListItem])
async def get_articles(
    request: Request,
    bucket: ArticleBucket = Query(...),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    ingested_from: str | None = Query(
        default=None,
        description="ISO date YYYY-MM-DD; only valid with bucket=qdrant",
    ),
    ingested_to: str | None = Query(
        default=None,
        description="ISO date YYYY-MM-DD; range is half-open [from, to+1d)",
    ),
    feed_id: int | None = Query(
        default=None,
        description="filter by feed_id; only valid with bucket=qdrant",
    ),
    title_q: str | None = Query(
        default=None,
        max_length=200,
        description="MatchText against the title text-index",
    ),
) -> list[ArticleListItem]:
    conn = get_conn()
    qdrant_filter_params = (ingested_from, ingested_to, feed_id, title_q)
    has_qdrant_filter = any(p is not None for p in qdrant_filter_params)

    if bucket in ("pending", "dead"):
        # qdrant-only filter params on a sqlite bucket are a client bug;
        # 422 surfaces it instead of silently dropping the params (D7).
        if has_qdrant_filter:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "ingested_from / ingested_to / feed_id / title_q are only "
                    "valid with bucket=qdrant"
                ),
            )
        if bucket == "pending":
            return await read_model.list_articles_pending(conn, limit, offset)
        return await read_model.list_articles_dead(conn, limit, offset)

    qdrant = getattr(request.app.state, "qdrant", None)
    qclient = getattr(qdrant, "client", None) if qdrant is not None else None
    parsed_from = _parse_iso_date(ingested_from) if ingested_from else None
    parsed_to = _parse_iso_date(ingested_to, end_of_day=True) if ingested_to else None
    return await read_model.list_articles_qdrant(
        qclient,
        limit,
        offset,
        ingested_from=parsed_from,
        ingested_to=parsed_to,
        feed_id=feed_id,
        title_q=title_q,
    )


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
