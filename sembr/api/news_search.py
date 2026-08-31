# SPDX-License-Identifier: Apache-2.0
"""Unified news retrieval endpoint.

``POST /api/news/search`` (prefix ``/api/news``, gated by
``DashboardTokenMiddleware``) is the single semantic / filter surface over the
whole news timeline. Storage is sharded — ``news_current`` holds the retention
window, ``news_archive`` holds everything the retention job has expired out of
it — and that sharding is an implementation detail the caller never sees:
no ``scope`` parameter, no per-segment cursor, no marker on a hit saying which
store it came from. One query in, one ranked list out.

One endpoint serves both retrieval modes so the filter surface is defined
exactly once: a request with ``query`` runs vector search across both stores
concurrently; without it, a newest-first filtered listing (Qdrant ``scroll``
with ``order_by``) does the same. Merging each store's top-``limit`` and
truncating is exactly the top-k of the union — every element of the union's
top-k ranks no worse inside its own store than it does in the union, so it
cannot be missing from that store's own top-``limit``.

Filter-mode pagination cannot use ``next_page_offset`` (Qdrant disables it
under ``order_by``); instead the response carries a cursor of the boundary
timestamp plus the point ids already returned AT that timestamp, which the
next request replays as ``order_by.start_from`` + ``must_not has_id``.
``ingested_at_ts`` is a global ordering key, so one cursor addresses both
stores — the same cursor goes to each and the next boundary is computed from
the merged page. ``start_from`` is value-inclusive, so only same-timestamp
points can repeat — the id list stays small. Semantic mode has no cursor;
iterative deepening uses ``exclude_ids``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sembr.db.feeds import get_feed_names
from sembr.db.match_seen import article_ids_for_intents, matched_intents_for_articles
from sembr.db.sqlite import get_conn
from sembr.vector_store.news import ALIAS_NAME
from sembr.vector_store.news_archive import ARCHIVE_ALIAS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/news", tags=["news-search"])

# Storage segments, in dedup precedence order. The retention job upserts into
# the archive and only then deletes from the current store, so mid-migration a
# point legitimately exists in both; keeping the CURRENT copy makes the merged
# result independent of how far that migration has progressed, and it is the
# copy whose matched-intent history is still live rather than frozen.
_SEGMENT_ALIASES: dict[str, str] = {"current": ALIAS_NAME, "archive": ARCHIVE_ALIAS}

# Bound on the cursor's same-timestamp exclusion list. Ingestion stamps at
# most one embedder batch (32 articles) with the same epoch second, so 500
# leaves ample margin; overflow is truncated (next page may repeat a few
# same-second points — clients dedupe by id) and logged.
_CURSOR_BOUNDARY_CAP = 500

# Ceiling on the article-id set derived from `matched_intent_ids` for the
# current segment. Production carries 7,717 match_seen rows across 10 intents
# (2,065 for the largest single intent), so 20k is ~2.6x headroom; past it the
# has_id request body reaches megabytes and Qdrant's own limits start to bite.
# Erroring beats silently truncating the id set, which would under-return with
# no indication.
_INTENT_ID_FILTER_CAP = 20_000

# Request fields that filter on a DERIVED payload key. Points written before
# the derived-field rollout carry none of these keys, and an absent key never
# matches — so while the backfill queue is non-empty these filters under-return
# and the caller has to be told. Note WHICH articles are affected: only
# `news_current` has a backfill queue, and it holds the retention window, so the
# gap is in RECENT coverage. Archived points were enriched point-by-point at
# migration time and are complete.
_DERIVED_FILTER_FIELDS = (
    "published_from_ts",
    "published_to_ts",
    "url_domains",
    "min_body_len",
    "langs",
)


class NewsCursor(BaseModel):
    # Round-tripped verbatim by clients — a mistyped field name must error,
    # not silently produce a cursor that never excludes anything.
    model_config = ConfigDict(extra="forbid")

    before_ts: int
    boundary_ids: list[str] = Field(default_factory=list, max_length=_CURSOR_BOUNDARY_CAP)


class NewsSearchRequest(BaseModel):
    # Filter params come in singular/plural pairs (feed_ids, langs, ...); a
    # mistyped name silently dropped would return the WHOLE corpus as if
    # filtered. Reject unknown fields instead.
    model_config = ConfigDict(extra="forbid")

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
    # Intents that matched the article. Live for articles still inside the
    # retention window, frozen at migration time for older ones — see the
    # agent skill; the difference is freshness, not meaning.
    matched_intent_ids: list[int] | None = Field(default=None, max_length=1000)
    langs: list[str] | None = None

    include_body: bool = True
    cursor: NewsCursor | None = None

    @field_validator("query", mode="before")
    @classmethod
    def _blank_query_is_filter_mode(cls, v: Any) -> Any:
        # "" / "   " is not a semantic query: normalize to None (filter mode)
        # instead of embedding whitespace and returning meaningless scores.
        # (Unlike the include-filters below, this degradation is visible: the
        # response comes back with mode="filter".)
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("feed_ids", "url_domains", "matched_intent_ids", "langs")
    @classmethod
    def _no_empty_include_list(cls, v: Any, info: Any) -> Any:
        # [] would be truthiness-dropped by the filter builder and return
        # EVERYTHING — indistinguishable from a real match. "restrict to the
        # empty set" vs "forgot to fill in" is ambiguous, and an agent
        # filtering ids down to [] is a normal program outcome, not a typo.
        # Omit the field to skip the filter. (exclude_* lists are exempt:
        # empty exclusion == no exclusion, no ambiguity.)
        if v is not None and len(v) == 0:
            raise ValueError(
                f"{info.field_name}: [] is ambiguous — omit the field to skip the filter"
            )
        return v

    @field_validator("title_contains")
    @classmethod
    def _no_blank_title_contains(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError(
                "title_contains: blank is ambiguous — omit the field to skip the filter"
            )
        return v

    @model_validator(mode="after")
    def _mode_consistency(self) -> NewsSearchRequest:
        # Explicit 422 beats silently ignoring a parameter that only exists
        # in the other retrieval mode.
        if self.query is None and self.min_score is not None:
            raise ValueError("min_score requires a semantic query")
        if self.query is not None and self.cursor is not None:
            raise ValueError("cursor pagination is filter-mode only; use exclude_ids")
        return self


class NewsHit(BaseModel):
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


class NewsSearchResponse(BaseModel):
    mode: Literal["semantic", "filter"]
    hits: list[NewsHit]
    warnings: list[str]
    next_cursor: NewsCursor | None = None


_FEED_NAME_WARNING = (
    "feed name resolution failed; feed_name fields are null but do not imply the feeds were deleted"
)

_MATCHED_INTENTS_WARNING = (
    "matched-intent lookup failed; matched_intents may be empty for some hits and does not "
    "imply those articles never matched an intent"
)

_BACKFILL_PENDING_WARNING = (
    "derived fields are still being backfilled; filters on publication time, language, "
    "url domain or body length may miss articles ingested before this deployment. "
    "Those articles are in the RECENT window, not the deep archive — archived "
    "articles were enriched individually and are complete. Filtering on "
    "ingested_at_ts instead is unaffected and can be used as a complete fallback"
)

_INTENT_SET_EMPTY_WARNING = (
    "matched_intent_ids matched no article in the recent window, so only archived "
    "matches are returned; an empty result does not mean the intent never matched. "
    "The live match table is reset whenever an intent's text or sub-texts change, "
    "when a summary-history row is deleted, and when the intent itself is deleted"
)


# Only codes where Qdrant explicitly rejected the request CONTENT count as
# caller errors. 404 (collection/alias missing — service not ready), 408/409/
# 429 (retryable service-side states) must map to 503: the agent callers
# treat 400 as "don't retry, fix your parameters", which would send them
# editing filters instead of reporting an outage.
_CALLER_ERROR_CODES = frozenset({400, 422})


def _qdrant_http_error(exc: Exception, context: str) -> HTTPException:
    """Map a Qdrant client failure to an HTTP status (whitelist, not range)."""
    # Truncate: UnexpectedResponse stringifies with the raw response body
    # attached; the operator gets the full traceback via server logs.
    raw = str(exc)
    msg = f"{context}: {raw[:200]}"
    if getattr(exc, "status_code", None) in _CALLER_ERROR_CODES:
        # One carve-out from the whitelist: a vector-dimension rejection is a
        # 400 from Qdrant but not something the caller can fix. It means an
        # alias still targets a previous model generation, which bootstrap
        # logs and deliberately does not repair. Reporting it as a caller
        # error sends the agent editing filters forever. Substring matching on
        # an upstream message is brittle, but the failure it prevents (an
        # unfixable request presented as fixable) is worse than the failure it
        # risks (an outage reported as an outage one release later).
        if "dimension" in raw.lower():
            return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=msg)
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=msg)


def _build_filter(req: NewsSearchRequest, *, segment: str, intent_article_ids: list[str]) -> Any:
    """Translate request filters into a Qdrant Filter (None when empty).

    Segment-aware in exactly one place: the matched-intent history lives in the
    payload for archived points and in SQLite for points still in the retention
    window, so the same request field compiles to two different conditions.
    Everything else is identical by construction — one definition, so the two
    halves of the timeline cannot drift apart.
    """
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
        if segment == "archive":
            must.append(
                FieldCondition(key="matched_intents", match=MatchAny(any=req.matched_intent_ids))
            )
        else:
            # Never reached with an empty list: the caller skips the segment
            # outright in that case, because `has_id: []` has no defined
            # "restrict to nothing" meaning in Qdrant.
            must.append(HasIdCondition(has_id=intent_article_ids))
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


async def _feed_names_for(payloads: list[dict]) -> tuple[dict[int, str], bool]:
    """Best-effort feed_id → name resolution.

    Returns ``(names, ok)`` — ok=False means the lookup itself failed, which
    callers surface as a response warning so "feed_name is null" stays
    distinguishable from "feed was deleted" (the known orphan display gap,
    out of scope here).
    """
    feed_ids = sorted({p.get("feed_id") for p in payloads if isinstance(p.get("feed_id"), int)})
    if not feed_ids:
        return {}, True
    try:
        return await get_feed_names(get_conn(), feed_ids), True
    except Exception:
        logger.warning("news search: feed name resolution failed", exc_info=True)
        return {}, False


def _hit(
    pid: str,
    score: float | None,
    payload: dict,
    feed_names: dict[int, str],
    matched_intents: list[int],
    *,
    include_body: bool,
) -> NewsHit:
    feed_id = payload.get("feed_id")
    return NewsHit(
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
        matched_intents=matched_intents,
        embedding_model_version=payload.get("embedding_model_version"),
    )


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------


class _SegmentRows:
    """One segment's contribution: rows plus its own representative version."""

    __slots__ = ("model_version", "rows")

    def __init__(self, rows: list[tuple[str, float | None, dict]]) -> None:
        self.rows = rows
        # Read per segment, not once per response: after merging, a
        # representative drawn from the matching store would hide a
        # different-generation store whose scores are not comparable.
        # (Residual: a store that itself straddles a model switch can still
        # slip past a single sample — each hit carries its own version for
        # callers that need certainty.)
        self.model_version = rows[0][2].get("embedding_model_version") if rows else None


