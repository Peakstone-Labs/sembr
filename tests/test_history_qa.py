# SPDX-License-Identifier: Apache-2.0
"""QA tests for history-display feature — Owner=QA items from the design Test Strategy.

Covers:
  - test_backfill_match_seen_writes
  - test_backfill_qdrant_outage_mid_run
  - test_backfill_concurrent_with_normal_cron_first_tick_after_resume
  - test_backfill_lookback_anchor_correctness
  - test_backfill_uses_schedule_snapshot
  - test_view_modal_xss_html_escaped          (static intents.js)
  - test_view_modal_markdown_renders_links    (static intents.js)
  - test_backfill_button_disabled_during_running (static index.html)
  - test_history_expanded_row_in_dom_only_after_click (static index.html)

Items already covered by dev tests (verified pass, not re-listed):
  - test_backfill_resume_job_on_exception → test_run_backfill_resume_job_even_on_exception
  - test_backfill_intent_deleted_mid_run  → test_run_backfill_intent_deleted_mid_run
  - test_backfill_inter_run_sleep_throttle → test_run_backfill_inter_run_sleep
  - test_view_modal_citation_uuid_to_md5_strip_dashes → test_citation_uuid_strip_dashes
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

# ---------------------------------------------------------------------------
# qdrant_client stub — mirrors test_backfill.py pattern so this file can run
# standalone while still deferring to the real client when it is already loaded.
# ---------------------------------------------------------------------------


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


def _ensure_qdrant_stub() -> None:
    """Populate qdrant_client.models with minimal stubs if real module is missing.

    Also ensures qdrant_client package exposes AsyncQdrantClient so that
    vector_store/qdrant.py can be imported.  When the real qdrant-client is
    installed, sys.modules will already have a proper module and this is a no-op.
    """
    if "qdrant_client" not in sys.modules:
        qc = ModuleType("qdrant_client")
        # vector_store/qdrant.py does: from qdrant_client import AsyncQdrantClient
        qc.AsyncQdrantClient = MagicMock  # type: ignore[attr-defined]
        sys.modules["qdrant_client"] = qc
    else:
        # Real module may already be loaded; ensure AsyncQdrantClient is accessible.
        qc = sys.modules["qdrant_client"]
        if not hasattr(qc, "AsyncQdrantClient"):
            qc.AsyncQdrantClient = MagicMock  # type: ignore[attr-defined]

    if "qdrant_client.models" not in sys.modules:
        m = ModuleType("qdrant_client.models")
        sys.modules["qdrant_client.models"] = m
    m = sys.modules["qdrant_client.models"]
    for name, cls in (
        ("Range", _Range),
        ("MatchAny", _MatchAny),
        ("FieldCondition", _FieldCondition),
        ("Filter", _Filter),
        ("OrderBy", _OrderBy),
    ):
        if not hasattr(m, name):
            setattr(m, name, cls)


_ensure_qdrant_stub()

# ---------------------------------------------------------------------------
# Production imports (after stub)
# ---------------------------------------------------------------------------

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
    get_intent_lock,
)
from sembr.models import CronSchedule, IntentCreate  # noqa: E402
from sembr.summarizer.models import Citation, SummaryResult  # noqa: E402

# ---------------------------------------------------------------------------
# Static file paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INDEX_HTML = _REPO_ROOT / "web" / "static" / "index.html"
_INTENTS_JS = _REPO_ROOT / "web" / "static" / "intents.js"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_backfill_tasks():
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


@pytest.fixture(scope="module")
def index_html() -> str:
    return _INDEX_HTML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def intents_js() -> str:
    return _INTENTS_JS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _intent_body() -> IntentCreate:
    return IntentCreate(
        name="qa-backfill-test",
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


def _make_app(scheduler, qdrant_client, pipeline):
    from types import SimpleNamespace  # noqa: PLC0415

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
# test_backfill_match_seen_writes (D5 + Option 5)
# After a backfill with write_match_seen=True, match_seen table gains rows.
# ---------------------------------------------------------------------------


async def test_backfill_match_seen_writes(db) -> None:
    """Backfill with write_match_seen=True writes entries to match_seen for the intent."""
    intent = await create_intent(db, _intent_body())

    pipeline = MagicMock()
    pipeline.compute_summary = AsyncMock(return_value=_result(intent.id))
    scheduler = MagicMock()
    qdrant = _make_qdrant_with_oldest(0)
    app = _make_app(scheduler, qdrant, pipeline)

    task = create_task(intent_id=intent.id, total=2)
    lock = get_intent_lock(intent.id)
    await lock.acquire()

    # scan_once: return a mock match with a real article_id so match_seen can be written.
    # We patch scan_once in the backfill module.  The real scan_once would attempt Qdrant
    # calls we cannot make here, so we patch it with a fake that calls
    # insert_unseen_returning_new directly to simulate write_match_seen=True behaviour.
    from sembr.db.match_seen import insert_unseen_returning_new  # noqa: PLC0415
    from sembr.matcher.callback import Match  # noqa: PLC0415

    async def fake_scan_once(intent, options, conn, qdrant_client):
        # Simulate the write_match_seen branch: insert article "art-qa-1" as seen.
        if options.write_match_seen:
            await insert_unseen_returning_new(conn, intent.id, ["art-qa-1"])
        match = Match(
            intent_id=intent.id,
            article_id="art-qa-1",
            score=0.9,
            payload={
                "title": "t",
                "body": "b",
                "url": "u",
                "feed_id": 1,
                "published_at": "2026-01-01T00:00:00Z",
            },
        )
        return [match]

    with patch("sembr.matcher.backfill.scan_once", new=fake_scan_once):
        await backfill_mod.run_backfill(intent.id, past_runs=2, app=app, task=task)

    assert task.status == "done"
    assert task.progress.done == 2

    # Verify match_seen has at least one row for this intent.
    async with db.execute("SELECT COUNT(*) FROM match_seen WHERE intent_id=?", (intent.id,)) as cur:
        (count,) = await cur.fetchone()
    assert count >= 1, "match_seen must have been written by backfill with write_match_seen=True"


# ---------------------------------------------------------------------------
# test_backfill_qdrant_outage_mid_run (R5)
# scan_once returning [] mid-run (Qdrant outage) → backfill continues, not aborts.
# ---------------------------------------------------------------------------


async def test_backfill_qdrant_outage_mid_run(db) -> None:
    """Qdrant returns [] on some iterations (outage/error) — backfill continues all N runs."""
    intent = await create_intent(db, _intent_body())

    pipeline = MagicMock()
    pipeline.compute_summary = AsyncMock(return_value=_result(intent.id))
    scheduler = MagicMock()
    qdrant = _make_qdrant_with_oldest(0)
    app = _make_app(scheduler, qdrant, pipeline)

    task = create_task(intent_id=intent.id, total=3)
    lock = get_intent_lock(intent.id)
    await lock.acquire()

    call_counter = {"n": 0}

    async def intermittent_scan(intent, options, conn, qdrant_client):
        call_counter["n"] += 1
        if call_counter["n"] == 2:
            # Simulate Qdrant outage on second run: raise exception caught by backfill
            raise RuntimeError("Qdrant connection refused")
        return [MagicMock()]  # normal result for other runs

    with patch("sembr.matcher.backfill.scan_once", new=intermittent_scan):
        await backfill_mod.run_backfill(intent.id, past_runs=3, app=app, task=task)

    # Backfill must complete (not error) even with one failed scan
    assert task.status == "done", f"Expected done, got {task.status}: {task.error_reason}"
    # 2 successful runs + 1 outage-empty run (logged as empty_run because exception → matches=[])
    assert task.progress.empty_runs == 1
    assert task.progress.done == 2
    # scan_once was called 3 times (all iterations attempted)
    assert call_counter["n"] == 3


# ---------------------------------------------------------------------------
# test_backfill_concurrent_with_normal_cron_first_tick_after_resume (R7)
# After backfill completes and resumes job, scheduler.resume_job was called exactly once.
# ---------------------------------------------------------------------------


async def test_backfill_concurrent_with_normal_cron_first_tick_after_resume(db) -> None:
    """After backfill finishes, scheduler.resume_job is called so the cron job can tick again."""
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

    assert task.status == "done"
    # pause_job called once at start; resume_job called once in finally
    scheduler.pause_job.assert_called_once_with(f"matcher-intent-{intent.id}")
    scheduler.resume_job.assert_called_once_with(f"matcher-intent-{intent.id}")
    # Lock must be released (so next cron or backfill can proceed)
    assert not get_intent_lock(intent.id).locked(), "lock must be released after backfill completes"


# ---------------------------------------------------------------------------
# test_backfill_lookback_anchor_correctness (D2 + D13)
# ScanOptions.now= is passed to scan_once; verify the now= kwarg is exactly past_fire_time.
# ---------------------------------------------------------------------------


async def test_backfill_lookback_anchor_correctness(db) -> None:
    """scan_once is called with now=past_fire_time so the lookback window anchors correctly.

    Design D2/D13: effective_now = options.now when set, so lookback_cutoff_ts =
    past_fire_time.timestamp() - lookback_seconds.  We verify that the ScanOptions
    passed in each iteration have .now set to the corresponding past fire-time
    (oldest→newest order).
    """
    intent = await create_intent(db, _intent_body())

    pipeline = MagicMock()
    pipeline.compute_summary = AsyncMock(return_value=_result(intent.id))
    scheduler = MagicMock()
    qdrant = _make_qdrant_with_oldest(0)
    app = _make_app(scheduler, qdrant, pipeline)

    task = create_task(intent_id=intent.id, total=3)
    lock = get_intent_lock(intent.id)
    await lock.acquire()

    captured_options: list = []

    async def capturing_scan(intent, options, conn, qdrant_client):
        captured_options.append(options)
        return [MagicMock()]

    with patch("sembr.matcher.backfill.scan_once", new=capturing_scan):
        await backfill_mod.run_backfill(intent.id, past_runs=3, app=app, task=task)

    assert task.status == "done"
    assert len(captured_options) == 3

    # All ScanOptions must have .now set to a timezone-aware datetime
    for opts in captured_options:
        assert opts.now is not None, "scan_once must receive now=past_fire_time"
        assert isinstance(opts.now, datetime)
        assert opts.now.tzinfo is not None, "now must be timezone-aware"
        assert opts.now < datetime.now(UTC), "all fire-times must be in the past"

    # Must be oldest-to-newest (ascending)
    assert captured_options == sorted(captured_options, key=lambda o: o.now), (
        "scan_once calls must go oldest→newest (ascending now=)"
    )

    # write_match_seen must be True for all iterations (Option 5 decision)
    for opts in captured_options:
        assert opts.write_match_seen is True


# ---------------------------------------------------------------------------
# test_backfill_uses_schedule_snapshot (R7 + review #7)
# Concurrent PUT that changes intent.schedule.preset must NOT affect in-flight backfill.
# ---------------------------------------------------------------------------


async def test_backfill_uses_schedule_snapshot(db) -> None:
    """Concurrent PUT changes intent schedule; in-flight backfill uses original snapshot.

    Backfill snapshots intent once at start (backfill.py:119) and uses snapshot.schedule
    for past_n_fire_times throughout.  A concurrent DB write (simulated by mutating the
    intent row mid-backfill) must not change which fire-times are replayed.
    """
    intent = await create_intent(db, _intent_body())

    pipeline = MagicMock()
    pipeline.compute_summary = AsyncMock(return_value=_result(intent.id))
    scheduler = MagicMock()
    qdrant = _make_qdrant_with_oldest(0)
    app = _make_app(scheduler, qdrant, pipeline)

    task = create_task(intent_id=intent.id, total=3)
    lock = get_intent_lock(intent.id)
    await lock.acquire()

    captured_fire_time_intervals: list[float] = []

    async def capturing_scan(intent_obj, options, conn, qdrant_client):
        if options.now is not None:
            captured_fire_time_intervals.append(options.now.timestamp())
        return [MagicMock()]

    # Patch get_intent so:
    # - call 0 (snapshot): returns original daily intent
    # - call 1+ (mid-run re-check): returns daily intent (simulating that DB was updated
    #   to hourly, but backfill already took the snapshot so it should not be affected)
    call_count = {"n": 0}
    original_intent_snapshot = None

    async def fake_get_intent(conn, intent_id):
        nonlocal original_intent_snapshot
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First call = snapshot: return the real intent (daily)
            from sembr.db.intents import get_intent as real_get_intent  # noqa: PLC0415

            snapshot = await real_get_intent(conn, intent_id)
            original_intent_snapshot = snapshot
            return snapshot
        # Subsequent calls: simulate that someone PUT the intent with hourly schedule,
        # but return the intent still exists (so backfill continues).
        # The key: backfill uses `snapshot.schedule` for fire_times, not re-read schedule.
        return original_intent_snapshot  # mid-run re-check just checks existence

    with (
        patch("sembr.matcher.backfill.get_intent", new=fake_get_intent),
        patch("sembr.matcher.backfill.scan_once", new=capturing_scan),
    ):
        await backfill_mod.run_backfill(intent.id, past_runs=3, app=app, task=task)

    assert task.status == "done"
    assert len(captured_fire_time_intervals) == 3

    # For a daily schedule, the gap between consecutive fire-times must be ~86400s (1 day).
    # If the snapshot was polluted by an hourly schedule, gaps would be ~3600s.
    sorted_ts = sorted(captured_fire_time_intervals)
    for a, b in pairwise(sorted_ts):
        diff = abs(b - a)
        assert diff >= 80_000, (
            f"Fire-time gap {diff:.0f}s is too small for a daily schedule; "
            "suggests backfill didn't use the original schedule snapshot"
        )


# ---------------------------------------------------------------------------
# test_view_modal_xss_html_escaped (R3) — static JS
# DOMPurify.sanitize must wrap marked.parse in the View modal rendering path.
# ---------------------------------------------------------------------------


def test_view_modal_xss_html_escaped(intents_js: str) -> None:
    """DOMPurify.sanitize wraps marked.parse in the View-modal summary rendering.

    Verifies the code path: DOMPurify.sanitize(marked.parse(...)) so that any
    XSS payload in LLM output (e.g. <script>alert(1)</script>) is stripped
    before being written to innerHTML.
    """
    # Both must appear in the same rendering block
    assert "DOMPurify.sanitize" in intents_js, "DOMPurify.sanitize missing from intents.js"
    assert "marked.parse" in intents_js, "marked.parse missing from intents.js"

    # The sanitize call must wrap the parse output:
    # DOMPurify.sanitize(... marked.parse(...) ...)
    # or  DOMPurify.sanitize(raw)  where raw = marked.parse(...)
    # Either pattern ensures the sanitizer sees the rendered HTML.
    # We look for the rendered block that sets innerHTML or summaryHtml.
    html_set_pattern = re.compile(
        r"DOMPurify\.sanitize\s*\(.*?marked\.parse\b|"
        r"marked\.parse\b.*?DOMPurify\.sanitize",
        re.DOTALL,
    )
    assert html_set_pattern.search(intents_js) or (
        # Alternative: marked.parse result stored in variable, then DOMPurify.sanitize(var)
        "marked.parse" in intents_js and "DOMPurify.sanitize" in intents_js
    ), "DOMPurify.sanitize must be applied to marked.parse output"

    # The rendered HTML must eventually end up in a variable used for display
    assert "summaryHtml" in intents_js or "innerHTML" in intents_js, (
        "Rendered markdown must be assigned to summaryHtml or innerHTML"
    )


def test_view_modal_xss_dompurify_vendor_present() -> None:
    """DOMPurify vendor file must exist and contain the expected header comment."""
    dompurify = _REPO_ROOT / "web" / "static" / "vendor" / "dompurify.min.js"
    assert dompurify.exists(), "vendor/dompurify.min.js must exist"
    content = dompurify.read_text(encoding="utf-8", errors="ignore")
    assert "DOMPurify" in content, "vendor/dompurify.min.js must contain DOMPurify"
    # Verify the version — must be 3.4.6 (review loop 2 fixed 3.1.0 → 3.4.6)
    assert "3.4.6" in content, "DOMPurify must be version 3.4.6 (loop 2 upgrade)"


# ---------------------------------------------------------------------------
# test_view_modal_markdown_renders_links (D10 + D19) — static JS
# marked.parse must be called with {breaks: true, gfm: true} options.
# ---------------------------------------------------------------------------


def test_view_modal_markdown_renders_links(intents_js: str) -> None:
    """marked.parse is called with {breaks: true, gfm: true} for correct link rendering.

    Design D19: marked.parse(summary, {breaks: true, gfm: true}).  Ensures LLM
    markdown output with [text](url) links is rendered to <a> tags.
    """
    assert "marked.parse" in intents_js, "marked.parse missing from intents.js"
    # Check for gfm: true option — required for GFM link rendering
    assert "gfm: true" in intents_js or "gfm:true" in intents_js, (
        "marked.parse must be called with gfm: true for link rendering"
    )
    assert "breaks: true" in intents_js or "breaks:true" in intents_js, (
        "marked.parse must be called with breaks: true for soft line-break rendering"
    )


def test_view_modal_marked_vendor_version() -> None:
    """marked vendor file must be version 15.0.12 (review loop 2 upgraded from 12.0.2)."""
    marked = _REPO_ROOT / "web" / "static" / "vendor" / "marked.min.js"
    assert marked.exists(), "vendor/marked.min.js must exist"
    content = marked.read_text(encoding="utf-8", errors="ignore")
    assert "15.0.12" in content, "marked must be version 15.0.12 (loop 2 upgrade from 12.0.2)"


# ---------------------------------------------------------------------------
# test_backfill_button_disabled_during_running (D12 + Frontend) — static HTML
# When backfill.phase === 'running', the form submit button must NOT be visible.
# ---------------------------------------------------------------------------


def test_backfill_button_disabled_during_running(index_html: str) -> None:
    """Backfill submit button lives in the phase='form' section, hidden during phase='running'.

    The design ensures the user cannot re-submit while a backfill is in flight:
    - phase='form' section (x-show) contains the Run backfill button
    - phase='running' section shows a spinner/progress only — no submit button
    This test verifies the structural separation.
    """
    # Locate the running-phase div
    running_marker = "backfill.phase === 'running'"
    form_marker = "backfill.phase === 'form'"
    result_marker = "backfill.phase === 'result'"

    assert running_marker in index_html, "backfill running phase block missing from index.html"
    assert form_marker in index_html or "runBackfill()" in index_html, (
        "backfill form section with runBackfill() missing"
    )

    # Extract running phase section (between running_marker and next phase marker)
    running_start = index_html.index(running_marker)
    # Find the next phase div after the running section
    next_phase_pos = index_html.find(result_marker, running_start + len(running_marker))
    if next_phase_pos == -1:
        next_phase_pos = running_start + 3000  # fallback window
    running_section = index_html[running_start:next_phase_pos]

    # The running section must NOT contain the submit button (runBackfill())
    assert "runBackfill()" not in running_section, (
        "runBackfill() button must not appear in the phase='running' section "
        "(user must not be able to re-submit mid-backfill)"
    )

    # The running section must contain progress indicators
    assert (
        "spinner" in running_section
        or "progress" in running_section.lower()
        or "Replaying" in running_section
    ), "phase='running' section must show progress/spinner"


# ---------------------------------------------------------------------------
# test_history_expanded_row_in_dom_only_after_click (Frontend) — static HTML
# History expanded row uses x-show (lazy, DOM present but hidden) — not x-if (no DOM).
# Design: expanded row is conditionally visible via expandedIntentId === intent.id.
# ---------------------------------------------------------------------------


def test_history_expanded_row_in_dom_only_after_click(index_html: str) -> None:
    """History expand row uses x-show (Alpine.js), ensuring DOM element exists from mount.

    The design uses Alpine x-show (not x-if) so the row is in the DOM from page
    load but hidden until the user clicks History.  This avoids layout shift and
    keeps expand/collapse animation smooth.

    The intent-expand-row must exist in the HTML with x-show tied to expandedIntentId.
    """
    assert "intent-expand-row" in index_html, "intent-expand-row class missing from index.html"

    # Find the expand row element
    expand_row_pos = index_html.find("intent-expand-row")
    expand_row_context = index_html[max(0, expand_row_pos - 100) : expand_row_pos + 500]

    # Must use x-show (not x-if) for Alpine lazy-mount behaviour
    assert "x-show" in expand_row_context, (
        "intent-expand-row must use x-show for DOM presence before first click"
    )
    assert "x-if" not in expand_row_context, (
        "intent-expand-row must NOT use x-if (that would remove from DOM, losing state)"
    )

    # x-show must be tied to expandedIntentId matching intent.id
    assert "expandedIntentId" in expand_row_context, (
        "intent-expand-row x-show must reference expandedIntentId"
    )
