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
    """The queue is `IsEmpty(body_len)`, so points that already have the
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
    """A batch that writes without shrinking the queue would otherwise be
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
    """The flag must have a defined value from lifespan onward, not from
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


@pytest.mark.asyncio
async def test_count_pending_derived_is_not_a_tautology():
    """QA-7: negative control for the reconciliation oracle both this job and
    the manual on-box backfill-completeness check key off of. A stub that
    just records kwargs and always returns a fixed count would make backfill
    completeness self-certifying no matter what the filter said — this one
    evaluates IsEmptyCondition against real payloads, so a point genuinely
    missing body_len must make the count non-zero."""
    from qdrant_client.models import IsEmptyCondition

    payloads = {
        "has-body-len": {"body_len": 40},
        "missing-body-len": {"title": "x"},  # the poison case: no body_len key at all
    }

    class _SemanticCountClient:
        async def count(self, *, collection_name, count_filter, exact):
            (cond,) = count_filter.must
            assert isinstance(cond, IsEmptyCondition)
            key = cond.is_empty.key
            n = sum(1 for payload in payloads.values() if key not in payload)
            return SimpleNamespace(count=n)

    # Negative: the oracle must FAIL (count >= 1) while a point really is
    # missing the field — this is what proves it is not vacuously always 0.
    assert await count_pending_derived(_SemanticCountClient()) == 1

    # Positive control: once every point has been backfilled the same oracle
    # must read back to zero, or the negative case above would be the only
    # value it can ever produce, which is a tautology of a different shape.
    payloads["missing-body-len"]["body_len"] = 1
    assert await count_pending_derived(_SemanticCountClient()) == 0


@pytest.mark.asyncio
async def test_backfill_stops_spending_rounds_once_quarantine_is_exhausted():
    """The quarantine set is replayed as a `must_not has_id` on every count and
    scroll, so letting it grow without bound turns the backfill into a load
    generator against the collection it is supposed to be repairing."""
    from sembr.maintenance import derived_backfill as mod

    state = BackfillState()
    state.quarantined = {f"id-{i}" for i in range(mod._QUARANTINE_CAP)}
    state.exhausted = True

    qc = MagicMock()
    qc.count = AsyncMock()
    qc.scroll = AsyncMock()
    app = _app()

    await _run_news_derived_backfill(_handle(qc), state, app)

    qc.count.assert_not_called()
    qc.scroll.assert_not_called()


@pytest.mark.asyncio
async def test_quarantine_then_skip_end_to_end_across_rounds(caplog):
    """QA-11: a batch that makes zero progress for three CONSECUTIVE real
    rounds of `_run_news_derived_backfill` (not a hand-set BackfillState, which
    the tests above already cover in isolation) must be quarantined, and the
    fourth round must actually exclude it from the queue rather than retry it
    forever — observable as: no fourth scroll call, and the per-round "made no
    progress" warning stops appearing once quarantine kicks in."""
    qc = MagicMock()
    # 2 count() calls per round while not yet quarantined (queue, remaining);
    # round 3 adds a third (the final raw-pending recount, now that
    # state.quarantined is non-empty); round 4's queue excludes the
    # quarantined id and converges, then the raw recount still reports 1.
    qc.count = AsyncMock(
        side_effect=[SimpleNamespace(count=n) for n in (1, 1, 1, 1, 1, 1, 1, 0, 1)]
    )
    qc.scroll = AsyncMock(return_value=([_point("bad-1")], None))
    qc.batch_update_points = AsyncMock()
    app = _app()
    state = BackfillState()

    with caplog.at_level(logging.WARNING):
        for _round in range(4):
            await _run_news_derived_backfill(_handle(qc), state, app)

    assert state.quarantined == {"bad-1"}
    # Only 3 scrolls: round 4 never re-fetches the quarantined batch.
    assert qc.scroll.call_count == 3
    # The per-round warning fires for round 1 and round 2 (before the streak
    # reaches quarantine) and must NOT fire again for round 4.
    made_no_progress = [r for r in caplog.records if "made no progress" in r.getMessage()]
    assert len(made_no_progress) == 2


def test_quarantine_cap_flips_exhausted():
    from sembr.maintenance import derived_backfill as mod

    state = BackfillState()
    batch = [f"id-{i}" for i in range(mod._QUARANTINE_CAP)]
    for _round in range(mod._STALL_ROUNDS_BEFORE_QUARANTINE - 1):
        assert state.note_stall(batch) is False
        assert state.exhausted is False
    assert state.note_stall(batch) is True
    assert state.exhausted is True


def test_note_stall_streak_resets_on_a_different_batch():
    """Consecutive rounds on the SAME batch is the signal; a different batch
    stalling means the queue moved, and carrying the streak over would
    quarantine points that were never actually stuck."""
    state = BackfillState()
    assert state.note_stall(["a"]) is False
    assert state.note_stall(["b"]) is False
    assert state.note_stall(["a"]) is False
    assert state.quarantined == set()


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