async def _gather_segments(coros: dict[str, Any]) -> dict[str, _SegmentRows]:
    """Run the segment queries concurrently; any failure fails the request.

    Returning whatever one segment managed to produce would be silent
    under-recall wearing a 200 — the exact failure this endpoint's derived
    fields, backfill job and pending warning all exist to prevent. Siblings are
    cancelled and awaited so a failed fan-out leaves no orphaned task and no
    "exception was never retrieved" noise.
    """
    tasks = {name: asyncio.ensure_future(coro) for name, coro in coros.items()}
    try:
        results = await asyncio.gather(*tasks.values())
    except BaseException:
        for task in tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        raise
    return dict(zip(tasks.keys(), results, strict=True))


async def _intent_article_ids(req: NewsSearchRequest) -> list[str]:
    """Article ids for the current segment's matched-intent filter.

    A failure here fails the whole request. Degrading to "no intent condition"
    would answer a narrowly-scoped question with the entire corpus, which is
    strictly worse than an error — it is the same failure mode the empty-list
    validator above exists to reject.
    """
    if not req.matched_intent_ids:
        return []
    try:
        ids = await article_ids_for_intents(get_conn(), req.matched_intent_ids)
    except Exception as exc:
        logger.warning("news search: intent article-id lookup failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"matched-intent lookup failed: {str(exc)[:200]}",
        ) from exc
    if len(ids) > _INTENT_ID_FILTER_CAP:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"matched_intent_ids selects {len(ids)} articles, over the "
                f"{_INTENT_ID_FILTER_CAP} limit; request fewer intents. "
                f"This limit is independent of the time window — narrowing "
                f"ingested_from_ts/ingested_to_ts will not reduce it."
            ),
        )
    return ids


