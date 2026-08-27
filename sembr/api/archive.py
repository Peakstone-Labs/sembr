# SPDX-License-Identifier: Apache-2.0
"""Search endpoints for the permanent news archive.

Endpoints (prefix ``/api/archive``, gated by ``DashboardTokenMiddleware``):

- ``POST /search``  semantic or pure-filter retrieval over ``news_archive``
- ``GET  /stats``   point count + ingestion time range (conservation checks)

One endpoint serves both retrieval modes so the filter surface is defined
exactly once: a request with ``query`` runs vector search; without it, a
newest-first filtered listing (Qdrant ``scroll`` with ``order_by``). The
caller-facing name is always the ``news_archive`` alias — physical collection
names never appear in the contract.

Filter-mode pagination cannot use ``next_page_offset`` (Qdrant disables it
under ``order_by``); instead the response carries a cursor of the boundary
timestamp plus the point ids already returned AT that timestamp, which the
next request replays as ``order_by.start_from`` + ``must_not has_id``.
``start_from`` is value-inclusive, so only same-timestamp points can repeat —
the id list stays small. Semantic mode has no cursor; iterative
deepening uses ``exclude_ids``.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator

from sembr.db.feeds import get_feed_names
from sembr.db.sqlite import get_conn
from sembr.vector_store.news_archive import ARCHIVE_ALIAS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/archive", tags=["archive"])

# Bound on the cursor's same-timestamp exclusion list. Ingestion stamps at
# most one embedder batch (32 articles) with the same epoch second, so 500
# leaves ample margin; overflow is truncated (next page may repeat a few
# same-second points — clients dedupe by id) and logged.
_CURSOR_BOUNDARY_CAP = 500


class ArchiveCursor(BaseModel):
    before_ts: int
    boundary_ids: list[str] = Field(default_factory=list, max_length=_CURSOR_BOUNDARY_CAP)


class ArchiveSearchRequest(BaseModel):
    # Retrieval mode: semantic when `query` is set, filter-listing otherwise.
    query: str | None = Field(default=None, max_length=2000)
    limit: int = Field(default=20, ge=1, le=100)
    min_score: float | None = Field(default=None, ge=-1.0, le=1.0)
    exclude_ids: list[str] = Field(default_factory=list, max_length=1000)

    # Time windows. ingested_at_ts is reliable (stamped at embed time);
    # published_at_ts is parsed from source data and absent when the source
    # timestamp was missing or unparseable — absent never matches a Range.
    ingested_from_ts: int | None = None
    ingested_to_ts: int | None = None
    published_from_ts: int | None = None
    published_to_ts: int | None = None

    feed_ids: list[int] | None = None
    exclude_feed_ids: list[int] | None = None
    title_contains: str | None = Field(default=None, max_length=200)
    url_domains: list[str] | None = None
    min_body_len: int | None = Field(default=None, ge=0)
    # Intents that had matched the article while it lived in news_current
    # (captured at migration time; intents deleted before migration are gone).
    matched_intent_ids: list[int] | None = None
    langs: list[str] | None = None

    include_body: bool = True
    cursor: ArchiveCursor | None = None

    @model_validator(mode="after")
    def _mode_consistency(self) -> ArchiveSearchRequest:
        # Explicit 422 beats silently ignoring a parameter that only exists
        # in the other retrieval mode.
        if self.query is None and self.min_score is not None:
            raise ValueError("min_score requires a semantic query")
        if self.query is not None and self.cursor is not None:
            raise ValueError("cursor pagination is filter-mode only; use exclude_ids")
        return self


class ArchiveHit(BaseModel):
    id: str
    score: float | None = None
    title: str = ""
    url: str = ""
    body: str | None = None
    published_at: str | None = None
    published_at_ts: int | None = None
    ingested_at_ts: int | None = None
    feed_id: int | None = None
    feed_name: str | None = None
    url_domain: str | None = None
    lang: str | None = None
    body_len: int | None = None
    matched_intents: list[int] = Field(default_factory=list)
    embedding_model_version: str | None = None
    archived_at_ts: int | None = None


class ArchiveSearchResponse(BaseModel):
    mode: Literal["semantic", "filter"]
    hits: list[ArchiveHit]
    warnings: list[str]
    next_cursor: ArchiveCursor | None = None


class ArchiveStatsResponse(BaseModel):
    points_count: int
    earliest_ingested_at_ts: int | None = None
    latest_ingested_at_ts: int | None = None


def _build_filter(req: ArchiveSearchRequest) -> Any | None:
    """Translate request filters into a Qdrant Filter (None when empty)."""
    from qdrant_client.models import (  # noqa: PLC0415
        FieldCondition,
        Filter,
        HasIdCondition,
        MatchAny,
        MatchText,
        Range,
    )

    must: list[Any] = []
    must_not: list[Any] = []

    if req.ingested_from_ts is not None or req.ingested_to_ts is not None:
        must.append(
            FieldCondition(
                key="ingested_at_ts",
                range=Range(gte=req.ingested_from_ts, lte=req.ingested_to_ts),
            )
        )
    if req.published_from_ts is not None or req.published_to_ts is not None:
        must.append(
            FieldCondition(
                key="published_at_ts",
                range=Range(gte=req.published_from_ts, lte=req.published_to_ts),
            )
        )
    if req.feed_ids:
        must.append(FieldCondition(key="feed_id", match=MatchAny(any=req.feed_ids)))
    if req.title_contains:
        must.append(FieldCondition(key="title", match=MatchText(text=req.title_contains)))
    if req.url_domains:
        must.append(FieldCondition(key="url_domain", match=MatchAny(any=req.url_domains)))
    if req.min_body_len is not None:
        must.append(FieldCondition(key="body_len", range=Range(gte=req.min_body_len)))
    if req.matched_intent_ids:
        must.append(
            FieldCondition(key="matched_intents", match=MatchAny(any=req.matched_intent_ids))
        )
    if req.langs:
        must.append(FieldCondition(key="lang", match=MatchAny(any=req.langs)))

    if req.exclude_feed_ids:
        must_not.append(FieldCondition(key="feed_id", match=MatchAny(any=req.exclude_feed_ids)))
    if req.exclude_ids:
        must_not.append(HasIdCondition(has_id=req.exclude_ids))
    if req.cursor and req.cursor.boundary_ids:
        must_not.append(HasIdCondition(has_id=req.cursor.boundary_ids))

    if not must and not must_not:
        return None
    return Filter(must=must or None, must_not=must_not or None)


async def _feed_names_for(payloads: list[dict]) -> dict[int, str]:
    """Best-effort feed_id → name resolution; deleted feeds resolve to None
    downstream (known orphan display gap, out of scope here)."""
    feed_ids = sorted({p.get("feed_id") for p in payloads if isinstance(p.get("feed_id"), int)})
    if not feed_ids:
        return {}
    try:
        return await get_feed_names(get_conn(), feed_ids)
    except Exception:
        logger.warning("archive search: feed name resolution failed", exc_info=True)
        return {}


def _hit(
    pid: str, score: float | None, payload: dict, feed_names: dict[int, str], *, include_body: bool
) -> ArchiveHit:
    feed_id = payload.get("feed_id")
    return ArchiveHit(
        id=pid,
        score=score,
        title=payload.get("title") or "",
        url=payload.get("url") or "",
        body=(payload.get("body") if include_body else None),
        published_at=payload.get("published_at"),
        published_at_ts=payload.get("published_at_ts"),
        ingested_at_ts=payload.get("ingested_at_ts"),
        feed_id=feed_id if isinstance(feed_id, int) else None,
        feed_name=feed_names.get(feed_id) if isinstance(feed_id, int) else None,
        url_domain=payload.get("url_domain"),
        lang=payload.get("lang"),
        body_len=payload.get("body_len"),
        matched_intents=payload.get("matched_intents") or [],
        embedding_model_version=payload.get("embedding_model_version"),
        archived_at_ts=payload.get("archived_at_ts"),
    )


async def _semantic_search(request: Request, req: ArchiveSearchRequest) -> ArchiveSearchResponse:
    embedder = request.app.state.embedder
    if not embedder.is_loaded:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="embedder not ready"
        )
    query_text = (req.query or "")[: embedder.max_input_chars]
    try:
        vector = (await embedder.aembed([query_text]))[0]
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"query embedding failed: {exc}",
        ) from exc

    qdrant_client = request.app.state.qdrant.client
    try:
        response = await qdrant_client.query_points(
            collection_name=ARCHIVE_ALIAS,
            query=vector,
            limit=req.limit,
            score_threshold=req.min_score,
            query_filter=_build_filter(req),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"archive query failed: {exc}",
        ) from exc

    points = response.points
    payloads = [(str(p.id), p.score, p.payload or {}) for p in points]
    feed_names = await _feed_names_for([pl for _, _, pl in payloads])

    warnings: list[str] = []
    if payloads:
        # One representative check per response: a generation mismatch means
        # the archive holds vectors from a different embedding model than the
        # one that embedded this query, so similarity scores are not
        # trustworthy until the model-upgrade flow reconciles the space.
        archived_version = payloads[0][2].get("embedding_model_version")
        if archived_version and archived_version != embedder.model_version:
            warnings.append(
                f"embedding model generation mismatch: archive point has "
                f"{archived_version!r}, current embedder is "
                f"{embedder.model_version!r}; similarity scores are unreliable"
            )

    return ArchiveSearchResponse(
        mode="semantic",
        hits=[
            _hit(pid, score, pl, feed_names, include_body=req.include_body)
            for pid, score, pl in payloads
        ],
        warnings=warnings,
        next_cursor=None,
    )


async def _filter_listing(request: Request, req: ArchiveSearchRequest) -> ArchiveSearchResponse:
    qdrant_client = request.app.state.qdrant.client

    order_by: dict[str, Any] = {"key": "ingested_at_ts", "direction": "desc"}
    if req.cursor is not None:
        order_by["start_from"] = req.cursor.before_ts

    scroll_kwargs: dict[str, Any] = {
        "collection_name": ARCHIVE_ALIAS,
        "with_payload": True,
        "with_vectors": False,
        "order_by": order_by,
        "limit": req.limit,
    }
    scroll_filter = _build_filter(req)
    if scroll_filter is not None:
        scroll_kwargs["scroll_filter"] = scroll_filter

    try:
        points, _next_unused = await qdrant_client.scroll(**scroll_kwargs)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"archive listing failed: {exc}",
        ) from exc

    payloads = [(str(p.id), p.payload or {}) for p in points]
    feed_names = await _feed_names_for([pl for _, pl in payloads])

    next_cursor: ArchiveCursor | None = None
    if len(points) == req.limit and payloads:
        boundary_ts = payloads[-1][1].get("ingested_at_ts")
        if isinstance(boundary_ts, int):
            boundary_ids = [pid for pid, pl in payloads if pl.get("ingested_at_ts") == boundary_ts]
            # A same-second cluster can span pages: the previous cursor's
            # exclusions are still at this timestamp and must carry over or
            # they'd reappear on the next page.
            if req.cursor is not None and req.cursor.before_ts == boundary_ts:
                boundary_ids = req.cursor.boundary_ids + boundary_ids
            if len(boundary_ids) > _CURSOR_BOUNDARY_CAP:
                logger.warning(
                    "archive listing: cursor boundary overflow (%d ids at ts=%d), "
                    "truncating — next page may repeat a few same-second points",
                    len(boundary_ids),
                    boundary_ts,
                )
                boundary_ids = boundary_ids[-_CURSOR_BOUNDARY_CAP:]
            next_cursor = ArchiveCursor(before_ts=boundary_ts, boundary_ids=boundary_ids)

    return ArchiveSearchResponse(
        mode="filter",
        hits=[
            _hit(pid, None, pl, feed_names, include_body=req.include_body) for pid, pl in payloads
        ],
        warnings=[],
        next_cursor=next_cursor,
    )


@router.post("/search", response_model=ArchiveSearchResponse)
async def archive_search(request: Request, req: ArchiveSearchRequest) -> ArchiveSearchResponse:
    if req.query is not None:
        return await _semantic_search(request, req)
    return await _filter_listing(request, req)


@router.get("/stats", response_model=ArchiveStatsResponse)
async def archive_stats(request: Request) -> ArchiveStatsResponse:
    """Count + ingestion time range, independent of the TTL job's own log
    counters, so migration conservation can be reconciled from two sides."""
    qdrant_client = request.app.state.qdrant.client

    async def _edge_ts(direction: str) -> int | None:
        points, _ = await qdrant_client.scroll(
            collection_name=ARCHIVE_ALIAS,
            with_payload=True,
            with_vectors=False,
            order_by={"key": "ingested_at_ts", "direction": direction},
            limit=1,
        )
        if not points:
            return None
        ts = (points[0].payload or {}).get("ingested_at_ts")
        return ts if isinstance(ts, int) else None

    try:
        count_result = await qdrant_client.count(collection_name=ARCHIVE_ALIAS, exact=True)
        earliest = await _edge_ts("asc")
        latest = await _edge_ts("desc")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"archive stats failed: {exc}",
        ) from exc

    return ArchiveStatsResponse(
        points_count=count_result.count,
        earliest_ingested_at_ts=earliest,
        latest_ingested_at_ts=latest,
    )
