"""POST /api/dashboard/sources/newsapi/fire — manual master-tick trigger."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sembr.collector.scheduler import (
    NEWSAPI_MASTER_JOB_ID,
    ensure_newsapi_master_job,
)
from sembr.config import get_settings
from sembr.dashboard.routes import router


@pytest.fixture(autouse=True)
def _clear_settings(monkeypatch):
    monkeypatch.setenv("NEWSAPI_API_KEY", "k")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_app(scheduler) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.scheduler = scheduler
    return app


@pytest.mark.asyncio
async def test_fire_404_when_master_not_registered() -> None:
    sch = AsyncIOScheduler(timezone="UTC")
    sch.start(paused=True)
    try:
        client = TestClient(_make_app(sch))
        r = client.post("/api/dashboard/sources/newsapi/fire")
        assert r.status_code == 404
        assert "newsapi master job" in r.json()["detail"]
    finally:
        sch.shutdown(wait=False)


@pytest.mark.asyncio
async def test_fire_advances_next_run_time_to_now() -> None:
    sch = AsyncIOScheduler(timezone="UTC")
    sch.start(paused=True)
    try:
        await ensure_newsapi_master_job(sch, get_settings())
        before = sch.get_job(NEWSAPI_MASTER_JOB_ID).next_run_time
        # Sanity: APScheduler's IntervalTrigger first run is well in the future
        assert before > datetime.now(timezone.utc) + timedelta(seconds=10)

        client = TestClient(_make_app(sch))
        r = client.post("/api/dashboard/sources/newsapi/fire")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["job_id"] == NEWSAPI_MASTER_JOB_ID

        after = sch.get_job(NEWSAPI_MASTER_JOB_ID).next_run_time
        # next_run_time should now be ~now, much earlier than the original.
        assert after < before
        assert after <= datetime.now(timezone.utc) + timedelta(seconds=2)
    finally:
        sch.shutdown(wait=False)


@pytest.mark.asyncio
async def test_fire_503_when_scheduler_missing() -> None:
    app = FastAPI()
    app.include_router(router)
    # Intentionally not setting app.state.scheduler.
    client = TestClient(app)
    r = client.post("/api/dashboard/sources/newsapi/fire")
    assert r.status_code == 503
