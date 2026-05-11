"""In-process cache of event-mode intent vectors.

D9: app.state.event_intent_cache holds one EventIntentEntry per event-mode intent.
Cache is loaded from Qdrant at lifespan startup; kept in sync by POST/PUT/DELETE intents.

intent-match-enhancement D11: each entry now carries `vectors: dict[str, list[float]]`
keyed by slot name {main, alt_0, alt_1, alt_2} instead of a single `vector`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sembr.vector_store.intents import ALIAS_NAME as _INTENTS_ALIAS

if TYPE_CHECKING:
    import aiosqlite
    from sembr.models import EventSchedule
    from sembr.vector_store.qdrant import QdrantHandle

logger = logging.getLogger(__name__)

_KNOWN_SLOTS = ("main", "alt_0", "alt_1", "alt_2")


@dataclass
class EventIntentEntry:
    vectors: dict[str, list[float]] = field(default_factory=dict)
    threshold: float = 0.0
    feed_filter_ids: list[int] | None = None
    schedule: "EventSchedule | None" = None


class EventIntentCache:
    """Thread-unsafe dict wrapper; safe under asyncio single-thread event loop."""

    def __init__(self) -> None:
        self._cache: dict[int, EventIntentEntry] = {}

    def add(self, intent_id: int, entry: EventIntentEntry) -> None:
        self._cache[intent_id] = entry

    def remove(self, intent_id: int) -> None:
        self._cache.pop(intent_id, None)

    def get(self, intent_id: int) -> EventIntentEntry | None:
        return self._cache.get(intent_id)

    def items(self):
        return self._cache.items()

    def __len__(self) -> int:
        return len(self._cache)

    def __contains__(self, intent_id: int) -> bool:
        return intent_id in self._cache


async def load_event_cache(
    cache: EventIntentCache,
    qdrant_handle: "QdrantHandle",
    conn: "aiosqlite.Connection",
) -> None:
    """Load all event-mode intents from SQLite + Qdrant into cache at startup.

    Called after register_all_enabled (D22) so cron intents are already registered.
    Intents whose Qdrant point uses a non-dict (legacy) layout or lacks the main
    slot are skipped with ERROR — same policy as register_all_enabled.
    """
    from sembr.db.intents import list_intents  # noqa: PLC0415
    from sembr.models import EventSchedule  # noqa: PLC0415

    intents = await list_intents(conn, enabled=True)
    event_intents = [i for i in intents if isinstance(i.schedule, EventSchedule)]
    if not event_intents:
        logger.info("load_event_cache: no event-mode intents found")
        return

    loaded = 0
    for intent in event_intents:
        points = await qdrant_handle.client.retrieve(
            collection_name=_INTENTS_ALIAS,
            ids=[intent.id],
            with_vectors=True,
        )
        if not points:
            logger.error(
                "intent_id=%d (event-mode) missing Qdrant point at startup; "
                "skipping. Disable or DELETE+POST to resolve.",
                intent.id,
            )
            continue
        raw = getattr(points[0], "vector", None)
        if not isinstance(raw, dict):
            logger.error(
                "intent_id=%d (event-mode) Qdrant point uses non-dict vector layout "
                "(migration not completed?); skipping cache load.",
                intent.id,
            )
            continue
        vectors: dict[str, list[float]] = {}
        for slot in _KNOWN_SLOTS:
            v = raw.get(slot)
            if isinstance(v, list):
                vectors[slot] = list(v)
        if "main" not in vectors:
            logger.error(
                "intent_id=%d (event-mode) Qdrant point missing main slot vector; "
                "skipping cache load.",
                intent.id,
            )
            continue
        entry = EventIntentEntry(
            vectors=vectors,
            threshold=intent.threshold,
            feed_filter_ids=intent.feed_filter.ids if intent.feed_filter else None,
            schedule=intent.schedule,  # type: ignore[arg-type]
        )
        cache.add(intent.id, entry)
        loaded += 1

    logger.info(
        "load_event_cache: loaded %d/%d event-mode intents",
        loaded,
        len(event_intents),
    )
