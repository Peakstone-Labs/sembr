# SPDX-License-Identifier: Apache-2.0
"""LogBus — in-process ring buffer with per-tag deques and asyncio fan-out.

Thread-safety: a single threading.Lock guards deques, subscribers, and
tag_levels.  The emit() hot path holds the lock for ~µs (dict lookup +
deque.append + N call_soon_threadsafe calls including fan-out scheduling).
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections import deque
from typing import Any

from sembr.logbus.router import ALL_TAGS


def _put_drop_oldest(q: asyncio.Queue, entry: dict[str, Any]) -> None:
    """Enqueue *entry*; if the queue is full, evict the oldest item first.

    Must only be called from within the asyncio event loop (e.g. via
    call_soon_threadsafe).  Never raises — swallows QueueFull silently so
    the asyncio default exception handler is not triggered.
    """
    try:
        q.put_nowait(entry)
    except asyncio.QueueFull:
        try:
            q.get_nowait()  # drop oldest
        except asyncio.QueueEmpty:
            return
        try:
            q.put_nowait(entry)
        except asyncio.QueueFull:
            return  # two concurrent emits both hit full; drop newest as last resort


class LogBus:
    """Module-level singleton; created once, referenced by handler and routes."""

    def __init__(self, buffer_per_tag: int = 1000) -> None:
        self._lock = threading.Lock()
        self._deques: dict[str, deque[dict[str, Any]]] = {
            tag: deque(maxlen=buffer_per_tag) for tag in ALL_TAGS
        }
        # Each subscriber records the tag it cares about (None = all tags).
        # emit() schedules a fan-out only for subscribers whose filter matches.
        self._subscribers: dict[asyncio.Queue[dict[str, Any] | None], str | None] = {}
        self._tag_levels: dict[str, int] = dict.fromkeys(ALL_TAGS, 20)  # INFO=20
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def set_buffer_size(self, buffer_per_tag: int) -> None:
        with self._lock:
            for tag in ALL_TAGS:
                old = self._deques[tag]
                new: deque[dict[str, Any]] = deque(maxlen=buffer_per_tag)
                new.extend(old)
                self._deques[tag] = new

    # ------------------------------------------------------------------
    # Tag level management
    # ------------------------------------------------------------------

    def get_tag_levels(self) -> dict[str, int]:
        with self._lock:
            return dict(self._tag_levels)

    def set_tag_level(self, tag: str, level: int) -> None:
        with self._lock:
            self._tag_levels[tag] = level

    # ------------------------------------------------------------------
    # Emit (called from any thread via RingBufferHandler)
    # ------------------------------------------------------------------

    def emit(self, tag: str, entry: dict[str, Any]) -> None:
        """Append *entry* to the ring buffer and fan-out to active subscribers.

        Drops silently when no event loop is available (pre-lifespan start).
        Fan-out only schedules a put for subscribers whose tag filter is None
        (all tags) or matches *tag* exactly — a single SSE consumer streaming
        ``embedder`` no longer pays for traffic on the other six tags.
        Overflow inside each subscriber queue uses drop-oldest semantics.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        with self._lock:
            if entry["level_no"] < self._tag_levels[tag]:
                return
            self._deques[tag].append(entry)
            for q, want_tag in self._subscribers.items():
                if want_tag is not None and want_tag != tag:
                    continue
                with contextlib.suppress(Exception):
                    loop.call_soon_threadsafe(_put_drop_oldest, q, entry)

    # ------------------------------------------------------------------
    # Subscribe / unsubscribe (called from asyncio context)
    # ------------------------------------------------------------------

    def subscribe(
        self,
        q: asyncio.Queue[dict[str, Any] | None],
        *,
        tag: str | None = None,
    ) -> list[dict[str, Any]]:
        """Atomically snapshot the relevant deque(s) and register *q* as a subscriber.

        When *tag* is given the snapshot is just that tag's deque (already in
        insertion order — no sort needed). When *tag* is None the snapshot
        flattens all tag deques and is sorted by timestamp; the sort runs
        outside the lock so emitters are not blocked by it.

        The caller must not yield between ``subscribe()`` and draining the
        returned snapshot — otherwise it risks missing or duplicating entries.
        """
        with self._lock:
            if tag is not None:
                snapshot: list[dict[str, Any]] = list(self._deques[tag])
                needs_sort = False
            else:
                snapshot = []
                for tag_deque in self._deques.values():
                    snapshot.extend(tag_deque)
                needs_sort = True
            self._subscribers[q] = tag
        if needs_sort:
            snapshot.sort(key=lambda e: e["ts"])
        return snapshot

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any] | None]) -> None:
        with self._lock:
            self._subscribers.pop(q, None)

    # ------------------------------------------------------------------
    # Snapshot for GET /tags (tag + current level)
    # ------------------------------------------------------------------

    def tag_info(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{"name": tag, "level": self._tag_levels[tag]} for tag in ALL_TAGS]


# Module-level singleton; swapped out in tests via _reset_for_test().
_bus: LogBus = LogBus()


def get_bus() -> LogBus:
    return _bus


def _reset_for_test(buffer_per_tag: int = 1000) -> LogBus:
    """Replace the singleton with a fresh instance and return it (test helper)."""
    global _bus
    _bus = LogBus(buffer_per_tag=buffer_per_tag)
    return _bus
