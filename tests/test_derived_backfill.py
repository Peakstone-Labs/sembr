# SPDX-License-Identifier: Apache-2.0
"""Self-healing derived-field backfill job (``maintenance/derived_backfill.py``).

The job's contract is narrow but load-bearing: it may only touch points that
are actually missing the fields, it must make monotonic progress, and it must
stop rather than spin when a batch refuses to leave the queue.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from qdrant_client.models import Filter as RealFilter
from qdrant_client.models import SetPayloadOperation as RealSetPayloadOperation

from sembr.maintenance.derived_backfill import (
    BackfillState,
    _run_news_derived_backfill,
    add_news_derived_backfill_job,
    count_pending_derived,
    initialise_pending_flag,
)


def _point(pid: str, **payload_overrides):
    payload = {
        "url": "https://www.reuters.com/world/x",
        "title": "Fed holds rates steady",
        "body": "The Federal Reserve kept rates unchanged " * 3,
        "published_at": "2026-07-27T06:49:53+00:00",
        "feed_id": 1,
        "embedding_model_version": "bge-m3_v1",
        "ingested_at_ts": 1785000000,
    }
    payload.update(payload_overrides)
    return SimpleNamespace(id=pid, payload=payload)


def _handle(client) -> SimpleNamespace:
    return SimpleNamespace(client=client)


def _app() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace())


@pytest.mark.asyncio
async def test_backfill_sets_payload_for_missing_points():
    """D4: the queue is `IsEmpty(body_len)`, so points that already have the
    derived fields are never in the working set — and each point gets ITS OWN
    payload (a single set_payload for the whole batch would stamp every point
    with the same body_len)."""
    qc = MagicMock()
    qc.count = AsyncMock(side_effect=[SimpleNamespace(count=2), SimpleNamespace(count=0)])
    qc.scroll = AsyncMock(
        return_value=([_point("id-1"), _point("id-2", body="短", title="中文")], None)
    )
    qc.batch_update_points = AsyncMock()
    app = _app()

    await _run_news_derived_backfill(_handle(qc), BackfillState(), app)

    # The working set is defined by a real Qdrant filter, not a duck-typed stub.
    scroll_filter = qc.scroll.call_args.kwargs["scroll_filter"]
    assert isinstance(scroll_filter, RealFilter)
    (cond,) = scroll_filter.must
    assert cond.is_empty.key == "body_len"
    assert scroll_filter.must_not is None
    assert qc.scroll.call_args.kwargs["with_vectors"] is False

    ops = qc.batch_update_points.call_args.kwargs["update_operations"]
    assert qc.batch_update_points.call_args.kwargs["wait"] is True
    assert len(ops) == 2
    assert all(isinstance(o, RealSetPayloadOperation) for o in ops)

    by_id = {o.set_payload.points[0]: o.set_payload.payload for o in ops}
    assert by_id["id-1"]["lang"] == "en"
    assert by_id["id-1"]["body_len"] == len("The Federal Reserve kept rates unchanged " * 3)
    assert by_id["id-1"]["url_domain"] == "reuters.com"
    assert by_id["id-1"]["published_at_ts"] == 1785134993
    # Second point derives its own values, not the first point's.
    assert by_id["id-2"]["lang"] == "zh"
    assert by_id["id-2"]["body_len"] == 1

    assert app.state.news_derived_backfill_pending == 0


@pytest.mark.asyncio
async def test_backfill_stops_when_queue_is_empty():
    qc = MagicMock()
    qc.count = AsyncMock(return_value=SimpleNamespace(count=0))
    qc.scroll = AsyncMock()
    qc.batch_update_points = AsyncMock()
    app = _app()

    await _run_news_derived_backfill(_handle(qc), BackfillState(), app)

    qc.scroll.assert_not_called()
    qc.batch_update_points.assert_not_called()
    assert app.state.news_derived_backfill_pending == 0


@pytest.mark.asyncio
async def test_backfill_zero_progress_aborts_run(caplog):
    """D6: a batch that writes without shrinking the queue would otherwise be
    re-fetched for the rest of the wall-clock budget."""
    qc = MagicMock()
    qc.count = AsyncMock(return_value=SimpleNamespace(count=3))
    qc.scroll = AsyncMock(return_value=([_point("id-1")], None))
    qc.batch_update_points = AsyncMock()
    app = _app()

    with caplog.at_level(logging.WARNING):
        await _run_news_derived_backfill(_handle(qc), BackfillState(), app)

    assert qc.scroll.call_count == 1
    assert qc.batch_update_points.call_count == 1
    assert any("made no progress" in r.getMessage() for r in caplog.records)
    assert app.state.news_derived_backfill_pending == 3


@pytest.mark.asyncio
async def test_backfill_drains_across_batches():
    qc = MagicMock()
    qc.count = AsyncMock(
        side_effect=[SimpleNamespace(count=2), SimpleNamespace(count=1), SimpleNamespace(count=0)]
    )
    qc.scroll = AsyncMock(side_effect=[([_point("id-1")], None), ([_point("id-2")], None)])
    qc.batch_update_points = AsyncMock()
    app = _app()

    await _run_news_derived_backfill(_handle(qc), BackfillState(), app)

    assert qc.scroll.call_count == 2
    assert qc.batch_update_points.call_count == 2
    assert app.state.news_derived_backfill_pending == 0


@pytest.mark.asyncio
async def test_backfill_qdrant_failure_marks_pending_unknown():
    """A run that cannot establish the queue depth must publish "unknown"
    (None), which the search endpoint treats as pending — never leave a stale
    zero behind that would silence the under-recall warning."""
    qc = MagicMock()
    qc.count = AsyncMock(side_effect=RuntimeError("qdrant down"))
    app = _app()
    app.state.news_derived_backfill_pending = 0

    await _run_news_derived_backfill(_handle(qc), BackfillState(), app)

    assert app.state.news_derived_backfill_pending is None


@pytest.mark.asyncio
async def test_backfill_scroll_failure_is_not_raised_out_of_the_job():
    qc = MagicMock()
    qc.count = AsyncMock(return_value=SimpleNamespace(count=5))
    qc.scroll = AsyncMock(side_effect=RuntimeError("boom"))
    app = _app()

    await _run_news_derived_backfill(_handle(qc), BackfillState(), app)

    assert app.state.news_derived_backfill_pending == 5


@pytest.mark.asyncio
async def test_initialise_pending_flag_sets_count_before_first_round():
    """D15: the flag must have a defined value from lifespan onward, not from
    the job's first fire two minutes later."""
    qc = MagicMock()
    qc.count = AsyncMock(return_value=SimpleNamespace(count=113_000))
    app = _app()

    await initialise_pending_flag(app, _handle(qc))

    assert app.state.news_derived_backfill_pending == 113_000


