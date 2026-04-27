"""GET /health.

设计决策 #5 / #6: real-time probe (no cache); 200 iff (qdrant_ok ∧ sqlite_ok ∧ embedder_ok).
Embedder reports three states: "loading" | "ok" | "error" (D18).
"""
from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from sembr.db.sqlite import sqlite_ok as _sqlite_ok

router = APIRouter()


@router.get("/health")
async def health(request: Request, response: Response) -> dict:
    # Guard against probes arriving before lifespan finishes assigning state (🟡-3).
    # Returning 503 (not 500/AttributeError) keeps K8s readiness semantics intact.
    qdrant = getattr(request.app.state, "qdrant", None)
    embedder = getattr(request.app.state, "embedder", None)
    if qdrant is None or embedder is None:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "starting", "components": {"qdrant": "starting", "sqlite": "starting", "embedder": "starting"}}

    qdrant_ok = await qdrant.ping()

    sqlite_ok_value = await _sqlite_ok()

    embedder_status = embedder.status  # "loading" | "ok" | "error"
    embedder_ok = embedder_status == "ok"

    components = {
        "qdrant": "ok" if qdrant_ok else "down",
        "sqlite": "ok" if sqlite_ok_value else "down",
        "embedder": embedder_status,
    }
    overall_ok = qdrant_ok and sqlite_ok_value and embedder_ok
    response.status_code = (
        status.HTTP_200_OK if overall_ok else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    return {"status": "ok" if overall_ok else "degraded", "components": components}
