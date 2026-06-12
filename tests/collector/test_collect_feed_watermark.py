# SPDX-License-Identifier: Apache-2.0
"""ignore_published_watermark opt-out: collect_feed must force since=None so
research-report style feeds (every item stamped with a coarse / back-dated
timestamp) aren't zeroed by the RSS published_at<=since pre-filter. Dedup then
falls back to the persistent feed_items MD5 ledger.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sembr.collector import scheduler as sched_mod


def _stub_conn(last_collected: str | None) -> MagicMock:
    """get_conn() stub whose SELECT last_collected_at returns one row."""
    fake_conn = MagicMock()
    cur = MagicMock()
    cur.__aenter__ = AsyncMock(return_value=cur)
    cur.__aexit__ = AsyncMock(return_value=None)
    cur.fetchone = AsyncMock(return_value=(last_collected,))
    fake_conn.execute = MagicMock(return_value=cur)
    return fake_conn


@pytest.mark.asyncio
async def test_ignore_published_watermark_forces_since_none(monkeypatch) -> None:
    """Flag set → source.fetch receives since=None despite a stored watermark;
    flag absent → since is the parsed last_collected_at."""
    captured: dict = {}

    class StubSource:
        def __init__(self, url, timeout=30.0):
            pass

        async def fetch(self, since=None):
            captured["since"] = since
            return []

    monkeypatch.setitem(sched_mod.SOURCE_REGISTRY, "stub", StubSource)
    monkeypatch.setattr(sched_mod, "_emit_fetch_event", AsyncMock())
    monkeypatch.setattr(sched_mod, "insert_article_pending", AsyncMock(return_value=False))
    monkeypatch.setattr(sched_mod, "update_last_collected", AsyncMock())
    monkeypatch.setattr(sched_mod, "get_conn", lambda: _stub_conn("2026-04-27T10:00:00Z"))
    sched_mod.set_host_limiter(None)

    # Baseline: no flag → since is the parsed watermark, so the pre-filter runs.
    await sched_mod.collect_feed(1, "n", "https://x/r.xml", "stub", {})
    assert captured["since"] is not None
    assert (captured["since"].year, captured["since"].month) == (2026, 4)

    # Flag set → since forced to None so every page item reaches MD5 dedup.
    await sched_mod.collect_feed(
        1, "n", "https://x/r.xml", "stub", {"ignore_published_watermark": True}
    )
    assert captured["since"] is None