def _segments_to_query(req: NewsSearchRequest, intent_article_ids: list[str]) -> list[str]:
    """Which segments this request can possibly match.

    An intent filter that resolves to no articles restricts the current
    segment to the empty set. That is expressed by not querying it at all —
    `has_id: []` is not a defined way to say "nothing".
    """
    if req.matched_intent_ids and not intent_article_ids:
        return ["archive"]
    return list(_SEGMENT_ALIASES)


def _pending_warning(request: Request, req: NewsSearchRequest) -> str | None:
    """Derived-field filters under-return until the backfill converges.

    A missing or unknown flag counts as pending. The alternative — assuming
    "no news is good news" — would restore exactly the silent gap the flag
    exists to close, for the whole window between process start and the first
    successful backfill round.
    """
    if not any(getattr(req, field) is not None for field in _DERIVED_FILTER_FIELDS):
        return None
    pending = getattr(request.app.state, "news_derived_backfill_pending", None)
    if pending == 0:
        return None
    return _BACKFILL_PENDING_WARNING


def _version_warning(segments: dict[str, _SegmentRows], embedder_version: str) -> str | None:
    stale = sorted(
        {s.model_version for s in segments.values() if s.model_version} - {embedder_version}
    )
    if not stale:
        return None
    return (
        f"embedding model generation mismatch: stored points report {stale}, "
        f"current embedder is {embedder_version!r}; similarity scores are unreliable"
    )


