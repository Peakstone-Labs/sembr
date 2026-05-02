"""In-process cache of event-mode intent vectors.

D9: app.state.event_intent_cache holds one EventIntentEntry per event-mode intent.
Cache is loaded from Qdrant at lifespan startup; kept in sync by POST/PUT/DELETE intents.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite
    from sembr.models import EventSchedule
    from sembr.vector_store.qdrant import QdrantHandle

logger = logging.getLogger(__name__)


@dataclass
class EventIntentEntry:
    vector: list[float]
    threshold: float
    feed_filter_ids: list[int] | None
    schedule: "EventSchedule"


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
    Intents missing their Qdrant vector are skipped with an ERROR (same policy as
    register_all_enabled for cron intents).
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
            collection_name="intents_current",
            ids=[intent.id],
            with_vectors=True,
        )
        if not points or points[0].vector is None:
            logger.error(
                "intent_id=%d (event-mode) has no Qdrant vector at startup; "
                "skipping cache load. Disable or DELETE+POST to resolve.",
                intent.id,
            )
            continue
        entry = EventIntentEntry(
            vector=list(points[0].vector),
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
