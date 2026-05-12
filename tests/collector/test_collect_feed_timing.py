# SPDX-License-Identifier: Apache-2.0
"""Loop 2 regression: started_at must reflect actual fetch start, not queue-wait
time. (#🟡-2 in dashboard-feeds-tab review.md)
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sembr.collector import scheduler as sched_mod
from sembr.collector.host_limiter import HostLimiter


@pytest.mark.asyncio
async def test_started_at_excludes_queue_wait(monkeypatch) -> None:
    """When the limiter makes a fetch wait, started_at should be sampled AFTER
    acquire (not before), so feed_fetch_log.elapsed_ms reflects fetch duration only.
    """
    captured: dict = {}

    async def fake_fetch(self, since=None):
        return []

    async def fake_emit(**kwargs):
        captured.update(kwargs)

    # Patch SOURCE_REGISTRY entry to a stub that doesn't hit network.
    class StubSource:
        def __init__(self, url, timeout=30.0):
            pass

        async def fetch(self, since=None):
            return []

    monkeypatch.setitem(sched_mod.SOURCE_REGISTRY, "stub", StubSource)
    monkeypatch.setattr(sched_mod, "_emit_fetch_event", fake_emit)
    monkeypatch.setattr(sched_mod, "insert_article_pending", AsyncMock(return_value=False))
    monkeypatch.setattr(sched_mod, "update_last_collected", AsyncMock())

    # Stub get_conn so SELECT last_collected_at returns a row with no since.
    fake_conn = MagicMock()
    cur = MagicMock()
    cur.__aenter__ = AsyncMock(return_value=cur)
    cur.__aexit__ = AsyncMock(return_value=None)
    cur.fetchone = AsyncMock(return_value=(None,))
    fake_conn.execute = MagicMock(return_value=cur)
    monkeypatch.setattr(sched_mod, "get_conn", lambda: fake_conn)

    # Limiter with capacity 1, then occupy it for 100ms before our fetch attempts.
    limiter = HostLimiter(frozenset(), max_per_host=1)
    sched_mod.set_host_limiter(limiter)

    hold_started = asyncio.Event()
    hold_release = asyncio.Event()

    async def hold_slot():
        async with limiter.acquire("example.com"):
            hold_started.set()
            await hold_release.wait()

    holder_task = asyncio.create_task(hold_slot())
    await hold_started.wait()

    # Now collect_feed must wait for the slot.
    fetch_task = asyncio.create_task(
        sched_mod.collect_feed(1, "n", "https://example.com/r.xml", "stub", {})
    )
    # Let it queue for ~80ms before releasing the holder.
    await asyncio.sleep(0.08)
    queue_wait_floor = datetime.utcnow()
    hold_release.set()
    await fetch_task
    await holder_task
    sched_mod.set_host_limiter(None)

    assert captured.get("ok") is True
    started_at = captured["started_at"]
    # started_at MUST be sampled after the holder released, so >= queue_wait_floor.
    # Tolerate small clock skew between datetime.utcnow() (naive) and timezone-aware.
    started_naive = started_at.replace(tzinfo=None)
    delta_ms = (started_naive - queue_wait_floor).total_seconds() * 1000
    assert delta_ms >= -5, (
        f"started_at={started_at} appears to be sampled BEFORE acquire "
        f"(queue_wait_floor={queue_wait_floor}, delta_ms={delta_ms:.1f})"
    )