async def _fill_matched_intents(
    rows_by_segment: dict[str, list[tuple[str, Any, dict]]],
    ordered_ids: list[str],
) -> tuple[dict[str, list[int]], bool]:
    """Matched intents per hit id — payload for archived hits, SQLite for live
    ones (the current store keeps no such payload key; it would need an atomic
    array append Qdrant does not offer).

    Returns ``(by_id, ok)``. Unlike the fan-out, a failure here degrades: this
    fills one display field and cannot change which articles were returned.
    """
    by_id: dict[str, list[int]] = {}
    wanted = set(ordered_ids)
    current_ids = [pid for pid, _s, _p in rows_by_segment.get("current", []) if pid in wanted]
    for pid, _score, payload in rows_by_segment.get("archive", []):
        if pid in wanted:
            value = payload.get("matched_intents")
            # sorted() on both sides: the field claims one meaning across the
            # whole timeline, and a point that is briefly in both stores must
            # not change shape depending on which copy won the dedupe.
            by_id[pid] = sorted(value) if isinstance(value, list) else []
    if not current_ids:
        return by_id, True
    try:
        live = await matched_intents_for_articles(get_conn(), current_ids)
    except Exception:
        logger.warning("news search: matched-intent fill failed", exc_info=True)
        for pid in current_ids:
            by_id.setdefault(pid, [])
        return by_id, False
    for pid, intents in live.items():
        by_id[pid] = sorted(intents)
    return by_id, True


# ---------------------------------------------------------------------------
# Semantic mode
# ---------------------------------------------------------------------------


