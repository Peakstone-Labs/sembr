# SPDX-License-Identifier: Apache-2.0
"""Tests for sembr/matcher/backfill.py — run_backfill orchestration."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

# Install qdrant_client stub before sembr.matcher.scan is imported.  Mirrors
# test_scan_once.py's pattern — qdrant-client may not be importable in every
# dev environment, and even when it is the test would prefer a lightweight
# stub it controls.


class _Range:
    def __init__(self, *, gte=None, lte=None, gt=None, lt=None):
        self.gte = gte
        self.lte = lte


class _MatchAny:
    def __init__(self, *, any=None):
        self.any = any


class _FieldCondition:
    def __init__(self, *, key, range=None, match=None):
        self.key = key
        self.range = range
        self.match = match


class _Filter:
    def __init__(self, *, must=None, should=None, must_not=None, min_should=None):
        self.must = must or []
        self.should = should or []
        self.must_not = must_not or []


class _OrderBy:
    def __init__(self, *, key, direction):
        self.key = key
        self.direction = direction


def _install_qdrant_stub() -> None:
    if "qdrant_client" not in sys.modules:
        sys.modules["qdrant_client"] = ModuleType("qdrant_client")
    if "qdrant_client.models" not in sys.modules:
        m = ModuleType("qdrant_client.models")
        sys.modules["qdrant_client.models"] = m
    m = sys.modules["qdrant_client.models"]
    m.Range = _Range  # type: ignore[attr-defined]
    m.MatchAny = _MatchAny  # type: ignore[attr-defined]
    m.FieldCondition = _FieldCondition  # type: ignore[attr-defined]
    m.Filter = _Filter  # type: ignore[attr-defined]
    m.OrderBy = _OrderBy  # type: ignore[attr-defined]


_install_qdrant_stub()

# Now import after stub
from sembr.db.intents import create_intent, init_intent_tables  # noqa: E402
from sembr.db.match_seen import init_match_seen_tables  # noqa: E402
from sembr.db.sqlite import install_for_test  # noqa: E402
from sembr.db.summary_history import (  # noqa: E402
    init_summary_history_table,
    migrate_summary_history_unique_index,
)
from sembr.matcher import backfill as backfill_mod  # noqa: E402
from sembr.matcher import backfill_tasks  # noqa: E402
from sembr.matcher.backfill_tasks import (  # noqa: E402
    create_task,
    forget_intent_lock,
    get_intent_lock,
    get_task,
    release_intent,
    sweep_expired,
    try_acquire_intent,
)
from sembr.models import CronSchedule, IntentCreate  # noqa: E402
from sembr.summarizer.models import Citation, SummaryResult  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_backfill_tasks_state():
    backfill_tasks._reset_for_testing()
    yield
    backfill_tasks._reset_for_testing()


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("PRAGMA foreign_keys=ON")
    await init_intent_tables(conn)
    await init_match_seen_tables(conn)
    await init_summary_history_table(conn)
    install_for_test(conn)
    await migrate_summary_history_unique_index(conn)
    yield conn
    await conn.close()


def _intent_body() -> IntentCreate:
    return IntentCreate(
        name="backfill-test",
        text="market movements",
        channels=[{"type": "email", "to": ["a@example.com"]}],
        schedule=CronSchedule(preset="daily", hour=9, minute=0, history_days=7),
    )


def _result(intent_id: int, summary: str = "digest") -> SummaryResult:
    c = Citation(
        article_id="a1",
        title="t",
        url="https://example.com",
        source=1,
        published_at="2026-05-26T00:00:00Z",
        score=0.9,
    )
    return SummaryResult(
        intent_id=intent_id,
        summary=summary,
        citations=[c],
        primary=c,
        other_sources=[],
    )


def _make_app(scheduler: MagicMock, qdrant_client: MagicMock, pipeline: MagicMock):
    app = SimpleNamespace()
    app.state = SimpleNamespace(
        scheduler=scheduler,
        qdrant=SimpleNamespace(client=qdrant_client),
        summary_pipeline=pipeline,
    )
    return app


def _make_qdrant_with_oldest(ts: int | None) -> MagicMock:
    client = MagicMock()
    if ts is None:
        client.scroll = AsyncMock(return_value=([], None))
    else:
        point = MagicMock()
        point.payload = {"ingested_at_ts": ts}
        client.scroll = AsyncMock(return_value=([point], None))
    return client


# ---------------------------------------------------------------------------
# probe_oldest_news_ts
# ---------------------------------------------------------------------------


async def test_probe_oldest_news_ts_returns_min() -> None:
    client = _make_qdrant_with_oldest(1_700_000_000)
    result = await backfill_mod.probe_oldest_news_ts(client)
    assert result == 1_700_000_000


async def test_probe_oldest_news_ts_empty_collection() -> None:
    client = _make_qdrant_with_oldest(None)
    assert await backfill_mod.probe_oldest_news_ts(client) is None


async def test_probe_oldest_news_ts_scroll_error() -> None:
    client = MagicMock()
    client.scroll = AsyncMock(side_effect=RuntimeError("qdrant down"))
    assert await backfill_mod.probe_oldest_news_ts(client) is None


# ---------------------------------------------------------------------------
# run_backfill — happy path
# ---------------------------------------------------------------------------


async def test_run_backfill_writes_summary_per_fire_time(db) -> None:
    intent = await create_intent(db, _intent_body())

    pipeline = MagicMock()
    pipeline.compute_summary = AsyncMock(return_value=_result(intent.id))

    scheduler = MagicMock()
    qdrant = _make_qdrant_with_oldest(0)
    app = _make_app(scheduler, qdrant, pipeline)

    task = create_task(intent_id=intent.id, total=3)
    # Pretend the handler has taken the lock
    lock = get_intent_lock(intent.id)
    await lock.acquire()

    with patch("sembr.matcher.backfill.scan_once", new_callable=AsyncMock) as scan_mock:
        scan_mock.return_value = [MagicMock()]  # one fake match per run
        await backfill_mod.run_backfill(intent.id, past_runs=3, app=app, task=task)

    assert task.status == "done"
    assert task.progress.done == 3
    assert task.progress.skipped == 0
    assert task.progress.empty_runs == 0
    # 3 rows written
    async with db.execute(
        "SELECT COUNT(*) FROM summary_history WHERE intent_id=?", (intent.id,)
    ) as cur:
        (count,) = await cur.fetchone()
    assert count == 3
    # Pipeline.compute_summary called with now= for each iteration
    assert pipeline.compute_summary.await_count == 3
    for call in pipeline.compute_summary.await_args_list:
        assert "now" in call.kwargs
        assert isinstance(call.kwargs["now"], datetime)


async def test_run_backfill_iterates_oldest_to_newest(db) -> None:
    intent = await create_intent(db, _intent_body())

    captured_nows: list[datetime] = []

    async def fake_compute(matches, now=None):
        captured_nows.append(now)
        return _result(intent.id, summary=f"sum-{now.isoformat()}")

    pipeline = MagicMock()
    pipeline.compute_summary = AsyncMock(side_effect=fake_compute)

    scheduler = MagicMock()
    qdrant = _make_qdrant_with_oldest(0)
    app = _make_app(scheduler, qdrant, pipeline)
    task = create_task(intent_id=intent.id, total=3)
    lock = get_intent_lock(intent.id)
    await lock.acquire()

    with patch("sembr.matcher.backfill.scan_once", new_callable=AsyncMock) as scan_mock:
        scan_mock.return_value = [MagicMock()]
        await backfill_mod.run_backfill(intent.id, past_runs=3, app=app, task=task)

    assert task.status == "done"
    assert captured_nows == sorted(captured_nows)  # strictly ascending


async def test_run_backfill_pauses_and_resumes_job(db) -> None:
    intent = await create_intent(db, _intent_body())

    pipeline = MagicMock()
    pipeline.compute_summary = AsyncMock(return_value=_result(intent.id))
    scheduler = MagicMock()
    qdrant = _make_qdrant_with_oldest(0)
    app = _make_app(scheduler, qdrant, pipeline)
    task = create_task(intent_id=intent.id, total=2)
    lock = get_intent_lock(intent.id)
    await lock.acquire()

    with patch("sembr.matcher.backfill.scan_once", new_callable=AsyncMock) as scan_mock:
        scan_mock.return_value = [MagicMock()]
        await backfill_mod.run_backfill(intent.id, past_runs=2, app=app, task=task)

    scheduler.pause_job.assert_called_once_with(f"matcher-intent-{intent.id}")
    scheduler.resume_job.assert_called_once_with(f"matcher-intent-{intent.id}")


async def test_run_backfill_idempotent_skip_on_second_run(db) -> None:
    intent = await create_intent(db, _intent_body())

    pipeline = MagicMock()
    pipeline.compute_summary = AsyncMock(return_value=_result(intent.id))
    scheduler = MagicMock()
    qdrant = _make_qdrant_with_oldest(0)
    app = _make_app(scheduler, qdrant, pipeline)
    task1 = create_task(intent_id=intent.id, total=2)
    lock = get_intent_lock(intent.id)
    await lock.acquire()

    with patch("sembr.matcher.backfill.scan_once", new_callable=AsyncMock) as scan_mock:
        scan_mock.return_value = [MagicMock()]
        await backfill_mod.run_backfill(intent.id, past_runs=2, app=app, task=task1)

    assert task1.progress.done == 2

    # Second run with the same N — same fire-times → UNIQUE collides
    task2 = create_task(intent_id=intent.id, total=2)
    await lock.acquire()
    with patch("sembr.matcher.backfill.scan_once", new_callable=AsyncMock) as scan_mock:
        scan_mock.return_value = [MagicMock()]
        await backfill_mod.run_backfill(intent.id, past_runs=2, app=app, task=task2)

    assert task2.progress.done == 0
    assert task2.progress.skipped == 2


# ---------------------------------------------------------------------------
# run_backfill — error paths
# ---------------------------------------------------------------------------


async def test_run_backfill_intent_missing(db) -> None:
    pipeline = MagicMock()
    pipeline.compute_summary = AsyncMock()
    scheduler = MagicMock()
    qdrant = _make_qdrant_with_oldest(0)
    app = _make_app(scheduler, qdrant, pipeline)
    task = create_task(intent_id=99999, total=2)
    lock = get_intent_lock(99999)
    await lock.acquire()

    await backfill_mod.run_backfill(99999, past_runs=2, app=app, task=task)
    assert task.status == "error"
    assert task.error_reason == "intent_invalid"
    pipeline.compute_summary.assert_not_called()
    scheduler.resume_job.assert_called_once()  # finally still ran


async def test_run_backfill_event_mode_rejected(db) -> None:
    body = IntentCreate(
        name="event-intent",
        text="x",
        channels=[{"type": "email", "to": ["a@example.com"]}],
        schedule={"mode": "event", "trigger_count": 3},
    )
    intent = await create_intent(db, body)

    pipeline = MagicMock()
    pipeline.compute_summary = AsyncMock()
    scheduler = MagicMock()
    qdrant = _make_qdrant_with_oldest(0)
    app = _make_app(scheduler, qdrant, pipeline)
    task = create_task(intent_id=intent.id, total=2)
    lock = get_intent_lock(intent.id)
    await lock.acquire()

    await backfill_mod.run_backfill(intent.id, past_runs=2, app=app, task=task)
    assert task.status == "error"
    assert task.error_reason == "intent_invalid"


async def test_run_backfill_qdrant_depth_insufficient(db) -> None:
    intent = await create_intent(db, _intent_body())

    pipeline = MagicMock()
    pipeline.compute_summary = AsyncMock(return_value=_result(intent.id))
    scheduler = MagicMock()
    # Oldest news_ts in the future → all fire-times older than coverage
    far_future = int(datetime.now(UTC).timestamp()) + 10_000_000
    qdrant = _make_qdrant_with_oldest(far_future)
    app = _make_app(scheduler, qdrant, pipeline)
    task = create_task(intent_id=intent.id, total=3)
    lock = get_intent_lock(intent.id)
    await lock.acquire()

    await backfill_mod.run_backfill(intent.id, past_runs=3, app=app, task=task)
    assert task.status == "error"
    assert task.error_reason == "qdrant_depth"
    pipeline.compute_summary.assert_not_called()


async def test_run_backfill_resume_job_even_on_exception(db) -> None:
    intent = await create_intent(db, _intent_body())

    pipeline = MagicMock()
    pipeline.compute_summary = AsyncMock(side_effect=RuntimeError("LLM boom"))
    scheduler = MagicMock()
    qdrant = _make_qdrant_with_oldest(0)
    app = _make_app(scheduler, qdrant, pipeline)
    task = create_task(intent_id=intent.id, total=2)
    lock = get_intent_lock(intent.id)
    await lock.acquire()

    with patch("sembr.matcher.backfill.scan_once", new_callable=AsyncMock) as scan_mock:
        scan_mock.return_value = [MagicMock()]
        # compute_summary errors are caught per-run; task should still finish.
        await backfill_mod.run_backfill(intent.id, past_runs=2, app=app, task=task)
    assert task.status == "done"
    scheduler.resume_job.assert_called_once()
    # Lock released
    assert not get_intent_lock(intent.id).locked()


async def test_run_backfill_no_notification_dispatch(db) -> None:
    """compute_summary called via public API; on_summary / on_persist not invoked."""
    intent = await create_intent(db, _intent_body())

    on_summary = AsyncMock()
    on_persist = AsyncMock()
    pipeline = MagicMock()
    pipeline.compute_summary = AsyncMock(return_value=_result(intent.id))
    pipeline._on_summary = on_summary
    pipeline._on_persist = on_persist

    scheduler = MagicMock()
    qdrant = _make_qdrant_with_oldest(0)
    app = _make_app(scheduler, qdrant, pipeline)
    task = create_task(intent_id=intent.id, total=2)
    lock = get_intent_lock(intent.id)
    await lock.acquire()

    with patch("sembr.matcher.backfill.scan_once", new_callable=AsyncMock) as scan_mock:
        scan_mock.return_value = [MagicMock()]
        await backfill_mod.run_backfill(intent.id, past_runs=2, app=app, task=task)

    on_summary.assert_not_awaited()
    on_persist.assert_not_awaited()


async def test_run_backfill_empty_matches_counted(db) -> None:
    intent = await create_intent(db, _intent_body())

    pipeline = MagicMock()
    pipeline.compute_summary = AsyncMock(return_value=_result(intent.id))
    scheduler = MagicMock()
    qdrant = _make_qdrant_with_oldest(0)
    app = _make_app(scheduler, qdrant, pipeline)
    task = create_task(intent_id=intent.id, total=3)
    lock = get_intent_lock(intent.id)
    await lock.acquire()

    with patch("sembr.matcher.backfill.scan_once", new_callable=AsyncMock) as scan_mock:
        scan_mock.return_value = []  # all runs return empty
        await backfill_mod.run_backfill(intent.id, past_runs=3, app=app, task=task)

    assert task.status == "done"
    assert task.progress.done == 0
    assert task.progress.empty_runs == 3
    pipeline.compute_summary.assert_not_called()


async def test_run_backfill_intent_deleted_mid_run(db) -> None:
    intent = await create_intent(db, _intent_body())

    pipeline = MagicMock()
    pipeline.compute_summary = AsyncMock(return_value=_result(intent.id))
    scheduler = MagicMock()
    qdrant = _make_qdrant_with_oldest(0)
    app = _make_app(scheduler, qdrant, pipeline)
    task = create_task(intent_id=intent.id, total=5)
    lock = get_intent_lock(intent.id)
    await lock.acquire()

    call_count = {"n": 0}

    async def fake_get_intent(conn, iid):
        call_count["n"] += 1
        # First call (snapshot) returns intent; later mid-run check returns None
        if call_count["n"] == 1:
            return intent
        if call_count["n"] >= 3:
            return None  # simulate DELETE mid-run
        return intent

    with (
        patch("sembr.matcher.backfill.get_intent", new=fake_get_intent),
        patch("sembr.matcher.backfill.scan_once", new_callable=AsyncMock) as scan_mock,
    ):
        scan_mock.return_value = [MagicMock()]
        await backfill_mod.run_backfill(intent.id, past_runs=5, app=app, task=task)

    assert task.status == "error"
    assert task.error_reason == "intent_deleted_mid_run"
    # resume_job still called in finally
    scheduler.resume_job.assert_called_once()


# ---------------------------------------------------------------------------
# Inter-run throttle
# ---------------------------------------------------------------------------


async def test_run_backfill_inter_run_sleep(db) -> None:
    intent = await create_intent(db, _intent_body())

    pipeline = MagicMock()
    pipeline.compute_summary = AsyncMock(return_value=_result(intent.id))
    scheduler = MagicMock()
    qdrant = _make_qdrant_with_oldest(0)
    app = _make_app(scheduler, qdrant, pipeline)
    task = create_task(intent_id=intent.id, total=3)
    lock = get_intent_lock(intent.id)
    await lock.acquire()

    real_sleep = asyncio.sleep
    sleep_calls: list[float] = []

    async def tracking_sleep(d):
        sleep_calls.append(d)
        await real_sleep(0)

    with (
        patch("sembr.matcher.backfill.asyncio.sleep", new=tracking_sleep),
        patch("sembr.matcher.backfill.scan_once", new_callable=AsyncMock) as scan_mock,
    ):
        scan_mock.return_value = [MagicMock()]
        await backfill_mod.run_backfill(intent.id, past_runs=3, app=app, task=task)

    # 3 successful runs → 3 sleep(0.2) calls
    assert sleep_calls == [0.2, 0.2, 0.2]


# ---------------------------------------------------------------------------
# backfill_tasks helpers
# ---------------------------------------------------------------------------


def test_create_task_status_running() -> None:
    task = create_task(intent_id=1, total=5)
    assert task.status == "running"
    assert task.progress.total == 5
    assert get_task(task.task_id) is task


def test_get_task_missing() -> None:
    assert get_task("not-a-real-id") is None


def test_sweep_expired_removes_old_tasks() -> None:
    task = create_task(intent_id=1, total=3)
    # Force expiry by backdating
    task._created_at = datetime.now(UTC).replace(year=2020)
    removed = sweep_expired()
    assert removed == 1
    assert get_task(task.task_id) is None


def test_sweep_expired_keeps_fresh_tasks() -> None:
    task = create_task(intent_id=1, total=3)
    removed = sweep_expired()
    assert removed == 0
    assert get_task(task.task_id) is task


def test_get_intent_lock_reuses_instance() -> None:
    l1 = get_intent_lock(1)
    l2 = get_intent_lock(1)
    assert l1 is l2
    l3 = get_intent_lock(2)
    assert l1 is not l3


def test_try_acquire_intent_atomic() -> None:
    """First call wins, second call returns False; release allows re-acquire."""
    assert try_acquire_intent(7) is True
    assert try_acquire_intent(7) is False
    release_intent(7)
    assert try_acquire_intent(7) is True
    release_intent(7)


def test_release_intent_idempotent() -> None:
    """release_intent on an unlocked / unknown intent is a no-op, not RuntimeError."""
    release_intent(99)  # never acquired
    assert try_acquire_intent(99) is True
    release_intent(99)
    release_intent(99)  # already released — must not raise


def test_forget_intent_lock_removes_entry() -> None:
    assert try_acquire_intent(11) is True
    release_intent(11)
    forget_intent_lock(11)
    # After forget, get_intent_lock yields a fresh instance
    lock_after = get_intent_lock(11)
    # Try-acquire on the fresh lock succeeds (no stuck state from prior owner)
    assert lock_after.locked() is False


def test_forget_intent_lock_skips_locked() -> None:
    """forget_intent_lock must not drop a still-held lock; that would corrupt state."""
    assert try_acquire_intent(13) is True
    held = get_intent_lock(13)
    forget_intent_lock(13)  # should be a no-op while held
    refetched = get_intent_lock(13)
    assert refetched is held
    release_intent(13)
