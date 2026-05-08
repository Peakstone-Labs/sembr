"""Unit tests for DD9: scan_once extracted function.

qdrant_client is not installed on the Windows dev machine, so the test shims
sys.modules with lightweight stubs before scan_once is called. The stubs
record call arguments so filter construction can be asserted.
"""
from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

# ---------------------------------------------------------------------------
# qdrant_client stub — must be registered before sembr.matcher.scan is imported
# ---------------------------------------------------------------------------


class _Range:
    def __init__(self, *, gte=None, lte=None, gt=None, lt=None):
        self.gte = gte
        self.lte = lte
        self.gt = gt
        self.lt = lt


class _MatchAny:
    def __init__(self, *, any=None):
        self.any = any


class _FieldCondition:
    def __init__(self, *, key, range=None, match=None):
        self.key = key
        self.range = range
        self.match = match


class _Filter:
    def __init__(self, *, must=None, should=None):
        self.must = must or []


class _PointIdsList:
    def __init__(self, *, points=None):
        self.points = points or []


def _install_qdrant_stub() -> None:
    """Register a minimal qdrant_client.models stub so scan_once imports succeed."""
    if "qdrant_client" not in sys.modules:
        qc = ModuleType("qdrant_client")
        sys.modules["qdrant_client"] = qc
    if "qdrant_client.models" not in sys.modules:
        qc_models = ModuleType("qdrant_client.models")
        sys.modules["qdrant_client.models"] = qc_models
    qc_models = sys.modules["qdrant_client.models"]
    qc_models.Range = _Range  # type: ignore[attr-defined]
    qc_models.MatchAny = _MatchAny  # type: ignore[attr-defined]
    qc_models.FieldCondition = _FieldCondition  # type: ignore[attr-defined]
    qc_models.Filter = _Filter  # type: ignore[attr-defined]
    qc_models.PointIdsList = _PointIdsList  # type: ignore[attr-defined]


_install_qdrant_stub()

# Import after stub is in place
from sembr.db.intents import create_intent, init_intent_tables  # noqa: E402
from sembr.db.match_seen import init_match_seen_tables  # noqa: E402
from sembr.db.sqlite import install_for_test  # noqa: E402
from sembr.matcher.scan import ScanOptions, scan_once  # noqa: E402
from sembr.models import FeedFilter, IntentCreate  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_INTENT_BODY = IntentCreate(
    name="scan-once-test",
    text="market movements",
    channels=[{"type": "email", "to": ["a@example.com"]}],
)


def _make_qdrant(hits: list | None = None) -> MagicMock:
    """Return a mock qdrant_client with configurable query_points results."""
    client = MagicMock()
    point = MagicMock()
    point.vector = [0.1] * 1024
    client.retrieve = AsyncMock(return_value=[point])

    result = MagicMock()
    result.points = hits or []
    client.query_points = AsyncMock(return_value=result)
    return client


def _make_hit(article_id: str = "art-1", score: float = 0.85, feed_id: int = 1) -> MagicMock:
    hit = MagicMock()
    hit.id = article_id
    hit.score = score
    hit.payload = {"feed_id": feed_id, "enabled": True, "title": "Test Article"}
    return hit


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("PRAGMA foreign_keys=ON")
    await init_intent_tables(conn)
    await init_match_seen_tables(conn)
    install_for_test(conn)
    yield conn
    await conn.close()


# ---------------------------------------------------------------------------
# feed_ids short-circuit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_once_empty_feed_ids_short_circuits(db) -> None:
    """feed_ids=[] returns [] without calling Qdrant."""
    intent = await create_intent(db, _INTENT_BODY)
    qdrant = _make_qdrant(hits=[_make_hit()])
    options = ScanOptions(
        lookback_seconds=86400,
        threshold=0.75,
        skip_seen=True,
        feed_ids=[],
        write_match_seen=False,
    )
    matches = await scan_once(intent, options, db, qdrant)
    assert matches == []
    qdrant.retrieve.assert_not_called()
    qdrant.query_points.assert_not_called()


# ---------------------------------------------------------------------------
# feed_ids filter passed to Qdrant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_once_feed_ids_filter_passed_to_qdrant(db) -> None:
    """Non-empty feed_ids builds a MatchAny filter in query_points."""
    intent = await create_intent(db, _INTENT_BODY)
    qdrant = _make_qdrant(hits=[_make_hit(feed_id=2)])
    options = ScanOptions(
        lookback_seconds=86400,
        threshold=0.75,
        skip_seen=False,
        feed_ids=[2, 5],
        write_match_seen=False,
    )
    await scan_once(intent, options, db, qdrant)

    call_kwargs = qdrant.query_points.call_args.kwargs
    q_filter = call_kwargs["query_filter"]
    field_keys = [cond.key for cond in q_filter.must]
    assert "feed_id" in field_keys


