"""SSE log-stream and level-control endpoints for the dashboard Logs tab.

Routes (all under /api/dashboard/logs):
  GET  /tags          → list of {name, level} for all 7 tags
  GET  /stream?tag=   → SSE text/event-stream (cookie auth — EventSource can't send headers)
  PUT  /level         → change per-tag level in LogBus (process-memory only, no persistence)
"""
from __future__ import annotations

import asyncio
import json
import logging
from enum import Enum
from typing import Any, AsyncGenerator

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from sembr.logbus.bus import get_bus
from sembr.logbus.router import ALL_TAGS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dashboard/logs", tags=["logs"])

_LEVEL_MAP: dict[str, int] = {
    "DEBUG":   logging.DEBUG,
    "INFO":    logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR":   logging.ERROR,
}

_LEVEL_NAMES = list(_LEVEL_MAP.keys())

# Tags that have associated stdlib loggers whose level must be kept in sync
# (see design L7 / R7).
_THIRD_PARTY_LOGGER_TAGS: dict[str, list[str]] = {
    "http": ["httpx", "httpcore", "uvicorn.access"],
}


class LevelEnum(str, Enum):
    DEBUG   = "DEBUG"
    INFO    = "INFO"
    WARNING = "WARNING"
    ERROR   = "ERROR"


class TagName(str, Enum):
    collector = "collector"
    embedder  = "embedder"
    matcher   = "matcher"
    notifier  = "notifier"
    api       = "api"
    scheduler = "scheduler"
    http      = "http"


class LevelRequest(BaseModel):
    tag: TagName
    level: LevelEnum


# ---------------------------------------------------------------------------
# GET /tags
# ---------------------------------------------------------------------------

@router.get("/tags")
async def get_tags() -> dict[str, Any]:
    return {
        "tags": get_bus().tag_info(),
        "available_levels": _LEVEL_NAMES,
    }


# ---------------------------------------------------------------------------
# PUT /level
# ---------------------------------------------------------------------------

@router.put("/level")
async def put_level(body: LevelRequest) -> Response:
    tag = body.tag.value
    level_int = _LEVEL_MAP[body.level.value]
    bus = get_bus()
    bus.set_tag_level(tag, level_int)

    # Sync third-party loggers whose records map to this tag (design L7 / R7).
    for logger_name in _THIRD_PARTY_LOGGER_TAGS.get(tag, []):
        logging.getLogger(logger_name).setLevel(level_int)

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# GET /stream   (SSE)
# ---------------------------------------------------------------------------

@router.get("/stream")
async def stream_logs(request: Request, tag: str = Query(default="api")) -> StreamingResponse:
    if tag not in ALL_TAGS:
        from fastapi import HTTPException  # noqa: PLC0415
        raise HTTPException(status_code=422, detail=f"Unknown tag: {tag!r}")

    return StreamingResponse(
        _log_generator(tag, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )


_PING_INTERVAL = 15.0   # seconds between SSE keepalive comments
_POLL_INTERVAL = 1.0    # disconnect-check granularity inside the ping window


async def _log_generator(tag: str, request: Request) -> AsyncGenerator[str, None]:
    bus = get_bus()
    q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=2000)

    snapshot = bus.subscribe(q)
    try:
        # Emit buffered history for the requested tag only.
        for entry in snapshot:
            if entry["tag"] == tag:
                yield f"event: log\ndata: {json.dumps(entry)}\n\n"

        yield "event: history-end\ndata: {}\n\n"

        # Stream live entries; check disconnect every _POLL_INTERVAL seconds
        # to avoid blocking until the 15-second ping window expires.
        ping_deadline = asyncio.get_event_loop().time() + _PING_INTERVAL
        while True:
            if await request.is_disconnected():
                break
            try:
                entry = await asyncio.wait_for(q.get(), timeout=_POLL_INTERVAL)
                if entry is not None and entry["tag"] == tag:
                    yield f"event: log\ndata: {json.dumps(entry)}\n\n"
                    ping_deadline = asyncio.get_event_loop().time() + _PING_INTERVAL
            except asyncio.TimeoutError:
                if asyncio.get_event_loop().time() >= ping_deadline:
                    yield ": ping\n\n"
                    ping_deadline = asyncio.get_event_loop().time() + _PING_INTERVAL
    except asyncio.CancelledError:
        pass
    finally:
        bus.unsubscribe(q)
