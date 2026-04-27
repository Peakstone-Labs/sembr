"""GET /health.

设计决策 #5 / #6: real-time probe (no cache); 200 iff (qdrant_ok ∧ sqlite_ok).
embedder 字段 fixed `"not_loaded"`, does not affect status code (requirements 成功标准 #2).
"""
from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from sembr.db.sqlite import sqlite_ok as _sqlite_ok

router = APIRouter()


@router.get("/health")
async def health(request: Request, response: Response) -> dict:
    qdrant = request.app.state.qdrant
    qdrant_ok = await qdrant.ping()

    sqlite_ok_value = await _sqlite_ok()

    components = {
        "qdrant": "ok" if qdrant_ok else "down",
        "sqlite": "ok" if sqlite_ok_value else "down",
        "embedder": "not_loaded",
    }
    overall_ok = qdrant_ok and sqlite_ok_value
    response.status_code = (
        status.HTTP_200_OK if overall_ok else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    return {"status": "ok" if overall_ok else "degraded", "components": components}