@pytest.mark.asyncio
async def test_scan_once_none_feed_ids_no_feed_filter(db) -> None:
    """feed_ids=None (全扫) does NOT add a feed_id condition."""
    intent = await create_intent(db, _INTENT_BODY)
    qdrant = _make_qdrant(hits=[])
    options = ScanOptions(
        lookback_seconds=86400,
        threshold=0.75,
        skip_seen=True,
        feed_ids=None,
        write_match_seen=False,
    )
    await scan_once(intent, options, db, qdrant)

    call_kwargs = qdrant.query_points.call_args.kwargs
    q_filter = call_kwargs["query_filter"]
    field_keys = [cond.key for cond in q_filter.must]
    assert "feed_id" not in field_keys


# ---------------------------------------------------------------------------
# write_match_seen=False (fire path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_once_write_match_seen_false_returns_all_hits(db) -> None:
    """write_match_seen=False returns all Qdrant hits without touching match_seen."""
    intent = await create_intent(db, _INTENT_BODY)
    hits = [_make_hit("a1"), _make_hit("a2")]
    qdrant = _make_qdrant(hits=hits)
    options = ScanOptions(
        lookback_seconds=86400,
        threshold=0.75,
        skip_seen=False,
        feed_ids=None,
        write_match_seen=False,
    )
    matches = await scan_once(intent, options, db, qdrant)
    assert len(matches) == 2
    assert {m.article_id for m in matches} == {"a1", "a2"}

    async with db.execute("SELECT count(*) FROM match_seen WHERE intent_id=?", (intent.id,)) as cur:
        row = await cur.fetchone()
    assert row[0] == 0


# ---------------------------------------------------------------------------
# skip_seen=True (write + filter)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_once_skip_seen_true_filters_already_seen(db) -> None:
    """skip_seen=True returns only new articles; already-seen are excluded."""
    intent = await create_intent(db, _INTENT_BODY)

    from sembr.db.match_seen import insert_unseen_returning_new  # noqa: PLC0415
    await insert_unseen_returning_new(db, intent.id, ["a1"])

    hits = [_make_hit("a1"), _make_hit("a2", score=0.9)]
    qdrant = _make_qdrant(hits=hits)
    options = ScanOptions(
        lookback_seconds=86400,
        threshold=0.75,
        skip_seen=True,
        feed_ids=None,
        write_match_seen=True,
    )
    matches = await scan_once(intent, options, db, qdrant)
    assert len(matches) == 1
    assert matches[0].article_id == "a2"


@pytest.mark.asyncio
async def test_scan_once_skip_seen_true_writes_new_to_match_seen(db) -> None:
    """skip_seen=True writes new article_ids to match_seen."""
    intent = await create_intent(db, _INTENT_BODY)
    hits = [_make_hit("b1"), _make_hit("b2")]
    qdrant = _make_qdrant(hits=hits)
    options = ScanOptions(
        lookback_seconds=86400,
        threshold=0.75,
        skip_seen=True,
        feed_ids=None,
        write_match_seen=True,
    )
    matches = await scan_once(intent, options, db, qdrant)
    assert len(matches) == 2

    async with db.execute(
        "SELECT count(*) FROM match_seen WHERE intent_id=?", (intent.id,)
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == 2


# ---------------------------------------------------------------------------
# skip_seen=False with write_match_seen=True
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_once_skip_seen_false_returns_all_writes_match_seen(db) -> None:
    """skip_seen=False returns all hits (incl. previously seen) and still writes."""
    intent = await create_intent(db, _INTENT_BODY)

    from sembr.db.match_seen import insert_unseen_returning_new  # noqa: PLC0415
    await insert_unseen_returning_new(db, intent.id, ["c1"])

    hits = [_make_hit("c1"), _make_hit("c2")]
    qdrant = _make_qdrant(hits=hits)
    options = ScanOptions(
        lookback_seconds=86400,
        threshold=0.75,
        skip_seen=False,
        feed_ids=None,
        write_match_seen=True,
    )
    matches = await scan_once(intent, options, db, qdrant)
    assert len(matches) == 2
    assert {m.article_id for m in matches} == {"c1", "c2"}