@pytest.mark.asyncio
async def test_initialise_pending_flag_unknown_on_failure():
    qc = MagicMock()
    qc.count = AsyncMock(side_effect=RuntimeError("qdrant not ready"))
    app = _app()

    await initialise_pending_flag(app, _handle(qc))

    assert app.state.news_derived_backfill_pending is None


@pytest.mark.asyncio
async def test_count_pending_derived_excludes_quarantined_ids():
    qc = MagicMock()
    qc.count = AsyncMock(return_value=SimpleNamespace(count=7))

    assert await count_pending_derived(qc, exclude_ids={"b", "a"}) == 7

    count_filter = qc.count.call_args.kwargs["count_filter"]
    assert isinstance(count_filter, RealFilter)
    assert count_filter.must_not[0].has_id == ["a", "b"]
    assert qc.count.call_args.kwargs["exact"] is True


def test_backfill_job_registration_parameters():
    """The schedule is a decision, not a default: a truncated round has to
    resume in minutes, not on the 24h maintenance cadence."""
    scheduler = MagicMock()
    state = add_news_derived_backfill_job(scheduler, _handle(MagicMock()), _app())

    assert isinstance(state, BackfillState)
    kwargs = scheduler.add_job.call_args.kwargs
    assert kwargs["id"] == "maintenance_news_derived_backfill"
    assert kwargs["coalesce"] is True
    assert kwargs["max_instances"] == 1
    trigger = kwargs["trigger"]
    assert trigger.interval.total_seconds() == 30 * 60
    # start_date is set (a first fire from the trigger would be 30 min out);
    # None here would mean the job never gets a startup offset.
    assert trigger.start_date is not None