async def _semantic_segment(
    client: Any,
    alias: str,
    req: NewsSearchRequest,
    vector: list[float],
    query_filter: Any,
) -> _SegmentRows:
    try:
        response = await client.query_points(
            collection_name=alias,
            query=vector,
            limit=req.limit,
            score_threshold=req.min_score,
            query_filter=query_filter,
        )
    except Exception as exc:
        # Context deliberately names no store: which shard failed is an
        # implementation detail, and the caller's decision (retry vs report)
        # is driven by the status code.
        raise _qdrant_http_error(exc, "news query failed") from exc
    return _SegmentRows([(str(p.id), p.score, p.payload or {}) for p in response.points])


async def _semantic_search(request: Request, req: NewsSearchRequest) -> NewsSearchResponse:
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

    intent_ids = await _intent_article_ids(req)
    client = request.app.state.qdrant.client
    segments = await _gather_segments(
        {
            name: _semantic_segment(
                client,
                _SEGMENT_ALIASES[name],
                req,
                vector,
                _build_filter(req, segment=name, intent_article_ids=intent_ids),
            )
            for name in _segments_to_query(req, intent_ids)
        }
    )

    rows_by_segment = {name: seg.rows for name, seg in segments.items()}
    merged = _merge_rows(rows_by_segment, key=lambda row: row[1] if row[1] is not None else 0.0)
    page = merged[: req.limit]

    return await _respond(
        request, req, "semantic", page, rows_by_segment, segments, embedder, intent_ids
    )


# ---------------------------------------------------------------------------
# Filter mode
# ---------------------------------------------------------------------------


async def _listing_segment(
    client: Any,
    alias: str,
    req: NewsSearchRequest,
    scroll_filter: Any,
) -> _SegmentRows:
    order_by: dict[str, Any] = {"key": "ingested_at_ts", "direction": "desc"}
    if req.cursor is not None:
        order_by["start_from"] = req.cursor.before_ts

    scroll_kwargs: dict[str, Any] = {
        "collection_name": alias,
        "with_payload": True,
        "with_vectors": False,
        "order_by": order_by,
        "limit": req.limit,
    }
    if scroll_filter is not None:
        scroll_kwargs["scroll_filter"] = scroll_filter

    try:
        points, _next_unused = await client.scroll(**scroll_kwargs)
    except Exception as exc:
        raise _qdrant_http_error(exc, "news listing failed") from exc
    return _SegmentRows([(str(p.id), None, p.payload or {}) for p in points])


def _listing_sort_key(row: tuple[str, Any, dict]) -> tuple[int, int]:
    ts = row[2].get("ingested_at_ts")
    # Points without a usable timestamp sort last under reverse=True rather
    # than crashing the comparison or jumping to the head.
    return (1, ts) if isinstance(ts, int) else (0, 0)


async def _filter_listing(request: Request, req: NewsSearchRequest) -> NewsSearchResponse:
    intent_ids = await _intent_article_ids(req)
    client = request.app.state.qdrant.client
    segments = await _gather_segments(
        {
            name: _listing_segment(
                client,
                _SEGMENT_ALIASES[name],
                req,
                _build_filter(req, segment=name, intent_article_ids=intent_ids),
            )
            for name in _segments_to_query(req, intent_ids)
        }
    )

    rows_by_segment = {name: seg.rows for name, seg in segments.items()}
    merged = _merge_rows(rows_by_segment, key=_listing_sort_key)
    page = merged[: req.limit]

    return await _respond(request, req, "filter", page, rows_by_segment, segments, None, intent_ids)


