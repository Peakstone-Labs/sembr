# SPDX-License-Identifier: Apache-2.0
"""Focused inline tests for the PUT/POST intent paths and embedder guard.

Tests: _extract_vector variants, POST defensive copy, PUT re-enable 500,
schedule-mode immutable 422, embedder_worker app=None guard.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── a. _extract_vector: single-vector list ────────────────────────────────────
def test_extract_vector_single_list():
    from sembr.vector_store.qdrant import extract_point_vector as _extract_vector

    point = MagicMock()
    point.vector = [1.0, 2.0]
    assert _extract_vector(point) == [1.0, 2.0]


# ── b. _extract_vector: named-vector dict ─────────────────────────────────────
def test_extract_vector_named_dict():
    from sembr.vector_store.qdrant import extract_point_vector as _extract_vector

    point = MagicMock()
    point.vector = {"default": [1.0, 2.0]}
    assert _extract_vector(point) == [1.0, 2.0]


# ── c. _extract_vector: None vector ──────────────────────────────────────────
def test_extract_vector_none():
    from sembr.vector_store.qdrant import extract_point_vector as _extract_vector

    point = MagicMock()
    point.vector = None
    assert _extract_vector(point) is None


# ── d. POST defensive copy ────────────────────────────────────────────────────
def test_post_defensive_copy():
    """Stored cache vector must be equal to the original but a different object."""
    from sembr.matcher.event_cache import EventIntentCache, EventIntentEntry
    from sembr.models import EventSchedule

    v = [0.5] * 1024
    cache = EventIntentCache()
    intent_id = 42
    cache.add(
        intent_id,
        EventIntentEntry(
            vectors={"main": list(v)},  # intent-match-enhancement: dict per slot
            threshold=0.75,
            feed_filter_ids=None,
            schedule=EventSchedule(trigger_count=5),
        ),
    )
    entry = cache.get(intent_id)
    assert entry is not None
    assert entry.vectors["main"] == v, "main vector values must match"
    assert entry.vectors["main"] is not v, "vector must be a defensive copy, not alias the original"


# ── e. PUT re-enable 500 on missing vector ────────────────────────────────────
@pytest.mark.asyncio
async def test_put_reenable_500_on_missing_vector():
    """PUT enabling an event-mode intent where Qdrant returns empty → HTTP 500.

    PUT writes the intents row + sub_texts inside a single transaction(), so
    we patch the in-txn helper and the transaction context manager rather
    than the old `update_intent`. `get_intent` returns the existing snapshot
    first, then the post-writeback state (re-read), matching the actual flow.
    """
    import contextlib

    import fastapi

    from sembr.api.intents import put_intent
    from sembr.matcher.event_cache import EventIntentCache
    from sembr.models import EventSchedule, IntentUpdate

    intent_id = 99
    existing_intent = MagicMock()
    existing_intent.id = intent_id
    existing_intent.text = "climate change"
    existing_intent.enabled = False
    existing_intent.threshold = 0.75
    existing_intent.schedule = EventSchedule(trigger_count=3)
    existing_intent.feed_filter = None
    existing_intent.system_template = "default"
    existing_intent.instruction_template = "default"
    existing_intent.timezone = "UTC"
    existing_intent.sub_texts = []

    updated_intent = MagicMock()
    updated_intent.id = intent_id
    updated_intent.text = "climate change"
    updated_intent.enabled = True
    updated_intent.threshold = 0.75
    updated_intent.schedule = EventSchedule(trigger_count=3)
    updated_intent.feed_filter = None
    updated_intent.system_template = "default"
    updated_intent.instruction_template = "default"
    updated_intent.sub_texts = []

    cache = EventIntentCache()  # empty — intent not in cache

    mock_request = MagicMock()
    mock_request.app.state.embedder.is_loaded = True
    mock_request.app.state.event_intent_cache = cache
    mock_request.app.state.qdrant.client = AsyncMock()
    mock_request.app.state.scheduler = MagicMock()
    # Qdrant retrieve returns empty → no vector
    mock_request.app.state.qdrant.client.retrieve = AsyncMock(return_value=[])

    body = IntentUpdate(enabled=True)

    # Snapshot at entry vs re-read after writeback return different states
    get_intent_mock = AsyncMock(side_effect=[existing_intent, updated_intent])

    @contextlib.asynccontextmanager
    async def fake_transaction():
        yield MagicMock()  # txn handle is unused by mocked _update_intent_in_txn

    with (
        patch("sembr.api.intents.get_conn", return_value=MagicMock()),
        patch("sembr.api.intents.get_intent", get_intent_mock),
        patch("sembr.api.intents._update_intent_in_txn", AsyncMock()),
        patch("sembr.api.intents.transaction", fake_transaction),
        patch("sembr.api.intents.update_intent_payload", AsyncMock()),
        patch("sembr.api.intents.template_exists", return_value=True),
        patch("sembr.api.intents.unregister_intent_job"),
        patch("sembr.api.intents.register_intent_job"),
    ):
        with pytest.raises(fastapi.HTTPException) as exc_info:
            await put_intent(intent_id, body, mock_request)
    assert exc_info.value.status_code == 500
    assert "vector missing" in exc_info.value.detail


# ── f. schedule.mode immutable → 422 ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_d16_mode_immutable_422():
    """PUT changing schedule.mode from cron→event must yield 422 with 'immutable'."""
    import fastapi

    from sembr.api.intents import put_intent
    from sembr.models import CronSchedule, EventSchedule, IntentUpdate

    existing_intent = MagicMock()
    existing_intent.id = 7
    existing_intent.text = "test"
    existing_intent.enabled = True
    existing_intent.threshold = 0.75
    existing_intent.schedule = CronSchedule(preset="daily")
    existing_intent.feed_filter = None
    existing_intent.system_template = "default"
    existing_intent.instruction_template = "default"
    existing_intent.timezone = "UTC"

    mock_request = MagicMock()
    mock_request.app.state.embedder.is_loaded = True

    body = IntentUpdate(schedule=EventSchedule(trigger_count=5))

    with (
        patch("sembr.api.intents.get_conn", return_value=MagicMock()),
        patch("sembr.api.intents.get_intent", AsyncMock(return_value=existing_intent)),
        patch("sembr.api.intents.template_exists", return_value=True),
    ):
        with pytest.raises(fastapi.HTTPException) as exc_info:
            await put_intent(7, body, mock_request)
    assert exc_info.value.status_code == 422
    assert "immutable" in exc_info.value.detail


# ── g. embedder_worker app=None guard ────────────────────────────────────────
@pytest.mark.asyncio
async def test_embedder_worker_app_none_no_event_match():
    """embedder_worker(app=None) must NOT call event_match_batch."""
    from sembr.db.articles import PendingRow
    from sembr.embedder.scheduler import embedder_worker

    mock_embedder = MagicMock()
    mock_embedder.is_loaded = True
    mock_embedder.model_version = "bge-m3-v1"
    mock_embedder.max_input_chars = 8_000
    mock_embedder.aembed = AsyncMock(return_value=[[0.1] * 1024])

    mock_qdrant = MagicMock()
    mock_qdrant.client.upsert = AsyncMock()

    fake_row = PendingRow(
        md5="a" * 32,
        url="http://x.com",
        title="T",
        body="B",
        published_at="2026-01-01T00:00:00",
        feed_id=1,
        retry_count=0,
    )

    event_match_mock = AsyncMock()

    with (
        patch("sembr.embedder.scheduler.pull_pending_batch", AsyncMock(return_value=[fake_row])),
        patch("sembr.embedder.scheduler.delete_pending", AsyncMock()),
        patch("sembr.embedder.scheduler.increment_retry", AsyncMock()),
        patch("sembr.embedder.scheduler.log_embed_event", AsyncMock()),
        patch("sembr.embedder.scheduler.get_conn", MagicMock()),
        patch("sembr.matcher.event_match.event_match_batch", event_match_mock),
    ):
        await embedder_worker(mock_embedder, mock_qdrant, app=None)

    event_match_mock.assert_not_called()
