# SPDX-License-Identifier: Apache-2.0
"""Integration test: build_snapshot reads SystemMetricsCollector via dependency injection.

Verifies that:
- ``build_snapshot(conn, qdrant, embedder, collector)`` calls
  ``collector.read()`` and embeds the result into ``SnapshotResponse``.
- ``collector=None`` (no metrics_collector on app.state) → ``system_metrics:
  None`` in the response, no exception (graceful degradation).
- A docker-unavailable collector returns ``system_metrics: None`` and the
  rest of the snapshot fields are unaffected.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from sembr.dashboard import system_metrics as sm
from sembr.dashboard.events import init_event_log_tables
from sembr.dashboard.read_model import build_snapshot
from sembr.dashboard.schemas import ContainerMetric
from sembr.db.articles import init_article_tables
from sembr.db.feeds import init_feed_tables
from sembr.db.sqlite import close_sqlite, get_conn, init_sqlite


async def _setup(tmp_path):
    conn = await init_sqlite(str(tmp_path / "sembr.db"))
    await init_feed_tables(conn)
    await init_article_tables(conn)
    await init_event_log_tables(conn)
    return conn


def _qdrant_handle():
    h = MagicMock()
    h.ping = AsyncMock(return_value=True)
    h.client.count = AsyncMock(return_value=MagicMock(count=0))
    return h


def _embedder():
    e = MagicMock()
    e.status = "ok"
    e.model_version = "bge-m3"
    return e


def test_build_snapshot_includes_system_metrics(tmp_path):
    """Collector with one sample → snapshot.system_metrics is populated."""

    async def run():
        await _setup(tmp_path)
        collector = sm.SystemMetricsCollector(interval_seconds=10)
        collector.append(
            sm._Sample(
                sampled_at=datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC),
                containers=[
                    ContainerMetric(
                        name="sembr-api",
                        uptime_seconds=42,
                        cpu_percent=12.5,
                        mem_used_bytes=100_000,
                        mem_limit_bytes=4_000_000_000,
                    )
                ],
            )
        )
        snap = await build_snapshot(get_conn(), _qdrant_handle(), _embedder(), collector)
        await close_sqlite()
        return snap

    snap = asyncio.run(run())
    assert snap.system_metrics is not None
    assert snap.system_metrics.interval_seconds == 10
    assert len(snap.system_metrics.containers) == 1
    cm = snap.system_metrics.containers[0]
    assert cm.cpu_percent == 12.5
    assert cm.uptime_seconds == 42
    assert cm.cpu_history == [12.5]


def test_build_snapshot_collector_none_returns_null_metrics(tmp_path):
    """No collector → system_metrics: None; rest of snapshot still valid."""

    async def run():
        await _setup(tmp_path)
        snap = await build_snapshot(get_conn(), _qdrant_handle(), _embedder(), None)
        await close_sqlite()
        return snap

    snap = asyncio.run(run())
    assert snap.system_metrics is None
    # Sanity: rest of snapshot still computed (graceful degradation)
    assert snap.articles.pending_count == 0
    assert snap.articles.dead_count == 0


def test_build_snapshot_collector_unavailable_returns_null_metrics(tmp_path):
    """Docker socket lost mid-run → collector flips to unavailable; snapshot
    still 200, system_metrics: None (graceful-degradation contract)."""

    async def run():
        await _setup(tmp_path)
        collector = sm.SystemMetricsCollector(interval_seconds=10)
        collector.mark_unavailable()
        snap = await build_snapshot(get_conn(), _qdrant_handle(), _embedder(), collector)
        await close_sqlite()
        return snap

    snap = asyncio.run(run())
    assert snap.system_metrics is None


def test_build_snapshot_default_collector_arg_is_none(tmp_path):
    """Backwards-compat sanity: existing callers without the new arg still work
    (collector defaults to None → system_metrics: None)."""

    async def run():
        await _setup(tmp_path)
        snap = await build_snapshot(get_conn(), _qdrant_handle(), _embedder())
        await close_sqlite()
        return snap

    snap = asyncio.run(run())
    assert snap.system_metrics is None