def _next_cursor_for(
    req: NewsSearchRequest, page: list[tuple[str, Any, dict]]
) -> tuple[NewsCursor | None, list[str]]:
    """Cursor for the MERGED page, plus any warnings it produced.

    The full-page test is applied after merge and truncation, not per segment:
    two segments each returning `limit` rows still yield one page of `limit`,
    and it is that page's tail that the next request must resume from.
    """
    warnings: list[str] = []
    if len(page) != req.limit or not page:
        return None, warnings
    boundary_ts = page[-1][2].get("ingested_at_ts")
    if not isinstance(boundary_ts, int):
        return None, warnings

    boundary_ids = [pid for pid, _s, pl in page if pl.get("ingested_at_ts") == boundary_ts]
    # A same-second cluster can span pages: the previous cursor's exclusions
    # are still at this timestamp and must carry over or they'd reappear on
    # the next page.
    if req.cursor is not None and req.cursor.before_ts == boundary_ts:
        boundary_ids = req.cursor.boundary_ids + boundary_ids
    if len(boundary_ids) > _CURSOR_BOUNDARY_CAP:
        # Truncation drops entries from the EXCLUSION list: points at this
        # timestamp re-qualify on the next page, and since the descending
        # order fills the page with that second first, paging may stop
        # advancing past it entirely. Must be visible to the caller, not just
        # the server log.
        logger.warning(
            "news listing: cursor boundary overflow (%d ids at ts=%d), truncating",
            len(boundary_ids),
            boundary_ts,
        )
        boundary_ids = boundary_ids[-_CURSOR_BOUNDARY_CAP:]
        warnings.append(
            f"more than {_CURSOR_BOUNDARY_CAP} points share "
            f"ingested_at_ts={boundary_ts}; pagination cannot exclude "
            f"them all — results at this timestamp may repeat and "
            f"paging past it may not advance"
        )
    return NewsCursor(before_ts=boundary_ts, boundary_ids=boundary_ids), warnings


# ---------------------------------------------------------------------------
# Merge + response assembly (shared by both modes)
# ---------------------------------------------------------------------------


def _merge_rows(
    rows_by_segment: dict[str, list[tuple[str, Any, dict]]],
    *,
    key: Any,
) -> list[tuple[str, Any, dict]]:
    """Dedupe by point id (current wins), then sort by the mode's ranking key."""
    seen: set[str] = set()
    pooled: list[tuple[str, Any, dict]] = []
    for name in _SEGMENT_ALIASES:
        for row in rows_by_segment.get(name, []):
            if row[0] in seen:
                continue
            seen.add(row[0])
            pooled.append(row)
    pooled.sort(key=key, reverse=True)
    return pooled


async def _respond(
    request: Request,
    req: NewsSearchRequest,
    mode: Literal["semantic", "filter"],
    page: list[tuple[str, Any, dict]],
    rows_by_segment: dict[str, list[tuple[str, Any, dict]]],
    segments: dict[str, _SegmentRows],
    embedder: Any,
    intent_article_ids: list[str],
) -> NewsSearchResponse:
    warnings: list[str] = []

    # Skipping the live segment is the only correct way to express "restrict to
    # the empty set", but on its own it is indistinguishable from "this intent
    # never matched anything recent" — and the live match table is reset by
    # three routine operations, not just intent deletion. Say so, or the caller
    # reads a reset as a fact about the news.
    if req.matched_intent_ids and not intent_article_ids:
        warnings.append(_INTENT_SET_EMPTY_WARNING)

    feed_names, feed_names_ok = await _feed_names_for([pl for _pid, _s, pl in page])
    if not feed_names_ok:
        warnings.append(_FEED_NAME_WARNING)

    page_ids = [pid for pid, _s, _pl in page]
    intents_by_id, intents_ok = await _fill_matched_intents(rows_by_segment, page_ids)
    if not intents_ok:
        warnings.append(_MATCHED_INTENTS_WARNING)

    if embedder is not None:
        version_warning = _version_warning(segments, embedder.model_version)
        if version_warning:
            warnings.append(version_warning)

    next_cursor: NewsCursor | None = None
    if mode == "filter":
        next_cursor, cursor_warnings = _next_cursor_for(req, page)
        warnings.extend(cursor_warnings)

    pending_warning = _pending_warning(request, req)
    if pending_warning:
        warnings.append(pending_warning)

    return NewsSearchResponse(
        mode=mode,
        hits=[
            _hit(
                pid,
                score,
                payload,
                feed_names,
                intents_by_id.get(pid, []),
                include_body=req.include_body,
            )
            for pid, score, payload in page
        ],
        warnings=warnings,
        next_cursor=next_cursor,
    )


@router.post("/search", response_model=NewsSearchResponse)
async def news_search(request: Request, req: NewsSearchRequest) -> NewsSearchResponse:
    if req.query is not None:
        return await _semantic_search(request, req)
    return await _filter_listing(request, req)
